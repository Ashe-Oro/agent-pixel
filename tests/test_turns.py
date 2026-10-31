from tests.conftest import auth, host, open_design, pin


def test_off_slice_is_rejected_and_pass_opens_the_next_turn(client):
    open_design(client, "2026-10-28T08:00:00")
    ross = client.get("/agent/turn", headers=auth("ross")).json()
    claude = client.get("/agent/turn", headers=auth("claude")).json()
    assert ross["to_act"] is True
    assert ross["turns_remaining"] == 30
    assert ross["turn"] == 1
    assert claude["to_act"] is False
    denied = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 0, "y": 0, "color": 1}]},
    )
    assert denied.status_code == 403
    assert denied.json()["error"] == "wrong_role"

    passed = client.post("/agent/act", headers=auth("ross"), json={"type": "pass", "note": "yield"})
    assert passed.status_code == 200, passed.text
    assert passed.json()["turns_remaining"] == 29
    assert passed.json()["to_act"] is False
    nxt = client.get("/agent/turn", headers=auth("ross")).json()
    assert nxt["to_act"] is True
    assert nxt["slice_index"] == 1
    assert nxt["turn"] == 2
    assert nxt["turns_remaining"] == 29


def test_pass_done_skips_remaining_rounds(client):
    open_design(client)
    response = client.post(
        "/agent/act",
        headers=auth("ross"),
        json={"type": "pass", "done": True, "note": "leaving the pines"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["turns_remaining"] == 0
    skipped = client.post(
        "/agent/act",
        headers=auth("ross"),
        json={"type": "paint", "pixels": [{"x": 0, "y": 0, "color": 1}]},
    )
    assert skipped.status_code == 409
    assert skipped.json()["error"] == "morning_locked"
    state = client.get("/watch/state").json()
    assert state["status"] == "locked"
    assert state["slices"][0]["state"] == "done"
    assert all(item["state"] == "skipped" for item in state["slices"][1:])


def test_missed_slice_does_not_block_a_later_slice(client):
    """A missed turn does not cancel the turns that follow it."""
    open_design(client, "2026-10-28T08:00:00")
    pin(client, "2026-10-28T08:06:00")
    missed_board = client.get("/watch/state").json()["slices"]
    assert missed_board[0]["state"] == "missed"
    assert missed_board[1]["state"] == "missed"
    assert missed_board[2]["state"] == "active"
    later_turn = client.get("/agent/turn", headers=auth("ross")).json()
    assert later_turn["to_act"] is True
    assert later_turn["slice_index"] == 2
    later_act = client.post(
        "/agent/act",
        headers=auth("ross"),
        json={"type": "paint", "pixels": [{"x": 4, "y": 5, "color": 2}], "note": "back on a later turn"},
    )
    assert later_act.status_code == 200, later_act.text
    assert later_act.json()["slice_index"] == 2
    state = client.get("/watch/state").json()
    assert state["current_agent_id"] == "ross"
    assert state["slice_index"] == 3


def test_missed_slice_does_not_roll_the_turn_forward(client):
    open_design(client, "2026-10-28T08:00:00")
    pin(client, "2026-10-28T08:03:00")
    ross = client.get("/agent/turn", headers=auth("ross")).json()
    assert ross["to_act"] is True
    assert ross["slice_index"] == 1
    assert ross["turns_remaining"] == 29
    state = client.get("/watch/state").json()
    assert state["slices"][0]["state"] == "missed"


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
        headers=auth("ross"),
        json={"type": "paint", "pixels": [{"x": 0, "y": 0, "color": 1}]},
    )
    assert parked.status_code == 409
    assert parked.json()["error"] == "before_routine"
    pin(client, "2026-10-28T11:00:00")
    locked = client.post(
        "/agent/act",
        headers=auth("ross"),
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
        headers=auth("ross"),
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
    ross = client.get("/agent/turn", headers=auth("ross")).json()
    claude = client.get("/agent/turn", headers=auth("claude")).json()
    assert ross["to_act"] is True
    assert claude["to_act"] is False
    assert ross["palette"][0]["id"] == 0


def test_routine_phases_and_seat_token(client):
    pin(client, "2026-10-28T07:00:00")
    early = client.post("/host/routine/tick", headers=host()).json()
    assert early["phase"] == "before_open"
    assert client.get("/watch/state").json()["status"] == "no_day"
    open_design(client, "2026-10-28T08:00:00")
    mid = client.post("/host/routine/tick", headers=host()).json()
    assert mid["phase"] == "slice"
    assert mid["current_agent_id"] == "ross"
    pin(client, "2026-10-28T11:00:00")
    done = client.post("/host/routine/tick", headers=host()).json()
    assert done["phase"] == "locked"
    # Rewinding to 08:00 reopens turn 1. Ross's seat token passes it without the host key.
    pin(client, "2026-10-28T08:00:00")
    response = client.post(
        "/agent/act",
        headers={"X-Seat-Token": "seat-ross"},
        json={"type": "pass"},
    )
    assert response.status_code == 200, response.text
