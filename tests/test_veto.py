"""Ross can veto the slice that just closed and send it back to the same artist."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from io import BytesIO

from PIL import Image
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.main import create_app
from app.schedule import NY, day_bounds
from tests.conftest import DESIGN, auth, host, open_design, pin
from tests.test_slices import cells
from tests.test_wakes import install_poster, payloads, set_hook

REASON = "SECRET_VETO_put_the_pine_back"


def _png(client, day_id, slices=None):
    params = {"day_id": day_id, "scale": 1}
    if slices is not None:
        params["slices"] = slices
    response = client.get("/watch/canvas.png", params=params)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["etag"]
    image = Image.open(BytesIO(response.content)).convert("RGB")
    return response, image


def test_veto_restores_overwrites_and_reopens_the_same_seat(client, monkeypatch):
    monkeypatch.setenv("TILE_NOTE_GATE_SECONDS", "75")
    monkeypatch.setenv("TILE_REDO_SECONDS", "120")
    monkeypatch.setenv("CRON_SECRET", "cron-secret")
    seen = install_poster(monkeypatch)
    opened = open_design(client, "2026-10-28T08:00:00")
    day_id = opened["day_id"]
    set_hook(client, "claude", ["slice_open"])
    set_hook(client, "grok-artist", ["slice_open"])
    set_hook(client, "openai", ["slice_open"])
    set_hook(client, "ross", ["slice_closed"])

    early = client.post("/agent/veto", headers=auth("ross"), json={"slice": 0})
    assert early.status_code == 409
    assert early.json()["error"] == "not_in_gate"
    denied = client.post("/agent/veto", headers=auth("claude"), json={"slice": 0, "reason": REASON})
    assert denied.status_code == 403
    assert denied.json()["error"] == "wrong_role"
    denied_token = client.post(
        "/agent/veto",
        headers={"X-Seat-Token": "seat-claude"},
        json={"slice": 0},
    )
    assert denied_token.status_code == 403
    assert client.post("/agent/veto", headers=auth("design"), json={"slice": 0}).status_code == 403

    painted = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 10, "y": 10, "color": 1}, {"x": 11, "y": 10, "color": 2}]},
    )
    assert painted.status_code == 200, painted.text
    noted = client.post("/agent/note", headers=auth("ross"), json={"note": "carry on", "slice": 1})
    assert noted.status_code == 200, noted.text
    before, before_image = _png(client, day_id)
    assert cells(client, day_id, 10, 10, 3, 1) == [1, 2, 0]
    preview_before = client.get("/agent/turn", headers=auth("grok-artist")).json()["preview_url"]
    assert preview_before.endswith(f"day_id={day_id}&scale=3")
    assert "&v=" not in preview_before
    turns_before = client.get("/agent/turn", headers=auth("grok-artist")).json()["turns_remaining"]

    pin(client, "2026-10-28T08:00:30")
    grok_paint = client.post(
        "/agent/act",
        headers=auth("grok-artist"),
        json={
            "type": "paint",
            "pixels": [{"x": 10, "y": 10, "color": 4}, {"x": 12, "y": 10, "color": 5}],
            "focus": {"x": 10, "y": 10, "w": 3, "h": 1},
        },
    )
    assert grok_paint.status_code == 200, grok_paint.text
    assert cells(client, day_id, 10, 10, 3, 1) == [4, 2, 5]
    covered, _covered_image = _png(client, day_id)
    assert covered.headers["etag"] != before.headers["etag"]
    page = client.get("/watch").text
    assert "Removed" in page
    assert "vetoed" in page

    outside = client.post("/agent/veto", headers=auth("ross"), json={"slice": 0, "reason": REASON})
    assert outside.status_code == 409
    assert outside.json()["error"] == "not_in_gate"
    assert cells(client, day_id, 10, 10, 3, 1) == [4, 2, 5]

    vetoed = client.post(
        "/agent/veto",
        headers={"X-Seat-Token": "seat-ross"},
        json={"slice": 1, "reason": REASON},
    )
    assert vetoed.status_code == 200, vetoed.text
    body = vetoed.json()
    assert body["redo"] is True
    assert body["slice"] == 1
    assert body["agent_id"] == "grok-artist"
    assert body["redo_seconds"] == 120
    assert body["veto_note"] == REASON
    assert cells(client, day_id, 10, 10, 3, 1) == [1, 2, 0]
    restored, restored_image = _png(client, day_id)
    assert restored.content == before.content
    assert list(restored_image.get_flattened_data()) == list(before_image.get_flattened_data())
    assert restored.headers["etag"] == before.headers["etag"]
    assert restored.headers["etag"] != covered.headers["etag"]
    only_grok, _only_grok = _png(client, day_id, slices="1")
    blank = Image.new("RGB", restored_image.size, restored_image.getpixel((0, 0)))
    assert list(only_grok_image(only_grok).get_flattened_data()) == list(blank.get_flattened_data())

    again = client.post("/agent/veto", headers=auth("ross"), json={"slice": 1, "reason": "nope"})
    assert again.status_code == 409
    assert again.json()["error"] == "already_vetoed"

    owner = client.get("/agent/turn", headers=auth("grok-artist")).json()
    assert owner["to_act"] is True
    assert owner["redo"] is True
    assert owner["veto_note"] == REASON
    assert owner["slice_index"] == 1
    assert owner["turns_remaining"] == turns_before
    assert owner["slice_ends_at_unix"] - owner["slice_starts_at_unix"] == 120
    assert f"day_id={day_id}&scale=3&v=" in owner["preview_url"]
    assert owner["focus_crop_url"] and "&v=" in owner["focus_crop_url"]
    redo_preview = client.get(owner["preview_url"])
    assert redo_preview.status_code == 200
    assert redo_preview.content == client.get(f"/watch/canvas.png?day_id={day_id}&scale=3").content
    assert redo_preview.content == client.get(preview_before).content
    crop = client.get(owner["focus_crop_url"])
    assert crop.status_code == 200
    assert crop.headers["etag"] != covered.headers["etag"]

    for seat in ("claude", "openai"):
        other = client.get("/agent/turn", headers=auth(seat)).json()
        assert other["to_act"] is False
        assert other["veto_note"] is None
        blocked = client.post("/agent/act", headers=auth(seat), json={"type": "pass"})
        assert blocked.status_code == 409

    state = client.get("/watch/state").json()
    chip = state["slices"][1]
    assert chip["state"] == "active"
    assert chip["redo"] is True
    assert chip["removed"] is True
    assert chip["history"][0] == {
        "state": "vetoed",
        "action": "paint",
        "pixel_count": 2,
        "removed": True,
    }
    assert REASON not in client.get("/watch/state").text
    assert REASON not in client.get("/watch").text
    assert REASON not in client.get("/watch/gallery").text
    assert REASON not in client.get(f"/host/days/{day_id}/ledger", headers=host()).text
    assert REASON not in client.get("/").text
    ledger = client.get(f"/host/days/{day_id}/ledger", headers=host()).json()
    veto_row = [stroke for stroke in ledger["strokes"] if stroke["state"] == "vetoed"]
    assert len(veto_row) == 1
    assert veto_row[0]["pixels"] == [{"x": 10, "y": 10, "color": 4}, {"x": 12, "y": 10, "color": 5}]
    assert veto_row[0]["slice_index"] == 1

    opens = [item for item in payloads(seen) if item["event"] == "slice_open" and item.get("redo") is True]
    assert len(opens) == 1
    assert opens[0]["seat"] == "grok-artist"
    assert opens[0]["slice"] == 1
    assert opens[0]["veto_note"] == REASON
    assert all("redo" not in item for item in payloads(seen) if item["event"] == "slice_open" and item["slice"] == 0)

    redone = client.post(
        "/agent/act",
        headers=auth("grok-artist"),
        json={"type": "paint", "pixels": [{"x": 10, "y": 10, "color": 6}]},
    )
    assert redone.status_code == 200, redone.text
    assert cells(client, day_id, 10, 10, 3, 1) == [6, 2, 0]
    after, _after_image = _png(client, day_id)
    assert after.content != before.content
    layer, _layer = _png(client, day_id, slices="1")
    assert layer.headers["etag"] != only_grok.headers["etag"]
    assert cells(client, day_id, 10, 10, 1, 1) == [6]
    stacked, _stacked = _png(client, day_id, slices="0,1")
    assert stacked.content == after.content
    later = client.get("/agent/turn", headers=auth("grok-artist")).json()
    assert later["to_act"] is False
    assert "&v=" in later["preview_url"]
    assert client.get(later["preview_url"]).content == client.get(
        f"/watch/canvas.png?day_id={day_id}&scale=3"
    ).content

    review = client.get("/watch/state").json()
    assert review["status"] == "review"
    assert review["slices"][1]["state"] == "done"
    assert review["slices"][1]["removed"] is False
    assert review["slices"][2]["state"] == "review"
    assert [item["state"] for item in review["slices"][1]["history"]] == ["vetoed", "done"]
    assert client.get("/agent/turn", headers=auth("openai")).json()["to_act"] is False
    second = client.post("/agent/veto", headers=auth("ross"), json={"slice": 1})
    assert second.status_code == 409
    assert second.json()["error"] == "already_vetoed"

    released = client.post("/agent/note", headers=auth("ross"), json={"note": "next", "slice": 2})
    assert released.status_code == 200, released.text
    assert released.json()["released"] is True
    nxt = client.get("/agent/turn", headers=auth("openai")).json()
    assert nxt["to_act"] is True
    assert nxt["slice_index"] == 2
    assert nxt["redo"] is False
    assert nxt["veto_note"] is None
    assert client.get(nxt["preview_url"]).content == client.get(
        f"/watch/canvas.png?day_id={day_id}&scale=3"
    ).content
    follow = [item for item in payloads(seen) if item["event"] == "slice_open" and item["seat"] == "openai"]
    assert len(follow) == 1
    assert "redo" not in follow[0]


def only_grok_image(response):
    return Image.open(BytesIO(response.content)).convert("RGB")


def test_pass_ends_the_redo_early(client, monkeypatch):
    monkeypatch.setenv("TILE_NOTE_GATE_SECONDS", "75")
    monkeypatch.setenv("TILE_REDO_SECONDS", "120")
    open_design(client, "2026-10-28T08:00:00")
    assert client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 1, "y": 1, "color": 3}]},
    ).status_code == 200
    vetoed = client.post("/agent/veto", headers=auth("ross"), json={"slice": 0, "reason": "repaint"})
    assert vetoed.status_code == 200, vetoed.text
    day_id = client.get("/watch/state").json()["day_id"]
    assert cells(client, day_id, 1, 1) == [0]
    passed = client.post("/agent/act", headers=auth("claude"), json={"type": "pass"})
    assert passed.status_code == 200, passed.text
    state = client.get("/watch/state").json()
    assert state["status"] == "review"
    assert state["slices"][0]["state"] == "done"
    assert state["slices"][0]["action"] == "pass"
    assert state["slices"][1]["state"] == "review"
    assert client.get("/agent/turn", headers=auth("claude")).json()["to_act"] is False
    assert client.get("/agent/turn", headers=auth("grok-artist")).json()["to_act"] is False
    noted = client.post("/agent/note", headers=auth("ross"), json={"note": "go", "slice": 1})
    assert noted.json()["released"] is True
    assert client.get("/agent/turn", headers=auth("grok-artist")).json()["to_act"] is True


def test_redo_timeout_then_gate_opens_the_next_slice(client, monkeypatch):
    monkeypatch.setenv("TILE_NOTE_GATE_SECONDS", "75")
    monkeypatch.setenv("TILE_REDO_SECONDS", "120")
    open_design(client, "2026-10-28T08:00:00")
    assert client.post("/agent/act", headers=auth("claude"), json={"type": "pass"}).status_code == 200
    vetoed = client.post("/agent/veto", headers=auth("ross"), json={"slice": 0, "reason": "again"})
    assert vetoed.status_code == 200, vetoed.text
    assert client.get("/agent/turn", headers=auth("claude")).json()["to_act"] is True
    pin(client, "2026-10-28T08:01:59")
    assert client.get("/agent/turn", headers=auth("claude")).json()["to_act"] is True
    pin(client, "2026-10-28T08:02:00")
    held = client.get("/watch/state").json()
    assert held["status"] == "review"
    assert held["slices"][0]["state"] == "missed"
    assert held["slices"][0]["history"][0]["state"] == "vetoed"
    assert held["slices"][1]["state"] == "review"
    assert held["slices"][1]["starts"] == "08:03:15"
    assert client.get("/agent/turn", headers=auth("claude")).json()["to_act"] is False
    assert client.get("/agent/turn", headers=auth("grok-artist")).json()["to_act"] is False
    pin(client, "2026-10-28T08:03:15")
    opened = client.get("/agent/turn", headers=auth("grok-artist")).json()
    assert opened["to_act"] is True
    assert opened["slice_index"] == 1
    assert opened["redo"] is False


def test_missed_slice_and_closed_gate_cannot_be_vetoed(client, monkeypatch):
    monkeypatch.setenv("TILE_NOTE_GATE_SECONDS", "75")
    open_design(client, "2026-10-28T08:00:00")
    pin(client, "2026-10-28T08:03:00")
    missed = client.post("/agent/veto", headers=auth("ross"), json={"slice": 0})
    assert missed.status_code == 409
    assert missed.json()["error"] == "nothing_to_veto"
    pin(client, "2026-10-28T08:04:15")
    started = client.get("/agent/turn", headers=auth("grok-artist")).json()
    assert started["to_act"] is True
    late = client.post("/agent/veto", headers=auth("ross"), json={"slice": 0})
    assert late.status_code == 409
    assert late.json()["error"] == "not_in_gate"


def test_mid_run_canvas_without_a_veto_keeps_its_clock(tmp_path, monkeypatch):
    """A canvas already painting keeps its pixels and slice when the veto columns arrive."""
    monkeypatch.setenv("TILE_NOTE_GATE_SECONDS", "0")
    db_path = tmp_path / "live.db"
    conn = sqlite3.connect(db_path)
    opens, _lock = day_bounds(datetime(2026, 10, 28).date())
    pinned = (opens + timedelta(minutes=1)).astimezone(timezone.utc).replace(microsecond=0)
    conn.executescript(
        """
        CREATE TABLE settings (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          clock_utc TEXT,
          roster_json TEXT NOT NULL,
          rotate INTEGER NOT NULL DEFAULT 1,
          pixels_per_turn INTEGER NOT NULL DEFAULT 0,
          pixels_per_artist_day INTEGER NOT NULL DEFAULT 0,
          morning_opened TEXT,
          morning_due TEXT
        );
        CREATE TABLE days (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          day_index INTEGER NOT NULL UNIQUE,
          ny_date TEXT NOT NULL,
          theme TEXT,
          brief TEXT,
          palette_json TEXT,
          canvas BLOB,
          artist_order_json TEXT,
          focus_json TEXT,
          demo_seed INTEGER NOT NULL DEFAULT 0,
          designed_at INTEGER,
          created_at INTEGER NOT NULL,
          opens_at INTEGER NOT NULL DEFAULT 0,
          kind TEXT NOT NULL DEFAULT 'legacy',
          archived INTEGER NOT NULL DEFAULT 0,
          archived_at INTEGER,
          slice_count INTEGER NOT NULL DEFAULT 12
        );
        CREATE TABLE actions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          day_id INTEGER NOT NULL,
          agent_id TEXT NOT NULL,
          slice_index INTEGER NOT NULL,
          round_index INTEGER NOT NULL,
          action TEXT NOT NULL,
          done INTEGER NOT NULL DEFAULT 0,
          pixel_count INTEGER NOT NULL DEFAULT 0,
          note TEXT,
          focus_json TEXT,
          pixels_json TEXT,
          created_at INTEGER NOT NULL,
          UNIQUE (day_id, slice_index)
        );
        """
    )
    conn.execute(
        """
        INSERT INTO settings (
          id, clock_utc, roster_json, rotate, pixels_per_turn, pixels_per_artist_day, morning_opened
        ) VALUES (1, ?, ?, 1, 0, 0, '2026-10-28')
        """,
        (pinned.isoformat(), '["claude","grok-artist","openai"]'),
    )
    canvas = bytearray(256 * 256)
    canvas[10 * 256 + 4] = 3
    conn.execute(
        """
        INSERT INTO days (
          id, day_index, ny_date, theme, brief, palette_json, canvas, artist_order_json,
          designed_at, created_at, opens_at, kind, slice_count
        ) VALUES (4, 4, '2026-10-28', 'Harbor', 'brief', ?, ?, ?, ?, ?, ?, 'morning', 12)
        """,
        (
            json.dumps(DESIGN["palette"]),
            bytes(canvas),
            '["claude","grok-artist","openai"]',
            int(opens.timestamp()),
            int(opens.timestamp()),
            int(opens.timestamp()),
        ),
    )
    conn.execute(
        """
        INSERT INTO actions (
          day_id, agent_id, slice_index, round_index, action, pixel_count, pixels_json, created_at
        ) VALUES (4, 'claude', 0, 1, 'paint', 1, ?, ?)
        """,
        (json.dumps([{"x": 4, "y": 10, "color": 3}]), int(opens.timestamp())),
    )
    conn.commit()
    conn.close()

    app = create_app(
        Settings(
            db_path=db_path,
            api_key="test-key",
            seed=False,
            pin_morning=False,
            skill_path=ROOT / "skill.md",
            seat_tokens={},
        )
    )
    try:
        with TestClient(app) as live:
            columns = {
                row["name"]
                for row in live.app.state.store.conn.execute("PRAGMA table_info(actions)")
            }
            assert {"state", "veto_reason", "vetoed_at", "redo_seconds"} <= columns
            stored = live.app.state.store.conn.execute(
                "SELECT state FROM actions WHERE day_id = 4 AND slice_index = 0"
            ).fetchone()
            assert stored["state"] == "active"
            state = live.get("/watch/state").json()
            assert state["day_id"] == 4
            assert state["status"] == "painting"
            assert len(state["slices"]) == 12
            assert state["slice_index"] == 1
            assert state["slices"][0]["state"] == "done"
            assert state["slices"][0]["history"][0]["state"] == "done"
            assert all(
                item["state"] != "vetoed"
                for chip in state["slices"]
                for item in chip["history"]
            )
            assert cells(live, 4, 4, 10) == [3]
            grok = live.get("/agent/turn", headers=auth("grok-artist")).json()
            assert grok["to_act"] is True
            assert grok["redo"] is False
            assert grok["veto_note"] is None
            assert "&v=" not in grok["preview_url"]
            refused = live.post("/agent/veto", headers=auth("ross"), json={"slice": 0})
            assert refused.status_code == 409
            assert refused.json()["error"] == "not_in_gate"
            assert cells(live, 4, 4, 10) == [3]
            painted = live.post(
                "/agent/act",
                headers=auth("grok-artist"),
                json={"type": "paint", "pixels": [{"x": 5, "y": 10, "color": 7}]},
            )
            assert painted.status_code == 200, painted.text
            assert cells(live, 4, 4, 10, 2, 1) == [3, 7]
            assert live.get("/watch/state").json()["slices"][0]["state"] == "done"
    finally:
        app.state.store.close()
