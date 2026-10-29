"""Sessions, 3-minute slices, gallery order, and host delete.

Production may not have day 1. These tests never require that id.
"""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import blob_db
from app.config import ROOT, Settings
from app.main import create_app
from app.schedule import NY, day_bounds
from tests.conftest import DESIGN, auth, host, open_design, pin


def test_on_time_act_opens_the_next_artist_immediately(client):
    opened = open_design(client, "2026-10-28T08:00:00")
    painted = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 2, "y": 3, "color": 4}], "note": "door"},
    )
    assert painted.status_code == 200, painted.text
    grok = client.get("/agent/turn", headers=auth("grok-artist")).json()
    claude = client.get("/agent/turn", headers=auth("claude")).json()
    assert grok["to_act"] is True
    assert grok["slice_index"] == 1
    assert claude["to_act"] is False
    state = client.get("/watch/state").json()
    assert state["slices"][0]["state"] == "done"
    assert state["slices"][1]["state"] == "active"
    assert state["slices"][0]["ends"] != state["slices"][1]["ends"] or state["current_agent_id"] == "grok-artist"
    assert state["current_agent_id"] == "grok-artist"
    assert "3 minutes" in client.post("/host/routine/tick", headers=host()).json()["hint"]
    assert opened["day_id"] == state["day_id"]


def test_timeout_misses_the_slice_and_late_paint_is_rejected(client):
    opened = open_design(client, "2026-10-28T08:00:00")
    day_id = opened["day_id"]
    pin(client, "2026-10-28T08:03:00")
    state = client.get("/watch/state").json()
    assert state["slices"][0]["state"] == "missed"
    assert state["current_agent_id"] == "grok-artist"
    late = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 8, "y": 8, "color": 7}]},
    )
    assert late.status_code == 409
    assert late.json()["error"] == "slice_missed"
    region = client.get(
        f"/host/days/{day_id}/region",
        params={"x": 8, "y": 8, "w": 1, "h": 1},
        headers=host(),
    )
    assert region.json()["cells"] == [0]
    grok = client.post(
        "/agent/act",
        headers=auth("grok-artist"),
        json={"type": "paint", "pixels": [{"x": 8, "y": 8, "color": 7}]},
    )
    assert grok.status_code == 200, grok.text
    assert client.get(
        f"/host/days/{day_id}/region",
        params={"x": 8, "y": 8, "w": 1, "h": 1},
        headers=host(),
    ).json()["cells"] == [7]


def test_next_morning_opens_one_new_canvas_after_a_finished_one(client):
    pin(client, "2026-10-30T08:00:00")
    design = dict(DESIGN)
    design["theme"] = "Harbor lighthouse at blue hour"
    created = client.post("/agent/design", headers=auth("design"), json=design)
    assert created.status_code == 200, created.text
    old_id = created.json()["day_id"]
    painted = client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 10, "y": 12, "color": 6}]},
    )
    assert painted.status_code == 200, painted.text
    assert client.post("/host/clock", headers=host(), json={"mode": "realtime"}).status_code == 200
    # 12:00 UTC on Oct 31 2026 is 08:00 EDT. Yesterday's canvas is locked.
    frozen = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    with patch("app.store.datetime", FrozenDateTime):
        before = client.get("/watch/state").json()
        assert before["day_id"] == old_id
        assert before["status"] == "locked"
        tick = client.post("/host/routine/tick", headers=host())
        assert tick.status_code == 200, tick.text
        body = tick.json()
        assert body["phase"] == "awaiting_design"
        assert body["day"]["day_id"] != old_id
        assert body["day"]["theme"] is None
        again = client.post("/host/routine/tick", headers=host()).json()
        assert again["day"]["day_id"] == body["day"]["day_id"]
        assert client.get("/watch/state").json()["day_id"] == body["day"]["day_id"]
    old = client.get(
        f"/host/days/{old_id}/region",
        params={"x": 10, "y": 12, "w": 1, "h": 1},
        headers=host(),
    )
    assert old.json()["cells"] == [6]
    gallery = client.get("/watch/gallery").json()["canvases"]
    assert any(item["day_id"] == old_id and item["theme"] == "Harbor lighthouse at blue hour" for item in gallery)


def test_host_bonus_canvas_steps_without_a_fake_date(client):
    first = open_design(client, "2026-10-28T08:00:00")
    client.post(
        "/agent/act",
        headers=auth("claude"),
        json={"type": "paint", "pixels": [{"x": 1, "y": 1, "color": 3}]},
    )
    pin(client, "2026-10-28T14:30:00")
    denied = client.post("/host/canvas/new")
    assert denied.status_code == 401
    opened = client.post("/host/canvas/new", headers=host())
    assert opened.status_code == 200, opened.text
    bonus = opened.json()
    assert bonus["phase"] == "awaiting_design"
    assert bonus["day"]["day_id"] != first["day_id"]
    assert bonus["day"]["ny_date"] == "2026-10-28"
    designed = client.post("/agent/design", headers=auth("design"), json=DESIGN)
    assert designed.status_code == 200, designed.text
    state = client.get("/watch/state").json()
    first_artist, second_artist, third_artist = [item["agent_id"] for item in state["artists"][:3]]
    assert state["artists"][0]["to_act"] is True
    assert client.post(
        "/agent/act",
        headers=auth(first_artist),
        json={"type": "paint", "pixels": [{"x": 4, "y": 4, "color": 5}]},
    ).status_code == 200
    assert client.get("/agent/turn", headers=auth(second_artist)).json()["to_act"] is True
    stepped = client.post("/host/clock", headers=host(), json={"mode": "next_slice"})
    assert stepped.status_code == 200, stepped.text
    assert client.get("/agent/turn", headers=auth(third_artist)).json()["to_act"] is True
    assert len(client.get("/watch/state").json()["slices"]) == 12
    second = client.post("/host/canvas/new", headers=host())
    assert second.status_code == 200, second.text
    assert second.json()["day"]["ny_date"] == "2026-10-28"
    assert second.json()["day"]["day_id"] != bonus["day"]["day_id"]
    old_pixel = client.get(
        f"/host/days/{first['day_id']}/region",
        params={"x": 1, "y": 1, "w": 1, "h": 1},
        headers=host(),
    )
    assert old_pixel.json()["cells"] == [3]


def test_gallery_orders_by_creation_and_hides_never_designed_days(client):
    # Inserted last, but labeled with the earliest date. That is the newest canvas.
    open_design(client, "2026-10-30T08:00:00")
    pin(client, "2026-10-27T08:00:00")
    pokemon = dict(DESIGN)
    pokemon["theme"] = "Pokemon"
    assert client.post("/host/canvas/new", headers=host()).status_code == 200
    assert client.post("/agent/design", headers=auth("design"), json=pokemon).status_code == 200
    pin(client, "2026-10-25T08:00:00")
    pepe = dict(DESIGN)
    pepe["theme"] = "Pepe"
    created = client.post("/host/canvas/new", headers=host())
    assert created.status_code == 200, created.text
    pepe_id = created.json()["day"]["day_id"]
    assert client.post("/agent/design", headers=auth("design"), json=pepe).status_code == 200
    # An empty row must not become a gallery card, and it must not take the home slot
    # away from Pepe once the bonus session is over.
    client.app.state.store.conn.execute(
        "INSERT INTO days (day_index, ny_date, created_at, opens_at, kind) VALUES (99, '2026-10-28', 1, 1, 'legacy')"
    )
    pin(client, "2026-10-25T08:36:00")
    gallery = client.get("/watch/gallery").json()["canvases"]
    assert [item["theme"] for item in gallery][:1] == ["Pepe"]
    assert gallery[0]["day_id"] == pepe_id
    assert gallery[0]["ny_date"] == "2026-10-25"
    assert all(item["theme"] for item in gallery)
    assert all(item["ny_date"] for item in gallery)
    assert all(item["open_url"] == f"/canvas/{item['day_id']}" for item in gallery)
    home = client.get("/watch/state").json()
    assert home["day_id"] == pepe_id
    assert home["theme"] == "Pepe"
    assert all(card["theme"] for card in home["archive"])
    assert "No design" not in client.get("/").text
    page = client.get(f"/canvas/{gallery[-1]['day_id']}")
    assert page.status_code == 200
    assert "/canvas/" in page.text
    empty_id = client.app.state.store.conn.execute(
        "SELECT id FROM days WHERE theme IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]
    hidden = client.get("/watch/state", params={"day_id": empty_id})
    assert hidden.status_code == 404
    assert hidden.json()["error"] == "no_canvas"


def test_delete_day_refuses_the_live_canvas_unless_forced(client):
    opened = open_design(client, "2026-10-28T08:00:00")
    day_id = opened["day_id"]
    assert client.delete(f"/host/days/{day_id}").status_code == 401
    refused = client.delete(f"/host/days/{day_id}", headers=host())
    assert refused.status_code == 409
    assert refused.json()["error"] == "day_live"
    assert client.get("/watch/state").json()["day_id"] == day_id
    forced = client.delete(f"/host/days/{day_id}?force=true", headers=host())
    assert forced.status_code == 200, forced.text
    assert forced.json()["ok"] is True
    gone = client.delete(f"/host/days/{day_id}", headers=host())
    assert gone.status_code == 404
    assert gone.json()["error"] == "no_day"
    assert client.get("/watch/gallery").json()["canvases"] == []
    assert client.get("/watch/state").json()["status"] == "no_day"


def test_migration_keeps_existing_ids_without_requiring_day_one(tmp_path):
    """Rows 2 and 6 stand in for production after day 1 was removed."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE settings (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          clock_utc TEXT,
          roster_json TEXT NOT NULL,
          rotate INTEGER NOT NULL DEFAULT 1,
          pixels_per_turn INTEGER NOT NULL DEFAULT 0,
          pixels_per_artist_day INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE days (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          day_index INTEGER NOT NULL UNIQUE,
          ny_date TEXT NOT NULL UNIQUE,
          theme TEXT,
          brief TEXT,
          palette_json TEXT,
          canvas BLOB,
          artist_order_json TEXT,
          focus_json TEXT,
          demo_seed INTEGER NOT NULL DEFAULT 0,
          designed_at INTEGER,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE artist_state (
          day_id INTEGER NOT NULL,
          agent_id TEXT NOT NULL,
          pixels_used INTEGER NOT NULL DEFAULT 0,
          skipped INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (day_id, agent_id)
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
        CREATE TABLE chat (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          day_id INTEGER NOT NULL,
          agent_id TEXT NOT NULL,
          body TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        """
    )
    opens, _ = day_bounds(datetime(2026, 10, 25).date())
    pinned = (opens + timedelta(minutes=1)).astimezone(timezone.utc).replace(microsecond=0)
    conn.execute(
        "INSERT INTO settings (id, clock_utc, roster_json, rotate, pixels_per_turn, pixels_per_artist_day) VALUES (1, ?, ?, 1, 0, 0)",
        (pinned.isoformat(), '["claude","grok-artist","openai"]'),
    )
    palette = __import__("json").dumps(DESIGN["palette"])
    order = '["claude","grok-artist","openai"]'
    canvas = bytearray(256 * 256)
    canvas[4] = 9
    conn.execute(
        """
        INSERT INTO days (id, day_index, ny_date, theme, brief, palette_json, canvas, artist_order_json, designed_at, created_at)
        VALUES (2, 2, '2026-10-30', 'Harbor', 'brief', ?, ?, ?, 1, 10)
        """,
        (palette, bytes(256 * 256), order),
    )
    conn.execute(
        """
        INSERT INTO days (id, day_index, ny_date, theme, brief, palette_json, canvas, artist_order_json, designed_at, created_at)
        VALUES (6, 6, '2026-10-25', 'Pepe', 'brief', ?, ?, ?, 1, 60)
        """,
        (palette, bytes(canvas), order),
    )
    conn.execute(
        "INSERT INTO actions (day_id, agent_id, slice_index, round_index, action, pixel_count, pixels_json, created_at) VALUES (2, 'claude', 0, 1, 'paint', 1, '[]', 10)"
    )
    conn.execute("INSERT INTO chat (day_id, agent_id, body, created_at) VALUES (6, 'claude', 'still painting', 60)")
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
    with TestClient(app) as client:
        assert client.app.state.store.conn.execute("SELECT id FROM days WHERE id = 1").fetchone() is None
        themes = {
            row["id"]: row["theme"]
            for row in client.app.state.store.conn.execute("SELECT id, theme FROM days").fetchall()
        }
        assert themes == {2: "Harbor", 6: "Pepe"}
        region = client.get("/host/days/6/region", params={"x": 4, "y": 0, "w": 1, "h": 1}, headers=host())
        assert region.json()["cells"] == [9]
        painted = client.post(
            "/agent/act",
            headers=auth("claude"),
            json={"type": "paint", "pixels": [{"x": 5, "y": 0, "color": 3}]},
        )
        assert painted.status_code == 200, painted.text
        assert client.get("/host/days/6/region", params={"x": 5, "y": 0, "w": 1, "h": 1}, headers=host()).json()["cells"] == [3]
        home = client.get("/watch/state").json()
        assert home["day_id"] == 6
        assert home["theme"] == "Pepe"
        gallery = client.get("/watch/gallery").json()["canvases"]
        assert [item["day_id"] for item in gallery] == [6, 2]
        ledger = client.get("/host/days/2/ledger", headers=host()).json()
        assert ledger["strokes"][0]["agent_id"] == "claude"
        chat = client.get("/watch/chat", params={"day_id": 6}).json()
        assert chat["messages"][0]["text"] == "still painting"
        bonus = client.post("/host/canvas/new", headers=host())
        assert bonus.status_code == 200, bonus.text
        assert bonus.json()["day"]["day_id"] == 7
    app.state.store.close()


def test_concurrent_writers_keep_both_updates(tmp_path, monkeypatch):
    state = {"body": None, "etag": None, "n": 0, "lock": threading.Lock(), "conflicts": 0}
    pair = {"left": 0, "barrier": threading.Barrier(2)}

    def download(_name):
        with state["lock"]:
            if state["body"] is None:
                raise blob_db.BlobMissing()
            return bytes(state["body"]), state["etag"]

    def stat(_name):
        with state["lock"]:
            if state["body"] is None:
                raise blob_db.BlobMissing()
            return state["etag"]

    def upload(_name, body, if_match=None):
        wait = False
        with state["lock"]:
            if pair["left"] > 0:
                pair["left"] -= 1
                wait = True
        if wait:
            pair["barrier"].wait(timeout=5)
        with state["lock"]:
            if state["body"] is not None and if_match != state["etag"]:
                state["conflicts"] += 1
                raise blob_db.BlobConflict()
            state["n"] += 1
            state["body"] = bytes(body)
            state["etag"] = f"e{state['n']}"
            return state["etag"]

    monkeypatch.setattr(blob_db, "configured", lambda: True)
    monkeypatch.setattr(blob_db, "download", download)
    monkeypatch.setattr(blob_db, "stat", stat)
    monkeypatch.setattr(blob_db, "upload", upload)

    def make_app():
        return create_app(
            Settings(
                db_path=tmp_path / "ignored.db",
                api_key="test-key",
                seed=False,
                pin_morning=False,
                skill_path=ROOT / "skill.md",
                seat_tokens={},
            )
        )

    first = make_app()
    second = make_app()
    store_a = first.state.store
    store_b = second.state.store
    store_a.adjust_clock({"at": "2026-10-28T08:00:00"})
    store_a.submit_design("design", dict(DESIGN))
    store_b.watch_state()
    pair["left"] = 2
    errors = []

    def paint():
        try:
            store_a.act("claude", {"type": "paint", "pixels": [{"x": 1, "y": 1, "color": 4}]})
        except Exception as exc:
            errors.append(exc)

    def chat():
        try:
            store_b.post_chat("grok-artist", "keeping the pines")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=paint), threading.Thread(target=chat)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert state["conflicts"] >= 1
    fresh = make_app()
    designed_id = fresh.state.store.conn.execute(
        "SELECT id FROM days WHERE theme IS NOT NULL ORDER BY id ASC LIMIT 1"
    ).fetchone()["id"]
    cells = fresh.state.store.region(designed_id, 1, 1, 1, 1)
    assert cells["cells"] == [4]
    notes = fresh.state.store.list_chat(designed_id)["messages"]
    assert notes[-1]["text"] == "keeping the pines"
    first.state.store.close()
    second.state.store.close()
    fresh.state.store.close()
