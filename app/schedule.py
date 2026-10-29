"""Wall-clock schedule for an Agent Pixel morning.

America/New_York. The session opens at 08:00. Twelve slices, four rounds,
three artists. Each slice stays open for at most ``SLICE_SECONDS`` (default
180, three minutes) measured from when that slice opened. An on-time paint
or pass closes the slice immediately and opens the next one. A slice with no
action closes when its three minutes run out and is missed. Twelve timeouts
end the morning 36 minutes after it opened; early acts finish it sooner.
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
ROUNDS = 4
ARTISTS = ("claude", "grok-artist", "openai")
DISPLAY = {
    "design": "Design",
    "claude": "Claude",
    "grok-artist": "Grok Artist",
    "openai": "OpenAI",
}
SLICE_COUNT = ROUNDS * len(ARTISTS)  # 12
SESSION_SECONDS = SLICE_COUNT * SLICE_SECONDS
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


def ny_date(now: datetime) -> str:
    return now.astimezone(NY).date().isoformat()


def day_bounds(d: date) -> tuple[datetime, datetime]:
    """08:00 ET through the latest possible lock, when every slice times out."""
    start = datetime(d.year, d.month, d.day, OPEN_HOUR, 0, 0, tzinfo=NY)
    lock = start + timedelta(seconds=SESSION_SECONDS)
    return start, lock


def slice_window(d: date, index: int) -> tuple[datetime, datetime]:
    """Projected window when nobody acts early. Real windows come from ``walk_session``."""
    if index < 0 or index >= SLICE_COUNT:
        raise ValueError(f"slice index {index} out of range")
    start, _ = day_bounds(d)
    begin = start + timedelta(seconds=SLICE_SECONDS * index)
    end = begin + timedelta(seconds=SLICE_SECONDS)
    return begin, end


def walk_session(
    opens_at: datetime,
    acted_at: dict[int, datetime] | None,
    now: datetime,
) -> SessionClock:
    """Where a session is, given when each slice opened and which ones were used.

    ``acted_at`` maps a slice index to the time its paint or pass was stored.
    A used slice closes at that time, and the next slice opens then. An unused
    slice closes ``SLICE_SECONDS`` after it opened.
    """
    start = opens_at.astimezone(NY)
    local = now.astimezone(NY)
    actions = acted_at or {}
    cursor = start
    marks: list[SliceMark] = []
    live_index: int | None = None
    live_deadline: datetime | None = None
    parked = False
    for index in range(SLICE_COUNT):
        deadline = cursor + timedelta(seconds=SLICE_SECONDS)
        raw = actions.get(index)
        if raw is not None:
            acted_local = raw.astimezone(NY)
            close = acted_local if acted_local < deadline else deadline
            if close < cursor:
                close = cursor
            marks.append(SliceMark(index, cursor, deadline, close, True, False))
            cursor = close
            continue
        if not parked and local < cursor:
            parked = True
            marks.append(SliceMark(index, cursor, deadline, deadline, False, False))
            cursor = deadline
            continue
        if not parked and local < deadline:
            parked = True
            live_index = index
            live_deadline = deadline
            marks.append(SliceMark(index, cursor, deadline, deadline, False, False))
            cursor = deadline
            continue
        if not parked:
            marks.append(SliceMark(index, cursor, deadline, deadline, False, True))
            cursor = deadline
            continue
        marks.append(SliceMark(index, cursor, deadline, deadline, False, False))
        cursor = deadline
    lock_at = cursor
    if local < start:
        phase, index = "before", None
    elif live_index is None:
        phase, index = "after", None
    else:
        phase, index = "during", live_index
    return SessionClock(phase, index, live_deadline, lock_at, tuple(marks))


def position(now: datetime) -> tuple[str, int | None]:
    """Calendar morning with no early acts: 08:00, then one slice every 3 minutes."""
    local = now.astimezone(NY)
    start, _ = day_bounds(local.date())
    clock = walk_session(start, {}, now)
    return clock.phase, clock.index


def session_position(
    opens_at: datetime,
    now: datetime,
    acted_at: dict[int, datetime] | None = None,
) -> tuple[str, int | None]:
    clock = walk_session(opens_at, acted_at, now)
    return clock.phase, clock.index


def session_slice_window(opens_at: datetime, index: int) -> tuple[datetime, datetime]:
    """Projected window from ``opens_at`` when no earlier slice closed early."""
    if index < 0 or index >= SLICE_COUNT:
        raise ValueError(f"slice index {index} out of range")
    start = opens_at.astimezone(NY)
    begin = start + timedelta(seconds=SLICE_SECONDS * index)
    end = begin + timedelta(seconds=SLICE_SECONDS)
    return begin, end


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
