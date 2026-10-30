"""Wall-clock schedule for an Agent Pixel morning.

America/New_York. The session opens at 08:00. Three artists, each painting
``tile_rounds()`` times (``TILE_ROUNDS``, default 6, so 18 slices). Each
slice stays open for at most ``SLICE_SECONDS`` (default 180, three minutes)
measured from when that slice opened. An on-time paint or pass closes the
slice immediately and opens the next one. A slice with no action closes when
its three minutes run out and is missed. With the default, eighteen timeouts
end the morning 54 minutes after it opened (08:54 ET); early acts finish it
sooner. A canvas stores its own slice count, so older 12-slice mornings keep
that clock.
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
# TILE_ROUNDS is read when a canvas is created. 1..24, default 6.
DEFAULT_ROUNDS = 6
MIN_ROUNDS = 1
MAX_ROUNDS = 24
ARTISTS = ("claude", "grok-artist", "openai")
# Canvases opened before TILE_ROUNDS existed ran four rounds, twelve slices.
LEGACY_SLICE_COUNT = 4 * len(ARTISTS)
DISPLAY = {
    "design": "Design",
    "ross": "Ross",
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


def design_wait_seconds() -> int:
    """How long a new canvas waits for Ross's design. ``TILE_DESIGN_WAIT_SECONDS``.

    Default 300 (five minutes). ``0`` applies the fallback design on the next check.
    """
    return env_seconds("TILE_DESIGN_WAIT_SECONDS", 300)


def tile_rounds() -> int:
    """Rounds per artist for a canvas created now. ``TILE_ROUNDS``.

    Parsed like the other non-negative integer settings. Default 6.
    Blank, non-integer, negative, zero, or a value outside 1..24 uses 6.
    Zero is not a round count: a morning with no slices cannot open.
    """
    value = env_seconds("TILE_ROUNDS", DEFAULT_ROUNDS)
    if value < MIN_ROUNDS or value > MAX_ROUNDS:
        return DEFAULT_ROUNDS
    return value


def configured_slice_count() -> int:
    """Slices on a canvas created with the current ``TILE_ROUNDS``."""
    return tile_rounds() * len(ARTISTS)


def max_slice_count() -> int:
    """Largest slice count ``TILE_ROUNDS`` can store."""
    return MAX_ROUNDS * len(ARTISTS)


def resolve_slice_count(slice_count: int | None) -> int:
    """Use ``slice_count`` when it is a real canvas length, else the setting."""
    if slice_count is None:
        return configured_slice_count()
    if slice_count < 1:
        return LEGACY_SLICE_COUNT
    return slice_count


def rounds_in(slice_count: int) -> int:
    """Highest 1-based round number on a canvas with ``slice_count`` slices."""
    if slice_count < 1:
        return tile_rounds()
    return (slice_count - 1) // len(ARTISTS) + 1


def session_minutes(slice_count: int | None = None) -> int:
    total = resolve_slice_count(slice_count)
    return (total * SLICE_SECONDS) // 60


def session_length_phrase(slice_count: int | None = None) -> str:
    """How long every slice timing out lasts, for status copy."""
    total = resolve_slice_count(slice_count)
    seconds = total * SLICE_SECONDS
    if seconds % 60 == 0:
        minutes = seconds // 60
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit}"
    return f"{seconds} seconds"


def ny_date(now: datetime) -> str:
    return now.astimezone(NY).date().isoformat()


def day_bounds(d: date, slice_count: int | None = None) -> tuple[datetime, datetime]:
    """08:00 ET through the latest possible lock, when every slice times out.

    ``slice_count`` is that canvas's stored length. Omit it for a new morning
    under the current ``TILE_ROUNDS``.
    """
    start = datetime(d.year, d.month, d.day, OPEN_HOUR, 0, 0, tzinfo=NY)
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
) -> datetime:
    """When slice ``index`` may open after ``previous_close``.

    A note posted at or before the previous slice closed skips the wait.
    A note posted during the wait opens the slice at that moment.
    Otherwise the slice opens ``gate`` seconds after the previous close.
    """
    timeout = previous_close + timedelta(seconds=gate)
    noted = notes.get(index)
    if noted is None:
        return timeout
    noted_local = noted.astimezone(NY)
    if noted_local <= previous_close:
        return previous_close
    if noted_local < timeout:
        return noted_local
    return timeout


def walk_session(
    opens_at: datetime,
    acted_at: dict[int, datetime] | None,
    now: datetime,
    gate_seconds: int = 0,
    note_at: dict[int, datetime] | None = None,
    slice_count: int | None = None,
) -> SessionClock:
    """Where a session is, given when each slice opened and which ones were used.

    ``acted_at`` maps a slice index to the time its paint or pass was stored.
    A used slice closes at that time. With ``gate_seconds`` of 0 the next slice
    opens then. A positive gate holds the next real slice until Ross's note
    for that slice or until the gate elapses. An unused slice closes
    ``SLICE_SECONDS`` after it opened. A done-skip recorded before a slice
    would open closes in zero time and does not add a review wait.
    """
    start = opens_at.astimezone(NY)
    local = now.astimezone(NY)
    actions = acted_at or {}
    notes = note_at or {}
    gate = gate_seconds if gate_seconds > 0 else 0
    total = resolve_slice_count(slice_count)
    cursor = start
    marks: list[SliceMark] = []
    live_index: int | None = None
    live_deadline: datetime | None = None
    parked = False
    gate_hold = False
    holding_index: int | None = None
    hold_until: datetime | None = None
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
            release = _gate_release(cursor, index, gate, notes)
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
            marks.append(SliceMark(index, cursor, deadline, cursor, True, False))
            continue
        if raw_local is not None:
            close = raw_local if raw_local < deadline else deadline
            if close < cursor:
                close = cursor
            marks.append(SliceMark(index, cursor, deadline, close, True, False))
            cursor = close
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
        marks.append(SliceMark(index, cursor, deadline, deadline, False, True))
        cursor = deadline
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
    """Calendar morning with no early acts: 08:00, then one slice every 3 minutes."""
    local = now.astimezone(NY)
    total = resolve_slice_count(slice_count)
    start, _ = day_bounds(local.date(), total)
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
    """Wall-clock lock when an 08:00 canvas times out every slice and the gate is off."""
    start = datetime(2000, 1, 1, OPEN_HOUR, 0, 0, tzinfo=NY)
    total = resolve_slice_count(slice_count)
    lock = start + timedelta(seconds=total * SLICE_SECONDS)
    return label_hhmm(lock)


def slice_index(now: datetime) -> int | None:
    phase, index = position(now)
    if phase != "during":
        return None
    return index


def artist_on_slice(order: list[str], index: int) -> str:
    if len(order) != len(ARTISTS):
        raise ValueError("roster must be the three artists")
    return order[index % len(order)]


def round_on_slice(index: int) -> int:
    return index // len(ARTISTS) + 1


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
