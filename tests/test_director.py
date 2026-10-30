"""Ross owns the design and the per-slice notes. The public site does not."""

import json

from tests.conftest import DESIGN, auth, host, open_design, pin
from tests.test_wakes import CRON, install_poster, payloads, set_hook

BRIEF = "SECRET_BRIEF_cabin_pines_do_not_publish"
NOTE = "SECRET_NOTE_keep_the_pines_left"
FALLBACK_SNIPPET = "did not publish a brief"


def _public_blobs(client, day_id):
    blobs = [
        client.get("/").text,
        client.get("/watch").text,
        client.get(f"/canvas/{day_id}").text,
        client.get("/watch/state").text,
        client.get("/watch/state", params={"day_id": day_id}).text,
        client.get("/watch/gallery").text,
        client.get("/watch/chat").text,
        client.get("/watch/chat", params={"day_id": day_id}).text,
        client.get("/host/days/current", headers=host()).text,
        client.get(f"/host/days/{day_id}/ledger", headers=host()).text,
    ]
    return blobs


def _assert_private(client, day_id, *needles):
    for blob in _public_blobs(client, day_id):
        for needle in needles:
            assert needle not in blob
    state = client.get("/watch/state").json()
    assert "brief" not in state
    assert "director_note" not in state
    assert "note" not in state


def test_cron_waits_for_design_then_times_out(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.setenv("TILE_DESIGN_WAIT_SECONDS", "120")
    monkeypatch.setenv("TILE_PUBLIC_BASE_URL", "https://pixel.example")
    seen = install_poster(monkeypatch)
    set_hook(client, "ross", ["design_needed", "slice_closed", "canvas_locked"])
    set_hook(client, "claude", ["slice_open"])

    pin(client, "2026-10-28T08:00:00")
    opened = client.get("/cron/tick", headers=CRON)
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["phase"] == "awaiting_design"
    day_id = body["day"]["day_id"]
    assert body["waiting_until_unix"] is not None
    assert [item["event"] for item in body["wakes"]] == ["design_needed"]
    wake = payloads(seen)[0]
    assert wake["seat"] == "ross"
    assert wake["slice"] is None
    assert "brief" not in wake
    assert "director_note" not in wake
    assert FALLBACK_SNIPPET not in json.dumps(wake)
    assert client.get("/agent/turn", headers=auth("claude")).json()["to_act"] is False
    desk = client.get("/agent/turn", headers={"X-Seat-Token": "seat-ross"})
    assert desk.status_code == 200, desk.text
    assert desk.json()["reason"] == "design_needed"
    assert desk.json()["to_act"] is True
    state = client.get("/watch/state").json()
    assert state["status"] == "awaiting_design"
    assert state["theme"] is None
    assert "Waiting for the art director" in state["message"]

    pin(client, "2026-10-28T08:01:59")
    still = client.get("/cron/tick", headers=CRON)
    assert still.json()["phase"] == "awaiting_design"
    assert still.json()["day"]["day_id"] == day_id
    assert [item["event"] for item in payloads(seen)] == ["design_needed"]

    pin(client, "2026-10-28T08:02:00")
    started = client.get("/cron/tick", headers=CRON)
    assert started.status_code == 200, started.text
    assert started.json()["phase"] == "slice"
    assert started.json()["current_agent_id"] == "claude"
    assert started.json()["day"]["theme"] == "Open canvas"
    assert FALLBACK_SNIPPET not in started.text
    turn = client.get("/agent/turn", headers=auth("claude")).json()
    assert turn["to_act"] is True
    assert turn["slice_index"] == 0
    assert FALLBACK_SNIPPET in turn["brief"]
    assert turn["director_note"] is None
    _assert_private(client, day_id, FALLBACK_SNIPPET, turn["brief"])
    again = client.get("/cron/tick", headers=CRON)
    assert [item["event"] for item in payloads(seen)] == ["design_needed", "slice_open"]
    assert payloads(seen)[1]["seat"] == "claude"
    assert payloads(seen)[1]["brief"] == turn["brief"]
    assert payloads(seen)[1]["director_note"] is None
    assert again.json()["wakes"] == []


def test_ross_design_starts_slice_zero_and_stays_private(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.setenv("TILE_DESIGN_WAIT_SECONDS", "300")
    seen = install_poster(monkeypatch)
    set_hook(client, "ross", ["design_needed"])
    set_hook(client, "claude", ["slice_open"])
    pin(client, "2026-10-28T08:00:00")
    opened = client.get("/cron/tick", headers=CRON)
    day_id = opened.json()["day"]["day_id"]
    too_long = dict(DESIGN)
    too_long["brief"] = "x" * 4001
    rejected = client.post("/agent/design", headers=auth("ross"), json=too_long)
    assert rejected.status_code == 400
    assert rejected.json()["error"] == "bad_design"
    body = dict(DESIGN)
    body["theme"] = "Public harbor"
    body["brief"] = "y" * 4000
    posted = client.post("/agent/design", headers={"X-Seat-Token": "seat-ross"}, json=body)
    assert posted.status_code == 200, posted.text
    assert posted.json()["slice_index"] == 0
    assert posted.json()["brief"] == "y" * 4000
    state = client.get("/watch/state").json()
    assert state["status"] == "painting"
    assert state["theme"] == "Public harbor"
    assert state["current_agent_id"] == "claude"
    turn = client.get("/agent/turn", headers=auth("claude")).json()
    assert turn["to_act"] is True
    assert turn["brief"] == "y" * 4000
    assert turn["day_id"] == day_id
    _assert_private(client, day_id, "y" * 4000, BRIEF)
    events = [item["event"] for item in payloads(seen)]
    assert events == ["design_needed", "slice_open"]
    assert payloads(seen)[1]["brief"] == "y" * 4000
    client.get("/cron/tick", headers=CRON)
    assert [item["event"] for item in payloads(seen)] == ["design_needed", "slice_open"]


def test_gate_opens_on_note_without_a_second_wake(client, monkeypatch):
    monkeypatch.setenv("TILE_NOTE_GATE_SECONDS", "75")
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    monkeypatch.setenv("TILE_PUBLIC_BASE_URL", "https://pixel.example")
    seen = install_poster(monkeypatch)
    opened = open_design(client, "2026-10-28T08:00:00")
    # Replace the cozy brief with one we can hunt for on public routes.
    client.app.state.store.conn.execute(
        "UPDATE days SET brief = ? WHERE id = ?",
        (BRIEF, opened["day_id"]),
    )
    set_hook(client, "claude", ["slice_open"])
    set_hook(client, "grok-artist", ["slice_open"])
    set_hook(client, "ross", ["slice_closed", "canvas_locked"])

    first = client.get("/agent/turn", headers=auth("claude"))
    assert first.json()["brief"] == BRIEF
    assert first.json()["director_note"] is None
    assert payloads(seen)[0]["event"] == "slice_open"
    assert payloads(seen)[0]["brief"] == BRIEF
    assert payloads(seen)[0]["director_note"] is None

    passed = client.post("/agent/act", headers=auth("claude"), json={"type": "pass"})
    assert passed.status_code == 200, passed.text
    assert client.get("/agent/turn", headers=auth("grok-artist")).json()["to_act"] is False
    state = client.get("/watch/state").json()
    assert state["status"] == "review"
    assert state["current_agent_id"] is None
    assert state["slices"][0]["state"] == "done"
    assert state["slices"][1]["state"] == "review"
    assert "Waiting for the art director" in state["message"]
    assert [item["seat"] for item in payloads(seen)] == ["claude", "ross"]
    closed = payloads(seen)[1]
    assert closed["event"] == "slice_closed"
    assert closed["slice"] == 0
    assert closed["next_slice"] == 1
    assert "brief" not in closed
    assert NOTE not in json.dumps(closed)
    assert BRIEF not in json.dumps(closed)

    pin(client, "2026-10-28T08:00:30")
    assert client.get("/cron/tick", headers=CRON).json()["phase"] == "review"
    assert [item["seat"] for item in payloads(seen)] == ["claude", "ross"]

    denied = client.post("/agent/note", headers=auth("claude"), json={"note": NOTE})
    assert denied.status_code == 403
    denied_design = client.post("/agent/note", headers=auth("design"), json={"note": NOTE})
    assert denied_design.status_code == 403
    empty = client.post("/agent/note", headers=auth("ross"), json={"note": "  "})
    assert empty.status_code == 400

    noted = client.post(
        "/agent/note",
        headers={"X-Seat-Token": "seat-ross"},
        json={"note": NOTE},
    )
    assert noted.status_code == 200, noted.text
    assert noted.json()["slice"] == 1
    assert noted.json()["released"] is True
    assert "seat-ross" not in noted.text
    grok = client.get("/agent/turn", headers=auth("grok-artist")).json()
    assert grok["to_act"] is True
    assert grok["slice_index"] == 1
    assert grok["brief"] == BRIEF
    assert grok["director_note"] == NOTE
    client.get("/cron/tick", headers=CRON)
    opens = [item for item in payloads(seen) if item["event"] == "slice_open"]
    assert [item["seat"] for item in opens] == ["claude", "grok-artist"]
    assert opens[1]["director_note"] == NOTE
    assert opens[1]["brief"] == BRIEF

    edited = client.post(
        "/agent/note",
        headers=auth("ross"),
        json={"note": NOTE + " again", "slice": 1},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["released"] is True
    assert client.get("/agent/turn", headers=auth("grok-artist")).json()["director_note"] == NOTE + " again"
    assert len([item for item in payloads(seen) if item["seat"] == "grok-artist"]) == 1

    _assert_private(client, opened["day_id"], BRIEF, NOTE, NOTE + " again")
    page = client.get("/watch").text
    assert "data.brief" not in page
    assert client.get("/cron/tick", headers=CRON).text.find(BRIEF) == -1


def test_gate_opens_on_timeout(client, monkeypatch):
    monkeypatch.setenv("TILE_NOTE_GATE_SECONDS", "75")
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    seen = install_poster(monkeypatch)
    open_design(client, "2026-10-28T08:00:00")
    set_hook(client, "grok-artist", ["slice_open"])
    set_hook(client, "ross", ["slice_closed"])
    assert client.post("/agent/act", headers=auth("claude"), json={"type": "pass"}).status_code == 200
    pin(client, "2026-10-28T08:01:14")
    holding = client.get("/cron/tick", headers=CRON)
    assert holding.json()["phase"] == "review"
    assert client.get("/agent/turn", headers=auth("grok-artist")).json()["to_act"] is False
    assert [item["event"] for item in payloads(seen)] == ["slice_closed"]

    pin(client, "2026-10-28T08:01:15")
    opened = client.get("/cron/tick", headers=CRON)
    assert opened.json()["phase"] == "slice"
    assert opened.json()["current_agent_id"] == "grok-artist"
    turn = client.get("/agent/turn", headers=auth("grok-artist")).json()
    assert turn["to_act"] is True
    assert turn["slice_index"] == 1
    assert turn["director_note"] is None
    assert [item["event"] for item in payloads(seen)] == ["slice_closed", "slice_open"]
    assert payloads(seen)[1]["director_note"] is None
    client.get("/cron/tick", headers=CRON)
    client.get("/watch/state")
    assert [item["event"] for item in payloads(seen)] == ["slice_closed", "slice_open"]


def test_ross_canvas_and_note_are_ross_only(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    pin(client, "2026-10-28T14:00:00")
    missing = client.post("/agent/canvas", json={})
    assert missing.status_code == 401
    artist = client.post("/agent/canvas", headers=auth("claude"), json={})
    assert artist.status_code == 403
    design_seat = client.post("/agent/canvas", headers=auth("design"), json={})
    assert design_seat.status_code == 403
    blank = client.post("/agent/canvas", headers=auth("ross"), json={})
    assert blank.status_code == 200, blank.text
    assert blank.json()["phase"] == "awaiting_design"
    early = client.post("/agent/note", headers=auth("ross"), json={"note": NOTE})
    assert early.status_code == 409
    assert early.json()["error"] == "awaiting_design"

    paint = client.post(
        "/agent/act",
        headers=auth("ross"),
        json={"type": "pass"},
    )
    assert paint.status_code == 403

    themed = dict(DESIGN)
    themed["theme"] = "Ross one-off"
    themed["brief"] = BRIEF
    opened = client.post("/agent/canvas", headers={"X-Seat-Token": "seat-ross"}, json=themed)
    assert opened.status_code == 200, opened.text
    assert opened.json()["phase"] == "slice"
    assert opened.json()["brief"] == BRIEF
    first = opened.json()["first_agent_id"]
    day_id = opened.json()["day"]["day_id"]
    turn = client.get("/agent/turn", headers=auth(first)).json()
    assert turn["to_act"] is True
    assert turn["brief"] == BRIEF
    _assert_private(client, day_id, BRIEF)

    host_canvas = client.post("/host/canvas/new", headers=host())
    assert host_canvas.status_code == 200, host_canvas.text
    assert host_canvas.json()["phase"] == "awaiting_design"
    assert client.post("/host/canvas/new").status_code == 401
