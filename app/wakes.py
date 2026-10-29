"""One webhook per open slice, recorded outside the canvas SQLite file.

The mural clock already decides which slice is open. This module only notices
that and POSTs a wake. Each seat is claimed once per slice: the first writer
creates the record, later writers see it and do not send a second wake. A
failed POST is retried on a later trigger, up to three times, and only while
that wake is still eligible.

The JSON body is day, slice, seat, end time, the public origin, and a render
path. Seat tokens, the host API key, and the webhook key never go in it. The
webhook key is sent only as ``Authorization: Bearer``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

from app import blob_db

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
POST_TIMEOUT_SECONDS = 4.0
PENDING_STALE_SECONDS = 15
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
Poster = Callable[[str, str, dict[str, Any]], int]


@dataclass(frozen=True)
class WakeSpec:
    storage_key: str
    event: str
    day_id: int
    slice_index: int
    seat: str
    ends_at_unix: int
    eligible: bool
    render_url: str


class WakeLog(Protocol):
    def read(self, key: str) -> dict[str, Any] | None: ...

    def create(self, key: str, record: dict[str, Any]) -> bool: ...

    def replace(self, key: str, expected_attempts: int, record: dict[str, Any]) -> bool: ...

    def save(self, key: str, record: dict[str, Any]) -> None: ...


def _check_key(key: str) -> str:
    if not _KEY_RE.fullmatch(key):
        raise ValueError(f"wake key {key!r} is not a storage name")
    return key


class FileWakeLog:
    """One JSON file per wake. Create uses O_EXCL so two callers cannot both own it."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._lock = threading.Lock()

    def _path(self, key: str) -> Path:
        return self.directory / f"{_check_key(key)}.json"

    def read(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        with self._lock:
            if not path.is_file():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

    def create(self, key: str, record: dict[str, Any]) -> bool:
        path = self._path(key)
        payload = json.dumps(record, separators=(",", ":")).encode()
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                return False
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            return True

    def replace(self, key: str, expected_attempts: int, record: dict[str, Any]) -> bool:
        path = self._path(key)
        with self._lock:
            if not path.is_file():
                return False
            current = json.loads(path.read_text(encoding="utf-8"))
            if int(current.get("attempts") or 0) != expected_attempts:
                return False
            path.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
            return True

    def save(self, key: str, record: dict[str, Any]) -> None:
        path = self._path(key)
        text = json.dumps(record, separators=(",", ":"))
        with self._lock:
            if not path.is_file():
                return
            path.write_text(text, encoding="utf-8")


class BlobWakeLog:
    """Same claim, stored at ``agent-pixel/wakes/{day}-{slice}-{seat}.json``."""

    def __init__(self, prefix: str | None = None):
        parent = prefix
        if parent is None:
            parent = str(Path(blob_db.pathname()).parent).replace("\\", "/")
            if parent in ("", "."):
                parent = "agent-pixel"
        self.prefix = parent.rstrip("/") + "/wakes"
        self._etags: dict[str, str | None] = {}
        self._lock = threading.Lock()

    def _name(self, key: str) -> str:
        return f"{self.prefix}/{_check_key(key)}.json"

    def read(self, key: str) -> dict[str, Any] | None:
        name = self._name(key)
        try:
            body, etag = blob_db.download(name)
        except blob_db.BlobMissing:
            return None
        with self._lock:
            self._etags[key] = etag
        return json.loads(body.decode())

    def create(self, key: str, record: dict[str, Any]) -> bool:
        name = self._name(key)
        payload = json.dumps(record, separators=(",", ":")).encode()
        try:
            etag = blob_db.put_bytes(name, payload, exclusive=True)
        except blob_db.BlobConflict:
            return False
        with self._lock:
            self._etags[key] = etag
        return True

    def replace(self, key: str, expected_attempts: int, record: dict[str, Any]) -> bool:
        current = self.read(key)
        if current is None or int(current.get("attempts") or 0) != expected_attempts:
            return False
        return self._write(key, record)

    def save(self, key: str, record: dict[str, Any]) -> None:
        self._write(key, record)

    def _write(self, key: str, record: dict[str, Any]) -> bool:
        name = self._name(key)
        payload = json.dumps(record, separators=(",", ":")).encode()
        with self._lock:
            etag = self._etags.get(key)
        try:
            new_etag = blob_db.put_bytes(name, payload, exclusive=False, if_match=etag)
        except blob_db.BlobConflict:
            return False
        with self._lock:
            self._etags[key] = new_etag
        return True


def post_wake(url: str, key: str, payload: dict[str, Any]) -> int:
    """POST the wake. The key travels only in the Authorization header."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "agent-pixel",
    }
    if key:
        headers["Authorization"] = "Bearer " + key
    response = httpx.post(
        url,
        json=payload,
        headers=headers,
        timeout=POST_TIMEOUT_SECONDS,
        follow_redirects=False,
    )
    return response.status_code


def _usable(url: str) -> bool:
    return url.startswith("https://") or url.startswith("http://")


def _payload(spec: WakeSpec, api_base_url: str) -> dict[str, Any]:
    return {
        "event": spec.event,
        "day_id": spec.day_id,
        "slice_index": spec.slice_index,
        "seat": spec.seat,
        "slice_ends_at_unix": spec.ends_at_unix,
        "api_base_url": api_base_url,
        "render_url": spec.render_url,
    }


def _view(record: dict[str, Any]) -> dict[str, Any]:
    """Fields safe to return from the cron. No URL and no key."""
    last_http = record.get("last_http")
    return {
        "event": record.get("event"),
        "day_id": record.get("day_id"),
        "slice_index": record.get("slice_index"),
        "seat": record.get("seat"),
        "status": record.get("status"),
        "attempts": int(record.get("attempts") or 0),
        "last_http": last_http if isinstance(last_http, int) else None,
    }


def _empty(spec: WakeSpec, status: str, attempts: int = 0) -> dict[str, Any]:
    return {
        "event": spec.event,
        "day_id": spec.day_id,
        "slice_index": spec.slice_index,
        "seat": spec.seat,
        "status": status,
        "attempts": attempts,
        "last_http": None,
    }


class WakeDispatcher:
    def __init__(
        self,
        hooks: dict[str, tuple[str, str]],
        api_base_url: str,
        log: WakeLog,
        post: Poster = post_wake,
    ):
        self.hooks = hooks
        self.api_base_url = api_base_url
        self.log = log
        self.post = post

    def enabled(self) -> bool:
        return any(_usable(url) for url, _key in self.hooks.values())

    def dispatch(self, specs: list[WakeSpec]) -> list[dict[str, Any]]:
        if not self.enabled():
            return []
        return [self._one(spec) for spec in specs]

    def _one(self, spec: WakeSpec) -> dict[str, Any]:
        pair = self.hooks.get(spec.seat)
        url = pair[0] if pair else ""
        key = pair[1] if pair else ""
        if not _usable(url):
            return _empty(spec, "unset")
        existing = self.log.read(spec.storage_key)
        if existing and existing.get("status") == "sent":
            return _view(existing)
        if not spec.eligible:
            attempts = int(existing.get("attempts") or 0) if existing else 0
            return _empty(spec, "skipped", attempts)
        attempts = int(existing.get("attempts") or 0) if existing else 0
        if existing and attempts >= MAX_ATTEMPTS:
            return _view(existing)
        record = self._claim(spec, existing)
        if record is None:
            current = self.log.read(spec.storage_key)
            return _view(current) if current else _empty(spec, "pending")
        payload = _payload(spec, self.api_base_url)
        encoded = json.dumps(payload)
        if key and key in encoded:
            logger.error("refusing wake for %s: payload contained the webhook key", spec.seat)
            record["status"] = "failed"
            record["last_http"] = None
            record["updated_at"] = int(time.time())
            self.log.save(spec.storage_key, record)
            return _view(record)
        try:
            code = self.post(url, key, payload)
        except Exception:
            logger.warning("wake post failed for %s slice %s", spec.seat, spec.slice_index)
            code = 0
        record["last_http"] = code
        record["status"] = "sent" if 200 <= code < 300 else "failed"
        record["updated_at"] = int(time.time())
        if record["status"] == "sent":
            record["sent_at"] = record["updated_at"]
        try:
            self.log.save(spec.storage_key, record)
        except Exception:
            logger.warning("could not record wake result for %s", spec.seat)
        return _view(record)

    def _claim(self, spec: WakeSpec, existing: dict[str, Any] | None) -> dict[str, Any] | None:
        now = int(time.time())
        if existing is None:
            record = {
                "event": spec.event,
                "day_id": spec.day_id,
                "slice_index": spec.slice_index,
                "seat": spec.seat,
                "status": "pending",
                "attempts": 1,
                "last_http": None,
                "sent_at": None,
                "updated_at": now,
            }
            if not self.log.create(spec.storage_key, record):
                return None
            return record
        status = existing.get("status")
        attempts = int(existing.get("attempts") or 0)
        if status == "pending" and now - int(existing.get("updated_at") or 0) < PENDING_STALE_SECONDS:
            return None
        if attempts >= MAX_ATTEMPTS:
            return None
        nxt = dict(existing)
        nxt["attempts"] = attempts + 1
        nxt["status"] = "pending"
        nxt["updated_at"] = now
        nxt["event"] = spec.event
        if not self.log.replace(spec.storage_key, attempts, nxt):
            return None
        return nxt
