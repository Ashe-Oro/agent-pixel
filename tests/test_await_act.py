"""await_act pacing: a solo turn waits for the painter instead of timing out."""

import json

from tests.conftest import DESIGN, auth, pin
from tests.test_wakes import CRON, install_poster, payloads, set_hook

ROSS = {"X-Seat-Token": "seat-ross"}


def _design(client, **extra):
    body = dict(DESIGN)
    body.update(extra)
    return client.post("/agent/design", headers=ROSS, json=body)


def _opens(seen):
    return [item for item in payloads(seen) if item["event"] == "slice_open"]


def _paint(client, color=1, n=4):
    pixels = [{"x": i, "y": 0, "color": color} for i in range(n)]
    return client.post("/agent/act", headers=ROSS, json={"type": "paint", "pixels": pixels})


def _setup(client, monkeypatch, events=("design_needed", "canvas_locked")):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    seen = install_poster(monkeypatch)
    set_hook(client, "ross", list(events))
    pin(client, "2026-11-05T08:00:00")
    assert client.get("/cron/tick", headers=CRON).status_code == 200
    return seen


def test_default_pacing_is_timed_and_turns_still_time_out(client, monkeypatch):
    seen = _setup(client, monkeypatch)
    posted = _design(client)
    assert posted.status_code == 200, posted.text
    assert posted.json()["pacing"] == "timed"
    assert posted.json()["seconds_per_turn"] == 180
    pin(client, "2026-11-05T08:03:30")
    client.get("/cron/tick", headers=CRON)
    desk = client.get("/agent/turn", headers=ROSS).json()
    assert desk["turn"] == 2
    assert desk["turn_times_out"] is True
    assert [item["slice"] for item in _opens(seen)] == [0, 1]


def test_await_act_turn_waits_and_wakes_once(client, monkeypatch):
    seen = _setup(client, monkeypatch)
    posted = _design(client, pacing="await_act")
    assert posted.status_code == 200, posted.text
    assert posted.json()["pacing"] == "await_act"
    assert posted.json()["seconds_per_turn"] is None

    wake = _opens(seen)[0]
    assert wake["turn"] == 1
    assert wake["pacing"] == "await_act"
    assert wake["turn_times_out"] is False
    assert wake["seconds_per_turn"] is None
    assert wake["ends_at_unix"] == wake["canvas_ends_at_unix"]

    # Forty minutes of idle cron ticks: still turn 1, still one wake.
    for minute in range(1, 41):
        pin(client, f"2026-11-05T08:{minute:02d}:00")
        assert client.get("/cron/tick", headers=CRON).status_code == 200
    desk = client.get("/agent/turn", headers=ROSS).json()
    assert desk["to_act"] is True
    assert desk["turn"] == 1
    assert desk["slice_index"] == 0
    assert desk["seconds_per_turn"] is None
    assert desk["turn_times_out"] is False
    assert desk["slice_ends_at_unix"] == desk["canvas_ends_at_unix"]
    assert len(_opens(seen)) == 1
    state = client.get("/watch/state").json()
    assert state["turn"] == 1
    assert len(state["slices"]) == 1
    assert state["schedule"]["pacing"] == "await_act"

    acted = _paint(client)
    assert acted.status_code == 200, acted.text
    body = acted.json()
    assert body["next_turn_open"] is True
    assert body["next_turn"] == 2
    assert body["next_slice_index"] == 1
    # Turn 2 is announced once, then waits too.
    for minute in range(41, 59):
        pin(client, f"2026-11-05T08:{minute:02d}:00")
        client.get("/cron/tick", headers=CRON)
    assert [item["slice"] for item in _opens(seen)] == [0, 1]
    assert client.get("/agent/turn", headers=ROSS).json()["turn"] == 2

    done = client.post("/agent/finish", headers=ROSS)
    assert done.status_code == 200, done.text
    assert done.json()["locked"] is True
    pin(client, "2026-11-05T09:00:00")
    client.get("/cron/tick", headers=CRON)
    assert any(item["event"] == "canvas_locked" for item in payloads(seen))
    assert client.get("/watch/state").json()["turn"] is None


def test_await_act_first_turn_wake_only(client, monkeypatch):
    seen = _setup(client, monkeypatch)
    posted = _design(client, pacing="await_act", turn_wakes="first")
    assert posted.status_code == 200, posted.text
    assert posted.json()["turn_wakes"] == "first"
    for _ in range(3):
        assert _paint(client).status_code == 200
        client.get("/cron/tick", headers=CRON)
    assert [item["slice"] for item in _opens(seen)] == [0]
    assert client.get("/agent/turn", headers=ROSS).json()["turn"] == 4


def test_await_act_still_locks_at_window_end(client, monkeypatch):
    monkeypatch.setenv("TILE_SOLO_WINDOW_SECONDS", "3600")
    seen = _setup(client, monkeypatch)
    assert _design(client, pacing="await_act").status_code == 200
    pin(client, "2026-11-05T08:59:00")
    assert client.get("/agent/turn", headers=ROSS).json()["to_act"] is True
    pin(client, "2026-11-05T09:00:01")
    client.get("/cron/tick", headers=CRON)
    desk = client.get("/agent/turn", headers=ROSS).json()
    assert desk["to_act"] is False
    assert _paint(client).status_code == 409
    assert len(_opens(seen)) == 1
    assert any(item["event"] == "canvas_locked" for item in payloads(seen))


def test_env_default_applies_to_design_and_fallback(client, monkeypatch):
    monkeypatch.setenv("TILE_SOLO_PACING", "await_act")
    monkeypatch.setenv("TILE_DESIGN_WAIT_SECONDS", "60")
    _setup(client, monkeypatch)
    pin(client, "2026-11-05T08:02:00")
    tick = client.get("/cron/tick", headers=CRON).json()
    assert tick["day"]["designed"] is True
    desk = client.get("/agent/turn", headers=ROSS).json()
    assert desk["pacing"] == "await_act"
    pin(client, "2026-11-05T08:30:00")
    assert client.get("/agent/turn", headers=ROSS).json()["turn"] == 1


def test_design_wait_default_is_one_hour(client, monkeypatch):
    monkeypatch.delenv("TILE_DESIGN_WAIT_SECONDS", raising=False)
    _setup(client, monkeypatch)
    pin(client, "2026-11-05T08:59:00")
    assert client.get("/cron/tick", headers=CRON).json()["day"]["designed"] is False
    posted = _design(client, pacing="await_act")
    assert posted.status_code == 200, posted.text
    assert posted.json()["theme"] == DESIGN["theme"]


def test_bad_pacing_is_rejected(client, monkeypatch):
    _setup(client, monkeypatch)
    assert _design(client, pacing="forever").status_code == 400
    assert _design(client, turn_wakes="never").status_code == 400
    assert _design(client, turn_wakes="first").status_code == 400
    assert _design(client, pacing="timed").status_code == 200


def test_await_act_is_refused_on_a_roster_canvas(client, monkeypatch):
    _setup(client, monkeypatch)
    store = client.app.state.store
    store.conn.execute(
        "UPDATE days SET artist_order_json = ?, slice_count = 18 WHERE theme IS NULL",
        (json.dumps(["claude", "grok-artist", "openai"]),),
    )
    response = client.post("/agent/design", headers=auth("design"), json={**DESIGN, "pacing": "await_act"})
    assert response.status_code == 400
    assert response.json()["error"] == "bad_pacing"
