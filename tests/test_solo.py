"""New canvases are painted by Ross alone. Older rows keep their artists."""

import json

from tests.conftest import DESIGN, auth, host, open_design, open_legacy_design, pin
from tests.test_slices import cells
from tests.test_wakes import CRON, install_poster, payloads, set_hook

SECRET_BRIEF = "SECRET_BRIEF_ross_solo_do_not_publish"


def test_new_canvas_is_thirty_ross_turns(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.setenv("TILE_PUBLIC_BASE_URL", "https://pixel.example")
    seen = install_poster(monkeypatch)
    set_hook(client, "ross", ["design_needed", "canvas_locked"])
    set_hook(client, "claude", ["slice_open"])
    set_hook(client, "grok-artist", ["slice_open"])
    set_hook(client, "openai", ["slice_open"])

    pin(client, "2026-10-28T08:00:00")
    opened = client.get("/cron/tick", headers=CRON)
    assert opened.status_code == 200, opened.text
    assert opened.json()["phase"] == "awaiting_design"
    wake = payloads(seen)[0]
    assert wake["event"] == "design_needed"
    assert wake["seat"] == "ross"
    assert wake["design_wait_seconds"] == 3600
    assert wake["turns"] is None
    assert wake["solo_window_seconds"] == 7200
    assert wake["seconds_per_turn"] == 180
    assert wake["pixels_per_turn"] == 16384
    assert "brief" not in wake
    assert [item["seat"] for item in payloads(seen)] == ["ross"]

    body = dict(DESIGN)
    body["brief"] = SECRET_BRIEF
    posted = client.post("/agent/design", headers={"X-Seat-Token": "seat-ross"}, json=body)
    assert posted.status_code == 200, posted.text
    assert posted.json()["first_agent_id"] == "ross"
    day_id = posted.json()["day_id"]

    state = client.get("/watch/state").json()
    assert state["solo"] is True
    assert state["turn"] == 1
    assert state["turns"] is None
    assert len(state["slices"]) == 1
    assert state["current_agent_id"] == "ross"
    assert state["current_display_name"] == "Artist"
    assert [item["agent_id"] for item in state["artists"]] == ["ross"]
    assert state["schedule"]["locks"] == "10:00"
    assert state["canvas_ends_at_unix"]
    assert state["seconds_left_canvas"] == 7200
    assert state["schedule"]["pixels_per_turn"] == 16384
    assert state["schedule"]["seconds_per_turn"] == 180
    assert state["pixels_remaining_turn"] == 16384
    assert "brief" not in state
    assert SECRET_BRIEF not in json.dumps(state)
    assert all(item["agent_id"] == "ross" for item in state["slices"])

    paint = [item for item in payloads(seen) if item["event"] == "slice_open"]
    assert len(paint) == 1
    turn_wake = paint[0]
    assert turn_wake["seat"] == "ross"
    assert turn_wake["turn"] == 1
    assert turn_wake["turns"] is None
    assert turn_wake["canvas_ends_at_unix"] == state["canvas_ends_at_unix"]
    assert turn_wake["seconds_left_canvas"] == 7200
    assert turn_wake["slice"] == 0
    assert turn_wake["seconds_per_turn"] == 180
    assert turn_wake["pixels_per_turn"] == 16384
    assert turn_wake["palette"][0]["id"] == 0
    assert turn_wake["brief"] == SECRET_BRIEF
    assert turn_wake["render_url"].startswith(f"/watch/canvas.png?day_id={day_id}&scale=2")
    assert turn_wake["preview_url"].startswith(f"/watch/canvas.png?day_id={day_id}&scale=3")
    assert turn_wake["canvas_url"] == "https://pixel.example" + turn_wake["render_url"]
    assert turn_wake["ends_at_unix"]
    assert {item["seat"] for item in payloads(seen)} == {"ross"}

    desk = client.get("/agent/turn", headers={"X-Seat-Token": "seat-ross"}).json()
    assert desk["to_act"] is True
    assert desk["turn"] == 1
    assert desk["turns"] is None
    assert desk["canvas_ends_at_unix"]
    assert desk["seconds_left_canvas"] == 7200
    assert desk["pixels_per_turn"] == 16384
    assert desk["pixels_remaining_turn"] == 16384
    assert desk["seconds_per_turn"] == 180
    assert desk["brief"] == SECRET_BRIEF
    assert SECRET_BRIEF not in client.get("/watch/gallery").text
    assert SECRET_BRIEF not in client.get("/").text


def test_turn_count_is_stored_and_old_canvases_keep_theirs(client, monkeypatch):
    monkeypatch.setenv("TILE_SOLO_WINDOW_SECONDS", "600")
    monkeypatch.setenv("TILE_PIXELS_PER_TURN", "100")
    legacy = open_legacy_design(client, "2026-10-28T08:00:00", slice_count=12)
    legacy_state = client.get("/watch/state").json()
    assert legacy_state["day_id"] == legacy["day_id"]
    assert legacy_state["solo"] is False
    assert len(legacy_state["slices"]) == 12
    assert [item["agent_id"] for item in legacy_state["artists"]] == [
        "claude",
        "grok-artist",
        "openai",
    ]
    assert legacy_state["schedule"]["locks"] == "08:36"
    assert legacy_state["schedule"]["pixels_per_turn"] is None
    assert legacy_state["current_agent_id"] == "claude"

    fresh = client.post("/host/canvas/new", headers=host())
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["phase"] == "awaiting_design"
    designed = client.post("/agent/design", headers=auth("ross"), json=DESIGN)
    assert designed.status_code == 200, designed.text
    state = client.get("/watch/state").json()
    assert state["solo"] is True
    assert state["turns"] is None
    assert len(state["slices"]) == 1
    assert state["solo_window_seconds"] == 600
    assert state["seconds_left_canvas"] == 600
    assert state["schedule"]["pixels_per_turn"] == 100
    assert state["current_agent_id"] == "ross"
    kept = client.get("/watch/state", params={"day_id": legacy["day_id"]}).json()
    assert kept["solo"] is False
    assert len(kept["slices"]) == 12
    assert kept["artists"][0]["agent_id"] == "claude"
    gallery = client.get("/watch/gallery").json()["canvases"]
    by_id = {item["day_id"]: item for item in gallery}
    assert by_id[state["day_id"]]["painters"] == ["Artist"]
    assert by_id[legacy["day_id"]]["painters"] == ["Claude", "Grok Artist", "OpenAI"]


def test_solo_turns_advance_without_a_note_gate_and_only_ross_is_woken(client, monkeypatch):
    monkeypatch.setenv("TILE_NOTE_GATE_SECONDS", "75")
    monkeypatch.setenv("TILE_SOLO_WINDOW_SECONDS", "1000")
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    seen = install_poster(monkeypatch)
    set_hook(client, "ross", ["design_needed"])
    set_hook(client, "claude", ["slice_open"])
    set_hook(client, "grok-artist", ["slice_open"])
    set_hook(client, "openai", ["slice_open"])
    open_design(client, "2026-10-28T08:00:00")

    first = client.post(
        "/agent/act",
        headers=auth("ross"),
        json={"type": "paint", "pixels": [{"x": 2, "y": 2, "color": 3}, {"x": 3, "y": 2, "color": 3}]},
    )
    assert first.status_code == 200, first.text
    state = client.get("/watch/state").json()
    assert state["status"] == "painting"
    assert state["turn"] == 2
    assert state["current_agent_id"] == "ross"
    assert state["slices"][0]["state"] == "done"
    assert state["slices"][1]["state"] == "active"
    assert "review" not in {item["state"] for item in state["slices"]}
    denied = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 0, "y": 0, "color": 1}]},
    )
    assert denied.status_code == 403
    assert denied.json()["error"] == "retired_seat"
    veto = client.post("/agent/veto", headers=auth("ross"), json={"slice": 0, "reason": "no"})
    assert veto.status_code == 409
    assert veto.json()["error"] == "solo_canvas"
    seats = {item["seat"] for item in payloads(seen)}
    assert seats == {"ross"}
    assert [item["turn"] for item in payloads(seen) if item["event"] == "slice_open"] == [1, 2]


def test_ross_overwrites_his_own_pixels_inside_the_budget(client, monkeypatch):
    monkeypatch.setenv("TILE_PIXELS_PER_TURN", "4")
    opened = open_design(client)
    day_id = opened["day_id"]
    painted = client.post(
        "/agent/act",
        headers=auth("ross"),
        json={"type": "paint", "pixels": [{"x": 4, "y": 4, "color": 1}, {"x": 5, "y": 4, "color": 2}]},
    )
    assert painted.status_code == 200, painted.text
    assert cells(client, day_id, 4, 4, 2, 1) == [1, 2]
    too_many = [{"x": index, "y": 0, "color": 3} for index in range(5)]
    rejected = client.post("/agent/act", headers=auth("ross"), json={"type": "paint", "pixels": too_many})
    assert rejected.status_code == 400
    assert rejected.json()["error"] == "over_budget"
    assert cells(client, day_id, 4, 4, 2, 1) == [1, 2]
    assert client.get("/watch/state").json()["turn"] == 2
    fixed = client.post(
        "/agent/act",
        headers=auth("ross"),
        json={
            "type": "paint",
            "pixels": [
                {"x": 4, "y": 4, "color": 8},
                {"x": 5, "y": 4, "color": 8},
                {"x": 4, "y": 4, "color": 9},
            ],
        },
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["charged"] == 2
    assert cells(client, day_id, 4, 4, 2, 1) == [9, 8]


def test_finish_locks_before_the_window_ends(client, monkeypatch):
    monkeypatch.setenv("TILE_SOLO_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    seen = install_poster(monkeypatch)
    open_design(client)
    set_hook(client, "host", ["canvas_locked"], url="http://127.0.0.1:9/host")
    assert client.post(
        "/agent/act",
        headers=auth("ross"),
        json={"type": "paint", "pixels": [{"x": 1, "y": 1, "color": 4}]},
    ).status_code == 200
    stranger = client.post("/agent/finish", headers=auth("claude"))
    assert stranger.status_code == 403
    finished = client.post("/agent/finish", headers={"X-Seat-Token": "seat-ross"})
    assert finished.status_code == 200, finished.text
    assert finished.json()["locked"] is True
    assert finished.json()["finished_early"] is True
    state = client.get("/watch/state").json()
    assert state["status"] == "locked"
    assert state["slices"][0]["state"] == "done"
    assert state["slices"][1]["state"] == "done"
    assert len(state["slices"]) == 2
    assert state["turn"] is None
    assert state["seconds_left_canvas"] == 0 or state["status"] == "locked"
    late = client.post(
        "/agent/act",
        headers=auth("ross"),
        json={"type": "paint", "pixels": [{"x": 2, "y": 2, "color": 1}]},
    )
    assert late.status_code == 409
    locked = [item for item in payloads(seen) if item["event"] == "canvas_locked"]
    assert len(locked) == 1
    assert locked[0]["seat"] == "host"
