"""SQLite state for sequential canvas sessions.

A canvas is a session with its own open time, not a unique New York date.
The realtime 08:00 ET routine opens a new morning session even when a pinned
clock already left a canvas on that date. The host can open extra bonus
sessions on the same date. Existing rows are kept: ``ny_date`` is display
metadata, and day ids never change.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app import blob_db
from app.config import Settings
from app.wakes import (
    WAKE_DDL,
    SeatHook,
    WakeIntent,
    WebhookConfigError,
    claim_intents,
    finish_wake,
    hooks_from_env,
    list_wake_rows,
    needs_attempt,
    parse_seat_hook,
    plan_wakes,
    post_webhook,
    public_base_url,
    normalize_seat,
)
from app.schedule import (
    ARTISTS,
    DISPLAY,
    GRID,
    LEGACY_SLICE_COUNT,
    PIXELS_PER_ARTIST_DAY,
    PIXELS_PER_TURN,
    SLICE_SECONDS,
    NY,
    SessionClock,
    artist_on_slice,
    configured_slice_count,
    day_bounds,
    design_wait_seconds,
    label_clock,
    label_hhmm,
    lock_label,
    RedoWindow,
    note_gate_seconds,
    redo_overfill,
    redo_seconds,
    resolve_slice_count,
    ny_date,
    parse_ny_local,
    position,
    rotate,
    round_on_slice,
    rounds_in,
    session_length_phrase,
    session_minutes,
    walk_session,
)

KIND_MORNING = "morning"
KIND_BONUS = "bonus"
KIND_LEGACY = "legacy"

# Shared by fresh databases and the in-place rebuild. ny_date is not unique:
# a morning and a bonus, or a leftover pin and the real morning, can share a date.
_DAYS_COLUMNS = f"""
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
              slice_count INTEGER NOT NULL DEFAULT {LEGACY_SLICE_COUNT},
              canvas_rev INTEGER NOT NULL DEFAULT 0
"""

# Latest-stroke cells stay in TurnView only while the list is small.
# A full-canvas paint is the preview PNG, not 65,536 coordinates in the prompt.
TURN_VIEW_INLINE_PIXELS = 512
BRIEF_MAX = 4000
DIRECTOR_NOTE_MAX = 2000
VETO_REASON_MAX = 2000
DIRECTOR_ID = "ross"
FALLBACK_THEME = "Open canvas"
FALLBACK_BRIEF = (
    "Paint freely. The art director did not publish a brief before the design wait ended."
)
FALLBACK_PALETTE = [
    {"id": 0, "hex": "#161616", "name": "ground"},
    {"id": 1, "hex": "#f4f4f4", "name": "paper"},
    {"id": 2, "hex": "#e23d3d", "name": "red"},
    {"id": 3, "hex": "#e8832f", "name": "orange"},
    {"id": 4, "hex": "#f0d040", "name": "yellow"},
    {"id": 5, "hex": "#3aaa4a", "name": "green"},
    {"id": 6, "hex": "#2f8f8a", "name": "teal"},
    {"id": 7, "hex": "#3a6fdb", "name": "blue"},
    {"id": 8, "hex": "#7a4fd4", "name": "violet"},
    {"id": 9, "hex": "#d23d8a", "name": "pink"},
    {"id": 10, "hex": "#8a5a3a", "name": "brown"},
    {"id": 11, "hex": "#c4a06a", "name": "tan"},
    {"id": 12, "hex": "#6e6e6e", "name": "gray"},
    {"id": 13, "hex": "#b8d4f0", "name": "sky"},
    {"id": 14, "hex": "#1c3a28", "name": "pine"},
    {"id": 15, "hex": "#101820", "name": "ink"},
]
CHAT_MAX_CHARS = 240
CHAT_COOLDOWN_SECONDS = 15
CHAT_PER_AGENT_DAY = 30
CHAT_PER_DAY = 120
CHAT_LIST_LIMIT = 80
from app.seed import COZY_BRIEF, COZY_PALETTE, COZY_THEME, blank_canvas, draw_cabin

KNOWN_AGENTS = ("design",) + ARTISTS
SEAT_IDS = KNOWN_AGENTS + (DIRECTOR_ID,)
logger = logging.getLogger(__name__)


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
              pixels_per_artist_day INTEGER NOT NULL DEFAULT 0,
              morning_opened TEXT,
              morning_due TEXT
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
              state TEXT NOT NULL DEFAULT 'active',
              veto_reason TEXT,
              vetoed_at INTEGER,
              redo_seconds INTEGER,
              revert_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS chat (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              day_id INTEGER NOT NULL,
              agent_id TEXT NOT NULL,
              body TEXT NOT NULL,
              created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS chat_day_agent ON chat (day_id, agent_id, id);
            CREATE TABLE IF NOT EXISTS director_notes (
              day_id INTEGER NOT NULL,
              slice_index INTEGER NOT NULL,
              body TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              PRIMARY KEY (day_id, slice_index)
            );
            """
        )
        self.conn.executescript(WAKE_DDL)
        self._ensure_webhook_columns()
        row = self.conn.execute("SELECT id FROM settings WHERE id = 1").fetchone()
        if row is None:
            self.conn.execute(
                """
                INSERT INTO settings (id, roster_json, rotate, pixels_per_turn, pixels_per_artist_day)
                VALUES (1, ?, 1, 0, 0)
                """,
                (json.dumps(list(ARTISTS)),),
            )
        self._ensure_settings_columns()
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
        self._ensure_slice_count()
        self._ensure_canvas_rev()
        self._ensure_action_history()
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(days)")}
        if {"kind", "archived", "ny_date", "id"} <= cols:
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS days_live ON days (kind, archived, ny_date, id)"
            )

    def _ensure_webhook_columns(self) -> None:
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(seat_webhooks)")}
        if "bearer" not in cols:
            self.conn.execute(
                "ALTER TABLE seat_webhooks ADD COLUMN bearer TEXT NOT NULL DEFAULT ''"
            )
            self.dirty = True

    def _ensure_settings_columns(self) -> None:
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(settings)")}
        for name in ("morning_opened", "morning_due"):
            if name in cols:
                continue
            self.conn.execute(f"ALTER TABLE settings ADD COLUMN {name} TEXT")
            self.dirty = True

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
                      opens_at, kind, archived, archived_at, slice_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)
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
                        LEGACY_SLICE_COUNT,
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

    def _ensure_slice_count(self) -> None:
        """Keep each canvas on the slice count it opened with.

        Rows from before ``TILE_ROUNDS`` have no column. They ran twelve
        slices, including a morning that is still in progress at deploy.
        New canvases set the column in ``_insert_day``.
        """
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(days)")}
        if "slice_count" not in cols:
            self.conn.execute(
                f"ALTER TABLE days ADD COLUMN slice_count INTEGER NOT NULL DEFAULT {LEGACY_SLICE_COUNT}"
            )
            self.dirty = True
            return
        cleared = self.conn.execute(
            "UPDATE days SET slice_count = ? WHERE slice_count IS NULL OR slice_count < 1",
            (LEGACY_SLICE_COUNT,),
        )
        if cleared.rowcount:
            self.dirty = True

    def _slice_total(self, day: sqlite3.Row | None) -> int:
        """Slice count for this canvas, or the current setting when there is no row."""
        if day is None:
            return configured_slice_count()
        if "slice_count" not in day.keys():
            return LEGACY_SLICE_COUNT
        stored = int(day["slice_count"] or 0)
        if stored < 1:
            return LEGACY_SLICE_COUNT
        return stored

    def _ensure_canvas_rev(self) -> None:
        """Cache-bust renders after a veto without touching older URLs.

        ``0`` keeps the preview path exactly as it was. A veto, and a paint
        during the redo that follows, increments it and appends ``&v=``.
        """
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(days)")}
        if "canvas_rev" in cols:
            return
        self.conn.execute(
            "ALTER TABLE days ADD COLUMN canvas_rev INTEGER NOT NULL DEFAULT 0"
        )
        self.dirty = True

    def _ensure_action_history(self) -> None:
        """Keep every stroke, and allow one later redo after a veto.

        Older databases have ``UNIQUE (day_id, slice_index)``, which cannot
        hold both the vetoed attempt and the redo. The rebuild copies every
        row, marks it active, and replaces that constraint with a partial
        unique index so only one live attempt exists per slice.
        """
        if self._actions_need_rebuild():
            self._rebuild_actions()
        else:
            cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(actions)")}
            for name, ddl in (
                ("veto_reason", "ALTER TABLE actions ADD COLUMN veto_reason TEXT"),
                ("vetoed_at", "ALTER TABLE actions ADD COLUMN vetoed_at INTEGER"),
                ("redo_seconds", "ALTER TABLE actions ADD COLUMN redo_seconds INTEGER"),
                ("revert_reason", "ALTER TABLE actions ADD COLUMN revert_reason TEXT"),
            ):
                if name in cols:
                    continue
                self.conn.execute(ddl)
                self.dirty = True
        self.conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS actions_one_active
            ON actions (day_id, slice_index) WHERE state = 'active'
            """
        )

    def _actions_need_rebuild(self) -> bool:
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(actions)")}
        if "state" not in cols:
            return True
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'actions'"
        ).fetchone()
        compact = " ".join((row[0] or "").lower().split()) if row else ""
        if "unique (day_id, slice_index)" in compact or "unique(day_id, slice_index)" in compact:
            return True
        for index in self.conn.execute("PRAGMA index_list(actions)").fetchall():
            if not index["unique"] or index["name"] == "actions_one_active":
                continue
            name = index["name"]
            if not name.replace("_", "").isalnum():
                continue
            info = self.conn.execute(f'PRAGMA index_info("{name}")').fetchall()
            if [col["name"] for col in info] != ["day_id", "slice_index"]:
                continue
            definition = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = ?",
                (name,),
            ).fetchone()
            text = " ".join((definition[0] or "").lower().split()) if definition else ""
            if "where" not in text:
                return True
        return False

    def _rebuild_actions(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DROP TABLE IF EXISTS actions_v2")
            self.conn.execute(
                """
                CREATE TABLE actions_v2 (
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
                  state TEXT NOT NULL DEFAULT 'active',
                  veto_reason TEXT,
                  vetoed_at INTEGER,
                  redo_seconds INTEGER,
                  revert_reason TEXT
                )
                """
            )
            self.conn.execute(
                """
                INSERT INTO actions_v2 (
                  id, day_id, agent_id, slice_index, round_index, action, done,
                  pixel_count, note, focus_json, pixels_json, created_at, state
                )
                SELECT
                  id, day_id, agent_id, slice_index, round_index, action, done,
                  pixel_count, note, focus_json, pixels_json, created_at, 'active'
                FROM actions
                """
            )
            self.conn.execute("DROP TABLE actions")
            self.conn.execute("ALTER TABLE actions_v2 RENAME TO actions")
            max_id = self.conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM actions").fetchone()["n"]
            self.conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('actions', 'actions_v2')")
            self.conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('actions', ?)",
                (max_id,),
            )
            self.conn.commit()
            self.dirty = True
        except Exception:
            self.conn.rollback()
            raise

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
                if mode == "lock" or clock.phase == "after":
                    self._write_clock(clock.lock_at)
                    return
                if clock.phase == "holding" and clock.hold_until is not None:
                    self._write_clock(clock.hold_until)
                    return
                if clock.phase == "awaiting" and clock.hold_until is not None:
                    self._write_clock(clock.hold_until)
                    return
                if clock.deadline is None:
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
        acted, _redos = self._slice_timing(day_id)
        return acted

    def _slice_timing(self, day_id: int) -> tuple[dict[int, datetime], dict[int, RedoWindow]]:
        """Original close times, plus a redo window for every vetoed slice.

        The first stored attempt is when the slice closed. A veto does not
        erase that close. The redo's paint or pass, when it exists, is a
        later active row.
        """
        rows = self.conn.execute(
            "SELECT * FROM actions WHERE day_id = ? ORDER BY id ASC",
            (day_id,),
        ).fetchall()
        acted: dict[int, datetime] = {}
        redos: dict[int, RedoWindow] = {}
        for row in rows:
            index = int(row["slice_index"])
            created = datetime.fromtimestamp(int(row["created_at"]), timezone.utc)
            if index not in acted:
                acted[index] = created
            if _is_vetoed(row):
                raw_veto = row["vetoed_at"] if "vetoed_at" in row.keys() else None
                when = datetime.fromtimestamp(int(raw_veto), timezone.utc) if raw_veto else created
                stored = row["redo_seconds"] if "redo_seconds" in row.keys() else None
                seconds = int(stored) if stored is not None else redo_seconds()
                redos[index] = RedoWindow(when, None, seconds)
            elif index in redos:
                window = redos[index]
                redos[index] = RedoWindow(window.opens, created, window.seconds)
        return acted, redos

    def _design_deadline(self, day: sqlite3.Row) -> datetime:
        created = datetime.fromtimestamp(int(day["created_at"]), timezone.utc)
        return created + timedelta(seconds=design_wait_seconds())

    def _awaiting_clock(self, deadline: datetime, slice_count: int):
        """Slices have not started. The board shows the plan from the design deadline."""
        projected = walk_session(
            deadline,
            {},
            deadline - timedelta(seconds=1),
            slice_count=slice_count,
        )
        return SessionClock(
            phase="awaiting",
            index=None,
            deadline=deadline,
            lock_at=projected.lock_at,
            slices=projected.slices,
            hold_until=deadline,
            holding_index=0,
        )

    def _director_note_times(self, day_id: int) -> dict[int, datetime]:
        rows = self.conn.execute(
            "SELECT slice_index, created_at FROM director_notes WHERE day_id = ?",
            (day_id,),
        ).fetchall()
        return {
            int(row["slice_index"]): datetime.fromtimestamp(int(row["created_at"]), timezone.utc)
            for row in rows
        }

    def _visible_note(self, day_id: int, slice_index: int | None) -> str | None:
        """Latest Ross note aimed at this slice or an earlier one."""
        if slice_index is None:
            row = self.conn.execute(
                """
                SELECT body FROM director_notes
                WHERE day_id = ?
                ORDER BY created_at DESC, slice_index DESC LIMIT 1
                """,
                (day_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT body FROM director_notes
                WHERE day_id = ? AND slice_index <= ?
                ORDER BY created_at DESC, slice_index DESC LIMIT 1
                """,
                (day_id, slice_index),
            ).fetchone()
        return row["body"] if row else None

    def _session_clock(self, day: sqlite3.Row, now: datetime):
        total = self._slice_total(day)
        if not day["theme"]:
            deadline = self._design_deadline(day)
            if now < deadline:
                return self._awaiting_clock(deadline, total)
            return walk_session(self._opens_at(day), {}, now, slice_count=total)
        acted, redos = self._slice_timing(day["id"])
        order = json.loads(day["artist_order_json"]) if day["artist_order_json"] else self._order_for(day)
        rows = self.conn.execute(
            """
            SELECT agent_id, slice_index, done, created_at FROM actions
            WHERE day_id = ? AND done = 1 AND state != 'vetoed'
            """,
            (day["id"],),
        ).fetchall()
        for row in rows:
            closed = datetime.fromtimestamp(int(row["created_at"]), timezone.utc)
            for index in range(int(row["slice_index"]) + 1, total):
                if artist_on_slice(order, index) == row["agent_id"] and index not in acted:
                    acted[index] = closed
        return walk_session(
            self._opens_at(day),
            acted,
            now,
            gate_seconds=note_gate_seconds(),
            note_at=self._director_note_times(day["id"]),
            slice_count=total,
            redos=redos,
        )

    def _ensure_design_timeout(self) -> None:
        def mutate() -> None:
            self._apply_design_timeout(self.now())

        self._run_write(mutate)

    def _timeout_blank(self, now: datetime) -> sqlite3.Row | None:
        """Newest open canvas that still has no theme and has passed its design wait.

        A blank whose wait ended more than a full session ago is left alone so a
        stray legacy row cannot be revived. The per-minute cron notices the
        deadline while that window is still open.
        """
        row = self.conn.execute(
            "SELECT * FROM days WHERE archived = 0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None or row["theme"]:
            return None
        deadline = self._design_deadline(row)
        if now < deadline:
            return None
        if now >= deadline + timedelta(seconds=self._slice_total(row) * SLICE_SECONDS):
            return None
        return row

    def _apply_design_timeout(self, now: datetime) -> bool:
        row = self._timeout_blank(now)
        if row is None:
            return False
        self._store_design(row, now, FALLBACK_THEME, FALLBACK_BRIEF, FALLBACK_PALETTE)
        return True

    def _store_design(
        self,
        day: sqlite3.Row,
        now: datetime,
        theme: str,
        brief: str,
        palette: list[dict[str, Any]],
    ) -> list[str]:
        """Write the theme and start slice 0 at ``now``. Caller holds the write."""
        settings = self._settings()
        order = rotate(
            json.loads(settings["roster_json"]),
            day["day_index"],
            bool(settings["rotate"]),
        )
        stamp = int(now.timestamp())
        cur = self.conn.execute(
            """
            UPDATE days
            SET theme = ?, brief = ?, palette_json = ?, canvas = ?,
                artist_order_json = ?, designed_at = ?, opens_at = ?
            WHERE id = ? AND (theme IS NULL OR theme = '')
            """,
            (
                theme,
                brief,
                json.dumps(palette),
                bytes(blank_canvas()),
                json.dumps(order),
                stamp,
                stamp,
                day["id"],
            ),
        )
        if cur.rowcount != 1:
            raise ApiError(409, "already_designed", "Today already has a theme and palette.")
        self.dirty = True
        return order

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
            INSERT INTO days (
              day_index, ny_date, created_at, opens_at, kind, archived, slice_count
            )
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (nxt, key, int(now.timestamp()), stamp, kind, configured_slice_count()),
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

    def _active_canvas(self, now: datetime) -> sqlite3.Row | None:
        """Newest canvas that is still open. Calendar dates are not consulted.

        Locked and archived rows are skipped, so a newer finished canvas does
        not hide an older one that is still running, and a past-dated or
        archived row cannot become the one seats paint.
        """
        rows = self.conn.execute(
            "SELECT * FROM days WHERE archived = 0 ORDER BY id DESC"
        ).fetchall()
        for row in rows:
            phase, _ = self._phase_for(row, now)
            if phase != "after":
                return row
        return None

    def today_row(self) -> sqlite3.Row | None:
        """The canvas seats paint: the newest one that has not locked."""
        return self._active_canvas(self.now())

    def _latest_unarchived(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM days WHERE archived = 0 ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def _mark_morning(self, *, opened: str | None = None, due: str | None = None, clear_due: bool = False) -> None:
        sets: list[str] = []
        params: list[str] = []
        if opened is not None:
            sets.append("morning_opened = ?")
            params.append(opened)
        if due is not None:
            sets.append("morning_due = ?")
            params.append(due)
        if clear_due:
            sets.append("morning_due = NULL")
        if not sets:
            return
        self.conn.execute(f"UPDATE settings SET {', '.join(sets)} WHERE id = 1", params)
        self.dirty = True

    def _start_scheduled_morning(self, now: datetime) -> None:
        """Open at most one morning canvas per New York date.

        A canvas still running from before 08:00 is left alone. The morning
        canvas is created on a later tick, once that canvas has locked, and
        its slice clock starts then so it is not born already half over.
        A canvas already open at or after 08:00 counts as this morning: the
        cron does not start a second one. After the latest lock for a new
        morning (08:54 ET at the default of 6 rounds), with nothing waiting,
        no already-locked canvas is minted.
        """
        start, lock = day_bounds(now.astimezone(NY).date())
        if now < start:
            return
        key = ny_date(now)
        settings = self._settings()
        if settings["morning_opened"] == key:
            return
        active = self._active_canvas(now)
        if active is not None:
            if self._opens_at(active) >= start:
                self._mark_morning(opened=key, clear_due=True)
                return
            if settings["morning_due"] != key:
                self._mark_morning(due=key)
            return
        if now >= lock and settings["morning_due"] != key:
            return
        opens = now if settings["morning_due"] == key else start
        self._insert_day(now, KIND_MORNING, opens)
        self._mark_morning(opened=key, clear_due=True)

    def _ensure_today(self, now: datetime) -> sqlite3.Row:
        self._start_scheduled_morning(now)
        day = self._active_canvas(now)
        if day is None:
            phase, _ = position(now)
            if phase == "before":
                raise ApiError(409, "before_routine", "The morning opens at 08:00 America/New_York.")
            raise ApiError(409, "morning_locked", "This session is locked.")
        return day

    def open_today(self) -> dict[str, Any]:
        def mutate() -> None:
            self._start_scheduled_morning(self.now())

        self._run_write(mutate)
        return self.watch_state()

    def open_bonus(self) -> dict[str, Any]:
        """Open a fresh session at the current clock, without moving the date."""

        def mutate() -> int:
            now = self.now().astimezone(timezone.utc).replace(microsecond=0)
            current = self.today_row()
            if current is not None:
                self._archive_day(current["id"], now)
            day = self._insert_day(now, KIND_BONUS, now)
            return int(day["id"])

        day_id = self._run_write(mutate)
        self._dispatch_quietly()
        return self._tick_payload(self.day_by_id(day_id), self.now())

    def open_for_director(self, agent_id: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        """Ross opens a one-off canvas. An optional design starts slice 0 immediately."""
        if agent_id != DIRECTOR_ID:
            raise ApiError(403, "wrong_role", "Only the art director can open a canvas.")
        body = payload or {}
        if not isinstance(body, dict):
            raise ApiError(400, "bad_canvas", "Canvas body must be an object.")
        design = None
        if any(key in body for key in ("theme", "brief", "palette", "type")):
            if body.get("type") not in (None, "design"):
                raise ApiError(400, "bad_action", "Design payload type must be 'design'.")
            design = _parse_design(body)
        saved: dict[str, Any] = {}

        def mutate() -> int:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                now = self.now().astimezone(timezone.utc).replace(microsecond=0)
                current = self.today_row()
                if current is not None:
                    self._archive_day(current["id"], now)
                day = self._insert_day(now, KIND_BONUS, now)
                if design is not None:
                    theme, brief, palette = design
                    order = self._store_design(day, now, theme, brief, palette)
                    saved["theme"] = theme
                    saved["brief"] = brief
                    saved["order"] = order
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            return int(day["id"])

        day_id = self._run_write(mutate)
        self._dispatch_quietly()
        payload_out = self._tick_payload(self.day_by_id(day_id), self.now())
        if design is not None:
            payload_out["theme"] = saved["theme"]
            payload_out["brief"] = saved["brief"]
            payload_out["first_agent_id"] = saved["order"][0]
        return payload_out

    def post_director_note(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if agent_id != DIRECTOR_ID:
            raise ApiError(403, "wrong_role", "Only the art director can post a slice note.")
        if not isinstance(payload, dict):
            raise ApiError(400, "bad_note", "Note body must be an object.")
        text = _parse_director_note(payload.get("note", payload.get("text")))
        requested = payload.get("slice", payload.get("for_slice"))
        saved: dict[str, Any] = {}

        def mutate() -> None:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                now = self.now()
                day = self.today_row()
                if day is None:
                    raise ApiError(409, "no_day", "No canvas is open.")
                if not day["theme"]:
                    raise ApiError(409, "awaiting_design", "Post the design before a slice note.")
                target = self._note_target(day, now, requested)
                stamp = int(now.timestamp())
                self.conn.execute(
                    """
                    INSERT INTO director_notes (day_id, slice_index, body, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(day_id, slice_index) DO UPDATE SET body = excluded.body
                    """,
                    (day["id"], target, text, stamp),
                )
                self.conn.commit()
                self.dirty = True
                saved["day_id"] = int(day["id"])
                saved["slice"] = target
            except Exception:
                self.conn.rollback()
                raise

        self._run_write(mutate)
        self._dispatch_quietly()
        fresh = self.day_by_id(saved["day_id"])
        clock = self._session_clock(fresh, self.now())
        released = clock.phase == "during" and clock.index == saved["slice"]
        return {
            "ok": True,
            "day_id": saved["day_id"],
            "slice": saved["slice"],
            "director_note": text,
            "released": released,
        }

    def veto_slice(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Ross undoes the slice that just closed and reopens it for that artist.

        Allowed once, and only while the review gate for the next slice is
        open. The vetoed stroke stays in the ledger with state ``vetoed`` and
        is left out of every render. The same seat gets a redo of ``TILE_REDO_SECONDS``.
        """
        if agent_id != DIRECTOR_ID:
            raise ApiError(403, "wrong_role", "Only the art director can veto a slice.")
        if not isinstance(payload, dict):
            raise ApiError(400, "bad_veto", "Veto body must be an object.")
        if "slice" not in payload:
            raise ApiError(400, "bad_veto", "slice is required.")
        requested = payload.get("slice")
        if type(requested) is not int:
            raise ApiError(400, "bad_veto", "slice must be an integer.")
        reason = _parse_veto_reason(payload.get("reason"))
        saved: dict[str, Any] = {}

        def mutate() -> None:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                now = self.now()
                day = self.today_row()
                if day is None:
                    raise ApiError(409, "no_day", "No canvas is open.")
                if not day["theme"]:
                    raise ApiError(409, "awaiting_design", "Post the design before vetoing a slice.")
                total = self._slice_total(day)
                if requested < 0 or requested >= total:
                    raise ApiError(400, "bad_veto", f"slice must be an integer from 0 through {total - 1}.")
                if self._vetoed_row(day["id"], requested) is not None:
                    raise ApiError(
                        409,
                        "already_vetoed",
                        f"Slice {requested} was already vetoed. A slice can be vetoed once.",
                    )
                clock = self._session_clock(day, now)
                closed = self._gate_closed_index(clock)
                if closed is None or requested != closed:
                    raise ApiError(
                        409,
                        "not_in_gate",
                        "A veto is only allowed for the most recently closed slice "
                        "while its review gate is open, before the next slice starts.",
                    )
                action = self._action_for_slice(day["id"], requested)
                if action is None:
                    raise ApiError(
                        409,
                        "nothing_to_veto",
                        "That slice has no paint or pass to veto.",
                    )
                window = redo_seconds()
                stamp = int(now.timestamp())
                restored = self._canvas_without_action(day, int(action["id"]))
                self.conn.execute(
                    """
                    UPDATE actions
                    SET state = 'vetoed', veto_reason = ?, vetoed_at = ?, redo_seconds = ?
                    WHERE id = ?
                    """,
                    (reason, stamp, window, int(action["id"])),
                )
                self._bump_canvas_rev(day["id"])
                self.conn.execute(
                    "UPDATE days SET canvas = ? WHERE id = ?",
                    (restored, day["id"]),
                )
                if int(action["pixel_count"]):
                    self.conn.execute(
                        """
                        UPDATE artist_state
                        SET pixels_used = MAX(0, pixels_used - ?)
                        WHERE day_id = ? AND agent_id = ?
                        """,
                        (int(action["pixel_count"]), day["id"], action["agent_id"]),
                    )
                if int(action["done"]):
                    self.conn.execute(
                        """
                        UPDATE artist_state SET skipped = 0
                        WHERE day_id = ? AND agent_id = ?
                        """,
                        (day["id"], action["agent_id"]),
                    )
                # The first open and the close of the vetoed attempt already
                # woke their seats. Drop those rows so the redo can wake the
                # artist again, and so the gate after the redo can wake Ross.
                self.conn.execute(
                    """
                    DELETE FROM wakes
                    WHERE day_id = ? AND slice_index = ? AND event = 'slice_open' AND seat_id = ?
                    """,
                    (day["id"], requested, action["agent_id"]),
                )
                self.conn.execute(
                    """
                    DELETE FROM wakes
                    WHERE day_id = ? AND slice_index = ? AND event = 'slice_closed'
                    """,
                    (day["id"], requested),
                )
                self.conn.commit()
                self.dirty = True
                saved["day_id"] = int(day["id"])
                saved["agent_id"] = action["agent_id"]
                saved["ends_at_unix"] = stamp + window
                saved["redo_seconds"] = window
            except Exception:
                self.conn.rollback()
                raise

        self._run_write(mutate)
        self._dispatch_quietly()
        return {
            "ok": True,
            "day_id": saved["day_id"],
            "slice": requested,
            "redo": True,
            "redo_seconds": saved["redo_seconds"],
            "agent_id": saved["agent_id"],
            "ends_at_unix": saved["ends_at_unix"],
            "veto_note": reason,
        }

    def ross_view(self) -> dict[str, Any]:
        self._ensure_design_timeout()
        with self.lock:
            self._refresh()
            now = self.now()
            day = self.today_row()
            if day is None:
                designed = self._designed_rows()
                day = designed[0] if designed else None
            view = self._ross_from(day, now)
        self._dispatch_quietly()
        return view

    def _gate_closed_index(self, clock: SessionClock) -> int | None:
        """Slice that just closed, while the next slice's review gate is open."""
        if clock.phase != "holding" or clock.holding_index is None:
            return None
        closed = clock.holding_index - 1
        if closed < 0:
            return None
        return closed

    def _note_target(self, day: sqlite3.Row, now: datetime, requested: Any) -> int:
        clock = self._session_clock(day, now)
        total = self._slice_total(day)
        if clock.phase == "after" or int(day["archived"]):
            raise ApiError(409, "morning_locked", "This session is locked.")
        if clock.phase == "holding" and clock.holding_index is not None:
            default = clock.holding_index
        elif clock.phase == "during" and clock.index is not None:
            default = clock.index if clock.index >= total - 1 else clock.index + 1
        elif clock.phase == "before":
            default = 0
        else:
            raise ApiError(409, "no_slice", "There is no slice waiting for a note.")
        if requested is None:
            if clock.phase == "during" and clock.index == total - 1:
                return clock.index
            return default
        if type(requested) is not int or requested < 0 or requested >= total:
            raise ApiError(400, "bad_slice", f"slice must be an integer from 0 through {total - 1}.")
        return requested

    def _ross_from(self, day: sqlite3.Row | None, now: datetime) -> dict[str, Any]:
        if day is None:
            return {
                "role": DIRECTOR_ID,
                "to_act": False,
                "reason": "no_day",
                "status": "no_day",
                "day_id": None,
                "theme": None,
                "brief": None,
                "director_note": None,
                "palette": [],
                "slice_index": None,
                "pending_slice": None,
                "hold_until_unix": None,
            }
        clock = self._session_clock(day, now)
        designed = bool(day["theme"])
        pending = None
        reason = "idle"
        to_act = False
        if not designed and clock.phase != "after":
            to_act = True
            reason = "design_needed"
            pending = 0
        elif clock.phase == "holding" and clock.holding_index is not None:
            to_act = True
            reason = "note_needed"
            pending = clock.holding_index
        elif clock.phase == "during":
            reason = "painting"
            pending = (
                None
                if clock.index is None or clock.index >= self._slice_total(day) - 1
                else clock.index + 1
            )
        elif clock.phase == "after":
            reason = "locked"
        elif clock.phase == "before":
            reason = "before_routine"
        note_slice = clock.index if clock.index is not None else pending
        preview = _png_url(day, 2)
        return {
            "role": DIRECTOR_ID,
            "to_act": to_act,
            "reason": reason,
            "status": self._status(day, now),
            "day_id": day["id"],
            "day_index": day["day_index"],
            "mural_id": f"day-{day['day_index']}",
            "ny_date": day["ny_date"],
            "theme": day["theme"],
            "brief": day["brief"] if designed else None,
            "director_note": self._redo_turn_note(day, note_slice, now) if designed else None,
            "palette": json.loads(day["palette_json"]) if day["palette_json"] else [],
            "slice_index": clock.index,
            "pending_slice": pending,
            "hold_until_unix": int(clock.hold_until.timestamp()) if clock.hold_until else None,
            "preview_url": preview,
            "render_url": preview,
        }

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
        if agent_id == DIRECTOR_ID:
            return self.ross_view()
        if agent_id != "design":
            raise ApiError(403, "wrong_role", "Only the art director or the design seat can read the design brief.")
        self._ensure_design_timeout()
        with self.lock:
            self._refresh()
            now = self.now()
            day = self.today_row()
            phase, _ = self._phase_for(day, now)
            designed = bool(day and day["theme"])
            to_act = phase in ("during", "awaiting") and not designed
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
                "theme": day["theme"] if day else None,
                "brief": day["brief"] if day and designed else None,
            }

    def submit_design(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if agent_id not in ("design", DIRECTOR_ID):
            raise ApiError(403, "wrong_role", "Only the art director or the design seat can publish a theme.")
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
                    self._start_scheduled_morning(now)
                    day = self._active_canvas(now)
                if day is None:
                    phase, _ = position(now)
                    if phase == "before":
                        raise ApiError(409, "before_routine", "Design opens at 08:00 America/New_York.")
                    raise ApiError(409, "morning_locked", "This session is locked.")
                phase, _index = self._phase_for(day, now)
                if phase == "before":
                    raise ApiError(409, "before_routine", "Design opens at 08:00 America/New_York.")
                if phase == "after":
                    raise ApiError(409, "morning_locked", "This session is locked.")
                if day["theme"]:
                    raise ApiError(409, "already_designed", "Today already has a theme and palette.")
                order = self._store_design(day, now, theme, brief, palette)
                self.conn.commit()
                saved["day_id"] = day["id"]
                saved["day_index"] = day["day_index"]
                saved["order"] = order
                # Slice 0 opens at this clock. The design wait does not burn slices.
                saved["index"] = 0
            except Exception:
                self.conn.rollback()
                raise

        self._run_write(mutate)
        self._dispatch_quietly()
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
        self._ensure_design_timeout()
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
                    latest = self._latest_unarchived()
                    if latest is not None and self._phase_for(latest, now)[0] == "after":
                        raise ApiError(409, "morning_locked", "The brush is closed.")
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
                outcome = "active"
                revert_reason = None
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
                    redo = self._session_clock(day, now).slices[index].redo
                    # Slice 0 is the background. Vetoing it restores a blank
                    # canvas, so the redo has to repaint most of it. Both
                    # overfill caps (TILE_REDO_MAX_FRACTION and
                    # TILE_REDO_MAX_DOMINANT) apply only to later slices.
                    if redo and index != 0:
                        revert_reason = redo_overfill(bytes(day["canvas"]), collapsed)
                    if revert_reason:
                        outcome = "reverted"
                        pixels_out = [{"x": x, "y": y, "color": color} for x, y, color in collapsed]
                        self._bump_canvas_rev(day["id"])
                    else:
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
                        if redo:
                            self._bump_canvas_rev(day["id"])
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
                          pixel_count, note, focus_json, pixels_json, created_at,
                          state, revert_reason
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            outcome,
                            revert_reason,
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
                saved["revert_reason"] = revert_reason
            except Exception:
                self.conn.rollback()
                raise

        self._run_write(mutate)
        self._dispatch_quietly()
        if saved.get("revert_reason"):
            raise ApiError(422, "overfill_reverted", saved["revert_reason"])
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

    def can_act(self, day: sqlite3.Row | None, agent_id: str, now: datetime) -> bool:
        """Whether this seat may paint or pass the live slice.

        A missed earlier slice does not matter. TurnView and ``POST /agent/act``
        both use this, so they cannot disagree about the open slice.
        """
        if day is None or int(day["archived"]) or not day["theme"]:
            return False
        phase, index = self._phase_for(day, now)
        if phase != "during" or index is None:
            return False
        order = self._order_for(day)
        if artist_on_slice(order, index) != agent_id:
            return False
        if self._is_skipped(day["id"], agent_id):
            return False
        if self._action_for_slice(day["id"], index) is not None:
            return False
        return True

    def _require_slice(self, day: sqlite3.Row, agent_id: str, now: datetime) -> None:
        if int(day["archived"]):
            raise ApiError(409, "morning_locked", "That canvas is archived.")
        clock = self._session_clock(day, now)
        phase, index = clock.phase, clock.index
        if phase == "before":
            if day["kind"] == KIND_BONUS:
                raise ApiError(409, "before_routine", "This session has not opened yet.")
            raise ApiError(409, "before_routine", "Artists paint from 08:00 ET.")
        if phase == "holding":
            closed = clock.holding_index - 1 if clock.holding_index else -1
            if closed >= 0 and day["artist_order_json"]:
                closed_artist = artist_on_slice(json.loads(day["artist_order_json"]), closed)
                if closed_artist == agent_id:
                    if self._action_for_slice(day["id"], closed):
                        raise ApiError(409, "slice_used", "This 3-minute slice already has an action.")
                    if clock.slices[closed].missed:
                        raise ApiError(
                            409,
                            "slice_missed",
                            "That 3-minute slice closed with no paint or pass. The turn has moved on.",
                        )
            raise ApiError(
                409,
                "review_wait",
                "The next slice opens when the art director posts a note, or when that wait ends.",
            )
        if phase == "after" or index is None:
            raise ApiError(409, "morning_locked", "This session is locked.")
        order = json.loads(day["artist_order_json"])
        if self._is_skipped(day["id"], agent_id):
            raise ApiError(409, "rounds_skipped", "You already ended your rounds for today.")
        if self.can_act(day, agent_id, now):
            return
        expected = artist_on_slice(order, index)
        if agent_id != expected:
            previous = index - 1
            if previous >= 0 and artist_on_slice(order, previous) == agent_id:
                if self._action_for_slice(day["id"], previous):
                    raise ApiError(409, "slice_used", "This 3-minute slice already has an action.")
                if clock.slices[previous].missed:
                    raise ApiError(
                        409,
                        "slice_missed",
                        "That 3-minute slice closed with no paint or pass. The turn has moved on.",
                    )
            raise ApiError(
                409,
                "not_your_turn",
                f"This slice belongs to {DISPLAY[expected]}.",
            )
        if self._action_for_slice(day["id"], index) is not None:
            raise ApiError(409, "slice_used", "This 3-minute slice already has an action.")

    def turn_view(self, agent_id: str) -> dict[str, Any]:
        if agent_id == DIRECTOR_ID:
            return self.ross_view()
        if agent_id not in ARTISTS:
            raise ApiError(403, "wrong_role", "TurnView is for an artist seat.")
        self._ensure_design_timeout()
        with self.lock:
            self._refresh()
            now = self.now()
            day = self.today_row()
            if day is None:
                designed = self._designed_rows()
                day = designed[0] if designed else None
            view = self._turn_from(day, agent_id, now)
        self._dispatch_quietly()
        return view

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
        to_act = self.can_act(day, agent_id, now)
        turn_left = None if to_act else 0
        total = self._slice_total(day)
        if phase == "during" and index is not None:
            round_index = round_on_slice(index)
        elif phase in ("before", "awaiting"):
            round_index = 1
        elif day is not None:
            clock = self._session_clock(day, now)
            if clock.holding_index is not None:
                round_index = round_on_slice(clock.holding_index)
            else:
                round_index = rounds_in(total)
        else:
            round_index = rounds_in(total)
        slice_start, slice_end = self._slice_bounds(day, now, phase, index)
        palette = json.loads(day["palette_json"]) if day and day["palette_json"] else []
        focus = None
        if day and day["focus_json"]:
            focus = json.loads(day["focus_json"]).get(agent_id)
        preview = _png_url(day, 3) if day is not None else None
        crop_url = _crop_url(day, focus) if day is not None else None
        strokes = self._recent_strokes(day["id"], include_latest_pixels=True) if day else []
        note_slice = index
        if day is not None and note_slice is None:
            clock = self._session_clock(day, now)
            if clock.phase == "holding" and clock.holding_index is not None:
                note_slice = clock.holding_index
        director_note = self._redo_turn_note(day, note_slice, now) if day else None
        redo = False
        veto_note = None
        if day is not None and phase == "during" and index is not None:
            live = self._session_clock(day, now)
            if live.slices[index].redo:
                redo = True
                brief = self._veto_brief(day["id"], index)
                if brief and artist_on_slice(order, index) == agent_id:
                    director_note = brief
                    veto_note = brief
        return {
            "mural_id": f"day-{day['day_index']}" if day else None,
            "day_id": day["id"] if day else None,
            "day_index": day["day_index"] if day else None,
            "ny_date": day["ny_date"] if day else ny_date(now),
            "status": self._status(day, now),
            "theme": day["theme"] if day else None,
            "brief": day["brief"] if day else None,
            "director_note": director_note,
            "redo": redo,
            "veto_note": veto_note,
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
        """Start this morning's canvas if it is due, then report the same canvas the watch page shows."""

        def mutate() -> None:
            now = self.now()
            self._start_scheduled_morning(now)
            self._apply_design_timeout(now)

        self._run_write(mutate)
        with self.lock:
            self._refresh()
            now = self.now()
            featured = self._default_feature(now)
            if featured is not None:
                return self._tick_payload(featured, now)
            phase, _ = position(now)
            if phase == "before":
                return {
                    "ok": True,
                    "phase": "before_open",
                    "ny_now": now.astimezone(NY).isoformat(),
                    "ny_label": label_clock(now),
                    "hint": "Wait until 08:00 America/New_York, then tick again and post a design.",
                    "day": None,
                }
            return {
                "ok": True,
                "phase": "locked",
                "ny_now": now.astimezone(NY).isoformat(),
                "ny_label": label_clock(now),
                "slice_index": None,
                "round_index": None,
                "current_agent_id": None,
                "hint": "The morning window has passed and no canvas is open.",
                "day": None,
            }

    def cron_tick(self) -> dict[str, Any]:
        """Create today's canvas if the morning is open, then deliver due wakes.

        Slice close and open are already decided by the session clock. This
        does not insert a missed-slice row; it notices the clock and wakes
        whoever that clock says should hear about it.
        """
        payload = self.routine_tick()
        payload["wakes"] = self.dispatch_wakes()
        return payload

    def dispatch_wakes(self) -> list[dict[str, Any]]:
        holder: dict[str, list[WakeIntent]] = {"claimed": []}

        def mutate() -> None:
            holder["claimed"] = []
            intents = self._due_intents()
            if not intents:
                return
            now_ts = int(self.now().timestamp())
            if not any(needs_attempt(self.conn, intent, now_ts) for intent in intents):
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                claimed = claim_intents(self.conn, intents, now_ts)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            if claimed:
                self.dirty = True
            holder["claimed"] = claimed

        self._run_write(mutate)
        return [self._post_and_finish(intent) for intent in holder["claimed"]]

    def _dispatch_quietly(self) -> None:
        try:
            self.dispatch_wakes()
        except Exception:
            logger.exception("wake dispatch failed")

    def _due_intents(self) -> list[WakeIntent]:
        hooks = self._seat_hooks()
        if not hooks:
            return []
        now = self.now()
        day = self.today_row()
        if day is None:
            # The canvas that just locked is no longer "open", but reviewers
            # still get slice_closed / canvas_locked during the short grace.
            day = self._latest_unarchived()
        if day is None or int(day["archived"]):
            return []
        clock = self._session_clock(day, now)
        note = None
        veto_note = None
        if day["theme"] and clock.phase == "during" and clock.index is not None:
            note = self._redo_turn_note(day, clock.index, now)
            if clock.slices[clock.index].redo:
                veto_note = self._veto_brief(int(day["id"]), clock.index)
        return plan_wakes(
            day_id=int(day["id"]),
            theme=bool(day["theme"]),
            archived=False,
            phase=clock.phase,
            index=clock.index,
            slices=clock.slices,
            order=self._order_for(day),
            hooks=hooks,
            now=now,
            lock_at=clock.lock_at,
            api_base_url=public_base_url(),
            brief=day["brief"] if day["theme"] else None,
            director_note=note,
            design_deadline=None if day["theme"] else self._design_deadline(day),
            holding_index=clock.holding_index,
            veto_note=veto_note,
            render_rev=_canvas_rev(day),
        )

    def _post_and_finish(self, intent: WakeIntent) -> dict[str, Any]:
        try:
            code = post_webhook(intent.url, intent.secret, intent.payload, intent.bearer)
        except Exception:
            logger.warning(
                "wake post failed seat=%s event=%s slice=%s",
                intent.seat_id,
                intent.event,
                intent.slice_index,
            )
            code = 0
        state = "sent" if 200 <= code < 300 else "failed"

        def mutate() -> None:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                changed = finish_wake(
                    self.conn,
                    intent,
                    state,
                    code,
                    int(self.now().timestamp()),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            if changed:
                self.dirty = True

        self._run_write(mutate)
        return {
            "day_id": intent.day_id,
            "slice": None if intent.slice_index < 0 else intent.slice_index,
            "seat": intent.seat_id,
            "event": intent.event,
            "status": state,
            "attempts": intent.attempts,
            "last_http": code,
        }

    def _env_hooks(self) -> dict[str, SeatHook]:
        try:
            return hooks_from_env()
        except WebhookConfigError as exc:
            raise ApiError(500, "bad_webhook_config", exc.message) from exc

    def _seat_hooks(self) -> list[SeatHook]:
        env_hooks = self._env_hooks()
        rows = self.conn.execute(
            "SELECT seat_id, url, secret, bearer, events_json FROM seat_webhooks"
        ).fetchall()
        disabled: set[str] = set()
        stored: dict[str, SeatHook] = {}
        for row in rows:
            seat = row["seat_id"]
            if not row["url"]:
                disabled.add(seat)
                continue
            try:
                events = json.loads(row["events_json"])
            except json.JSONDecodeError as exc:
                raise ApiError(500, "bad_webhook_config", "Stored seat webhook config is unreadable.") from exc
            if not isinstance(events, list):
                raise ApiError(500, "bad_webhook_config", "Stored seat webhook config is unreadable.")
            stored[seat] = SeatHook(
                seat,
                row["url"],
                row["secret"] or "",
                frozenset(events),
                row["bearer"] or "",
            )
        merged = {
            seat: hook
            for seat, hook in env_hooks.items()
            if seat not in disabled and seat not in stored
        }
        merged.update(stored)
        return list(merged.values())

    def set_seat_webhook(self, seat_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            hook = parse_seat_hook(seat_id, body)
        except WebhookConfigError as exc:
            raise ApiError(400, "bad_webhook", exc.message) from exc

        def mutate() -> None:
            self.conn.execute(
                """
                INSERT INTO seat_webhooks (seat_id, url, secret, bearer, events_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(seat_id) DO UPDATE SET
                  url = excluded.url,
                  secret = excluded.secret,
                  bearer = excluded.bearer,
                  events_json = excluded.events_json,
                  updated_at = excluded.updated_at
                """,
                (
                    hook.seat_id,
                    hook.url,
                    hook.secret,
                    hook.bearer,
                    json.dumps(sorted(hook.events)),
                    int(self.now().timestamp()),
                ),
            )
            self.dirty = True

        self._run_write(mutate)
        return {
            "ok": True,
            "seat": hook.seat_id,
            "enabled": True,
            "source": "host",
            "events": sorted(hook.events),
            "url": hook.url,
            "bearer_set": bool(hook.bearer),
        }

    def clear_seat_webhook(self, seat_id: str) -> dict[str, Any]:
        try:
            seat = normalize_seat(seat_id)
        except WebhookConfigError as exc:
            raise ApiError(400, "bad_seat", exc.message) from exc

        def mutate() -> None:
            self.conn.execute(
                """
                INSERT INTO seat_webhooks (seat_id, url, secret, bearer, events_json, updated_at)
                VALUES (?, '', '', '', '[]', ?)
                ON CONFLICT(seat_id) DO UPDATE SET
                  url = '',
                  secret = '',
                  bearer = '',
                  events_json = '[]',
                  updated_at = excluded.updated_at
                """,
                (seat, int(self.now().timestamp())),
            )
            self.dirty = True

        self._run_write(mutate)
        return {"ok": True, "seat": seat, "enabled": False}

    def seat_webhook_directory(self) -> dict[str, Any]:
        with self.lock:
            self._refresh()
            try:
                env_hooks = hooks_from_env()
            except WebhookConfigError as exc:
                raise ApiError(500, "bad_webhook_config", exc.message) from exc
            rows = self.conn.execute(
                "SELECT seat_id, url, events_json, bearer FROM seat_webhooks ORDER BY seat_id"
            ).fetchall()
        by_seat: dict[str, dict[str, Any]] = {}
        for seat, hook in env_hooks.items():
            by_seat[seat] = {
                "seat": seat,
                "enabled": True,
                "source": "env",
                "events": sorted(hook.events),
                "url": hook.url,
                "bearer_set": bool(hook.bearer),
            }
        for row in rows:
            seat = row["seat_id"]
            if not row["url"]:
                by_seat[seat] = {
                    "seat": seat,
                    "enabled": False,
                    "source": "host",
                    "events": [],
                    "url": "",
                    "bearer_set": False,
                }
                continue
            events = json.loads(row["events_json"])
            by_seat[seat] = {
                "seat": seat,
                "enabled": True,
                "source": "host",
                "events": sorted(events) if isinstance(events, list) else [],
                "url": row["url"],
                "bearer_set": bool(row["bearer"]),
            }
        return {"seats": [by_seat[key] for key in sorted(by_seat)]}

    def wakes_for(self, day_id: int | None) -> dict[str, Any]:
        with self.lock:
            self._refresh()
            if day_id is None:
                day = self.today_row()
                if day is None:
                    return {"day_id": None, "wakes": []}
                day_id = int(day["id"])
            else:
                self.day_by_id(day_id)
            return {"day_id": day_id, "wakes": list_wake_rows(self.conn, day_id)}

    def _tick_payload(self, day: sqlite3.Row, now: datetime) -> dict[str, Any]:
        clock = self._session_clock(day, now)
        phase, index = clock.phase, clock.index
        designed = bool(day["theme"])
        waiting_until = None
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
        elif not designed or phase == "awaiting":
            public_phase = "awaiting_design"
            current = None
            index = None
            if clock.hold_until is not None:
                waiting_until = int(clock.hold_until.timestamp())
            if day["kind"] == KIND_BONUS:
                hint = (
                    "Waiting for the art director to post the theme and palette. "
                    "The first slice starts when that design lands."
                )
            else:
                hint = (
                    "Waiting for the art director to post the theme and palette. "
                    "The first slice stays closed until then."
                )
        elif phase == "holding":
            public_phase = "review"
            current = None
            index = None
            if clock.hold_until is not None:
                waiting_until = int(clock.hold_until.timestamp())
            hint = (
                "Waiting for the art director's note before the next slice. "
                "It opens when that note is posted, or when the review wait ends."
            )
        else:
            public_phase = "slice"
            order = json.loads(day["artist_order_json"])
            current = artist_on_slice(order, index)
            clock = self._session_clock(day, now)
            slice_end = clock.deadline or clock.lock_at
            live = clock.slices[index] if index is not None else None
            if live is not None and live.redo:
                hint = (
                    f"GET /agent/turn as {current}, then POST /agent/act with one paint or pass "
                    f"before {label_hhmm(slice_end)} ET. This redo replaces the vetoed slice. "
                    f"The review gate runs after it closes."
                )
            else:
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
            "waiting_until_unix": waiting_until,
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
        self._ensure_design_timeout()
        with self.lock:
            self._refresh()
            state = self._watch_state(day_id)
        self._dispatch_quietly()
        return state

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
            self.conn.execute("DELETE FROM wakes WHERE day_id = ?", (day_id,))
            self.conn.execute("DELETE FROM director_notes WHERE day_id = ?", (day_id,))
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
        total = self._slice_total(featured)
        is_live = today is not None and featured["id"] == today["id"] and not int(featured["archived"])
        session = self._session_clock(featured, now) if is_live else None
        if not is_live:
            phase, index = "after", None
            live_index = None
            miss_before = total
            holding_index = None
        else:
            phase, index = session.phase, session.index
            holding_index = session.holding_index if phase == "holding" else None
            if phase == "after":
                live_index = None
                miss_before = total
            elif phase == "holding":
                live_index = None
                index = None
                miss_before = session.holding_index
            elif phase in ("before", "awaiting") or index is None:
                live_index = None
                miss_before = None
            elif not featured["theme"]:
                live_index = None
                miss_before = index
            else:
                live_index = index
                miss_before = index
        slices = self._slice_board(featured, order, live_index, miss_before, now, holding_index)
        current = None
        if is_live and phase == "during" and featured["theme"] and index is not None:
            current = artist_on_slice(order, index)
        preview = _png_url(featured, 3)
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
                    "to_act": bool(is_live and self.can_act(featured, agent, now)),
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
            "message": _status_message(status, total),
            "mural_id": f"day-{featured['day_index']}",
            "day_id": featured["id"],
            "day_index": featured["day_index"],
            "ny_date": featured["ny_date"],
            "theme": featured["theme"],
            "palette": palette,
            "grid_w": GRID,
            "grid_h": GRID,
            "kind": featured["kind"],
            "archived": bool(featured["archived"]),
            "demo_seed": bool(featured["demo_seed"]),
            "preview_url": preview,
            "focus": focus,
            "focus_crop_url": _crop_url(featured, focus),
            "current_agent_id": current,
            "current_display_name": DISPLAY[current] if current else None,
            "round_index": round_on_slice(live_index) if live_index is not None else None,
            "slice_index": live_index,
            "slice_ends_at_unix": slice_end,
            "waiting_until_unix": (
                int(session.hold_until.timestamp())
                if session is not None and session.hold_until is not None and phase in ("awaiting", "holding")
                else None
            ),
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
            "schedule": _schedule_meta(total),
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
                    "state": row["state"] if "state" in row.keys() and row["state"] else "active",
                    "revert_reason": row["revert_reason"] if "revert_reason" in row.keys() else None,
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
        total = self._slice_total(day)
        for index in slices:
            if type(index) is not int or index < 0 or index >= total:
                raise ApiError(
                    400,
                    "bad_slices",
                    f"Slice indices must be integers from 0 through {total - 1}.",
                )
        wanted = set(slices)
        if wanted >= set(range(total)):
            return stored, palette
        canvas = blank_canvas()
        rows = self.conn.execute(
            "SELECT slice_index, pixels_json, state FROM actions WHERE day_id = ? ORDER BY id ASC",
            (day_id,),
        ).fetchall()
        for row in rows:
            if _is_omitted(row) or row["slice_index"] not in wanted or not row["pixels_json"]:
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
        if phase == "holding":
            return "review"
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
        """The live attempt on a slice. A vetoed row stays in the table for audit."""
        return self.conn.execute(
            """
            SELECT * FROM actions
            WHERE day_id = ? AND slice_index = ? AND state != 'vetoed'
            ORDER BY id DESC LIMIT 1
            """,
            (day_id, index),
        ).fetchone()

    def _vetoed_row(self, day_id: int, index: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM actions
            WHERE day_id = ? AND slice_index = ? AND state = 'vetoed'
            ORDER BY id DESC LIMIT 1
            """,
            (day_id, index),
        ).fetchone()

    def _veto_note(self, day_id: int, index: int) -> str | None:
        row = self._vetoed_row(day_id, index)
        if row is None or "veto_reason" not in row.keys():
            return None
        reason = row["veto_reason"]
        return reason if isinstance(reason, str) and reason else None

    def _veto_brief(self, day_id: int, index: int) -> str | None:
        reason = self._veto_note(day_id, index)
        if not reason:
            return None
        return "VETO: " + reason

    def _redo_turn_note(self, day: sqlite3.Row | None, slice_index: int | None, now: datetime) -> str | None:
        """Note a seat reads for this slice.

        A live redo replaces it with ``VETO: `` plus the reason on the vetoed
        action. ``_visible_note`` only sees ``director_notes``, so the reason
        would otherwise never reach the redo.
        """
        if day is None:
            return None
        base = self._visible_note(int(day["id"]), slice_index)
        if slice_index is None or not day["theme"]:
            return base
        clock = self._session_clock(day, now)
        if clock.phase != "during" or clock.index != slice_index:
            return base
        if slice_index >= len(clock.slices) or not clock.slices[slice_index].redo:
            return base
        brief = self._veto_brief(int(day["id"]), slice_index)
        return brief if brief else base

    def _bump_canvas_rev(self, day_id: int) -> None:
        self.conn.execute(
            "UPDATE days SET canvas_rev = COALESCE(canvas_rev, 0) + 1 WHERE id = ?",
            (day_id,),
        )

    def _opening_canvas(self, day: sqlite3.Row) -> bytes:
        if int(day["demo_seed"] or 0):
            canvas = blank_canvas()
            draw_cabin(canvas)
            return bytes(canvas)
        return bytes(blank_canvas())

    def _canvas_without_action(self, day: sqlite3.Row, action_id: int) -> bytes:
        """Pixels as they were before ``action_id``, including earlier overwrites.

        Replaying the ledger onto the opening canvas is the source of truth
        when that replay still matches the stored painting. A demo cabin or
        any other pixel that never went through an action stays put: those
        cells are restored from the opening canvas only where this action
        painted them, and from the previous stroke where it overwrote one.
        """
        stored = bytes(day["canvas"])
        base = self._opening_canvas(day)
        rows = self.conn.execute(
            "SELECT * FROM actions WHERE day_id = ? ORDER BY id ASC",
            (day["id"],),
        ).fetchall()
        active = [row for row in rows if not _is_omitted(row)]
        if _replay_actions(base, active) == stored:
            kept = [row for row in active if int(row["id"]) != action_id]
            return _replay_actions(base, kept)
        prior: dict[tuple[int, int], int] = {}
        painted: list[tuple[int, int]] = []
        for row in rows:
            if int(row["id"]) == action_id:
                painted = [(pixel["x"], pixel["y"]) for pixel in _iter_pixels(row["pixels_json"])]
                break
            if _is_omitted(row):
                continue
            for pixel in _iter_pixels(row["pixels_json"]):
                prior[(pixel["x"], pixel["y"])] = pixel["color"]
        canvas = bytearray(stored)
        for x, y in painted:
            offset = y * GRID + x
            canvas[offset] = prior.get((x, y), base[offset])
        return bytes(canvas)

    def _turns_remaining(self, day: sqlite3.Row | None, agent_id: str, now: datetime) -> int:
        if day is None:
            phase, index = position(now)
        else:
            if int(day["archived"]) or self._is_skipped(day["id"], agent_id):
                return 0
            phase, index = self._phase_for(day, now)
        if phase == "after":
            return 0
        if day is not None:
            clock = self._session_clock(day, now)
            if clock.phase == "after":
                return 0
            if clock.phase == "holding" and clock.holding_index is not None:
                floor = clock.holding_index
            elif clock.phase in ("before", "awaiting") or clock.index is None:
                floor = 0
            else:
                floor = clock.index
        else:
            floor = 0 if phase == "before" or index is None else index
        order = self._order_for(day)
        remaining = 0
        for slice_i in range(self._slice_total(day)):
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
            """
            SELECT * FROM actions
            WHERE day_id = ? AND state NOT IN ('vetoed', 'reverted')
            ORDER BY id DESC LIMIT 6
            """,
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
        holding_index: int | None = None,
    ) -> list[dict[str, Any]]:
        clock = self._session_clock(day, now)
        actions: dict[int, sqlite3.Row] = {}
        vetoes: dict[int, sqlite3.Row] = {}
        reverts: dict[int, sqlite3.Row] = {}
        for row in self.conn.execute(
            "SELECT * FROM actions WHERE day_id = ? ORDER BY id ASC",
            (day["id"],),
        ).fetchall():
            index = int(row["slice_index"])
            if _is_vetoed(row):
                vetoes[index] = row
            elif _is_reverted(row):
                reverts[index] = row
            else:
                actions[index] = row
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
            elif reverts.get(index) is not None:
                state = "reverted"
            elif ended_early is not None and index > ended_early:
                state = "skipped"
            elif miss_before is not None and index < miss_before:
                state = "missed"
            elif live_index is not None and index < live_index:
                state = "missed"
            elif holding_index is not None and index == holding_index:
                state = "review"
            elif live_index == index:
                state = "active"
            else:
                state = "upcoming"
            history: list[dict[str, Any]] = []
            vetoed = vetoes.get(index)
            if vetoed is not None:
                history.append(
                    {
                        "state": "vetoed",
                        "action": vetoed["action"],
                        "pixel_count": vetoed["pixel_count"],
                        "removed": True,
                    }
                )
            reverted = reverts.get(index)
            if reverted is not None:
                history.append(
                    {
                        "state": "reverted",
                        "action": reverted["action"],
                        "pixel_count": reverted["pixel_count"],
                        "removed": True,
                    }
                )
            if action is not None:
                history.append(
                    {
                        "state": "done",
                        "action": action["action"],
                        "pixel_count": action["pixel_count"],
                        "removed": False,
                    }
                )
            pixel_count = action["pixel_count"] if action else 0
            removed = vetoed is not None and not pixel_count
            board.append(
                {
                    "index": index,
                    "round": round_on_slice(index),
                    "agent_id": agent,
                    "display_name": DISPLAY[agent],
                    "starts": label_hhmm(begin),
                    "ends": label_hhmm(end),
                    "state": state,
                    "pixel_count": pixel_count,
                    "action": action["action"] if action else None,
                    "redo": bool(mark.redo),
                    "removed": removed,
                    "history": history,
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
            "preview_url": _png_url(row, 2),
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


def _lock_time(ny_date_text: str, slice_count: int | None = None) -> datetime:
    year, month, day = (int(part) for part in ny_date_text.split("-"))
    _, lock = day_bounds(datetime(year, month, day).date(), slice_count)
    return lock


def _schedule_meta(slice_count: int | None = None) -> dict[str, Any]:
    """Schedule block for one canvas, or for a new morning when ``slice_count`` is omitted."""
    total = resolve_slice_count(slice_count)
    rounds = rounds_in(total)
    return {
        "tz": "America/New_York",
        "opens": "08:00",
        "locks": lock_label(total),
        "slice_seconds": SLICE_SECONDS,
        "slice_minutes": SLICE_SECONDS // 60,
        "rounds": rounds,
        "slices": total,
        "pixels_per_turn": PIXELS_PER_TURN,
        "pixels_per_artist_day": PIXELS_PER_ARTIST_DAY,
        "turns_per_artist": rounds,
        "session_minutes": session_minutes(total),
        "session_length": session_length_phrase(total),
    }


def _status_message(status: str, slice_count: int | None = None) -> str:
    length = session_length_phrase(slice_count)
    return {
        "no_day": "No canvas for this date yet. The morning routine opens one at 8:00 ET.",
        "before_open": "The canvas is ready and waiting. Painting starts at 8:00 ET.",
        "awaiting_design": "Waiting for the art director to publish today's theme and 16 colors.",
        "review": "Waiting for the art director before the next slice opens.",
        "painting": "The morning is open. One artist holds the brush for this 3-minute slice.",
        "locked": (
            f"Locked. A morning lasts at most {length} after 08:00 ET, "
            "and the next one starts blank."
        ),
        "missed": "The morning passed without a design. Tomorrow starts blank.",
        "scheduled": "This canvas is scheduled.",
    }.get(status, status)


def _is_vetoed(row: sqlite3.Row) -> bool:
    if "state" not in row.keys():
        return False
    return row["state"] == "vetoed"


def _is_reverted(row: sqlite3.Row) -> bool:
    if "state" not in row.keys():
        return False
    return row["state"] == "reverted"


def _is_omitted(row: sqlite3.Row) -> bool:
    """Vetoed and reverted strokes stay in the ledger and off the canvas."""
    return _is_vetoed(row) or _is_reverted(row)


def _canvas_rev(day: sqlite3.Row | None) -> int:
    if day is None or "canvas_rev" not in day.keys() or day["canvas_rev"] is None:
        return 0
    return int(day["canvas_rev"])


def _png_url(day: sqlite3.Row | None, scale: int) -> str | None:
    if day is None or day["canvas"] is None:
        return None
    url = f"/watch/canvas.png?day_id={day['id']}&scale={scale}"
    rev = _canvas_rev(day)
    if rev:
        url += f"&v={rev}"
    return url


def _crop_url(day: sqlite3.Row | None, focus: dict[str, int] | None) -> str | None:
    if day is None or not focus or day["canvas"] is None:
        return None
    url = (
        f"/watch/crop.png?day_id={day['id']}&x={focus['x']}&y={focus['y']}"
        f"&w={focus['w']}&h={focus['h']}&scale=4"
    )
    rev = _canvas_rev(day)
    if rev:
        url += f"&v={rev}"
    return url


def _iter_pixels(raw: str | None) -> list[dict[str, int]]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    pixels = []
    for pixel in payload:
        if not isinstance(pixel, dict):
            continue
        x, y, color = pixel.get("x"), pixel.get("y"), pixel.get("color")
        if type(x) is not int or type(y) is not int or type(color) is not int:
            continue
        if 0 <= x < GRID and 0 <= y < GRID and 0 <= color <= 255:
            pixels.append({"x": x, "y": y, "color": color})
    return pixels


def _replay_actions(base: bytes, rows: list[sqlite3.Row]) -> bytes:
    canvas = bytearray(base)
    for row in rows:
        for pixel in _iter_pixels(row["pixels_json"]):
            canvas[pixel["y"] * GRID + pixel["x"]] = pixel["color"]
    return bytes(canvas)


def _parse_veto_reason(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ApiError(400, "bad_veto", "reason must be a string.")
    text = raw.strip()
    if not text:
        return None
    if len(text) > VETO_REASON_MAX:
        raise ApiError(400, "bad_veto", f"reason must be {VETO_REASON_MAX} characters or fewer.")
    return text


def _parse_director_note(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ApiError(400, "bad_note", "Note must be a string.")
    text = raw.strip()
    if not text or len(text) > DIRECTOR_NOTE_MAX:
        raise ApiError(400, "bad_note", f"Note must be 1–{DIRECTOR_NOTE_MAX} characters.")
    return text


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
    if not isinstance(brief, str) or not brief.strip() or len(brief) > BRIEF_MAX:
        raise ApiError(400, "bad_design", f"Brief must be 1–{BRIEF_MAX} characters.")
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
