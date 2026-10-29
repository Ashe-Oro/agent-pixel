"""Per-seat wake webhooks.

A seat is any id the host configures. The painter rotation is still the
morning roster; delivery does not special-case the three artists. A seat
with no webhook is never POSTed and keeps polling ``GET /agent/turn``.

The signed body is the raw JSON bytes. Artists verify
``X-Agent-Pixel-Signature`` over ``{timestamp}.{body}``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.schedule import SLICE_SECONDS, SliceMark, artist_on_slice

ALLOWED_EVENTS = ("slice_open", "slice_closed", "canvas_locked", "awaiting_design")
MAX_ATTEMPTS = 3
INFLIGHT_STALE_SECONDS = 15
MAX_POSTS_PER_DISPATCH = 8
TIMESTAMP_HEADER = "X-Agent-Pixel-Timestamp"
SIGNATURE_HEADER = "X-Agent-Pixel-Signature"
_SEAT_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_BANNED_PAYLOAD_KEYS = ("secret", "token", "api_key", "authorization", "password")

WAKE_DDL = """
CREATE TABLE IF NOT EXISTS wakes (
  day_id INTEGER NOT NULL,
  slice_index INTEGER NOT NULL,
  seat_id TEXT NOT NULL,
  event TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL,
  last_http INTEGER,
  claim TEXT,
  updated_at INTEGER NOT NULL,
  sent_at INTEGER,
  PRIMARY KEY (day_id, slice_index, seat_id, event)
);
CREATE TABLE IF NOT EXISTS seat_webhooks (
  seat_id TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  secret TEXT NOT NULL,
  events_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
"""


class WebhookConfigError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class SeatHook:
    seat_id: str
    url: str
    secret: str
    events: frozenset[str]


@dataclass
class WakeIntent:
    day_id: int
    slice_index: int
    seat_id: str
    event: str
    url: str
    secret: str
    payload: dict[str, Any]
    claim: str = ""
    attempts: int = 0


def public_base_url() -> str:
    raw = os.environ.get("TILE_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if raw:
        return raw
    prod = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL", "").strip()
    if prod:
        return "https://" + prod.rstrip("/")
    deployed = os.environ.get("VERCEL_URL", "").strip()
    if deployed:
        return "https://" + deployed.rstrip("/")
    return ""


def normalize_seat(seat_id: str) -> str:
    text = seat_id.strip().lower()
    if not _SEAT_RE.fullmatch(text):
        raise WebhookConfigError(
            "Seat id must be 1–64 characters: lowercase letters, digits, and hyphens."
        )
    return text


def parse_seat_hook(seat_id: str, body: dict[str, Any]) -> SeatHook:
    if not isinstance(body, dict):
        raise WebhookConfigError("Webhook config must be an object.")
    seat = normalize_seat(seat_id)
    url = _validate_url(body.get("url"))
    secret = _validate_secret(body.get("secret"))
    events = _validate_events(body.get("events", ["slice_open"]))
    return SeatHook(seat, url, secret, events)


def hooks_from_env() -> dict[str, SeatHook]:
    raw = os.environ.get("TILE_SEAT_WEBHOOKS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WebhookConfigError("TILE_SEAT_WEBHOOKS is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise WebhookConfigError("TILE_SEAT_WEBHOOKS must be a JSON object keyed by seat id.")
    hooks: dict[str, SeatHook] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise WebhookConfigError("TILE_SEAT_WEBHOOKS keys must be seat ids.")
        if not isinstance(value, dict):
            raise WebhookConfigError("Each TILE_SEAT_WEBHOOKS value must be an object.")
        try:
            hook = parse_seat_hook(key, value)
        except WebhookConfigError as exc:
            raise WebhookConfigError(f"Seat webhook config was rejected: {exc.message}") from exc
        hooks[hook.seat_id] = hook
    return hooks


def sign_body(secret: str, timestamp: str, body: bytes) -> str:
    """Hex HMAC-SHA256 of ``timestamp + '.' + raw_body``."""
    message = timestamp.encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def post_webhook(url: str, secret: str, payload: dict[str, Any]) -> int:
    _reject_credential_fields(payload)
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    timestamp = str(int(time.time()))
    signature = sign_body(secret, timestamp, body)
    response = httpx.post(
        url,
        content=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "agent-pixel-wake",
            TIMESTAMP_HEADER: timestamp,
            SIGNATURE_HEADER: f"sha256={signature}",
        },
        timeout=4.0,
        follow_redirects=False,
    )
    return int(response.status_code)


def plan_wakes(
    *,
    day_id: int,
    theme: bool,
    archived: bool,
    phase: str,
    index: int | None,
    slices: tuple[SliceMark, ...],
    order: list[str],
    hooks: list[SeatHook],
    now: datetime,
    lock_at: datetime,
    api_base_url: str,
) -> list[WakeIntent]:
    """Wakes that are still in their delivery window.

    ``slice_open`` is only the seat that owns the live slice.
    ``slice_closed`` is the slice that just closed, while the next one is open
    (reviewers such as ross). After the session locks, that review and
    ``canvas_locked`` stay eligible for one slice length.
    ``awaiting_design`` is only while the morning is open and has no theme.
    """
    if archived or not hooks:
        return []
    by_event: dict[str, list[SeatHook]] = {event: [] for event in ALLOWED_EVENTS}
    for hook in hooks:
        for event in hook.events:
            if event in by_event:
                by_event[event].append(hook)
    intents: list[WakeIntent] = []

    def add(hook: SeatHook, event: str, slice_index: int, ends_at: datetime) -> None:
        payload: dict[str, Any] = {
            "event": event,
            "day_id": day_id,
            "slice": None if slice_index < 0 else slice_index,
            "seat": hook.seat_id,
            "ends_at_unix": int(ends_at.timestamp()),
            "api_base_url": api_base_url,
        }
        if event in ("slice_closed", "canvas_locked") or hook.seat_id == "ross":
            payload["render_url"] = f"/watch/canvas.png?day_id={day_id}&scale=2"
        intents.append(
            WakeIntent(
                day_id=day_id,
                slice_index=slice_index,
                seat_id=hook.seat_id,
                event=event,
                url=hook.url,
                secret=hook.secret,
                payload=payload,
            )
        )

    if not theme:
        if phase == "during":
            for hook in by_event["awaiting_design"]:
                add(hook, "awaiting_design", -2, lock_at)
        return _sorted(intents)

    if phase == "during" and index is not None and order:
        seat = artist_on_slice(order, index)
        mark = slices[index]
        for hook in by_event["slice_open"]:
            if hook.seat_id == seat:
                add(hook, "slice_open", index, mark.deadline)
        if index > 0:
            prev = slices[index - 1]
            if prev.acted or prev.missed:
                for hook in by_event["slice_closed"]:
                    add(hook, "slice_closed", prev.index, prev.closes)
        return _sorted(intents)

    grace = lock_at + timedelta(seconds=SLICE_SECONDS)
    if phase == "after" and now < grace and slices:
        last = slices[-1]
        for hook in by_event["slice_closed"]:
            add(hook, "slice_closed", last.index, last.closes)
        for hook in by_event["canvas_locked"]:
            add(hook, "canvas_locked", -1, lock_at)
    return _sorted(intents)


def needs_attempt(conn: Any, intent: WakeIntent, now_ts: int) -> bool:
    row = _wake_row(conn, intent)
    if row is None:
        return True
    if row["status"] == "sent" or int(row["attempts"]) >= MAX_ATTEMPTS:
        return False
    if row["status"] == "inflight" and now_ts - int(row["updated_at"]) < INFLIGHT_STALE_SECONDS:
        return False
    return True


def claim_intents(conn: Any, intents: list[WakeIntent], now_ts: int, limit: int = MAX_POSTS_PER_DISPATCH) -> list[WakeIntent]:
    """Claim rows this caller will POST. Already-sent wakes are left alone."""
    claimed: list[WakeIntent] = []
    for intent in intents:
        if len(claimed) >= limit:
            break
        row = _wake_row(conn, intent)
        token = uuid.uuid4().hex
        if row is None:
            conn.execute(
                """
                INSERT INTO wakes (
                  day_id, slice_index, seat_id, event, status, attempts,
                  last_http, claim, updated_at, sent_at
                ) VALUES (?, ?, ?, ?, 'inflight', 1, NULL, ?, ?, NULL)
                """,
                (intent.day_id, intent.slice_index, intent.seat_id, intent.event, token, now_ts),
            )
            intent.claim = token
            intent.attempts = 1
            claimed.append(intent)
            continue
        if not needs_attempt(conn, intent, now_ts):
            continue
        cur = conn.execute(
            """
            UPDATE wakes
            SET status = 'inflight', attempts = attempts + 1, claim = ?, updated_at = ?
            WHERE day_id = ? AND slice_index = ? AND seat_id = ? AND event = ? AND claim = ?
            """,
            (
                token,
                now_ts,
                intent.day_id,
                intent.slice_index,
                intent.seat_id,
                intent.event,
                row["claim"],
            ),
        )
        if cur.rowcount != 1:
            continue
        intent.claim = token
        intent.attempts = int(row["attempts"]) + 1
        claimed.append(intent)
    return claimed


def finish_wake(conn: Any, intent: WakeIntent, status: str, last_http: int, now_ts: int) -> bool:
    sent_at = now_ts if status == "sent" else None
    cur = conn.execute(
        """
        UPDATE wakes
        SET status = ?, last_http = ?, updated_at = ?, sent_at = COALESCE(?, sent_at)
        WHERE day_id = ? AND slice_index = ? AND seat_id = ? AND event = ? AND claim = ?
        """,
        (
            status,
            last_http,
            now_ts,
            sent_at,
            intent.day_id,
            intent.slice_index,
            intent.seat_id,
            intent.event,
            intent.claim,
        ),
    )
    return cur.rowcount == 1


def list_wake_rows(conn: Any, day_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT day_id, slice_index, seat_id, event, status, attempts, last_http
        FROM wakes WHERE day_id = ?
        ORDER BY slice_index, seat_id, event
        """,
        (day_id,),
    ).fetchall()
    listed = []
    for row in rows:
        slice_index = int(row["slice_index"])
        listed.append(
            {
                "day_id": int(row["day_id"]),
                "slice": None if slice_index < 0 else slice_index,
                "seat": row["seat_id"],
                "event": row["event"],
                "status": row["status"],
                "attempts": int(row["attempts"]),
                "last_http": row["last_http"],
            }
        )
    return listed


def _wake_row(conn: Any, intent: WakeIntent) -> Any:
    return conn.execute(
        """
        SELECT status, attempts, claim, updated_at FROM wakes
        WHERE day_id = ? AND slice_index = ? AND seat_id = ? AND event = ?
        """,
        (intent.day_id, intent.slice_index, intent.seat_id, intent.event),
    ).fetchone()


def _sorted(intents: list[WakeIntent]) -> list[WakeIntent]:
    priority = {"awaiting_design": 0, "slice_open": 1, "slice_closed": 2, "canvas_locked": 3}
    return sorted(intents, key=lambda item: (priority.get(item.event, 9), item.slice_index, item.seat_id))


def _validate_url(url: object) -> str:
    if not isinstance(url, str):
        raise WebhookConfigError("Webhook url must be a string.")
    text = url.strip()
    if not text or len(text) > 500 or any(char.isspace() for char in text):
        raise WebhookConfigError("Webhook url must be an http(s) URL.")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebhookConfigError("Webhook url must be an http(s) URL.")
    if parts.username or parts.password:
        raise WebhookConfigError("Webhook url must not include credentials.")
    return text


def _validate_secret(secret: object) -> str:
    if not isinstance(secret, str) or secret != secret.strip() or not secret:
        raise WebhookConfigError("Webhook secret must be a non-empty string.")
    if len(secret) > 256 or "\n" in secret or "\r" in secret:
        raise WebhookConfigError("Webhook secret is too long.")
    return secret


def _validate_events(events: object) -> frozenset[str]:
    if events is None:
        return frozenset({"slice_open"})
    if not isinstance(events, list) or not events or not all(isinstance(item, str) for item in events):
        raise WebhookConfigError(
            "events must list slice_open, slice_closed, canvas_locked, or awaiting_design."
        )
    if any(item not in ALLOWED_EVENTS for item in events):
        raise WebhookConfigError(
            "events must list slice_open, slice_closed, canvas_locked, or awaiting_design."
        )
    return frozenset(events)


def _reject_credential_fields(payload: dict[str, Any]) -> None:
    for key in payload:
        lowered = key.lower()
        if any(banned in lowered for banned in _BANNED_PAYLOAD_KEYS):
            raise WebhookConfigError("Wake payload must not carry credentials.")

