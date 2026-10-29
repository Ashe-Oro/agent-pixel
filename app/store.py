"""SQLite state for one fresh canvas per New York calendar day."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app import blob_db
from app.config import Settings
from app.schedule import (
    ARTISTS,
    DISPLAY,
    GRID,
    PIXELS_PER_ARTIST_DAY,
    PIXELS_PER_TURN,
    SLICE_COUNT,
    NY,
    artist_on_slice,
    day_bounds,
    label_clock,
    label_hhmm,
    ny_date,
    parse_ny_local,
    position,
    rotate,
    round_on_slice,
    slice_window,
)

# Latest-stroke cells stay in TurnView only while the list is small.
# A full-canvas paint is the preview PNG, not 65,536 coordinates in the prompt.
TURN_VIEW_INLINE_PIXELS = 512
from app.seed import COZY_BRIEF, COZY_PALETTE, COZY_THEME, blank_canvas, draw_cabin

KNOWN_AGENTS = ("design",) + ARTISTS


class ApiError(Exception):
    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.remote = blob_db.configured()
        self.blob_name = blob_db.pathname()
        self.etag: str | None = None
        self.dirty = False
        # Vercel's deployment filesystem is read-only. /tmp survives for one instance.
        if self.remote or os.environ.get("VERCEL"):
            self.path = Path("/tmp/agent-pixel.sqlite")
        else:
            self.path = settings.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        downloaded = False
        if self.remote:
            downloaded = self._download()
        self._connect()
        self._init_schema()
        if self.remote and not downloaded:
            self.dirty = True
        if self.remote and self.dirty:
            self._flush()

    def close(self) -> None:
        self.conn.close()

    @property
    def persistent(self) -> bool:
        return self.remote

    def _connect(self) -> None:
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None
        journal = "DELETE" if self.remote else "WAL"
        self.conn.execute(f"PRAGMA journal_mode={journal}")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=3000")

    def _download(self) -> bool:
        try:
            payload, etag = blob_db.download(self.blob_name)
        except blob_db.BlobMissing:
            return False
        except Exception as exc:
            raise ApiError(503, "store_unavailable", "Could not load the canvas from Blob.") from exc
        if not payload:
            return False
        self.path.write_bytes(payload)
        self.etag = etag
        return True

    def _refresh(self) -> None:
        if not self.remote:
            return
        if self.dirty:
            self._flush()
            return
        try:
            etag = blob_db.stat(self.blob_name)
        except blob_db.BlobMissing:
            return
        except Exception as exc:
            raise ApiError(503, "store_unavailable", "Could not load the canvas from Blob.") from exc
        if etag and etag == self.etag:
            return
        try:
            payload, etag = blob_db.download(self.blob_name)
        except blob_db.BlobMissing:
            return
        except Exception as exc:
            raise ApiError(503, "store_unavailable", "Could not load the canvas from Blob.") from exc
        self.conn.close()
        self.path.write_bytes(payload)
        self.etag = etag
        self._connect()
        self._init_schema()
        if self.dirty:
            self._flush()

    def _flush(self) -> None:
        if not self.remote or not self.dirty:
            return
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        try:
            self.etag = blob_db.upload(self.blob_name, self.path.read_bytes())
        except Exception as exc:
            raise ApiError(503, "store_unavailable", "Could not save the canvas to Blob.") from exc
        self.dirty = False

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              clock_utc TEXT,
              roster_json TEXT NOT NULL,
              rotate INTEGER NOT NULL DEFAULT 1,
              pixels_per_turn INTEGER NOT NULL DEFAULT 0,
              pixels_per_artist_day INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS days (
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
            CREATE TABLE IF NOT EXISTS artist_state (
              day_id INTEGER NOT NULL,
              agent_id TEXT NOT NULL,
              pixels_used INTEGER NOT NULL DEFAULT 0,
              skipped INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (day_id, agent_id)
            );
            CREATE TABLE IF NOT EXISTS actions (
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
        row = self.conn.execute("SELECT id FROM settings WHERE id = 1").fetchone()
        if row is None:
            self.conn.execute(
                """
                INSERT INTO settings (id, roster_json, rotate, pixels_per_turn, pixels_per_artist_day)
                VALUES (1, ?, 1, 0, 0)
                """,
                (json.dumps(list(ARTISTS)),),
            )
        # 0 means unlimited. Rows created with the old 64 / 256 caps would
        # still block a full-grid stroke, so clear them on open.
        cleared = self.conn.execute(
            """
            UPDATE settings
            SET pixels_per_turn = 0, pixels_per_artist_day = 0
            WHERE id = 1 AND (pixels_per_turn != 0 OR pixels_per_artist_day != 0)
            """
        )
        if cleared.rowcount:
            self.dirty = True

    # --- clock -------------------------------------------------------------

    def now(self) -> datetime:
        row = self._settings()
        if row["clock_utc"]:
            return datetime.fromisoformat(row["clock_utc"])
        return datetime.now(timezone.utc)

    def _settings(self) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()

    def set_clock(self, when: datetime | None) -> dict[str, Any]:
        with self.lock:
            self._refresh()
            if when is None:
                self.conn.execute("UPDATE settings SET clock_utc = NULL WHERE id = 1")
            else:
                utc = when.astimezone(timezone.utc).replace(microsecond=0)
                self.conn.execute(
                    "UPDATE settings SET clock_utc = ? WHERE id = 1",
                    (utc.isoformat(),),
                )
            self.dirty = True
            self._flush()
        return self.clock_info()

    def clock_info(self) -> dict[str, Any]:
        self._refresh()
        row = self._settings()
        current = self.now()
        phase, index = position(current)
        return {
            "ny_now": current.astimezone(NY).isoformat(),
            "ny_label": label_clock(current),
            "pinned": row["clock_utc"] is not None,
            "tz": "America/New_York",
            "phase": phase,
            "slice_index": index,
        }

    def adjust_clock(self, body: dict[str, Any]) -> dict[str, Any]:
        mode = body.get("mode")
        if mode == "realtime":
            return self.set_clock(None)
        if "at" in body:
            when = parse_ny_local(str(body["at"]))
            return self.set_clock(when)
        current = self.now()
        if mode == "advance_seconds" or "advance_seconds" in body:
            seconds = int(body.get("seconds", body.get("advance_seconds", 0)))
            return self.set_clock(current + timedelta(seconds=seconds))
        local = current.astimezone(NY)
        start, lock = day_bounds(local.date())
        if mode == "next_slice":
            phase, index = position(current)
            if phase == "before":
                target = start
            elif phase == "after":
                target = lock
            else:
                _, target = slice_window(local.date(), index)
            return self.set_clock(target)
        if mode == "lock":
            return self.set_clock(lock)
        if mode == "next_morning":
            nxt = local.date() + timedelta(days=1)
            target, _ = day_bounds(nxt)
            return self.set_clock(target)
        raise ApiError(400, "bad_clock", "Pass at, mode, or advance_seconds.")

    # --- days --------------------------------------------------------------

    def _ensure_today(self, now: datetime) -> sqlite3.Row:
        key = ny_date(now)
        row = self.conn.execute("SELECT * FROM days WHERE ny_date = ?", (key,)).fetchone()
        if row:
            return row
        nxt = self.conn.execute("SELECT COALESCE(MAX(day_index), 0) + 1 AS n FROM days").fetchone()["n"]
        self.conn.execute(
            "INSERT INTO days (day_index, ny_date, created_at) VALUES (?, ?, ?)",
            (nxt, key, int(now.timestamp())),
        )
        self.dirty = True
        return self.conn.execute("SELECT * FROM days WHERE ny_date = ?", (key,)).fetchone()

    def open_today(self) -> dict[str, Any]:
        with self.lock:
            self._refresh()
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                day = self._ensure_today(self.now())
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            self._flush()
        return self.watch_state(day_id=day["id"])

    def today_row(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM days WHERE ny_date = ?",
            (ny_date(self.now()),),
        ).fetchone()

    def day_by_id(self, day_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM days WHERE id = ?", (day_id,)).fetchone()
        if row is None:
            raise ApiError(404, "no_day", "No mural with that id.")
        return row

    def set_roster(self, order: list[str], rotate: bool) -> dict[str, Any]:
        if sorted(order) != sorted(ARTISTS) or len(order) != len(ARTISTS):
            raise ApiError(
                400,
                "bad_roster",
                "Order must be a permutation of claude, grok-artist, openai.",
            )
        with self.lock:
            self._refresh()
            self.conn.execute(
                "UPDATE settings SET roster_json = ?, rotate = ? WHERE id = 1",
                (json.dumps(order), 1 if rotate else 0),
            )
            self.dirty = True
            self._flush()
        return {"order": order, "rotate": rotate}

    def set_budgets(self) -> dict[str, Any]:
        """Pixel caps stay off. Stored 0 is the unlimited sentinel, not a budget of zero."""
        with self.lock:
            self._refresh()
            self.conn.execute(
                "UPDATE settings SET pixels_per_turn = 0, pixels_per_artist_day = 0 WHERE id = 1"
            )
            self.dirty = True
            self._flush()
        return {"pixels_per_turn": PIXELS_PER_TURN, "pixels_per_artist_day": PIXELS_PER_ARTIST_DAY}

    # --- design / act ------------------------------------------------------

    def design_view(self, agent_id: str) -> dict[str, Any]:
        if agent_id != "design":
            raise ApiError(403, "wrong_role", "Only the design agent can read the design brief.")
        self._refresh()
        now = self.now()
        day = self.today_row()
        phase, _ = position(now)
        designed = bool(day and day["theme"])
        to_act = phase == "during" and not designed
        reason = "ready"
        if phase == "before":
            reason = "before_routine"
        elif phase == "after":
            reason = "morning_locked"
        elif designed:
            reason = "already_designed"
        return {
            "role": "design",
            "to_act": to_act,
            "reason": reason,
            "ny_date": ny_date(now),
            "day_index": day["day_index"] if day else None,
            "mural_id": f"day-{day['day_index']}" if day else None,
            "status": self._status(day, now),
        }

    def submit_design(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if agent_id != "design":
            raise ApiError(403, "wrong_role", "Only the design agent can publish a theme.")
        if payload.get("type") != "design":
            raise ApiError(400, "bad_action", "Design payload type must be 'design'.")
        theme, brief, palette = _parse_design(payload)
        with self.lock:
            self._refresh()
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                now = self.now()
                phase, index = position(now)
                if phase == "before":
                    raise ApiError(409, "before_routine", "Design opens at 08:00 America/New_York.")
                if phase == "after":
                    raise ApiError(409, "morning_locked", "The morning locked at 11:00 ET.")
                day = self._ensure_today(now)
                if day["theme"]:
                    raise ApiError(409, "already_designed", "Today already has a theme and palette.")
                settings = self._settings()
                order = rotate(
                    json.loads(settings["roster_json"]),
                    day["day_index"],
                    bool(settings["rotate"]),
                )
                canvas = bytes(blank_canvas())
                self.conn.execute(
                    """
                    UPDATE days
                    SET theme = ?, brief = ?, palette_json = ?, canvas = ?,
                        artist_order_json = ?, designed_at = ?
                    WHERE id = ?
                    """,
                    (
                        theme,
                        brief,
                        json.dumps(palette),
                        canvas,
                        json.dumps(order),
                        int(now.timestamp()),
                        day["id"],
                    ),
                )
                self.conn.commit()
                self.dirty = True
                day_id = day["id"]
                day_index = day["day_index"]
            except Exception:
                self.conn.rollback()
                raise
            self._flush()
        return {
            "ok": True,
            "mural_id": f"day-{day_index}",
            "day_id": day_id,
            "day_index": day_index,
            "theme": theme,
            "brief": brief,
            "palette": palette,
            "grid_w": GRID,
            "grid_h": GRID,
            "first_agent_id": order[index % len(order)],
            "round_index": round_on_slice(index),
            "slice_index": index,
        }

    def act(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if agent_id not in ARTISTS:
            raise ApiError(403, "wrong_role", "Only an artist can paint or pass.")
        action = payload.get("type")
        if action not in ("paint", "pass"):
            raise ApiError(400, "bad_action", "Action type must be paint or pass.")
        note = _parse_note(payload.get("note"))
        with self.lock:
            self._refresh()
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                now = self.now()
                day = self.today_row()
                if day is None:
                    raise ApiError(409, "no_day", "Today's canvas is not open yet.")
                if not day["theme"]:
                    raise ApiError(409, "awaiting_design", "The design agent has not opened the canvas.")
                self._require_slice(day, agent_id, now)
                phase, index = position(now)
                if index is None or phase != "during":
                    raise ApiError(409, "morning_locked", "The brush is closed.")
                focus = None
                charged = 0
                pixels_out: list[dict[str, int]] = []
                done = False
                if action == "paint":
                    raw = _parse_pixels(payload.get("pixels"))
                    palette_ids = {swatch["id"] for swatch in json.loads(day["palette_json"])}
                    for pixel in raw:
                        if not (0 <= pixel["x"] < GRID and 0 <= pixel["y"] < GRID):
                            raise ApiError(400, "out_of_bounds", "A pixel is outside the 256×256 grid.")
                        if pixel["color"] not in palette_ids:
                            raise ApiError(400, "bad_color", "Color is not in today's palette.")
                    collapsed = _collapse(raw)
                    charged = len(collapsed)
                    focus = _parse_focus(payload.get("focus"))
                    canvas = bytearray(bytes(day["canvas"]))
                    for x, y, color in collapsed:
                        canvas[y * GRID + x] = color
                        pixels_out.append({"x": x, "y": y, "color": color})
                    focus_map = json.loads(day["focus_json"]) if day["focus_json"] else {}
                    if focus is not None:
                        focus_map[agent_id] = focus
                    self.conn.execute(
                        "UPDATE days SET canvas = ?, focus_json = ? WHERE id = ?",
                        (bytes(canvas), json.dumps(focus_map), day["id"]),
                    )
                    self._add_pixels(day["id"], agent_id, charged)
                else:
                    done = bool(payload.get("done", False))
                    if payload.get("done") not in (None, True, False):
                        raise ApiError(400, "bad_action", "done must be a boolean.")
                    if done:
                        self._skip_artist(day["id"], agent_id)
                try:
                    self.conn.execute(
                        """
                        INSERT INTO actions (
                          day_id, agent_id, slice_index, round_index, action, done,
                          pixel_count, note, focus_json, pixels_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            day["id"],
                            agent_id,
                            index,
                            round_on_slice(index),
                            action,
                            1 if done else 0,
                            charged,
                            note,
                            json.dumps(focus) if focus else None,
                            json.dumps(pixels_out),
                            int(now.timestamp()),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ApiError(409, "slice_used", "This 15-minute slice already has an action.") from exc
                self.conn.commit()
                self.dirty = True
                day_id = day["id"]
            except Exception:
                self.conn.rollback()
                raise
            self._flush()
        fresh = self.day_by_id(day_id)
        return {
            "ok": True,
            "action": action,
            "charged": charged,
            "done": done,
            "slice_index": index,
            "round_index": round_on_slice(index),
            "pixels_remaining_day": PIXELS_PER_ARTIST_DAY,
            "pixels_remaining_turn": 0,
            "turns_remaining": self._turns_remaining(fresh, agent_id, self.now()),
            "to_act": False,
        }

    def _require_slice(self, day: sqlite3.Row, agent_id: str, now: datetime) -> None:
        if day["ny_date"] != ny_date(now):
            raise ApiError(409, "morning_locked", "That canvas is archived.")
        phase, index = position(now)
        if phase == "before":
            raise ApiError(409, "before_routine", "Artists paint from 08:00 ET.")
        if phase == "after":
            raise ApiError(409, "morning_locked", "The canvas locked at 11:00 ET.")
        order = json.loads(day["artist_order_json"])
        expected = artist_on_slice(order, index)
        if agent_id != expected:
            raise ApiError(
                409,
                "not_your_turn",
                f"This slice belongs to {DISPLAY[expected]}.",
            )
        if self._is_skipped(day["id"], agent_id):
            raise ApiError(409, "rounds_skipped", "You already ended your rounds for today.")
        if self._action_for_slice(day["id"], index) is not None:
            raise ApiError(409, "slice_used", "This 15-minute slice already has an action.")

    def turn_view(self, agent_id: str) -> dict[str, Any]:
        if agent_id not in ARTISTS:
            raise ApiError(403, "wrong_role", "TurnView is for an artist seat.")
        self._refresh()
        now = self.now()
        day = self.today_row()
        return self._turn_from(day, agent_id, now)

    def _turn_from(self, day: sqlite3.Row | None, agent_id: str, now: datetime) -> dict[str, Any]:
        phase, index = position(now)
        designed = bool(day and day["theme"])
        order = self._order_for(day)
        to_act = False
        if designed and phase == "during" and day["ny_date"] == ny_date(now):
            to_act = (
                artist_on_slice(order, index) == agent_id
                and not self._is_skipped(day["id"], agent_id)
                and self._action_for_slice(day["id"], index) is None
            )
        turn_left = None if to_act else 0
        if phase == "during":
            round_index = round_on_slice(index)
            _, slice_end = slice_window(now.astimezone(NY).date(), index)
            slice_start, _ = slice_window(now.astimezone(NY).date(), index)
        elif phase == "before":
            round_index = 1
            slice_start, slice_end = slice_window(now.astimezone(NY).date(), 0)
        else:
            round_index = 4
            slice_start, slice_end = day_bounds(now.astimezone(NY).date())
            slice_start = slice_end
        palette = json.loads(day["palette_json"]) if day and day["palette_json"] else []
        focus = None
        if day and day["focus_json"]:
            focus = json.loads(day["focus_json"]).get(agent_id)
        preview = None
        crop_url = None
        if day and day["canvas"] is not None:
            preview = f"/watch/canvas.png?day_id={day['id']}&scale=3"
            if focus:
                crop_url = (
                    f"/watch/crop.png?day_id={day['id']}"
                    f"&x={focus['x']}&y={focus['y']}&w={focus['w']}&h={focus['h']}&scale=4"
                )
        strokes = self._recent_strokes(day["id"], include_latest_pixels=True) if day else []
        return {
            "mural_id": f"day-{day['day_index']}" if day else None,
            "day_id": day["id"] if day else None,
            "day_index": day["day_index"] if day else None,
            "ny_date": day["ny_date"] if day else ny_date(now),
            "status": self._status(day, now),
            "theme": day["theme"] if day else None,
            "brief": day["brief"] if day else None,
            "palette": palette,
            "grid_w": GRID,
            "grid_h": GRID,
            "preview_url": preview,
            "focus_crop_url": crop_url,
            "my_agent_id": agent_id,
            "my_display_name": DISPLAY[agent_id],
            "to_act": to_act,
            "turns_remaining": self._turns_remaining(day, agent_id, now),
            "round_index": round_index,
            "slice_index": index,
            "slice_starts_at_unix": int(slice_start.timestamp()),
            "slice_ends_at_unix": int(slice_end.timestamp()),
            "pixels_remaining_day": PIXELS_PER_ARTIST_DAY,
            "pixels_remaining_turn": turn_left,
            "recent_strokes": strokes,
            "canvas_stats": _stats(bytes(day["canvas"]) if day and day["canvas"] is not None else None, palette),
            "roster": [{"agent_id": agent, "display_name": DISPLAY[agent]} for agent in order],
            "current_agent_id": artist_on_slice(order, index) if index is not None and designed else None,
        }

    def routine_tick(self) -> dict[str, Any]:
        self._refresh()
        now = self.now()
        phase, index = position(now)
        if phase == "before":
            return {
                "ok": True,
                "phase": "before_open",
                "ny_now": now.astimezone(NY).isoformat(),
                "ny_label": label_clock(now),
                "hint": "Wait until 08:00 America/New_York, then tick again and post a design.",
                "day": None,
            }
        with self.lock:
            self._refresh()
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                day = self._ensure_today(now)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            self._flush()
        designed = bool(day["theme"])
        if phase == "after":
            public_phase = "locked"
            hint = "Canvas is locked until tomorrow's 08:00 ET routine."
            current = None
        elif not designed:
            public_phase = "awaiting_design"
            hint = "POST /agent/design as the design agent, then artists paint on their slices."
            current = None
        else:
            public_phase = "slice"
            order = json.loads(day["artist_order_json"])
            current = artist_on_slice(order, index)
            hint = (
                f"GET /agent/turn as {current}, then POST /agent/act with one paint or pass "
                f"before {label_hhmm(slice_window(now.astimezone(NY).date(), index)[1])} ET."
            )
        return {
            "ok": True,
            "phase": public_phase,
            "ny_now": now.astimezone(NY).isoformat(),
            "ny_label": label_clock(now),
            "slice_index": index,
            "round_index": round_on_slice(index) if index is not None else None,
            "current_agent_id": current,
            "hint": hint,
            "day": {
                "day_id": day["id"],
                "day_index": day["day_index"],
                "mural_id": f"day-{day['day_index']}",
                "ny_date": day["ny_date"],
                "theme": day["theme"],
                "designed": designed,
            },
        }

    # --- reads -------------------------------------------------------------

    def watch_state(self, day_id: int | None = None) -> dict[str, Any]:
        self._refresh()
        now = self.now()
        today = self.today_row()
        if day_id is None:
            featured = today
        else:
            featured = self.day_by_id(day_id)
        clock = self.clock_info()
        archive = self._archive(exclude_id=featured["id"] if featured else None)
        if featured is None:
            return {
                "status": "no_day",
                "message": "No canvas for this date yet. The morning routine opens one at 8:00 ET.",
                "ny_date": ny_date(now),
                "grid_w": GRID,
                "grid_h": GRID,
                "clock": clock,
                "schedule": _schedule_meta(),
                "slices": [],
                "archive": archive,
                "preview_url": None,
                "viewing_archive": False,
            }
        order = self._order_for(featured)
        palette = json.loads(featured["palette_json"]) if featured["palette_json"] else []
        phase, index = position(now)
        is_today = featured["ny_date"] == ny_date(now)
        if not is_today or phase == "after":
            live_index = None
            miss_before = SLICE_COUNT
        elif phase == "before" or index is None:
            live_index = None
            miss_before = None
        elif not featured["theme"]:
            live_index = None
            miss_before = index
        else:
            live_index = index
            miss_before = index
        slices = self._slice_board(featured, order, live_index, miss_before)
        current = None
        if is_today and phase == "during" and featured["theme"]:
            current = artist_on_slice(order, index)
        preview = None
        if featured["canvas"] is not None:
            preview = f"/watch/canvas.png?day_id={featured['id']}&scale=3"
        focus = None
        if featured["focus_json"]:
            focus_map = json.loads(featured["focus_json"])
            if current and current in focus_map:
                focus = focus_map[current]
            elif focus_map:
                focus = next(iter(focus_map.values()))
        artists = []
        for agent in order:
            used = self._pixels_used(featured["id"], agent)
            artists.append(
                {
                    "agent_id": agent,
                    "display_name": DISPLAY[agent],
                    "pixels_used": used,
                    "pixels_remaining_day": PIXELS_PER_ARTIST_DAY,
                    "turns_remaining": self._turns_remaining(featured, agent, now if is_today else _lock_time(featured["ny_date"])),
                    "skipped": self._is_skipped(featured["id"], agent),
                    "to_act": bool(
                        is_today
                        and current == agent
                        and not self._is_skipped(featured["id"], agent)
                        and live_index is not None
                        and self._action_for_slice(featured["id"], live_index) is None
                    ),
                }
            )
        active = next((item for item in artists if item["to_act"]), None)
        return {
            "status": self._status(featured, now if is_today else _lock_time(featured["ny_date"])),
            "message": _status_message(self._status(featured, now if is_today else _lock_time(featured["ny_date"]))),
            "mural_id": f"day-{featured['day_index']}",
            "day_id": featured["id"],
            "day_index": featured["day_index"],
            "ny_date": featured["ny_date"],
            "theme": featured["theme"],
            "brief": featured["brief"],
            "palette": palette,
            "grid_w": GRID,
            "grid_h": GRID,
            "demo_seed": bool(featured["demo_seed"]),
            "preview_url": preview,
            "focus": focus,
            "focus_crop_url": (
                f"/watch/crop.png?day_id={featured['id']}&x={focus['x']}&y={focus['y']}"
                f"&w={focus['w']}&h={focus['h']}&scale=4"
                if focus
                else None
            ),
            "current_agent_id": current,
            "current_display_name": DISPLAY[current] if current else None,
            "round_index": round_on_slice(live_index) if live_index is not None else None,
            "slice_index": live_index,
            "slice_ends_at_unix": (
                int(slice_window(now.astimezone(NY).date(), live_index)[1].timestamp())
                if live_index is not None
                else None
            ),
            "turns_remaining": active["turns_remaining"] if active else None,
            "pixels_remaining_turn": None if active else 0,
            "artists": artists,
            "slices": slices,
            "recent_strokes": self._recent_strokes(featured["id"], include_latest_pixels=False),
            "canvas_stats": _stats(
                bytes(featured["canvas"]) if featured["canvas"] is not None else None,
                palette,
            ),
            "clock": clock,
            "schedule": _schedule_meta(),
            "archive": archive,
            "viewing_archive": not is_today,
        }

    def ledger(self, day_id: int) -> dict[str, Any]:
        self._refresh()
        day = self.day_by_id(day_id)
        rows = self.conn.execute(
            "SELECT * FROM actions WHERE day_id = ? ORDER BY id ASC",
            (day_id,),
        ).fetchall()
        strokes = []
        for row in rows:
            strokes.append(
                {
                    "id": row["id"],
                    "agent_id": row["agent_id"],
                    "display_name": DISPLAY.get(row["agent_id"], row["agent_id"]),
                    "slice_index": row["slice_index"],
                    "round_index": row["round_index"],
                    "action": row["action"],
                    "done": bool(row["done"]),
                    "pixel_count": row["pixel_count"],
                    "note": row["note"],
                    "pixels": json.loads(row["pixels_json"]) if row["pixels_json"] else [],
                    "created_at_unix": row["created_at"],
                }
            )
        return {
            "day_id": day["id"],
            "mural_id": f"day-{day['day_index']}",
            "ny_date": day["ny_date"],
            "strokes": strokes,
        }

    def region(self, day_id: int, x: int, y: int, w: int, h: int) -> dict[str, Any]:
        self._refresh()
        if w < 1 or h < 1 or w > 64 or h > 64 or x < 0 or y < 0 or x + w > GRID or y + h > GRID:
            raise ApiError(400, "out_of_bounds", "Region must sit on the canvas and be at most 64×64.")
        day = self.day_by_id(day_id)
        if day["canvas"] is None:
            raise ApiError(409, "awaiting_design", "Canvas is not open yet.")
        canvas = bytes(day["canvas"])
        cells = []
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                cells.append(canvas[yy * GRID + xx])
        return {"day_id": day_id, "x": x, "y": y, "w": w, "h": h, "cells": cells}

    def canvas_of(self, day_id: int) -> tuple[bytes, list[dict]]:
        self._refresh()
        day = self.day_by_id(day_id)
        if day["canvas"] is None or not day["palette_json"]:
            raise ApiError(404, "no_canvas", "That day has no pixels yet.")
        return bytes(day["canvas"]), json.loads(day["palette_json"])

    # --- internals ---------------------------------------------------------

    def _order_for(self, day: sqlite3.Row | None) -> list[str]:
        if day and day["artist_order_json"]:
            return json.loads(day["artist_order_json"])
        settings = self._settings()
        base = json.loads(settings["roster_json"])
        index = day["day_index"] if day else 1
        return rotate(base, index, bool(settings["rotate"]))

    def _status(self, day: sqlite3.Row | None, now: datetime) -> str:
        if day is None:
            return "no_day"
        if day["ny_date"] < ny_date(now):
            return "locked" if day["theme"] else "missed"
        if day["ny_date"] > ny_date(now):
            return "scheduled"
        phase, _ = position(now)
        if not day["theme"]:
            if phase == "after":
                return "missed"
            if phase == "before":
                return "before_open"
            return "awaiting_design"
        if phase == "before":
            return "before_open"
        if phase == "after":
            return "locked"
        return "painting"

    def _pixels_used(self, day_id: int, agent_id: str) -> int:
        row = self.conn.execute(
            "SELECT pixels_used FROM artist_state WHERE day_id = ? AND agent_id = ?",
            (day_id, agent_id),
        ).fetchone()
        return int(row["pixels_used"]) if row else 0

    def _is_skipped(self, day_id: int, agent_id: str) -> bool:
        row = self.conn.execute(
            "SELECT skipped FROM artist_state WHERE day_id = ? AND agent_id = ?",
            (day_id, agent_id),
        ).fetchone()
        return bool(row and row["skipped"])

    def _add_pixels(self, day_id: int, agent_id: str, count: int) -> None:
        self.conn.execute(
            """
            INSERT INTO artist_state (day_id, agent_id, pixels_used, skipped)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(day_id, agent_id) DO UPDATE SET pixels_used = pixels_used + excluded.pixels_used
            """,
            (day_id, agent_id, count),
        )

    def _skip_artist(self, day_id: int, agent_id: str) -> None:
        self.conn.execute(
            """
            INSERT INTO artist_state (day_id, agent_id, pixels_used, skipped)
            VALUES (?, ?, 0, 1)
            ON CONFLICT(day_id, agent_id) DO UPDATE SET skipped = 1
            """,
            (day_id, agent_id),
        )

    def _action_for_slice(self, day_id: int, index: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM actions WHERE day_id = ? AND slice_index = ?",
            (day_id, index),
        ).fetchone()

    def _turns_remaining(self, day: sqlite3.Row | None, agent_id: str, now: datetime) -> int:
        if day is None or day["ny_date"] != ny_date(now):
            if day and day["ny_date"] < ny_date(now):
                return 0
        if day and self._is_skipped(day["id"], agent_id):
            return 0
        phase, index = position(now)
        if day and day["ny_date"] < ny_date(now):
            return 0
        if phase == "after" and (day is None or day["ny_date"] == ny_date(now)):
            return 0
        floor = 0 if phase == "before" else (index or 0)
        if phase == "after":
            return 0
        order = self._order_for(day)
        remaining = 0
        for slice_i in range(SLICE_COUNT):
            if artist_on_slice(order, slice_i) != agent_id:
                continue
            if slice_i < floor:
                continue
            if day and self._action_for_slice(day["id"], slice_i) is not None:
                continue
            remaining += 1
        return remaining

    def _recent_strokes(self, day_id: int, include_latest_pixels: bool) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM actions WHERE day_id = ? ORDER BY id DESC LIMIT 6",
            (day_id,),
        ).fetchall()
        strokes = []
        for offset, row in enumerate(rows):
            item = {
                "id": row["id"],
                "agent_id": row["agent_id"],
                "display_name": DISPLAY.get(row["agent_id"], row["agent_id"]),
                "slice_index": row["slice_index"],
                "round_index": row["round_index"],
                "action": row["action"],
                "done": bool(row["done"]),
                "pixel_count": row["pixel_count"],
                "note": row["note"],
                "created_at_unix": row["created_at"],
            }
            if include_latest_pixels and offset == 0 and row["action"] == "paint" and row["pixels_json"]:
                if int(row["pixel_count"]) <= TURN_VIEW_INLINE_PIXELS:
                    item["pixels"] = json.loads(row["pixels_json"])
                else:
                    item["pixels_omitted"] = True
            strokes.append(item)
        return strokes

    def _slice_board(
        self,
        day: sqlite3.Row,
        order: list[str],
        live_index: int | None,
        miss_before: int | None,
    ) -> list[dict[str, Any]]:
        local_date = datetime.fromisoformat(day["ny_date"]).date()
        actions = {
            row["slice_index"]: row
            for row in self.conn.execute(
                "SELECT * FROM actions WHERE day_id = ?",
                (day["id"],),
            ).fetchall()
        }
        skip_from: dict[str, int] = {}
        for row in actions.values():
            if row["done"]:
                skip_from[row["agent_id"]] = row["slice_index"]
        board = []
        for index in range(SLICE_COUNT):
            begin, end = slice_window(local_date, index)
            agent = artist_on_slice(order, index)
            action = actions.get(index)
            ended_early = skip_from.get(agent)
            if action is not None:
                state = "done"
            elif ended_early is not None and index > ended_early:
                state = "skipped"
            elif miss_before is not None and index < miss_before:
                state = "missed"
            elif live_index is not None and index < live_index:
                state = "missed"
            elif live_index == index:
                state = "active"
            else:
                state = "upcoming"
            board.append(
                {
                    "index": index,
                    "round": round_on_slice(index),
                    "agent_id": agent,
                    "display_name": DISPLAY[agent],
                    "starts": label_hhmm(begin),
                    "ends": label_hhmm(end),
                    "state": state,
                    "pixel_count": action["pixel_count"] if action else 0,
                    "action": action["action"] if action else None,
                }
            )
        return board

    def _archive(self, exclude_id: int | None) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM days ORDER BY day_index DESC").fetchall()
        items = []
        for row in rows:
            if exclude_id is not None and row["id"] == exclude_id:
                continue
            items.append(
                {
                    "day_id": row["id"],
                    "day_index": row["day_index"],
                    "ny_date": row["ny_date"],
                    "theme": row["theme"],
                    "status": "locked" if row["theme"] else "missed",
                    "preview_url": (
                        f"/watch/canvas.png?day_id={row['id']}&scale=2" if row["canvas"] is not None else None
                    ),
                }
            )
        return items

    def seed_demo(self) -> None:
        """Pin 08:05 ET and bake a cabin so /watch is paintable right away."""
        with self.lock:
            self._refresh()
            existing = self.conn.execute("SELECT COUNT(*) AS n FROM days").fetchone()["n"]
            if existing:
                return
            if self.settings.pin_morning:
                real_today = datetime.now(timezone.utc).astimezone(NY).date()
                pinned, _ = day_bounds(real_today)
                pinned = pinned + timedelta(minutes=5)
                utc = pinned.astimezone(timezone.utc).replace(microsecond=0)
                self.conn.execute(
                    "UPDATE settings SET clock_utc = ? WHERE id = 1",
                    (utc.isoformat(),),
                )
                self.dirty = True
            now = self.now()
            phase, _ = position(now)
            if phase != "during":
                self._flush()
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                day = self._ensure_today(now)
                if day["theme"]:
                    self.conn.commit()
                    self.dirty = True
                    self._flush()
                    return
                settings = self._settings()
                order = rotate(
                    json.loads(settings["roster_json"]),
                    day["day_index"],
                    bool(settings["rotate"]),
                )
                canvas = blank_canvas()
                draw_cabin(canvas)
                self.conn.execute(
                    """
                    UPDATE days
                    SET theme = ?, brief = ?, palette_json = ?, canvas = ?,
                        artist_order_json = ?, demo_seed = 1, designed_at = ?
                    WHERE id = ?
                    """,
                    (
                        COZY_THEME,
                        COZY_BRIEF,
                        json.dumps(COZY_PALETTE),
                        bytes(canvas),
                        json.dumps(order),
                        int(now.timestamp()),
                        day["id"],
                    ),
                )
                self.conn.commit()
                self.dirty = True
            except Exception:
                self.conn.rollback()
                raise
            self._flush()


def _lock_time(ny_date_text: str) -> datetime:
    year, month, day = (int(part) for part in ny_date_text.split("-"))
    _, lock = day_bounds(datetime(year, month, day).date())
    return lock


def _schedule_meta() -> dict[str, Any]:
    return {
        "tz": "America/New_York",
        "opens": "08:00",
        "locks": "11:00",
        "slice_minutes": 15,
        "rounds": 4,
        "slices": SLICE_COUNT,
        "pixels_per_turn": PIXELS_PER_TURN,
        "pixels_per_artist_day": PIXELS_PER_ARTIST_DAY,
        "turns_per_artist": 4,
    }


def _status_message(status: str) -> str:
    return {
        "no_day": "No canvas for this date yet. The morning routine opens one at 8:00 ET.",
        "before_open": "The canvas is ready and waiting. Painting starts at 8:00 ET.",
        "awaiting_design": "Waiting for the design agent to publish today's theme and 16 colors.",
        "painting": "The morning is open. One artist holds the brush for this 15-minute slice.",
        "locked": "Locked at 11:00 ET. Tomorrow starts with a blank canvas and a new palette.",
        "missed": "The morning passed without a design. Tomorrow starts blank.",
        "scheduled": "This canvas is scheduled.",
    }.get(status, status)


def _parse_note(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ApiError(400, "bad_note", "Note must be a string.")
    if len(raw) > 120:
        raise ApiError(400, "bad_note", "Note must be 120 characters or fewer.")
    return raw


def _parse_focus(raw: Any) -> dict[str, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ApiError(400, "bad_focus", "Focus must be an object with x, y, w, h.")
    box = {}
    for key in ("x", "y", "w", "h"):
        value = raw.get(key)
        if type(value) is not int:
            raise ApiError(400, "bad_focus", "Focus x, y, w, h must be integers.")
        box[key] = value
    if box["w"] < 1 or box["h"] < 1 or box["x"] < 0 or box["y"] < 0:
        raise ApiError(400, "bad_focus", "Focus box is outside the canvas.")
    if box["x"] + box["w"] > GRID or box["y"] + box["h"] > GRID:
        raise ApiError(400, "bad_focus", "Focus box is outside the canvas.")
    return box


def _parse_pixels(raw: Any) -> list[dict[str, int]]:
    if not isinstance(raw, list) or len(raw) < 1:
        raise ApiError(400, "bad_pixels", "pixels must be a non-empty list.")
    parsed = []
    for item in raw:
        if not isinstance(item, dict):
            raise ApiError(400, "bad_pixels", "Each pixel must be an object.")
        x, y, color = item.get("x"), item.get("y"), item.get("color")
        if type(x) is not int or type(y) is not int or type(color) is not int:
            raise ApiError(400, "bad_pixels", "Pixel x, y, and color must be integers.")
        parsed.append({"x": x, "y": y, "color": color})
    return parsed


def _collapse(pixels: list[dict[str, int]]) -> list[tuple[int, int, int]]:
    order: list[tuple[int, int]] = []
    colors: dict[tuple[int, int], int] = {}
    for pixel in pixels:
        key = (pixel["x"], pixel["y"])
        if key not in colors:
            order.append(key)
        colors[key] = pixel["color"]
    return [(x, y, colors[(x, y)]) for x, y in order]


def _parse_design(payload: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    theme = payload.get("theme")
    brief = payload.get("brief")
    palette = payload.get("palette")
    if not isinstance(theme, str) or not theme.strip() or len(theme) > 80:
        raise ApiError(400, "bad_design", "Theme must be 1–80 characters.")
    if not isinstance(brief, str) or not brief.strip() or len(brief) > 400:
        raise ApiError(400, "bad_design", "Brief must be 1–400 characters.")
    if not isinstance(palette, list) or len(palette) != 16:
        raise ApiError(400, "bad_design", "Palette must contain exactly 16 colors.")
    seen: dict[int, dict[str, Any]] = {}
    for swatch in palette:
        if not isinstance(swatch, dict):
            raise ApiError(400, "bad_design", "Each palette entry must be an object.")
        color_id = swatch.get("id")
        hex_value = swatch.get("hex")
        name = swatch.get("name", "")
        if type(color_id) is not int or color_id < 0 or color_id > 15:
            raise ApiError(400, "bad_design", "Palette ids must be integers 0 through 15.")
        if color_id in seen:
            raise ApiError(400, "bad_design", "Palette ids must be unique.")
        if not isinstance(hex_value, str) or len(hex_value) != 7 or not _hex_ok(hex_value):
            raise ApiError(400, "bad_design", "Each color needs a #rrggbb hex.")
        if not isinstance(name, str) or len(name) > 32:
            raise ApiError(400, "bad_design", "Color name must be 32 characters or fewer.")
        seen[color_id] = {"id": color_id, "hex": hex_value.lower(), "name": name}
    if set(seen) != set(range(16)):
        raise ApiError(400, "bad_design", "Palette ids must be exactly 0 through 15.")
    return theme.strip(), brief.strip(), [seen[i] for i in range(16)]


def _hex_ok(value: str) -> bool:
    if not value.startswith("#"):
        return False
    try:
        int(value[1:], 16)
    except ValueError:
        return False
    return True


def _stats(canvas: bytes | None, palette: list[dict]) -> dict[str, Any]:
    if not canvas:
        return {"filled_pct": 0.0, "background_id": 0, "dominant": []}
    counts = [0] * 16
    for color_id in canvas:
        if 0 <= color_id < 16:
            counts[color_id] += 1
    total = GRID * GRID
    filled = total - counts[0]
    by_id = {swatch["id"]: swatch for swatch in palette}
    ranked = sorted(((index, count) for index, count in enumerate(counts) if count), key=lambda item: -item[1])
    dominant = []
    for color_id, count in ranked[:4]:
        swatch = by_id.get(color_id, {})
        dominant.append(
            {
                "id": color_id,
                "name": swatch.get("name", ""),
                "hex": swatch.get("hex", "#000000"),
                "pct": round(100 * count / total, 2),
            }
        )
    return {
        "filled_pct": round(100 * filled / total, 2),
        "background_id": 0,
        "dominant": dominant,
    }
