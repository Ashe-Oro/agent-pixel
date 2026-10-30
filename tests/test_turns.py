from tests.conftest import auth, host, open_design, pin


def test_off_slice_is_rejected_and_pass_opens_the_next_artist(client):
    open_design(client, "2026-10-28T08:00:00")
    claude = client.get("/agent/turn", headers=auth("claude")).json()
    grok = client.get("/agent/turn", headers=auth("grok-artist")).json()
    assert claude["to_act"] is True
    assert claude["turns_remaining"] == 6
    assert claude["round_index"] == 1
    assert grok["to_act"] is False
    denied = client.post(
        "/agent/act",
        headers=auth("grok-artist"),
        json={"type": "paint", "pixels": [{"x": 0, "y": 0, "color": 1}]},
    )
    assert denied.status_code == 409
    assert denied.json()["error"] == "not_your_turn"

    passed = client.post("/agent/act", headers=auth("claude"), json={"type": "pass", "note": "yield"})
    assert passed.status_code == 200, passed.text
    assert passed.json()["turns_remaining"] == 5
    assert passed.json()["to_act"] is False
    # Passing closes this slice immediately. Grok's 3 minutes start now.
    grok_now = client.get("/agent/turn", headers=auth("grok-artist")).json()
    assert grok_now["to_act"] is True
    assert grok_now["round_index"] == 1
    assert grok_now["turns_remaining"] == 6
    claude_later = client.get("/agent/turn", headers=auth("claude")).json()
    assert claude_later["to_act"] is False
    assert claude_later["turns_remaining"] == 5


def test_pass_done_skips_remaining_rounds(client):
    open_design(client)
    response = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "pass", "done": True, "note": "leaving the pines"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["turns_remaining"] == 0
    skipped = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 0, "y": 0, "color": 1}]},
    )
    assert skipped.status_code == 409
    assert skipped.json()["error"] == "rounds_skipped"
    state = client.get("/watch/state").json()
    claude_slices = [item for item in state["slices"] if item["agent_id"] == "claude"]
    assert [item["state"] for item in claude_slices] == ["done", "skipped", "skipped", "skipped", "skipped", "skipped"]
    grok = client.get("/agent/turn", headers=auth("grok-artist")).json()
    assert grok["to_act"] is True
    assert grok["round_index"] == 1


def test_missed_slice_does_not_block_a_later_slice(client):
    """Missing slice k must not refuse slice k+3. Turn and act agree."""
    open_design(client, "2026-10-28T08:00:00")
    pin(client, "2026-10-28T08:06:00")
    missed_turn = client.get("/agent/turn", headers=auth("grok-artist")).json()
    assert missed_turn["to_act"] is False
    assert missed_turn["slice_index"] == 2
    missed_board = client.get("/watch/state").json()["slices"]
    assert missed_board[1]["agent_id"] == "grok-artist"
    assert missed_board[1]["state"] == "missed"
    missed_act = client.post("/agent/act", headers=auth("grok-artist"), json={"type": "pass"})
    assert missed_act.status_code == 409
    assert missed_act.json()["error"] == "slice_missed"

    pin(client, "2026-10-28T08:12:00")
    later_turn = client.get("/agent/turn", headers=auth("grok-artist")).json()
    assert later_turn["to_act"] is True
    assert later_turn["slice_index"] == 4
    later_act = client.post(
        "/agent/act",
        headers=auth("grok-artist"),
        json={"type": "paint", "pixels": [{"x": 4, "y": 5, "color": 2}], "note": "back on a later slice"},
    )
    assert later_act.status_code == 200, later_act.text
    assert later_act.json()["slice_index"] == 4
    assert later_act.json()["to_act"] is False
    state = client.get("/watch/state").json()
    grok = next(item for item in state["artists"] if item["agent_id"] == "grok-artist")
    assert grok["to_act"] is False
    assert state["slice_index"] == 5


def test_missed_slice_does_not_roll_the_turn_forward(client):
    open_design(client, "2026-10-28T08:00:00")
    pin(client, "2026-10-28T08:03:00")
    claude = client.get("/agent/turn", headers=auth("claude")).json()
    assert claude["to_act"] is False
    assert claude["turns_remaining"] == 5
    late = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 0, "y": 0, "color": 1}]},
    )
    assert late.status_code == 409
    assert late.json()["error"] == "slice_missed"


def test_before_eight_and_eleven_lock(client):
    pin(client, "2026-10-28T07:59:00")
    too_early = client.post(
        "/agent/design",
        headers=auth("design"),
        json={
            "type": "design",
            "theme": "Cozy cabin in the woods",
            "brief": "Warm cabin",
            "palette": [{"id": i, "hex": "#112233", "name": "n"} for i in range(16)],
        },
    )
    assert too_early.status_code == 409
    assert too_early.json()["error"] == "before_routine"
    open_design(client, "2026-10-28T08:00:00")
    pin(client, "2026-10-28T07:30:00")
    parked = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 0, "y": 0, "color": 1}]},
    )
    assert parked.status_code == 409
    assert parked.json()["error"] == "before_routine"
    pin(client, "2026-10-28T11:00:00")
    locked = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 0, "y": 0, "color": 1}]},
    )
    assert locked.status_code == 409
    assert locked.json()["error"] == "morning_locked"
    state = client.get("/watch/state").json()
    assert state["status"] == "locked"


def test_design_validation_and_roles(client):
    pin(client, "2026-10-28T08:00:00")
    short = client.post(
        "/agent/design",
        headers=auth("design"),
        json={"type": "design", "theme": "Cabin", "brief": "Pines", "palette": []},
    )
    assert short.status_code == 400
    assert short.json()["error"] == "bad_design"
    denied = client.post(
        "/agent/design",
        headers=auth("claude"),
        json={"type": "design", "theme": "Cabin", "brief": "Pines", "palette": []},
    )
    assert denied.status_code == 403
    opened = open_design(client)
    assert opened["grid_w"] == 256
    assert opened["grid_h"] == 256
    assert len(opened["palette"]) == 16
    again = client.post("/agent/design", headers=auth("design"), json={
        "type": "design",
        "theme": "Another",
        "brief": "Nope",
        "palette": opened["palette"],
    })
    assert again.status_code == 409
    assert again.json()["error"] == "already_designed"
    painter = client.post("/agent/act", headers=auth("design"), json={"type": "pass"})
    assert painter.status_code == 403


def test_new_day_is_a_blank_canvas(client):
    opened = open_design(client)
    painted = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 10, "y": 12, "color": 6}]},
    )
    assert painted.status_code == 200, painted.text
    pin(client, "2026-10-29T08:00:00")
    state = client.get("/watch/state")
    assert state.json()["day_id"] == opened["day_id"]
    assert state.json()["status"] == "locked"
    tick = client.post("/host/routine/tick", headers=host())
    assert tick.status_code == 200
    assert tick.json()["phase"] == "awaiting_design"
    second = open_design(client, "2026-10-29T08:00:00")
    assert second["day_index"] == 2
    fresh = client.get(
        f"/host/days/{second['day_id']}/region",
        params={"x": 10, "y": 12, "w": 1, "h": 1},
        headers=host(),
    )
    assert fresh.json()["cells"] == [0]
    old = client.get(
        f"/host/days/{opened['day_id']}/region",
        params={"x": 10, "y": 12, "w": 1, "h": 1},
        headers=host(),
    )
    assert old.json()["cells"] == [6]
    grok = client.get("/agent/turn", headers=auth("grok-artist")).json()
    claude = client.get("/agent/turn", headers=auth("claude")).json()
    assert grok["to_act"] is True
    assert claude["to_act"] is False
    assert grok["palette"][0]["id"] == 0


def test_routine_phases_and_seat_token(client):
    pin(client, "2026-10-28T07:00:00")
    early = client.post("/host/routine/tick", headers=host()).json()
    assert early["phase"] == "before_open"
    assert client.get("/watch/state").json()["status"] == "no_day"
    open_design(client, "2026-10-28T08:00:00")
    mid = client.post("/host/routine/tick", headers=host()).json()
    assert mid["phase"] == "slice"
    assert mid["current_agent_id"] == "claude"
    pin(client, "2026-10-28T11:00:00")
    done = client.post("/host/routine/tick", headers=host()).json()
    assert done["phase"] == "locked"
    # Seat token acts as claude without the shared host key. Morning is locked, so the
    # important check is that the token resolves the seat (not 401).
    pin(client, "2026-10-28T08:00:00")
    response = client.post(
        "/agent/act",
        headers={"X-Seat-Token": "seat-claude"},
        json={"type": "pass"},
    )
    assert response.status_code == 200, response.text
