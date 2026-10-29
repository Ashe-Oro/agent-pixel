"""SQLite state for sequential canvas sessions.

A canvas is a session with its own open time, not a unique New York date.
The realtime 08:00 ET routine opens a new morning session even when a pinned
clock already left a canvas on that date. The host can open extra bonus
sessions on the same date. Existing rows are kept: ``ny_date`` is display
metadata, and day ids never change.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
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
    SLICE_SECONDS,
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
    walk_session,
)

KIND_MORNING = "morning"
KIND_PINNED = "pinned"
KIND_BONUS = "bonus"
KIND_LEGACY = "legacy"

# Shared by fresh databases and the in-place rebuild. ny_date is not unique:
# a morning and a bonus, or a leftover pin and the real morning, can share a date.
_DAYS_COLUMNS = """
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
              archived_at INTEGER
"""

# Latest-stroke cells stay in TurnView only while the list is small.
# A full-canvas paint is the preview PNG, not 65,536 coordinates in the prompt.
TURN_VIEW_INLINE_PIXELS = 512
CHAT_MAX_CHARS = 240
CHAT_COOLDOWN_SECONDS = 15
CHAT_PER_AGENT_DAY = 30
CHAT_PER_DAY = 120
CHAT_LIST_LIMIT = 80
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
        self._force_reload = False
        # Vercel's deployment filesystem is read-only. /tmp survives for one instance.
        # The file is per store object so two instances in one process (tests) cannot
        # share a SQLite handle and clobber each other before the blob CAS.
        if self.remote or os.environ.get("VERCEL"):
            self.path = Path("/tmp") / f"agent-pixel-{id(self)}.sqlite"
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
            self._publish_startup()

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

    def _etag_same(self, left: str | None, right: str | None) -> bool:
        return blob_db.etag_key(left) == blob_db.etag_key(right)

    def _refresh(self) -> None:
        """Load the latest blob when this process is behind it.

        A dirty local file is kept only when the catalog etag is still the one
        we edited. If someone else stored a newer file, their bytes win and
        the local edit is discarded so it cannot be uploaded over them.
        """
        if not self.remote:
            return
        force = self._force_reload
        self._force_reload = False
        try:
            remote = blob_db.stat(self.blob_name)
        except blob_db.BlobMissing:
            return
        except blob_db.BlobStale as exc:
            raise ApiError(503, "store_unavailable", "Could not load the canvas from Blob.") from exc
        except Exception as exc:
            raise ApiError(503, "store_unavailable", "Could not load the canvas from Blob.") from exc
        if not force and self._etag_same(remote, self.etag):
            return
        self._replace_from_blob()

    def _replace_from_blob(self, _attempt: int = 0) -> None:
        try:
            payload, etag = blob_db.download(self.blob_name)
        except blob_db.BlobMissing:
            return
        except blob_db.BlobStale as exc:
            raise ApiError(503, "store_unavailable", "Could not load the canvas from Blob.") from exc
        except Exception as exc:
            raise ApiError(503, "store_unavailable", "Could not load the canvas from Blob.") from exc
        self.conn.close()
        self.path.write_bytes(payload)
        self.etag = etag
        self.dirty = False
        self._connect()
        self._init_schema()
        if self.remote and self.dirty:
            try:
                self._flush()
            except blob_db.BlobConflict:
                if _attempt >= 7:
                    raise ApiError(
                        503,
                        "store_conflict",
                        "Could not publish the canvas migration. Retry.",
                    ) from None
                self._replace_from_blob(_attempt + 1)

    def _flush(self) -> None:
        """Upload only if the blob still has the etag this edit started from."""
        if not self.remote or not self.dirty:
            return
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        payload = self.path.read_bytes()
        try:
            self.etag = blob_db.upload(self.blob_name, payload, if_match=self.etag)
        except blob_db.BlobConflict:
            self.dirty = False
            self._force_reload = True
            raise
        except blob_db.BlobStale as exc:
            self.dirty = False
            self._force_reload = True
            raise ApiError(503, "store_unavailable", "Could not save the canvas to Blob.") from exc
        except Exception as exc:
            self.dirty = False
            self._force_reload = True
            raise ApiError(503, "store_unavailable", "Could not save the canvas to Blob.") from exc
        self.dirty = False

    def _publish_startup(self) -> None:
        """Upload a migrated file, rebasing if a paint landed during boot."""
        for _ in range(8):
            if not self.dirty:
                return
            try:
                self._flush()
                return
            except blob_db.BlobConflict:
                self._replace_from_blob()
        raise ApiError(503, "store_conflict", "Could not publish the canvas migration. Retry.")

    def _run_write(self, mutate):
        """Run ``mutate`` on the latest canvas and publish it with compare-and-swap.

        ``mutate`` may set ``self.dirty``. It must not take ``self.lock`` as its
        own write barrier, and it must not upload. On a conflict the blob is
        unchanged, the latest file is loaded, and ``mutate`` runs again.
        """
        with self.lock:
            if not self.remote:
                self._refresh()
                return mutate()
            for _ in range(8):
                self._refresh()
                self.dirty = False
                try:
                    result = mutate()
                except blob_db.BlobConflict:
                    if self.conn.in_transaction:
                        self.conn.rollback()
                    continue
                except Exception:
                    if self.conn.in_transaction:
                        self.conn.rollback()
                    self.dirty = False
                    raise
                if not self.dirty:
                    return result
                try:
                    self._flush()
                    return result
                except blob_db.BlobConflict:
                    continue
            raise ApiError(
                503,
                "store_conflict",
                "Another save landed while this one was in flight. Retry the request.",
            )

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
""" + _DAYS_COLUMNS + """
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
            CREATE TABLE IF NOT EXISTS chat (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              day_id INTEGER NOT NULL,
              agent_id TEXT NOT NULL,
              body TEXT NOT NULL,
              created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS chat_day_agent ON chat (day_id, agent_id, id);
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
        self._migrate_days()
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(days)")}
        if {"kind", "archived", "ny_date", "id"} <= cols:
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS days_live ON days (kind, archived, ny_date, id)"
            )

    def _migrate_days(self) -> None:
        """Rebuild days when ny_date is still unique, keeping every row.

        SQLite cannot drop a UNIQUE constraint in place. Production canvases
        stay on their ids, with pixels, themes, and strokes untouched. Nothing
        is archived here: a pinned in-progress session keeps the brush.
        """
        exists = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'days'"
        ).fetchone()
        if exists is None:
            return
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(days)")}
        needs = self._ny_date_is_unique() or not {"opens_at", "kind", "archived"} <= cols
        if not needs:
            self._backfill_opens_at()
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DROP TABLE IF EXISTS days_v2")
            self.conn.execute(f"CREATE TABLE days_v2 ({_DAYS_COLUMNS})")
            rows = self.conn.execute("SELECT * FROM days").fetchall()
            for row in rows:
                opens = int(day_bounds(date.fromisoformat(row["ny_date"]))[0].timestamp())
                canvas = row["canvas"]
                if canvas is not None:
                    canvas = bytes(canvas)
                self.conn.execute(
                    """
                    INSERT INTO days_v2 (
                      id, day_index, ny_date, theme, brief, palette_json, canvas,
                      artist_order_json, focus_json, demo_seed, designed_at, created_at,
                      opens_at, kind, archived, archived_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
                    """,
                    (
                        row["id"],
                        row["day_index"],
                        row["ny_date"],
                        row["theme"],
                        row["brief"],
                        row["palette_json"],
                        canvas,
                        row["artist_order_json"],
                        row["focus_json"],
                        row["demo_seed"],
                        row["designed_at"],
                        row["created_at"],
                        opens,
                        KIND_LEGACY,
                    ),
                )
            self.conn.execute("DROP TABLE days")
            self.conn.execute("ALTER TABLE days_v2 RENAME TO days")
            max_id = self.conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM days").fetchone()["n"]
            self.conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('days', 'days_v2')")
            self.conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('days', ?)",
                (max_id,),
            )
            self.conn.commit()
            self.dirty = True
        except Exception:
            self.conn.rollback()
            raise

    def _ny_date_is_unique(self) -> bool:
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'days'"
        ).fetchone()
        if row is None or not row[0]:
            return False
        compact = " ".join(row[0].split())
        if "ny_date TEXT NOT NULL UNIQUE" in compact:
            return True
        for index in self.conn.execute("PRAGMA index_list(days)").fetchall():
            if not index["unique"]:
                continue
            name = index["name"]
            if not name.replace("_", "").isalnum():
                continue
            info = self.conn.execute(f'PRAGMA index_info("{name}")').fetchall()
            if [col["name"] for col in info] == ["ny_date"]:
                return True
        return False

    def _backfill_opens_at(self) -> None:
        rows = self.conn.execute(
            "SELECT id, ny_date FROM days WHERE opens_at IS NULL OR opens_at = 0"
        ).fetchall()
        for row in rows:
            opens = int(day_bounds(date.fromisoformat(row["ny_date"]))[0].timestamp())
            self.conn.execute("UPDATE days SET opens_at = ? WHERE id = ?", (opens, row["id"]))
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
        def mutate() -> None:
            self._write_clock(when)

        self._run_write(mutate)
        return self.clock_info()

    def _write_clock(self, when: datetime | None) -> None:
        if when is None:
            self.conn.execute("UPDATE settings SET clock_utc = NULL WHERE id = 1")
        else:
            utc = when.astimezone(timezone.utc).replace(microsecond=0)
            self.conn.execute(
                "UPDATE settings SET clock_utc = ? WHERE id = 1",
                (utc.isoformat(),),
            )
        self.dirty = True

    def clock_info(self) -> dict[str, Any]:
        with self.lock:
            self._refresh()
            return self._clock_info()

    def _clock_info(self) -> dict[str, Any]:
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
            return self.set_clock(parse_ny_local(str(body["at"])))

        def mutate() -> None:
            current = self.now()
            if mode == "advance_seconds" or "advance_seconds" in body:
                seconds = int(body.get("seconds", body.get("advance_seconds", 0)))
                self._write_clock(current + timedelta(seconds=seconds))
                return
            day = self.today_row()
            if day is not None and mode in ("next_slice", "lock"):
                clock = self._session_clock(day, current)
                if mode == "lock" or clock.phase == "after" or clock.deadline is None:
                    self._write_clock(clock.lock_at)
                    return
                if clock.phase == "before":
                    self._write_clock(self._opens_at(day))
                    return
                self._write_clock(clock.deadline)
                return
            local = current.astimezone(NY)
            start, lock = day_bounds(local.date())
            if mode == "next_slice":
                phase, index = position(current)
                if phase == "before":
                    target = start
                elif phase == "after" or index is None:
                    target = lock
                else:
                    target = start + timedelta(seconds=SLICE_SECONDS * (index + 1))
                self._write_clock(target)
                return
            if mode == "lock":
                self._write_clock(lock)
                return
            if mode == "next_morning":
                nxt = local.date() + timedelta(days=1)
                target, _ = day_bounds(nxt)
                self._write_clock(target)
                return
            raise ApiError(400, "bad_clock", "Pass at, mode, or advance_seconds.")

        self._run_write(mutate)
        return self.clock_info()

    # --- days --------------------------------------------------------------

    def _clock_pinned(self) -> bool:
        return self._settings()["clock_utc"] is not None

    def _opens_at(self, day: sqlite3.Row) -> datetime:
        raw = day["opens_at"] if "opens_at" in day.keys() else None
        if raw:
            return datetime.fromtimestamp(int(raw), timezone.utc)
        year, month, day_n = (int(part) for part in day["ny_date"].split("-"))
        start, _ = day_bounds(datetime(year, month, day_n).date())
        return start

    def _acted_at(self, day_id: int) -> dict[int, datetime]:
        rows = self.conn.execute(
            "SELECT slice_index, created_at FROM actions WHERE day_id = ?",
            (day_id,),
        ).fetchall()
        return {
            int(row["slice_index"]): datetime.fromtimestamp(int(row["created_at"]), timezone.utc)
            for row in rows
        }

    def _session_clock(self, day: sqlite3.Row, now: datetime):
        acted = self._acted_at(day["id"])
        order = json.loads(day["artist_order_json"]) if day["artist_order_json"] else self._order_for(day)
        rows = self.conn.execute(
            "SELECT agent_id, slice_index, done, created_at FROM actions WHERE day_id = ? AND done = 1",
            (day["id"],),
        ).fetchall()
        for row in rows:
            closed = datetime.fromtimestamp(int(row["created_at"]), timezone.utc)
            for index in range(int(row["slice_index"]) + 1, SLICE_COUNT):
                if artist_on_slice(order, index) == row["agent_id"] and index not in acted:
                    acted[index] = closed
        return walk_session(self._opens_at(day), acted, now)

    def _phase_for(self, day: sqlite3.Row | None, now: datetime) -> tuple[str, int | None]:
        if day is None:
            return position(now)
        if int(day["archived"]):
            return "after", None
        clock = self._session_clock(day, now)
        return clock.phase, clock.index

    def _insert_day(self, now: datetime, kind: str, opens_at: datetime) -> sqlite3.Row:
        key = ny_date(now)
        nxt = self.conn.execute("SELECT COALESCE(MAX(day_index), 0) + 1 AS n FROM days").fetchone()["n"]
        stamp = int(opens_at.astimezone(timezone.utc).replace(microsecond=0).timestamp())
        cursor = self.conn.execute(
            """
            INSERT INTO days (day_index, ny_date, created_at, opens_at, kind, archived)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (nxt, key, int(now.timestamp()), stamp, kind),
        )
        self.dirty = True
        return self.conn.execute("SELECT * FROM days WHERE id = ?", (cursor.lastrowid,)).fetchone()

    def _archive_day(self, day_id: int, now: datetime) -> None:
        cur = self.conn.execute(
            """
            UPDATE days SET archived = 1, archived_at = ?
            WHERE id = ? AND archived = 0
            """,
            (int(now.timestamp()), day_id),
        )
        if cur.rowcount:
            self.dirty = True

    def _archive_open(self, now: datetime) -> None:
        cur = self.conn.execute(
            "UPDATE days SET archived = 1, archived_at = ? WHERE archived = 0",
            (int(now.timestamp()),),
        )
        if cur.rowcount:
            self.dirty = True

    def _morning_row(self, key: str, *, include_archived: bool) -> sqlite3.Row | None:
        if include_archived:
            return self.conn.execute(
                """
                SELECT * FROM days WHERE ny_date = ? AND kind = ?
                ORDER BY id DESC LIMIT 1
                """,
                (key, KIND_MORNING),
            ).fetchone()
        return self.conn.execute(
            """
            SELECT * FROM days
            WHERE ny_date = ? AND kind = ? AND archived = 0
            ORDER BY id DESC LIMIT 1
            """,
            (key, KIND_MORNING),
        ).fetchone()

    def _calendar_row(self, key: str) -> sqlite3.Row | None:
        """Newest unarchived canvas for a pinned date, other than a bonus session."""
        return self.conn.execute(
            """
            SELECT * FROM days
            WHERE ny_date = ? AND archived = 0 AND kind != ?
            ORDER BY id DESC LIMIT 1
            """,
            (key, KIND_BONUS),
        ).fetchone()

    def _live_bonus(self, now: datetime) -> sqlite3.Row | None:
        row = self.conn.execute(
            """
            SELECT * FROM days WHERE kind = ? AND archived = 0
            ORDER BY id DESC LIMIT 1
            """,
            (KIND_BONUS,),
        ).fetchone()
        if row is None:
            return None
        phase, _ = self._phase_for(row, now)
        if phase == "after":
            return None
        return row

    def today_row(self) -> sqlite3.Row | None:
        """The canvas seats paint. A live bonus wins over a finished morning."""
        now = self.now()
        bonus = self._live_bonus(now)
        if self._clock_pinned():
            if bonus is not None:
                return bonus
            return self._calendar_row(ny_date(now))
        if bonus is not None:
            return bonus
        return self._morning_row(ny_date(now), include_archived=False)

    def _newest_row(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM days ORDER BY id DESC LIMIT 1").fetchone()

    def _morning_start(self, now: datetime) -> datetime:
        start, _ = day_bounds(now.astimezone(NY).date())
        return start

    def _ensure_morning(self, now: datetime) -> sqlite3.Row | None:
        """Open today's morning only inside 08:00–08:36 ET. Never insert outside it.

        A live bonus that opened at or after today's 08:00 is left alone. A
        leftover bonus from before that 08:00 is archived when the new morning
        is created.
        """
        key = ny_date(now)
        phase, _ = position(now)
        existing = self._morning_row(key, include_archived=True)
        if existing is not None:
            return existing
        if phase != "during":
            return None
        start = self._morning_start(now)
        bonus = self._live_bonus(now)
        if bonus is not None and self._opens_at(bonus) >= start:
            return None
        self._archive_open(now)
        return self._insert_day(now, KIND_MORNING, start)

    def _ensure_today(self, now: datetime) -> sqlite3.Row | None:
        if not self._clock_pinned():
            phase, _ = position(now)
            if phase == "before":
                existing = self._morning_row(ny_date(now), include_archived=False)
                if existing is not None:
                    return existing
                raise ApiError(409, "before_routine", "The morning opens at 08:00 America/New_York.")
            if phase != "during":
                bonus = self._live_bonus(now)
                if bonus is not None:
                    return bonus
                morning = self._morning_row(ny_date(now), include_archived=False)
                if morning is not None:
                    return morning
                newest = self._newest_row()
                if newest is not None:
                    return newest
                raise ApiError(409, "morning_locked", "The morning window has passed.")
            return self._ensure_morning(now)
        bonus = self._live_bonus(now)
        if bonus is not None:
            return bonus
        existing = self._calendar_row(ny_date(now))
        if existing is not None:
            return existing
        opens, _ = day_bounds(now.astimezone(NY).date())
        return self._insert_day(now, KIND_PINNED, opens)

    def open_today(self) -> dict[str, Any]:
        def mutate() -> int | None:
            now = self.now()
            if not self._clock_pinned() and position(now)[0] == "before":
                current = self.today_row()
                return current["id"] if current is not None else None
            try:
                day = self._ensure_today(now)
            except ApiError:
                return None
            if day is None:
                current = self.today_row()
                return current["id"] if current is not None else None
            return day["id"]

        day_id = self._run_write(mutate)
        return self.watch_state(day_id=day_id)

    def open_bonus(self) -> dict[str, Any]:
        """Open a fresh 12-slice session at the current clock, without moving the date."""

        def mutate() -> int:
            now = self.now().astimezone(timezone.utc).replace(microsecond=0)
            current = self.today_row()
            if current is not None:
                self._archive_day(current["id"], now)
            day = self._insert_day(now, KIND_BONUS, now)
            return int(day["id"])

        day_id = self._run_write(mutate)
        return self._tick_payload(self.day_by_id(day_id), self.now())

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
        def mutate() -> None:
            self.conn.execute(
                "UPDATE settings SET roster_json = ?, rotate = ? WHERE id = 1",
                (json.dumps(order), 1 if rotate else 0),
            )
            self.dirty = True

        self._run_write(mutate)
        return {"order": order, "rotate": rotate}

    def set_budgets(self) -> dict[str, Any]:
        """Pixel caps stay off. Stored 0 is the unlimited sentinel, not a budget of zero."""
        def mutate() -> None:
            self.conn.execute(
                "UPDATE settings SET pixels_per_turn = 0, pixels_per_artist_day = 0 WHERE id = 1"
            )
            self.dirty = True

        self._run_write(mutate)
        return {"pixels_per_turn": PIXELS_PER_TURN, "pixels_per_artist_day": PIXELS_PER_ARTIST_DAY}

    # --- design / act ------------------------------------------------------

    def design_view(self, agent_id: str) -> dict[str, Any]:
        if agent_id != "design":
            raise ApiError(403, "wrong_role", "Only the design agent can read the design brief.")
        with self.lock:
            self._refresh()
            now = self.now()
            day = self.today_row()
            if day is None and not self._clock_pinned():
                parked = self._morning_row(ny_date(now), include_archived=True)
                if parked is not None and int(parked["archived"]):
                    day = parked
            phase, _ = self._phase_for(day, now)
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
        saved: dict[str, Any] = {}

        def mutate() -> None:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                now = self.now()
                day = self.today_row()
                if day is None:
                    phase, _ = position(now)
                    if phase == "before":
                        raise ApiError(409, "before_routine", "Design opens at 08:00 America/New_York.")
                    if phase == "after":
                        raise ApiError(409, "morning_locked", "This session is locked.")
                    day = self._ensure_today(now)
                phase, index = self._phase_for(day, now)
                if phase == "before":
                    raise ApiError(409, "before_routine", "Design opens at 08:00 America/New_York.")
                if phase == "after":
                    raise ApiError(409, "morning_locked", "This session is locked.")
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
                saved["day_id"] = day["id"]
                saved["day_index"] = day["day_index"]
                saved["order"] = order
                saved["index"] = index
            except Exception:
                self.conn.rollback()
                raise

        self._run_write(mutate)
        order = saved["order"]
        index = saved["index"]
        return {
            "ok": True,
            "mural_id": f"day-{saved['day_index']}",
            "day_id": saved["day_id"],
            "day_index": saved["day_index"],
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
        saved: dict[str, Any] = {}

        def mutate() -> None:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                now = self.now()
                day = self.today_row()
                if day is None:
                    raise ApiError(409, "no_day", "Today's canvas is not open yet.")
                if not day["theme"]:
                    raise ApiError(409, "awaiting_design", "The design agent has not opened the canvas.")
                self._require_slice(day, agent_id, now)
                phase, index = self._phase_for(day, now)
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
                    raise ApiError(409, "slice_used", "This 3-minute slice already has an action.") from exc
                self.conn.commit()
                self.dirty = True
                saved["day_id"] = day["id"]
                saved["index"] = index
                saved["charged"] = charged
                saved["done"] = done
            except Exception:
                self.conn.rollback()
                raise

        self._run_write(mutate)
        fresh = self.day_by_id(saved["day_id"])
        return {
            "ok": True,
            "action": action,
            "charged": saved["charged"],
            "done": saved["done"],
            "slice_index": saved["index"],
            "round_index": round_on_slice(saved["index"]),
            "pixels_remaining_day": PIXELS_PER_ARTIST_DAY,
            "pixels_remaining_turn": 0,
            "turns_remaining": self._turns_remaining(fresh, agent_id, self.now()),
            "to_act": False,
        }

    def _require_slice(self, day: sqlite3.Row, agent_id: str, now: datetime) -> None:
        if int(day["archived"]):
            raise ApiError(409, "morning_locked", "That canvas is archived.")
        clock = self._session_clock(day, now)
        phase, index = clock.phase, clock.index
        if phase == "before":
            if day["kind"] == KIND_BONUS:
                raise ApiError(409, "before_routine", "This session has not opened yet.")
            raise ApiError(409, "before_routine", "Artists paint from 08:00 ET.")
        if phase == "after" or index is None:
            raise ApiError(409, "morning_locked", "This session is locked.")
        order = json.loads(day["artist_order_json"])
        if self._is_skipped(day["id"], agent_id):
            raise ApiError(409, "rounds_skipped", "You already ended your rounds for today.")
        for mark in clock.slices:
            if mark.missed and artist_on_slice(order, mark.index) == agent_id:
                raise ApiError(
                    409,
                    "slice_missed",
                    "That 3-minute slice closed with no paint or pass. The turn has moved on.",
                )
        expected = artist_on_slice(order, index)
        if agent_id != expected:
            previous = index - 1
            if previous >= 0 and artist_on_slice(order, previous) == agent_id and self._action_for_slice(day["id"], previous):
                raise ApiError(409, "slice_used", "This 3-minute slice already has an action.")
            raise ApiError(
                409,
                "not_your_turn",
                f"This slice belongs to {DISPLAY[expected]}.",
            )
        if self._action_for_slice(day["id"], index) is not None:
            raise ApiError(409, "slice_used", "This 3-minute slice already has an action.")

    def turn_view(self, agent_id: str) -> dict[str, Any]:
        if agent_id not in ARTISTS:
            raise ApiError(403, "wrong_role", "TurnView is for an artist seat.")
        with self.lock:
            self._refresh()
            now = self.now()
            day = self.today_row()
            return self._turn_from(day, agent_id, now)

    def _slice_bounds(
        self,
        day: sqlite3.Row | None,
        now: datetime,
        phase: str,
        index: int | None,
    ) -> tuple[datetime, datetime]:
        if day is not None:
            clock = self._session_clock(day, now)
            if phase == "during" and index is not None and clock.deadline is not None:
                mark = clock.slices[index]
                return mark.opens, clock.deadline
            if phase == "before":
                return clock.slices[0].opens, clock.slices[0].deadline
            return clock.lock_at, clock.lock_at
        local = now.astimezone(NY).date()
        if phase == "during" and index is not None:
            return slice_window(local, index)
        if phase == "before":
            return slice_window(local, 0)
        _, lock = day_bounds(local)
        return lock, lock

    def _turn_from(self, day: sqlite3.Row | None, agent_id: str, now: datetime) -> dict[str, Any]:
        phase, index = self._phase_for(day, now)
        designed = bool(day and day["theme"])
        order = self._order_for(day)
        to_act = False
        if designed and phase == "during" and index is not None:
            to_act = (
                artist_on_slice(order, index) == agent_id
                and not self._is_skipped(day["id"], agent_id)
                and self._action_for_slice(day["id"], index) is None
            )
        turn_left = None if to_act else 0
        if phase == "during" and index is not None:
            round_index = round_on_slice(index)
        elif phase == "before":
            round_index = 1
        else:
            round_index = 4
        slice_start, slice_end = self._slice_bounds(day, now, phase, index)
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
        saved: dict[str, Any] = {}

        def mutate() -> None:
            now = self.now()
            pinned = self._clock_pinned()
            phase, _ = position(now)
            bonus = self._live_bonus(now)
            if not pinned and phase == "during":
                start = self._morning_start(now)
                if bonus is not None and self._opens_at(bonus) >= start:
                    saved["day_id"] = bonus["id"]
                    return
                day = self._ensure_morning(now)
                if day is None:
                    saved["closed"] = True
                    return
                if int(day["archived"]):
                    bonus = self._live_bonus(now)
                    if bonus is not None:
                        saved["day_id"] = bonus["id"]
                        return
                saved["day_id"] = day["id"]
                return
            if bonus is not None:
                saved["day_id"] = bonus["id"]
                return
            if phase == "before":
                saved["before"] = True
                return
            if not pinned:
                newest = self._newest_row()
                if newest is not None:
                    saved["day_id"] = newest["id"]
                    return
                saved["closed"] = True
                return
            day = self._ensure_today(now)
            if day is None:
                saved["closed"] = True
                return
            saved["day_id"] = day["id"]

        self._run_write(mutate)
        now = self.now()
        if saved.get("before"):
            return {
                "ok": True,
                "phase": "before_open",
                "ny_now": now.astimezone(NY).isoformat(),
                "ny_label": label_clock(now),
                "hint": "Wait until 08:00 America/New_York, then tick again and post a design.",
                "day": None,
            }
        if saved.get("closed"):
            return {
                "ok": True,
                "phase": "locked",
                "ny_now": now.astimezone(NY).isoformat(),
                "ny_label": label_clock(now),
                "slice_index": None,
                "round_index": None,
                "current_agent_id": None,
                "hint": "The morning window has passed. POST /host/canvas/new to open a bonus, or wait until 08:00 ET.",
                "day": None,
            }
        return self._tick_payload(self.day_by_id(saved["day_id"]), now)

    def _tick_payload(self, day: sqlite3.Row, now: datetime) -> dict[str, Any]:
        phase, index = self._phase_for(day, now)
        designed = bool(day["theme"])
        if phase == "after":
            public_phase = "locked"
            current = None
            if day["kind"] == KIND_BONUS:
                hint = (
                    "This bonus session is locked. POST /host/canvas/new to open another, "
                    "or wait for the 08:00 ET morning."
                )
            else:
                hint = "Canvas is locked until tomorrow's 08:00 ET routine."
        elif not designed:
            public_phase = "awaiting_design"
            current = None
            if day["kind"] == KIND_BONUS:
                hint = (
                    'POST /agent/design as the design agent. '
                    'Step slices with POST /host/clock {"mode":"next_slice"}.'
                )
            else:
                hint = "POST /agent/design as the design agent, then artists paint on their slices."
        else:
            public_phase = "slice"
            order = json.loads(day["artist_order_json"])
            current = artist_on_slice(order, index)
            clock = self._session_clock(day, now)
            slice_end = clock.deadline or clock.lock_at
            hint = (
                f"GET /agent/turn as {current}, then POST /agent/act with one paint or pass "
                f"before {label_hhmm(slice_end)} ET. This slice is 3 minutes, and the next "
                f"artist opens as soon as you act."
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
                "kind": day["kind"],
                "archived": bool(day["archived"]),
            },
        }

    # --- reads -------------------------------------------------------------

    def watch_state(self, day_id: int | None = None) -> dict[str, Any]:
        with self.lock:
            self._refresh()
            return self._watch_state(day_id)

    def gallery(self) -> dict[str, Any]:
        with self.lock:
            self._refresh()
            now = self.now()
            return {"canvases": [self._gallery_item(row, now) for row in self._designed_rows()]}

    def delete_day(self, day_id: int, force: bool = False) -> dict[str, Any]:
        """Remove one canvas and its strokes. The live session needs force=true."""

        def mutate() -> dict[str, Any]:
            day = self.conn.execute("SELECT * FROM days WHERE id = ?", (day_id,)).fetchone()
            if day is None:
                raise ApiError(404, "no_day", "No mural with that id.")
            if self._is_live_day(day, self.now()) and not force:
                raise ApiError(
                    409,
                    "day_live",
                    "That canvas is live. Pass force=true to delete it.",
                )
            self.conn.execute("DELETE FROM actions WHERE day_id = ?", (day_id,))
            self.conn.execute("DELETE FROM chat WHERE day_id = ?", (day_id,))
            self.conn.execute("DELETE FROM artist_state WHERE day_id = ?", (day_id,))
            self.conn.execute("DELETE FROM days WHERE id = ?", (day_id,))
            self.dirty = True
            return {"ok": True, "day_id": day_id}

        return self._run_write(mutate)

    def _is_designed(self, row: sqlite3.Row | None) -> bool:
        if row is None:
            return False
        theme = row["theme"]
        return isinstance(theme, str) and bool(theme.strip()) and bool(row["palette_json"])

    def _is_live_day(self, row: sqlite3.Row, now: datetime) -> bool:
        live = self.today_row()
        if live is None or live["id"] != row["id"] or int(row["archived"]):
            return False
        phase, _ = self._phase_for(row, now)
        return phase != "after"

    def _designed_rows(self) -> list[sqlite3.Row]:
        """Newest canvas first. id is creation order, not the New York date label."""
        rows = self.conn.execute("SELECT * FROM days ORDER BY id DESC").fetchall()
        return [row for row in rows if self._is_designed(row)]

    def _default_feature(self, now: datetime) -> sqlite3.Row | None:
        """Live session if one is open, otherwise the newest designed canvas."""
        live = self.today_row()
        if self._is_live_day(live, now) if live is not None else False:
            return live
        designed = self._designed_rows()
        return designed[0] if designed else None

    def _watch_state(self, day_id: int | None) -> dict[str, Any]:
        now = self.now()
        today = self.today_row()
        default_feature = self._default_feature(now)
        if day_id is None:
            featured = default_feature
        else:
            featured = self.day_by_id(day_id)
            if not self._is_designed(featured) and not self._is_live_day(featured, now):
                raise ApiError(404, "no_canvas", "That canvas was never designed.")
        clock = self._clock_info()
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
                "chat": [],
                "archive": archive,
                "preview_url": None,
                "viewing_archive": False,
            }
        order = self._order_for(featured)
        palette = json.loads(featured["palette_json"]) if featured["palette_json"] else []
        is_live = today is not None and featured["id"] == today["id"] and not int(featured["archived"])
        if not is_live:
            phase, index = "after", None
            live_index = None
            miss_before = SLICE_COUNT
        else:
            phase, index = self._phase_for(featured, now)
            if phase == "after":
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
        slices = self._slice_board(featured, order, live_index, miss_before, now)
        current = None
        if is_live and phase == "during" and featured["theme"] and index is not None:
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
                    "turns_remaining": self._turns_remaining(featured, agent, now) if is_live else 0,
                    "skipped": self._is_skipped(featured["id"], agent),
                    "to_act": bool(
                        is_live
                        and current == agent
                        and not self._is_skipped(featured["id"], agent)
                        and live_index is not None
                        and self._action_for_slice(featured["id"], live_index) is None
                    ),
                }
            )
        active = next((item for item in artists if item["to_act"]), None)
        status = self._status(featured, now)
        slice_end = None
        if live_index is not None:
            clock = self._session_clock(featured, now)
            if clock.deadline is not None:
                slice_end = int(clock.deadline.timestamp())
        return {
            "status": status,
            "message": _status_message(status),
            "mural_id": f"day-{featured['day_index']}",
            "day_id": featured["id"],
            "day_index": featured["day_index"],
            "ny_date": featured["ny_date"],
            "theme": featured["theme"],
            "brief": featured["brief"],
            "palette": palette,
            "grid_w": GRID,
            "grid_h": GRID,
            "kind": featured["kind"],
            "archived": bool(featured["archived"]),
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
            "slice_ends_at_unix": slice_end,
            "turns_remaining": active["turns_remaining"] if active else None,
            "pixels_remaining_turn": None if active else 0,
            "artists": artists,
            "slices": slices,
            "chat": self._chat_rows(featured["id"]),
            "recent_strokes": self._recent_strokes(featured["id"], include_latest_pixels=False),
            "canvas_stats": _stats(
                bytes(featured["canvas"]) if featured["canvas"] is not None else None,
                palette,
            ),
            "clock": clock,
            "schedule": _schedule_meta(),
            "archive": archive,
            "viewing_archive": bool(day_id is not None and default_feature is not None and featured["id"] != default_feature["id"]),
        }

    def list_chat(self, day_id: int | None = None) -> dict[str, Any]:
        with self.lock:
            self._refresh()
            return self._list_chat(day_id)

    def _list_chat(self, day_id: int | None) -> dict[str, Any]:
        now = self.now()
        if day_id is None:
            day = self.today_row()
        else:
            day = self.day_by_id(day_id)
        if day is None:
            return {"day_id": None, "ny_date": ny_date(now), "messages": []}
        return {
            "day_id": day["id"],
            "ny_date": day["ny_date"],
            "messages": self._chat_rows(day["id"]),
        }

    def post_chat(self, agent_id: str, text: Any) -> dict[str, Any]:
        if agent_id not in KNOWN_AGENTS:
            raise ApiError(403, "wrong_role", "Only a seat can post chatter.")
        body = _parse_chat(text)
        saved: dict[str, Any] = {}

        def mutate() -> None:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                now = self.now()
                day = self.today_row()
                if day is None:
                    raise ApiError(409, "no_day", "Today's canvas is not open yet.")
                stamp = int(now.timestamp())
                last = self.conn.execute(
                    """
                    SELECT created_at FROM chat
                    WHERE day_id = ? AND agent_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (day["id"], agent_id),
                ).fetchone()
                if last and stamp - int(last["created_at"]) < CHAT_COOLDOWN_SECONDS:
                    raise ApiError(429, "chat_slow", "Wait a few seconds before posting again.")
                mine = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM chat WHERE day_id = ? AND agent_id = ?",
                    (day["id"], agent_id),
                ).fetchone()["n"]
                if mine >= CHAT_PER_AGENT_DAY:
                    raise ApiError(429, "chat_limit", "That seat has used today's chatter posts.")
                total = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM chat WHERE day_id = ?",
                    (day["id"],),
                ).fetchone()["n"]
                if total >= CHAT_PER_DAY:
                    raise ApiError(429, "chat_limit", "Today's chatter is full.")
                cursor = self.conn.execute(
                    "INSERT INTO chat (day_id, agent_id, body, created_at) VALUES (?, ?, ?, ?)",
                    (day["id"], agent_id, body, stamp),
                )
                self.conn.commit()
                self.dirty = True
                saved["message_id"] = int(cursor.lastrowid)
                saved["day_id"] = day["id"]
                saved["stamp"] = stamp
            except Exception:
                self.conn.rollback()
                raise

        self._run_write(mutate)
        return {
            "ok": True,
            "day_id": saved["day_id"],
            "message": {
                "id": saved["message_id"],
                "agent_id": agent_id,
                "display_name": DISPLAY[agent_id],
                "text": body,
                "created_at_unix": saved["stamp"],
                "time_label": _chat_label(saved["stamp"]),
            },
        }

    def ledger(self, day_id: int) -> dict[str, Any]:
        with self.lock:
            self._refresh()
            return self._ledger(day_id)

    def _ledger(self, day_id: int) -> dict[str, Any]:
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
        with self.lock:
            self._refresh()
            return self._region(day_id, x, y, w, h)

    def _region(self, day_id: int, x: int, y: int, w: int, h: int) -> dict[str, Any]:
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
        return self.canvas_for_slices(day_id, None)

    def canvas_for_slices(self, day_id: int, slices: list[int] | None) -> tuple[bytes, list[dict]]:
        """Full stored canvas, or a blank grid replayed from the chosen slices.

        ``None``, an empty list, and the complete set of slice indices all
        return the stored painting, including pixels that were on the canvas
        before the first stroke. A proper subset replays only those slices'
        ledger strokes onto unpainted background id 0. Strokes apply in ledger
        order, so a later selected slice wins on a shared cell.
        """
        with self.lock:
            self._refresh()
            return self._canvas_for_slices(day_id, slices)

    def _canvas_for_slices(self, day_id: int, slices: list[int] | None) -> tuple[bytes, list[dict]]:
        day = self.day_by_id(day_id)
        if day["canvas"] is None or not day["palette_json"]:
            raise ApiError(404, "no_canvas", "That day has no pixels yet.")
        palette = json.loads(day["palette_json"])
        stored = bytes(day["canvas"])
        if not slices:
            return stored, palette
        for index in slices:
            if type(index) is not int or index < 0 or index >= SLICE_COUNT:
                raise ApiError(400, "bad_slices", "Slice indices must be integers from 0 through 11.")
        wanted = set(slices)
        if wanted >= set(range(SLICE_COUNT)):
            return stored, palette
        canvas = blank_canvas()
        rows = self.conn.execute(
            "SELECT slice_index, pixels_json FROM actions WHERE day_id = ? ORDER BY id ASC",
            (day_id,),
        ).fetchall()
        for row in rows:
            if row["slice_index"] not in wanted or not row["pixels_json"]:
                continue
            for pixel in json.loads(row["pixels_json"]):
                if not isinstance(pixel, dict):
                    continue
                x, y, color = pixel.get("x"), pixel.get("y"), pixel.get("color")
                if type(x) is not int or type(y) is not int or type(color) is not int:
                    continue
                if 0 <= x < GRID and 0 <= y < GRID and 0 <= color <= 255:
                    canvas[y * GRID + x] = color
        return bytes(canvas), palette

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
        live = self.today_row()
        is_live = live is not None and live["id"] == day["id"] and not int(day["archived"])
        if not is_live:
            if not day["theme"]:
                return "missed"
            phase, _ = self._phase_for(day, now)
            if phase == "before" and not int(day["archived"]):
                return "scheduled"
            return "locked"
        phase, _ = self._phase_for(day, now)
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
        if day is None:
            phase, index = position(now)
        else:
            if int(day["archived"]) or self._is_skipped(day["id"], agent_id):
                return 0
            phase, index = self._phase_for(day, now)
        if phase == "after":
            return 0
        floor = 0 if phase == "before" or index is None else index
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

    def _chat_rows(self, day_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM (
              SELECT * FROM chat WHERE day_id = ? ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (day_id, CHAT_LIST_LIMIT),
        ).fetchall()
        return [_chat_message(row) for row in rows]

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
        now: datetime,
    ) -> list[dict[str, Any]]:
        clock = self._session_clock(day, now)
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
        for mark in clock.slices:
            index = mark.index
            begin, end = mark.opens, mark.closes
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

    def _gallery_item(self, row: sqlite3.Row, now: datetime) -> dict[str, Any]:
        return {
            "day_id": row["id"],
            "day_index": row["day_index"],
            "ny_date": row["ny_date"],
            "theme": row["theme"],
            "kind": row["kind"],
            "status": self._status(row, now),
            "archived": bool(row["archived"]),
            "preview_url": (
                f"/watch/canvas.png?day_id={row['id']}&scale=2" if row["canvas"] is not None else None
            ),
            "open_url": f"/canvas/{row['id']}",
        }

    def _archive(self, exclude_id: int | None) -> list[dict[str, Any]]:
        now = self.now()
        items = []
        for row in self._designed_rows():
            if exclude_id is not None and row["id"] == exclude_id:
                continue
            items.append(self._gallery_item(row, now))
        return items

    def seed_demo(self) -> None:
        """Pin 08:05 ET and bake a cabin so /watch is paintable right away."""

        def mutate() -> None:
            existing = self.conn.execute("SELECT COUNT(*) AS n FROM days").fetchone()["n"]
            if existing:
                return
            if self.settings.pin_morning:
                real_today = datetime.now(timezone.utc).astimezone(NY).date()
                pinned, _ = day_bounds(real_today)
                pinned = pinned + timedelta(minutes=1)
                utc = pinned.astimezone(timezone.utc).replace(microsecond=0)
                self.conn.execute(
                    "UPDATE settings SET clock_utc = ? WHERE id = 1",
                    (utc.isoformat(),),
                )
                self.dirty = True
            now = self.now()
            phase, _ = position(now)
            if phase != "during":
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                day = self._ensure_today(now)
                if day["theme"]:
                    self.conn.commit()
                    self.dirty = True
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

        self._run_write(mutate)


def _chat_label(ts: int) -> str:
    local = datetime.fromtimestamp(ts, timezone.utc).astimezone(NY)
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{hour}:{local.minute:02d} {suffix}"


def _chat_message(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "display_name": DISPLAY.get(row["agent_id"], row["agent_id"]),
        "text": row["body"],
        "created_at_unix": row["created_at"],
        "time_label": _chat_label(int(row["created_at"])),
    }


def _parse_chat(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ApiError(400, "bad_chat", "text must be a string.")
    text = " ".join(raw.split())
    if not text:
        raise ApiError(400, "bad_chat", "text must not be empty.")
    if len(text) > CHAT_MAX_CHARS:
        raise ApiError(400, "bad_chat", f"text must be {CHAT_MAX_CHARS} characters or fewer.")
    return text


def _lock_time(ny_date_text: str) -> datetime:
    year, month, day = (int(part) for part in ny_date_text.split("-"))
    _, lock = day_bounds(datetime(year, month, day).date())
    return lock


def _schedule_meta() -> dict[str, Any]:
    return {
        "tz": "America/New_York",
        "opens": "08:00",
        "locks": "08:36",
        "slice_seconds": SLICE_SECONDS,
        "slice_minutes": SLICE_SECONDS // 60,
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
        "painting": "The morning is open. One artist holds the brush for this 3-minute slice.",
        "locked": "Locked. A morning lasts at most 36 minutes after 08:00 ET, and the next one starts blank.",
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
