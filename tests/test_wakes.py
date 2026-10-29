"""Server cron, wake dedup, and JSON errors for a crashed tick.

Slice close is still derived from the clock. These tests check that the server
notices the open slice and wakes its seat without a host ping.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import blob_db
from app.config import ROOT, Settings, clean_base_url
from app.main import create_app
from app.wakes import BlobWakeLog, FileWakeLog, WakeDispatcher, WakeSpec
from tests.conftest import KEY, auth, host, open_design, pin

CRON = "cron-test-secret"
CLAUDE_KEY = "wake-key-claude-do-not-leak"
GROK_KEY = "wake-key-grok-do-not-leak"
ROSS_KEY = "wake-key-ross-do-not-leak"
HOST_KEY = "wake-key-host-do-not-leak"
BASE = "https://pixel.test"


class HookBox:
    def __init__(self, codes: list[int]):
        self.codes = list(codes)
        self.hits: list[dict] = []
        self.url = ""
        self._lock = threading.Lock()

    def next_code(self, path: str, headers, body: bytes) -> int:
        with self._lock:
            code = self.codes.pop(0) if self.codes else 200
            self.hits.append(
                {
                    "path": path,
                    "auth": headers.get("Authorization"),
                    "body": body,
                    "code": code,
                }
            )
            return code


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        code = self.server.box.next_code(self.path, self.headers, body)  # type: ignore[attr-defined]
        raw = b"{}"
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        return


@contextmanager
def webhook(codes: list[int] | None = None):
    box = HookBox(codes or [200])
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.box = box  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host_name, port = server.server_address
    box.url = f"http://{host_name}:{port}/wake"
    try:
        yield box
    finally:
        server.shutdown()
        server.server_close()


def cron_headers():
    return {"Authorization": f"Bearer {CRON}"}


def app_client(tmp_path, wakes=None, cron_secret=CRON, base=BASE):
    app = create_app(
        Settings(
            db_path=tmp_path / "tile.db",
            api_key=KEY,
            seed=False,
            pin_morning=False,
            skill_path=ROOT / "skill.md",
            seat_tokens={"seat-claude": "claude"},
            cron_secret=cron_secret,
            public_base_url=base,
            wakes=wakes or {},
        )
    )
    return app, TestClient(app)


def bodies(box: HookBox) -> list[dict]:
    return [json.loads(hit["body"].decode()) for hit in box.hits]


def assert_no_secrets(raw: str):
    for secret in (KEY, "seat-claude", CLAUDE_KEY, GROK_KEY, ROSS_KEY, HOST_KEY, CRON):
        assert secret not in raw


def test_base_url_drops_userinfo_path_and_query():
    assert clean_base_url("https://user:secret@pixel.test/watch?k=1") == "https://pixel.test"
    assert clean_base_url("pixel.test") == "https://pixel.test"
    assert clean_base_url("") == ""


def test_cron_is_refused_without_its_own_secret_and_opens_the_morning_once(tmp_path):
    app, client = app_client(tmp_path)
    frozen = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    try:
        with client:
            with patch("app.store.datetime", FrozenDateTime):
                assert client.get("/cron/tick").status_code == 401
                assert client.get("/cron/tick", headers=host()).status_code == 401
                assert client.get("/cron/tick", headers={"Authorization": "Bearer no"}).status_code == 401
                assert client.get("/watch/state").json()["status"] == "no_day"
                first = client.get("/cron/tick", headers=cron_headers())
                assert first.status_code == 200, first.text
                assert first.headers["cache-control"] == "no-store"
                body = first.json()
                assert body["phase"] == "awaiting_design"
                assert body["wakes"] == []
                day_id = body["day"]["day_id"]
                second = client.get("/cron/tick", headers=cron_headers()).json()
                assert second["day"]["day_id"] == day_id
                host_tick = client.post("/host/routine/tick", headers=host()).json()
                assert host_tick["day"]["day_id"] == day_id
                assert "wakes" not in host_tick
    finally:
        app.state.store.close()


def test_cron_before_eight_does_not_open_a_canvas(tmp_path):
    app, client = app_client(tmp_path)
    try:
        with client:
            pin(client, "2026-10-28T07:00:00")
            early = client.get("/cron/tick", headers=cron_headers())
            assert early.status_code == 200, early.text
            assert early.json()["phase"] == "before_open"
            assert client.get("/watch/state").json()["status"] == "no_day"
    finally:
        app.state.store.close()


def test_watch_turn_and_act_wake_once_and_the_body_has_no_secrets(tmp_path):
    with webhook() as box:
        app, client = app_client(
            tmp_path,
            wakes={
                "claude": (box.url, CLAUDE_KEY),
                "grok-artist": (box.url, GROK_KEY),
                "ross": (box.url, ROSS_KEY),
            },
        )
        try:
            with client:
                opened = open_design(client, "2026-10-28T08:00:00")
                day_id = opened["day_id"]
                assert box.hits == []
                watched = client.get("/watch/state")
                assert watched.status_code == 200
                assert len(box.hits) == 1
                payload = bodies(box)[0]
                turn = client.get("/agent/turn", headers=auth("claude")).json()
                assert len(box.hits) == 1
                assert payload == {
                    "event": "slice_open",
                    "day_id": day_id,
                    "slice_index": 0,
                    "seat": "claude",
                    "slice_ends_at_unix": turn["slice_ends_at_unix"],
                    "api_base_url": BASE,
                    "render_url": f"/watch/canvas.png?day_id={day_id}&scale=2",
                }
                assert box.hits[0]["auth"] == f"Bearer {CLAUDE_KEY}"
                assert_no_secrets(box.hits[0]["body"].decode())
                stored = "".join(
                    path.read_text(encoding="utf-8") for path in (tmp_path / "wakes").glob("*.json")
                )
                assert_no_secrets(stored)
                assert "http://" not in stored
                painted = client.post(
                    "/agent/act",
                    headers=auth("claude"),
                    json={"type": "paint", "pixels": [{"x": 2, "y": 2, "color": 3}]},
                )
                assert painted.status_code == 200, painted.text
                sent = bodies(box)
                assert [item["seat"] for item in sent] == ["claude", "grok-artist", "ross"]
                assert sent[1]["event"] == "slice_open"
                assert sent[1]["slice_index"] == 1
                assert sent[1]["seat"] == "grok-artist"
                assert sent[2]["event"] == "slice_closed"
                assert sent[2]["seat"] == "ross"
                assert sent[2]["slice_index"] == 0
                assert sent[2]["render_url"] == f"/watch/canvas.png?day_id={day_id}&scale=2"
                assert box.hits[1]["auth"] == f"Bearer {GROK_KEY}"
                assert box.hits[2]["auth"] == f"Bearer {ROSS_KEY}"
                for hit in box.hits:
                    assert_no_secrets(hit["body"].decode())
                client.get("/watch/state")
                assert len(box.hits) == 3
        finally:
            app.state.store.close()


def test_cron_wakes_the_artist_who_owns_the_slice_after_a_timeout(tmp_path):
    with webhook() as box:
        app, client = app_client(
            tmp_path,
            wakes={
                "claude": (box.url, CLAUDE_KEY),
                "grok-artist": (box.url, GROK_KEY),
                "ross": (box.url, ROSS_KEY),
            },
        )
        try:
            with client:
                open_design(client, "2026-10-28T08:00:00")
                pin(client, "2026-10-28T08:03:00")
                tick = client.get("/cron/tick", headers=cron_headers())
                assert tick.status_code == 200, tick.text
                body = tick.json()
                assert body["phase"] == "slice"
                assert body["slice_index"] == 1
                assert body["current_agent_id"] == "grok-artist"
                assert_no_secrets(tick.text)
                assert box.url not in tick.text
                state = client.get("/watch/state").json()
                assert state["slices"][0]["state"] == "missed"
                assert state["current_agent_id"] == "grok-artist"
                sent = bodies(box)
                assert [item["seat"] for item in sent] == ["grok-artist", "ross"]
                assert sent[0]["slice_index"] == 1
                assert sent[0]["event"] == "slice_open"
                assert sent[1]["event"] == "slice_closed"
                assert sent[1]["slice_index"] == 0
                again = client.get("/cron/tick", headers=cron_headers())
                assert again.status_code == 200
                assert len(box.hits) == 2
        finally:
            app.state.store.close()


def test_failed_wakes_retry_then_stop_and_do_not_fire_after_the_slice_closes(tmp_path):
    with webhook([500, 500, 200]) as box:
        app, client = app_client(tmp_path, wakes={"claude": (box.url, CLAUDE_KEY)})
        try:
            with client:
                open_design(client, "2026-10-28T08:00:00")
                assert client.get("/watch/state").status_code == 200
                assert client.get("/agent/turn", headers=auth("grok-artist")).status_code == 200
                tick = client.get("/cron/tick", headers=cron_headers())
                assert tick.status_code == 200
                assert [hit["code"] for hit in box.hits] == [500, 500, 200]
                assert tick.json()["wakes"][0]["status"] == "sent"
                assert tick.json()["wakes"][0]["attempts"] == 3
                client.get("/watch/state")
                assert len(box.hits) == 3
        finally:
            app.state.store.close()

    with webhook([500, 200]) as box:
        app, client = app_client(tmp_path / "late", wakes={"claude": (box.url, CLAUDE_KEY)})
        try:
            with client:
                open_design(client, "2026-10-28T08:00:00")
                assert client.get("/watch/state").status_code == 200
                assert box.hits[0]["code"] == 500
                pin(client, "2026-10-28T08:03:00")
                client.get("/cron/tick", headers=cron_headers())
                client.get("/agent/turn", headers=auth("claude"))
                assert len(box.hits) == 1
        finally:
            app.state.store.close()


def test_three_failures_are_not_followed_by_a_fourth_while_the_slice_is_open(tmp_path):
    with webhook([500, 500, 500, 200]) as box:
        app, client = app_client(tmp_path, wakes={"claude": (box.url, CLAUDE_KEY)})
        try:
            with client:
                open_design(client, "2026-10-28T08:00:00")
                for _ in range(4):
                    assert client.get("/watch/state").status_code == 200
                assert [hit["code"] for hit in box.hits] == [500, 500, 500]
        finally:
            app.state.store.close()


def test_unset_wake_urls_are_a_noop(tmp_path):
    app, client = app_client(tmp_path, wakes={})
    try:
        with client:
            open_design(client, "2026-10-28T08:00:00")
            painted = client.post(
                "/agent/act",
                headers=auth("claude"),
                json={"type": "pass"},
            )
            assert painted.status_code == 200, painted.text
            assert client.get("/cron/tick", headers=cron_headers()).json()["wakes"] == []
            assert list((tmp_path / "wakes").glob("*.json")) == []
    finally:
        app.state.store.close()


def test_host_is_woken_once_when_the_canvas_locks(tmp_path):
    with webhook() as box:
        app, client = app_client(tmp_path, wakes={"host": (box.url, HOST_KEY), "ross": (box.url, ROSS_KEY)})
        try:
            with client:
                opened = open_design(client, "2026-10-28T08:00:00")
                pin(client, "2026-10-28T08:36:00")
                tick = client.get("/cron/tick", headers=cron_headers())
                assert tick.status_code == 200, tick.text
                assert tick.json()["phase"] == "locked"
                sent = bodies(box)
                assert {item["event"] for item in sent} == {"slice_closed", "canvas_locked"}
                host_wake = next(item for item in sent if item["seat"] == "host")
                assert host_wake["event"] == "canvas_locked"
                assert host_wake["render_url"] == f"/watch/canvas.png?day_id={opened['day_id']}&scale=2"
                assert host_wake["api_base_url"] == BASE
                assert box.hits[sent.index(host_wake)]["auth"] == f"Bearer {HOST_KEY}"
                client.get("/cron/tick", headers=cron_headers())
                assert len(box.hits) == 2
        finally:
            app.state.store.close()


def test_lock_wake_is_not_sent_after_the_grace_window(tmp_path):
    with webhook([200]) as box:
        app, client = app_client(tmp_path, wakes={"host": (box.url, HOST_KEY), "ross": (box.url, ROSS_KEY)})
        try:
            with client:
                open_design(client, "2026-10-28T08:00:00")
                pin(client, "2026-10-28T08:40:00")
                client.get("/cron/tick", headers=cron_headers())
                assert box.hits == []
        finally:
            app.state.store.close()


def test_tick_crash_returns_json(tmp_path):
    app, client = app_client(tmp_path)
    try:
        quiet = TestClient(app, raise_server_exceptions=False)

        def boom(*_args, **_kwargs):
            raise RuntimeError("disk failed")

        app.state.store.routine_tick = boom
        with quiet:
            missing = quiet.post("/host/routine/tick")
            assert missing.status_code == 401
            assert missing.json()["error"] == "unauthorized"
            crashed = quiet.post("/host/routine/tick", headers=host())
            assert crashed.status_code == 500
            assert crashed.json() == {"error": "internal", "message": "The request failed."}
            assert "disk failed" not in crashed.text
            assert crashed.headers["content-type"].startswith("application/json")
            cron = quiet.get("/cron/tick", headers=cron_headers())
            assert cron.status_code == 500
            assert cron.json()["error"] == "internal"
            unknown = quiet.get("/no-such-route")
            assert unknown.status_code == 404
            assert unknown.json().get("error") != "internal"
    finally:
        app.state.store.close()


def test_concurrent_claims_post_one_wake(tmp_path):
    calls: list[str] = []
    lock = threading.Lock()

    def post(_url, key, payload):
        with lock:
            calls.append(payload["seat"])
            assert key not in json.dumps(payload)
        time.sleep(0.05)
        return 200

    dispatcher = WakeDispatcher(
        {"claude": ("http://hooks.test/wake", CLAUDE_KEY)},
        BASE,
        FileWakeLog(tmp_path / "wakes"),
        post=post,
    )
    spec = WakeSpec(
        storage_key="4-0-claude",
        event="slice_open",
        day_id=4,
        slice_index=0,
        seat="claude",
        ends_at_unix=1_700_000_000,
        eligible=True,
        render_url="/watch/canvas.png?day_id=4&scale=2",
    )
    threads = [threading.Thread(target=dispatcher.dispatch, args=([spec],)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == ["claude"]


def test_blob_wake_log_rejects_a_second_claim(tmp_path, monkeypatch):
    blobs: dict[str, tuple[bytes, str]] = {}

    def put_bytes(name, body, *, exclusive, if_match=None, content_type="application/json"):
        current = blobs.get(name)
        if exclusive and current is not None:
            raise blob_db.BlobConflict()
        if if_match is not None and (current is None or current[1] != if_match):
            raise blob_db.BlobConflict()
        etag = f"e{len(blobs)}-{len(body)}"
        blobs[name] = (bytes(body), etag)
        return etag

    def download(name):
        if name not in blobs:
            raise blob_db.BlobMissing()
        body, etag = blobs[name]
        return body, etag

    monkeypatch.setattr(blob_db, "put_bytes", put_bytes)
    monkeypatch.setattr(blob_db, "download", download)
    calls: list[int] = []

    def post(_url, _key, _payload):
        calls.append(1)
        return 200

    dispatcher = WakeDispatcher(
        {"openai": ("https://hooks.test/wake", "blob-key")},
        BASE,
        BlobWakeLog(prefix="agent-pixel"),
        post=post,
    )
    spec = WakeSpec(
        storage_key="9-2-openai",
        event="slice_open",
        day_id=9,
        slice_index=2,
        seat="openai",
        ends_at_unix=1_700_000_180,
        eligible=True,
        render_url="/watch/canvas.png?day_id=9&scale=2",
    )
    first = dispatcher.dispatch([spec])
    second = dispatcher.dispatch([spec])
    assert first[0]["status"] == "sent"
    assert second[0]["status"] == "sent"
    assert calls == [1]
    assert list(blobs) == ["agent-pixel/wakes/9-2-openai.json"]
    assert b"blob-key" not in blobs["agent-pixel/wakes/9-2-openai.json"][0]
