"""Server cron, shared slice eligibility side effects, and per-seat wakes."""

import hashlib
import hmac
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.schedule import SLICE_SECONDS
from app.wakes import sign_body
from tests.conftest import auth, host, open_design, pin

NY = ZoneInfo("America/New_York")
SECRET = "signing-secret"
CRON = {"Authorization": "Bearer cron-secret"}


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code


def install_poster(monkeypatch, status=200):
    seen: list[dict] = []

    def fake(url, content=None, headers=None, timeout=None, follow_redirects=None):
        seen.append({"url": url, "body": content, "headers": dict(headers or {})})
        code = status() if callable(status) else status
        return _Response(code)

    monkeypatch.setattr("app.wakes.httpx.post", fake)
    return seen


def set_hook(client, seat, events, url="http://127.0.0.1:9/wake", secret=SECRET, extra=None):
    body = {"url": url, "events": events}
    if secret:
        body["secret"] = secret
    if extra:
        body.update(extra)
    response = client.put(f"/host/seats/{seat}/webhook", headers=host(), json=body)
    assert response.status_code == 200, response.text
    if secret:
        assert secret not in response.text
    return response.json()


def payloads(seen):
    return [json.loads(item["body"]) for item in seen]


def test_sign_body_covers_timestamp_and_raw_body():
    body = b'{"day_id":1,"slice":0}'
    digest = sign_body("top-secret", "1700000000", body)
    assert digest == hmac.new(b"top-secret", b"1700000000." + body, hashlib.sha256).hexdigest()
    assert SLICE_SECONDS == 180


def test_cron_is_authorized_and_opens_one_morning(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    assert client.get("/cron/tick").status_code == 401
    assert client.get("/cron/tick", headers={"Authorization": "Bearer no"}).status_code == 401
    pin(client, "2026-10-28T07:59:00")
    early = client.get("/cron/tick", headers=CRON)
    assert early.status_code == 200, early.text
    assert early.json()["phase"] == "before_open"
    assert early.json()["wakes"] == []
    assert client.get("/watch/state").json()["status"] == "no_day"

    pin(client, "2026-10-28T08:00:00")
    opened = client.get("/cron/tick", headers=CRON)
    assert opened.status_code == 200, opened.text
    assert opened.json()["phase"] == "awaiting_design"
    day_id = opened.json()["day"]["day_id"]
    again = client.get("/cron/tick", headers=CRON)
    assert again.json()["day"]["day_id"] == day_id
    ticked = client.post("/host/routine/tick", headers=host())
    assert ticked.json()["day"]["day_id"] == day_id


def test_cron_advances_a_slice_when_its_deadline_passes(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    open_design(client, "2026-10-28T08:00:00")
    first = client.get("/cron/tick", headers=CRON)
    assert first.json()["phase"] == "slice"
    assert first.json()["slice_index"] == 0
    assert first.json()["current_agent_id"] == "claude"
    pin(client, "2026-10-28T08:03:00")
    second = client.get("/cron/tick", headers=CRON)
    assert second.json()["slice_index"] == 1
    assert second.json()["current_agent_id"] == "grok-artist"
    assert client.get("/agent/turn", headers=auth("grok-artist")).json()["to_act"] is True
    assert client.get("/agent/turn", headers=auth("claude")).json()["to_act"] is False


def test_wake_is_signed_once_and_a_later_slice_still_wakes(client, monkeypatch):
    seen = install_poster(monkeypatch)
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.setenv("TILE_PUBLIC_BASE_URL", "https://pixel.example")
    open_design(client, "2026-10-28T08:00:00")
    saved = set_hook(client, "Claude", ["slice_open"], url="http://127.0.0.1:9/claude")
    assert saved["seat"] == "claude"
    set_hook(client, "ross", ["slice_closed"], url="http://127.0.0.1:9/ross")
    set_hook(client, "Studio-Guest", ["slice_closed"], url="http://127.0.0.1:9/guest")

    turn = client.get("/agent/turn", headers=auth("claude"))
    assert turn.status_code == 200
    assert len(seen) == 1
    client.get("/watch/state")
    client.get("/cron/tick", headers=CRON)
    assert len(seen) == 1

    body = seen[0]["body"]
    headers = {key.lower(): value for key, value in seen[0]["headers"].items()}
    stamp = headers["x-agent-pixel-timestamp"]
    expected = hmac.new(SECRET.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert headers["x-agent-pixel-signature"] == f"sha256={expected}"
    assert headers["user-agent"] == "agent-pixel-wake"
    payload = json.loads(body)
    start = datetime(2026, 10, 28, 8, 0, tzinfo=NY)
    assert payload == {
        "api_base_url": "https://pixel.example",
        "brief": "Warm cabin, tall pines, soft chimney smoke, night sky",
        "day_id": turn.json()["day_id"],
        "director_note": None,
        "ends_at_unix": int((start + timedelta(seconds=180)).timestamp()),
        "event": "slice_open",
        "seat": "claude",
        "slice": 0,
    }
    assert "brief" not in client.get("/watch/state").json()
    assert SECRET not in body.decode()
    assert "test-key" not in body.decode()
    assert "seat-claude" not in body.decode()

    passed = client.post("/agent/act", headers=auth("claude"), json={"type": "pass"})
    assert passed.status_code == 200, passed.text
    assert [item["seat"] for item in payloads(seen)] == ["claude", "ross", "studio-guest"]
    for item in payloads(seen)[1:]:
        assert item["event"] == "slice_closed"
        assert item["slice"] == 0
        assert item["render_url"] == f"/watch/canvas.png?day_id={item['day_id']}&scale=2"
        assert item["seat"] in ("ross", "studio-guest")
    client.get("/cron/tick", headers=CRON)
    assert len(seen) == 3

    listed = client.get("/host/wakes", headers=host())
    assert listed.status_code == 200
    assert {row["status"] for row in listed.json()["wakes"]} == {"sent"}
    assert client.get("/host/wakes").status_code == 401
    directory = client.get("/host/seats/webhooks", headers=host()).json()
    assert SECRET not in json.dumps(directory)
    assert {item["seat"] for item in directory["seats"]} == {"claude", "ross", "studio-guest"}


def test_failed_wakes_retry_until_sent_and_stop_when_the_slice_closes(client, monkeypatch):
    codes = iter([500, 200])
    seen = install_poster(monkeypatch, status=lambda: next(codes))
    open_design(client, "2026-10-28T08:00:00")
    set_hook(client, "claude", ["slice_open"])
    client.get("/watch/state")
    assert len(seen) == 1
    assert payloads(seen)[0]["slice"] == 0
    client.get("/agent/turn", headers=auth("claude"))
    assert len(seen) == 2
    client.get("/watch/state")
    assert len(seen) == 2
    row = client.get("/host/wakes", headers=host()).json()["wakes"][0]
    assert row["status"] == "sent"
    assert row["attempts"] == 2
    assert row["last_http"] == 200


def test_retries_stop_at_three_while_the_slice_is_open(client, monkeypatch):
    seen = install_poster(monkeypatch, status=500)
    open_design(client, "2026-10-28T08:00:00")
    set_hook(client, "claude", ["slice_open"])
    for _ in range(4):
        client.get("/watch/state")
    assert len(seen) == 3
    row = client.get("/host/wakes", headers=host()).json()["wakes"][0]
    assert row["attempts"] == 3
    assert row["status"] == "failed"


def test_a_failed_wake_is_not_retried_after_the_slice_closes(client, monkeypatch):
    seen = install_poster(monkeypatch, status=500)
    open_design(client, "2026-10-28T08:00:00")
    set_hook(client, "claude", ["slice_open"])
    client.get("/watch/state")
    assert len(seen) == 1
    pin(client, "2026-10-28T08:03:00")
    client.get("/watch/state")
    client.get("/agent/turn", headers=auth("claude"))
    assert len(seen) == 1


def test_clearing_a_webhook_beats_the_env_map(client, monkeypatch):
    seen = install_poster(monkeypatch)
    monkeypatch.setenv(
        "TILE_SEAT_WEBHOOKS",
        json.dumps({"grok-artist": {"url": "http://127.0.0.1:9/grok", "secret": "env-secret-value"}}),
    )
    open_design(client, "2026-10-28T08:00:00")
    cleared = client.delete("/host/seats/grok-artist/webhook", headers=host())
    assert cleared.status_code == 200
    assert cleared.json()["enabled"] is False
    passed = client.post("/agent/act", headers=auth("claude"), json={"type": "pass"})
    assert passed.status_code == 200, passed.text
    assert seen == []
    directory = client.get("/host/seats/webhooks", headers=host()).json()["seats"]
    assert directory == [
        {
            "seat": "grok-artist",
            "enabled": False,
            "source": "host",
            "events": [],
            "url": "",
            "bearer_set": False,
        }
    ]


def test_env_webhook_wakes_the_open_seat(client, monkeypatch):
    seen = install_poster(monkeypatch)
    monkeypatch.setenv(
        "TILE_SEAT_WEBHOOKS",
        json.dumps(
            {
                "openai": {"url": "http://127.0.0.1:9/openai", "secret": "env-secret-value"},
                "design": {
                    "url": "http://127.0.0.1:9/design",
                    "secret": "env-secret-value",
                    "events": ["awaiting_design"],
                },
            }
        ),
    )
    pin(client, "2026-10-28T08:00:00")
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    opened = client.get("/cron/tick", headers=CRON)
    assert opened.status_code == 200, opened.text
    assert [item["event"] for item in payloads(seen)] == ["awaiting_design"]
    assert payloads(seen)[0]["seat"] == "design"
    assert payloads(seen)[0]["slice"] is None
    client.get("/cron/tick", headers=CRON)
    assert len(seen) == 1


def test_canvas_locked_wakes_a_host_seat(client, monkeypatch):
    seen = install_poster(monkeypatch)
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.setenv("TILE_PUBLIC_BASE_URL", "https://pixel.example")
    open_design(client, "2026-10-28T08:00:00")
    set_hook(client, "host", ["canvas_locked"], url="http://127.0.0.1:9/host")
    set_hook(client, "ross", ["slice_closed"], url="http://127.0.0.1:9/ross")
    pin(client, "2026-10-28T08:36:00")
    response = client.get("/cron/tick", headers=CRON)
    assert response.status_code == 200, response.text
    assert response.json()["phase"] == "locked"
    by_seat = {item["seat"]: item for item in payloads(seen)}
    assert set(by_seat) == {"host", "ross"}
    assert by_seat["host"]["event"] == "canvas_locked"
    assert by_seat["host"]["slice"] is None
    assert by_seat["host"]["render_url"] == f"/watch/canvas.png?day_id={by_seat['host']['day_id']}&scale=2"
    assert by_seat["ross"]["event"] == "slice_closed"
    assert by_seat["ross"]["slice"] == 11
    client.get("/cron/tick", headers=CRON)
    assert len(seen) == 2


def test_bad_webhook_config_and_tick_crashes_are_json(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    rejected = client.put(
        "/host/seats/claude/webhook",
        headers=host(),
        json={"url": "javascript:alert(1)", "secret": "signing-secret", "events": ["nope"]},
    )
    assert rejected.status_code == 400
    assert rejected.headers["content-type"].startswith("application/json")
    monkeypatch.setenv("TILE_SEAT_WEBHOOKS", "{")
    pin(client, "2026-10-28T08:00:00")
    cron = client.get("/cron/tick", headers=CRON)
    assert cron.status_code == 500
    assert cron.json()["error"] == "bad_webhook_config"

    def boom():
        raise RuntimeError("disk failed")

    monkeypatch.setattr(client.app.state.store, "routine_tick", boom)
    crashed = client.post("/host/routine/tick", headers=host())
    assert crashed.status_code == 500
    assert crashed.json() == {"error": "internal", "message": "Internal server error."}
    assert "disk failed" not in crashed.text
    cron_crashed = client.get("/cron/tick", headers=CRON)
    assert cron_crashed.status_code == 500
    assert cron_crashed.json()["error"] == "internal"


def test_bearer_is_sent_and_never_returned(client, monkeypatch):
    seen = install_poster(monkeypatch)
    bearer = "sender-key-do-not-leak"
    saved = set_hook(
        client,
        "claude",
        ["slice_open"],
        secret="",
        extra={"bearer": bearer},
    )
    assert saved["bearer_set"] is True
    assert bearer not in json.dumps(saved)
    open_design(client, "2026-10-28T08:00:00")
    client.get("/agent/turn", headers=auth("claude"))
    assert len(seen) == 1
    headers = {key.lower(): value for key, value in seen[0]["headers"].items()}
    assert headers["authorization"] == f"Bearer {bearer}"
    assert "x-agent-pixel-signature" not in headers
    assert bearer.encode() not in seen[0]["body"]
    listed = client.get("/host/seats/webhooks", headers=host())
    assert listed.status_code == 200
    assert bearer not in listed.text
    assert listed.json()["seats"][0]["bearer_set"] is True
    wakes = client.get("/host/wakes", headers=host())
    assert bearer not in wakes.text

    both = client.put(
        "/host/seats/grok-artist/webhook",
        headers=host(),
        json={
            "url": "http://127.0.0.1:9/grok",
            "secret": SECRET,
            "bearer": bearer,
            "events": ["slice_open"],
        },
    )
    assert both.status_code == 200, both.text
    assert bearer not in both.text
    assert SECRET not in both.text
    assert both.json()["bearer_set"] is True
    client.post("/agent/act", headers=auth("claude"), json={"type": "pass"})
    grok = next(item for item in seen if item["url"].endswith("/grok"))
    grok_headers = {key.lower(): value for key, value in grok["headers"].items()}
    assert grok_headers["authorization"] == f"Bearer {bearer}"
    body = grok["body"]
    stamp = grok_headers["x-agent-pixel-timestamp"]
    expected = hmac.new(SECRET.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert grok_headers["x-agent-pixel-signature"] == f"sha256={expected}"
    assert bearer.encode() not in body

    missing = client.put(
        "/host/seats/openai/webhook",
        headers=host(),
        json={"url": "http://127.0.0.1:9/openai", "events": ["slice_open"]},
    )
    assert missing.status_code == 400
    assert missing.json()["error"] == "bad_webhook"


def test_env_bearer_wakes_without_an_hmac_secret(client, monkeypatch):
    seen = install_poster(monkeypatch)
    bearer = "env-sender-key-do-not-leak"
    monkeypatch.setenv(
        "TILE_SEAT_WEBHOOKS",
        json.dumps({"claude": {"url": "http://127.0.0.1:9/claude", "bearer": bearer}}),
    )
    open_design(client, "2026-10-28T08:00:00")
    client.get("/watch/state")
    assert len(seen) == 1
    headers = {key.lower(): value for key, value in seen[0]["headers"].items()}
    assert headers["authorization"] == f"Bearer {bearer}"
    directory = client.get("/host/seats/webhooks", headers=host())
    assert bearer not in directory.text
    assert directory.json()["seats"][0]["bearer_set"] is True
    assert directory.json()["seats"][0]["source"] == "env"


def test_seat_without_a_webhook_still_polls(client, monkeypatch):
    seen = install_poster(monkeypatch)
    open_design(client, "2026-10-28T08:00:00")
    turn = client.get("/agent/turn", headers=auth("claude"))
    assert turn.json()["to_act"] is True
    assert seen == []
