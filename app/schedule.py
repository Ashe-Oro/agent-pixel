"""Wall-clock schedule for an Agent Pixel morning.

America/New_York. The session opens at 08:00. A new canvas is painted by Ross
alone until he finishes or ``TILE_SOLO_WINDOW_SECONDS`` (default 7200, two
hours) elapse from the moment turn 1 opens. Each turn stays open for at most
``SLICE_SECONDS`` (default 180) and never past that window. An on-time paint
or pass closes the turn and opens the next one immediately. A canvas stores
its own slice count, so older mornings keep the turn count they opened with.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

OPEN_HOUR = 8
# One place for the slice length. TILE_SLICE_SECONDS overrides it; 180 is 3 minutes.
SLICE_SECONDS = int(os.environ.get("TILE_SLICE_SECONDS", "180"))
# TILE_ROUNDS is the retired three-artist round count.
DEFAULT_ROUNDS = 6
MIN_ROUNDS = 1
MAX_ROUNDS = 24
# Stored slice_count 0 means Ross paints with no turn cap, only a time window.
UNLIMITED_SLICE_COUNT = 0
# How long a new solo canvas stays open after turn 1 starts. Default 2 hours.
DEFAULT_SOLO_WINDOW = 7200
MIN_SOLO_WINDOW = 1
MAX_SOLO_WINDOW = 7 * 24 * 3600
# Parser ceiling for slice indices on a fast solo canvas. Not a turn cap.
MAX_ADDRESSED_TURNS = 5000
# Unique cells Ross may place in one turn, including overwrites of his own pixels.
DEFAULT_TURN_PIXELS = 16384
ARTISTS = ("claude", "grok-artist", "openai")
SOLO_PAINTER = "ross"
# Canvases opened before TILE_ROUNDS existed ran four rounds, twelve slices.
LEGACY_SLICE_COUNT = 4 * len(ARTISTS)
DISPLAY = {
    "design": "Design",
    # Public label only. The seat id stays "ross"; nothing here is stored.
    "ross": "Artist",
    "claude": "Claude",
    "grok-artist": "Grok Artist",
    "openai": "OpenAI",
}
# None means unlimited. A paint in an open slice may cover every cell of the
# grid. Settings still store 0 for these so older 64 / 256 rows can be cleared.
PIXELS_PER_TURN = None
PIXELS_PER_ARTIST_DAY = None
GRID = 256

DEFAULT_ROSTER = list(ARTISTS)


@dataclass(frozen=True)
class SliceMark:
    index: int
    opens: datetime
    deadline: datetime
    closes: datetime
    acted: bool
    missed: bool
    # A veto reopens this index. ``opens``/``deadline`` are the redo window.
    redo: bool = False
    vetoed: bool = False


@dataclass(frozen=True)
class RedoWindow:
    """One veto of a slice. ``opens`` is when the redo started.

    ``acted_at`` is when the redo paint or pass was stored. None means the
    redo has not been used yet. ``seconds`` is the window length captured
    when Ross vetoed, so a later settings change does not move it.
    """

    opens: datetime
    acted_at: datetime | None
    seconds: int


@dataclass(frozen=True)
class SessionClock:
    phase: str
    index: int | None
    deadline: datetime | None
    lock_at: datetime
    slices: tuple[SliceMark, ...]
    # When phase is "holding", the next slice is waiting on the review gate.
    hold_until: datetime | None = None
    holding_index: int | None = None


def env_seconds(name: str, default: int) -> int:
    """Non-negative integer from the environment. Blank or invalid uses ``default``.

    Zero is a real value: it disables the wait that uses this setting.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < 0:
        return default
    return value


def note_gate_seconds() -> int:
    """How long the next slice waits for Ross's note. ``TILE_NOTE_GATE_SECONDS``.

    Default 75. ``0`` opens the next slice as soon as the previous one closes.
    """
    return env_seconds("TILE_NOTE_GATE_SECONDS", 75)


def redo_seconds() -> int:
    """How long a vetoed slice reopens for the same artist. ``TILE_REDO_SECONDS``.

    Default 120 (two minutes). ``0`` closes the redo as soon as the veto lands,
    and the review gate for the next slice starts then.
    """
    return env_seconds("TILE_REDO_SECONDS", 120)


def env_fraction(name: str, default: float) -> float:
    """Fraction from the environment, from 0 through 1. Blank or invalid uses ``default``."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < 0 or value > 1:
        return default
    return value


def redo_max_fraction() -> float:
    """Share of the canvas one redo paint may change. ``TILE_REDO_MAX_FRACTION``.

    Default 0.35. A redo that changes more than this is reverted.
    """
    return env_fraction("TILE_REDO_MAX_FRACTION", 0.35)


def redo_max_dominant() -> float:
    """Largest single-color share of a redo's changed cells. ``TILE_REDO_MAX_DOMINANT``.

    Default 0.6. Applied only when the redo also changes more than 5% of the canvas.
    """
    return env_fraction("TILE_REDO_MAX_DOMINANT", 0.6)


# The one-color rule stays quiet for a small touch-up.
REDO_DOMINANT_MIN_SHARE = 0.05


def redo_overfill(before: bytes, collapsed: list[tuple[int, int, int]]) -> str | None:
    """Why a redo paint should be reverted, or None when it may stay.

    ``collapsed`` is unique cells, last color wins, in paint order. A cell
    counts only when the new color differs from the canvas the redo started on.
    """
    total = GRID * GRID
    if total < 1 or len(before) != total:
        return None
    changed: list[int] = []
    for x, y, color in collapsed:
        if before[y * GRID + x] != color:
            changed.append(color)
    if not changed:
        return None
    share = len(changed) / total
    fraction = redo_max_fraction()
    if share > fraction:
        return (
            f"That redo changed {len(changed)} cells, {round(100 * share)}% of the canvas, "
            f"over the {round(100 * fraction)}% limit. The paint was reverted and the review gate is open."
        )
    if share > REDO_DOMINANT_MIN_SHARE:
        counts: dict[int, int] = {}
        for color in changed:
            counts[color] = counts.get(color, 0) + 1
        dominant = max(counts.values()) / len(changed)
        cap = redo_max_dominant()
        if dominant > cap:
            return (
                f"That redo changed {len(changed)} cells and one color was "
                f"{round(100 * dominant)}% of them, over the {round(100 * cap)}% limit. "
                f"The paint was reverted and the review gate is open."
            )
    return None


def design_wait_seconds() -> int:
    """How long a new canvas waits for Ross's design. ``TILE_DESIGN_WAIT_SECONDS``.

    Default 900 (fifteen minutes), so he can study references before the
    fallback palette. ``0`` applies the fallback design on the next check.
    """
    return env_seconds("TILE_DESIGN_WAIT_SECONDS", 900)


def tile_rounds() -> int:
    """Retired rounds-per-artist setting. ``TILE_ROUNDS``.

    Parsed like the other non-negative integer settings. Default 6.
    Blank, non-integer, negative, zero, or a value outside 1..24 uses 6.
    New canvases do not use this. Older rows keep the slice count they stored.
    """
    value = env_seconds("TILE_ROUNDS", DEFAULT_ROUNDS)
    if value < MIN_ROUNDS or value > MAX_ROUNDS:
        return DEFAULT_ROUNDS
    return value


def solo_window_seconds() -> int:
    """How long a new solo canvas stays open after turn 1. ``TILE_SOLO_WINDOW_SECONDS``.

    Default 7200 (two hours). Measured from the moment turn 1 opens, which is
    when the design is accepted. Blank, zero, or a value outside 1..604800
    uses 7200. Stored on the canvas so a later change does not move it.
    """
    value = env_seconds("TILE_SOLO_WINDOW_SECONDS", DEFAULT_SOLO_WINDOW)
    if value < MIN_SOLO_WINDOW or value > MAX_SOLO_WINDOW:
        return DEFAULT_SOLO_WINDOW
    return value


def turn_pixel_budget() -> int:
    """Unique cells one Ross turn may place. ``TILE_PIXELS_PER_TURN``.

    Default 16384, a quarter of the 256×256 grid, so one turn can lay a large
    region or correct one. Blank, zero, or a value outside 1..65536 uses 16384.
    Overwrites of cells he already painted count as cells in that stroke.
    """
    value = env_seconds("TILE_PIXELS_PER_TURN", DEFAULT_TURN_PIXELS)
    if value < 1 or value > GRID * GRID:
        return DEFAULT_TURN_PIXELS
    return value


def configured_slice_count() -> int:
    """Slice count stored on a canvas created now.

    ``0`` means unlimited turns. The canvas is capped by ``TILE_SOLO_WINDOW_SECONDS``.
    """
    return UNLIMITED_SLICE_COUNT


def is_unlimited_count(slice_count: int | None) -> bool:
    """True when ``slice_count`` is the unlimited-turn sentinel."""
    if slice_count is None:
        return configured_slice_count() == UNLIMITED_SLICE_COUNT
    return slice_count == UNLIMITED_SLICE_COUNT


def max_slice_count() -> int:
    """Largest slice index the public slice query will parse.

    Finite mornings stay small. A solo canvas can open a new turn every act,
    so the parser allows several thousand indices. The canvas itself still
    rejects an index that never opened.
    """
    return max(MAX_ROUNDS * len(ARTISTS), MAX_ADDRESSED_TURNS)


def is_solo_order(order: list[str]) -> bool:
    """True when one seat paints every slice. New canvases store ``["ross"]``."""
    return len(order) == 1


def seats_for(order: list[str] | None) -> int:
    """How many painters share the slices. Solo is 1. A roster morning is 3."""
    if order and is_solo_order(order):
        return 1
    return len(ARTISTS)


def resolve_slice_count(slice_count: int | None) -> int:
    """Use ``slice_count`` when it is a real canvas length, else the setting.

    ``0`` stays ``0`` (unlimited turns). A missing count uses the setting for
    a canvas created now, which is also ``0``.
    """
    if slice_count is None:
        return configured_slice_count()
    if slice_count == UNLIMITED_SLICE_COUNT:
        return UNLIMITED_SLICE_COUNT
    if slice_count < 1:
        return LEGACY_SLICE_COUNT
    return slice_count


def rounds_in(slice_count: int, seats: int | None = None) -> int:
    """Highest 1-based round number on a canvas with ``slice_count`` slices.

    ``seats`` is 1 on a finite solo canvas, so each turn is its own round.
    The retired roster uses three seats. Unlimited canvases have no round count.
    """
    size = len(ARTISTS) if not seats else seats
    if size < 1:
        size = 1
    if slice_count < 1:
        return tile_rounds()
    return (slice_count - 1) // size + 1


def session_minutes(slice_count: int | None = None) -> int:
    if is_unlimited_count(slice_count):
        return solo_window_seconds() // 60
    total = resolve_slice_count(slice_count)
    return (total * SLICE_SECONDS) // 60


def session_length_phrase(slice_count: int | None = None) -> str:
    """How long a full session lasts, for status copy."""
    if is_unlimited_count(slice_count):
        seconds = solo_window_seconds()
    else:
        seconds = resolve_slice_count(slice_count) * SLICE_SECONDS
    if seconds % 3600 == 0:
        hours = seconds // 3600
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit}"
    if seconds % 60 == 0:
        minutes = seconds // 60
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit}"
    return f"{seconds} seconds"


def ny_date(now: datetime) -> str:
    return now.astimezone(NY).date().isoformat()


def day_bounds(d: date, slice_count: int | None = None) -> tuple[datetime, datetime]:
    """08:00 ET through the latest moment that morning can still be opened.

    A finite canvas locks when its stored slices time out. An unlimited solo
    canvas can still be opened any time after 08:00 on that New York date.
    Its paint window is separate: two hours from turn 1, not from 08:00.
    """
    start = datetime(d.year, d.month, d.day, OPEN_HOUR, 0, 0, tzinfo=NY)
    if is_unlimited_count(slice_count):
        return start, start + timedelta(days=1)
    lock = start + timedelta(seconds=resolve_slice_count(slice_count) * SLICE_SECONDS)
    return start, lock


def slice_window(d: date, index: int, slice_count: int | None = None) -> tuple[datetime, datetime]:
    """Projected window when nobody acts early. Real windows come from ``walk_session``."""
    total = resolve_slice_count(slice_count)
    if index < 0 or index >= total:
        raise ValueError(f"slice index {index} out of range")
    start, _ = day_bounds(d, total)
    begin = start + timedelta(seconds=SLICE_SECONDS * index)
    end = begin + timedelta(seconds=SLICE_SECONDS)
    return begin, end


def _gate_release(
    previous_close: datetime,
    index: int,
    gate: int,
    notes: dict[int, datetime],
    not_before: datetime | None = None,
) -> datetime:
    """When slice ``index`` may open after ``previous_close``.

    A note posted at or before the previous slice closed skips the wait.
    A note posted during the wait opens the slice at that moment.
    Otherwise the slice opens ``gate`` seconds after the previous close.
    ``not_before`` drops notes posted earlier than that instant. A veto uses
    it so a note about the removed attempt does not skip the gate that runs
    after the redo.
    """
    timeout = previous_close + timedelta(seconds=gate)
    noted = notes.get(index)
    if noted is not None and not_before is not None and noted.astimezone(NY) < not_before.astimezone(NY):
        noted = None
    if noted is None:
        return timeout
    noted_local = noted.astimezone(NY)
    if noted_local <= previous_close:
        return previous_close
    if noted_local < timeout:
        return noted_local
    return timeout


def _apply_redo(
    mark: SliceMark,
    close: datetime,
    local: datetime,
    redos: dict[int, RedoWindow] | None,
) -> tuple[SliceMark, datetime, bool]:
    """Stretch a closed slice by its redo window.

    Returns the mark to keep, the cursor for the following slice, and whether
    the redo is the live slice. No redo leaves the mark and ``close`` alone.
    """
    if not redos or mark.index not in redos:
        return mark, close, False
    redo = redos[mark.index]
    start = redo.opens.astimezone(NY)
    if start < close:
        start = close
    acted = redo.acted_at.astimezone(NY) if redo.acted_at is not None else None
    # A clock pinned before the veto still shows the original close.
    if local < start and (acted is None or acted > local):
        return mark, close, False
    span = redo.seconds if redo.seconds > 0 else 0
    deadline = start + timedelta(seconds=span)
    if acted is not None and acted >= start:
        redo_close = acted if acted < deadline else deadline
        return (
            SliceMark(mark.index, start, deadline, redo_close, True, False, True, True),
            redo_close,
            False,
        )
    if local < deadline:
        return (
            SliceMark(mark.index, start, deadline, deadline, False, False, True, True),
            deadline,
            True,
        )
    return (
        SliceMark(mark.index, start, deadline, deadline, False, True, True, True),
        deadline,
        False,
    )


def walk_session(
    opens_at: datetime,
    acted_at: dict[int, datetime] | None,
    now: datetime,
    gate_seconds: int = 0,
    note_at: dict[int, datetime] | None = None,
    slice_count: int | None = None,
    redos: dict[int, RedoWindow] | None = None,
) -> SessionClock:
    """Where a session is, given when each slice opened and which ones were used.

    ``acted_at`` maps a slice index to the time its paint or pass was stored.
    A used slice closes at that time. With ``gate_seconds`` of 0 the next slice
    opens then. A positive gate holds the next real slice until Ross's note
    for that slice or until the gate elapses. An unused slice closes
    ``SLICE_SECONDS`` after it opened. A done-skip recorded before a slice
    would open closes in zero time and does not add a review wait.

    ``redos`` reopens a closed slice for the same artist before that gate.
    The following slices, including their review gates and the lock, start
    after the redo closes. Notes posted before the veto do not release the
    gate that follows the redo.
    """
    start = opens_at.astimezone(NY)
    local = now.astimezone(NY)
    actions = acted_at or {}
    notes = note_at or {}
    redo_windows = redos or {}
    gate = gate_seconds if gate_seconds > 0 else 0
    # Omitted length is the retired 18-slice roster clock. Callers that mean
    # a stored canvas pass that count, including 0 for an unlimited solo canvas.
    if slice_count is None:
        total = 6 * len(ARTISTS)
    else:
        total = resolve_slice_count(slice_count)
    cursor = start
    marks: list[SliceMark] = []
    live_index: int | None = None
    live_deadline: datetime | None = None
    parked = False
    gate_hold = False
    holding_index: int | None = None
    hold_until: datetime | None = None

    def finish_closed(mark: SliceMark, close: datetime) -> None:
        nonlocal cursor, parked, live_index, live_deadline
        revised, cursor, redo_live = _apply_redo(mark, close, local, redo_windows)
        marks.append(revised)
        if redo_live:
            parked = True
            live_index = revised.index
            live_deadline = revised.deadline

    for index in range(total):
        raw = actions.get(index)
        raw_local = raw.astimezone(NY) if raw is not None else None
        if parked:
            if raw_local is not None and raw_local <= cursor:
                deadline = cursor + timedelta(seconds=SLICE_SECONDS)
                marks.append(SliceMark(index, cursor, deadline, cursor, True, False))
                continue
            deadline = cursor + timedelta(seconds=SLICE_SECONDS)
            marks.append(SliceMark(index, cursor, deadline, deadline, False, False))
            cursor = deadline
            continue
        instant = raw_local is not None and raw_local <= cursor
        if not instant and index > 0 and gate:
            not_before = None
            previous = redo_windows.get(index - 1)
            if previous is not None:
                not_before = previous.opens
            release = _gate_release(cursor, index, gate, notes, not_before)
            if local < release:
                parked = True
                gate_hold = True
                holding_index = index
                hold_until = release
                deadline = release + timedelta(seconds=SLICE_SECONDS)
                marks.append(SliceMark(index, release, deadline, deadline, False, False))
                cursor = deadline
                continue
            if release > cursor:
                cursor = release
        deadline = cursor + timedelta(seconds=SLICE_SECONDS)
        if instant:
            finish_closed(SliceMark(index, cursor, deadline, cursor, True, False), cursor)
            continue
        if raw_local is not None:
            close = raw_local if raw_local < deadline else deadline
            if close < cursor:
                close = cursor
            finish_closed(SliceMark(index, cursor, deadline, close, True, False), close)
            continue
        if local < cursor:
            parked = True
            marks.append(SliceMark(index, cursor, deadline, deadline, False, False))
            cursor = deadline
            continue
        if local < deadline:
            parked = True
            live_index = index
            live_deadline = deadline
            marks.append(SliceMark(index, cursor, deadline, deadline, False, False))
            cursor = deadline
            continue
        finish_closed(SliceMark(index, cursor, deadline, deadline, False, True), deadline)
    lock_at = cursor
    if local < start:
        phase, index = "before", None
    elif gate_hold and live_index is None:
        phase, index = "holding", None
    elif live_index is None:
        phase, index = "after", None
    else:
        phase, index = "during", live_index
    return SessionClock(
        phase,
        index,
        live_deadline,
        lock_at,
        tuple(marks),
        hold_until,
        holding_index,
    )


def position(now: datetime, slice_count: int | None = None) -> tuple[str, int | None]:
    """Where a calendar morning is when no canvas row is driving the clock.

    An unlimited morning can still be opened any time after 08:00 that date.
    A finite morning follows its stored slice clock.
    """
    local = now.astimezone(NY)
    total = resolve_slice_count(slice_count)
    start, lock = day_bounds(local.date(), total)
    if is_unlimited_count(total):
        if local < start:
            return "before", None
        if local >= lock:
            return "after", None
        return "during", None
    clock = walk_session(start, {}, now, slice_count=total)
    return clock.phase, clock.index


def session_position(
    opens_at: datetime,
    now: datetime,
    acted_at: dict[int, datetime] | None = None,
    slice_count: int | None = None,
) -> tuple[str, int | None]:
    clock = walk_session(opens_at, acted_at, now, slice_count=slice_count)
    return clock.phase, clock.index


def session_slice_window(
    opens_at: datetime,
    index: int,
    slice_count: int | None = None,
) -> tuple[datetime, datetime]:
    """Projected window from ``opens_at`` when no earlier slice closed early."""
    total = resolve_slice_count(slice_count)
    if index < 0 or index >= total:
        raise ValueError(f"slice index {index} out of range")
    start = opens_at.astimezone(NY)
    begin = start + timedelta(seconds=SLICE_SECONDS * index)
    end = begin + timedelta(seconds=SLICE_SECONDS)
    return begin, end


def lock_label(slice_count: int | None = None) -> str:
    """Wall-clock lock when painting starts at 08:00 and runs to its cap.

    Unlimited solo canvases show 08:00 plus ``TILE_SOLO_WINDOW_SECONDS``
    (10:00 ET at the default). Finite canvases add one slice length per turn.
    """
    start = datetime(2000, 1, 1, OPEN_HOUR, 0, 0, tzinfo=NY)
    if is_unlimited_count(slice_count):
        return label_hhmm(start + timedelta(seconds=solo_window_seconds()))
    total = resolve_slice_count(slice_count)
    lock = start + timedelta(seconds=total * SLICE_SECONDS)
    return label_hhmm(lock)


def walk_timed_solo(
    opens_at: datetime,
    acted_at: dict[int, datetime] | None,
    now: datetime,
    window_end: datetime,
    done_index: int | None = None,
) -> SessionClock:
    """Ross's open-ended turn clock, capped by ``window_end``.

    Turn 1 opens at ``opens_at``. Each turn lasts at most ``SLICE_SECONDS``
    and is clamped so it cannot run past ``window_end``. A paint or pass
    closes the turn at that instant and opens the next one. ``done_index``
    is the turn Ross finished on; nothing opens after it.
    """
    start = opens_at.astimezone(NY)
    end = window_end.astimezone(NY)
    if end < start:
        end = start
    local = now.astimezone(NY)
    actions = acted_at or {}
    cursor = start
    marks: list[SliceMark] = []
    live_index: int | None = None
    live_deadline: datetime | None = None
    index = 0
    guard = 0
    while cursor < end and guard < MAX_ADDRESSED_TURNS:
        guard += 1
        if done_index is not None and index > done_index:
            break
        remaining = (end - cursor).total_seconds()
        if remaining <= 0:
            break
        span = SLICE_SECONDS if remaining > SLICE_SECONDS else remaining
        deadline = cursor + timedelta(seconds=span)
        raw = actions.get(index)
        raw_local = raw.astimezone(NY) if raw is not None else None
        if raw_local is not None and raw_local <= cursor:
            marks.append(SliceMark(index, cursor, deadline, cursor, True, False))
            if done_index is not None and index == done_index:
                cursor = cursor
                break
            index += 1
            continue
        if raw_local is not None:
            close = raw_local if raw_local < deadline else deadline
            if close < cursor:
                close = cursor
            marks.append(SliceMark(index, cursor, deadline, close, True, False))
            cursor = close
            if done_index is not None and index == done_index:
                break
            index += 1
            continue
        if local < cursor:
            break
        if local < deadline:
            marks.append(SliceMark(index, cursor, deadline, deadline, False, False))
            live_index = index
            live_deadline = deadline
            break
        marks.append(SliceMark(index, cursor, deadline, deadline, False, True))
        cursor = deadline
        index += 1
    if done_index is not None:
        for mark in marks:
            if mark.index == done_index:
                cursor = mark.closes
                break
    if local < start:
        phase, shown = "before", None
    elif live_index is not None and (done_index is None or live_index <= done_index):
        phase, shown = "during", live_index
    else:
        phase, shown = "after", None
        live_deadline = None
    return SessionClock(phase, shown, live_deadline, end if done_index is None else cursor, tuple(marks))


def slice_index(now: datetime) -> int | None:
    phase, index = position(now)
    if phase != "during":
        return None
    return index


def artist_on_slice(order: list[str], index: int) -> str:
    if is_solo_order(order):
        return order[0]
    if len(order) != len(ARTISTS):
        raise ValueError("roster must be the three artists")
    return order[index % len(order)]


def round_on_slice(index: int, seats: int | None = None) -> int:
    size = len(ARTISTS) if not seats else seats
    if size < 1:
        size = 1
    return index // size + 1


def rotate(order: list[str], day_index: int, enabled: bool) -> list[str]:
    if not enabled:
        return list(order)
    shift = (day_index - 1) % len(order)
    return order[shift:] + order[:shift]


def label_hhmm(dt: datetime) -> str:
    local = dt.astimezone(NY)
    if local.second:
        return f"{local.hour:02d}:{local.minute:02d}:{local.second:02d}"
    return f"{local.hour:02d}:{local.minute:02d}"


def label_clock(now: datetime) -> str:
    local = now.astimezone(NY)
    hour = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return f"{local.strftime('%a %b')} {local.day}, {hour}:{local.minute:02d} {ampm} ET"


def parse_ny_local(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=NY)
    return parsed.astimezone(NY)
