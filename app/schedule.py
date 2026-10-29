"""Wall-clock schedule for an Agent Pixel morning.

America/New_York, 08:00–11:00. Twelve 15-minute slices, four rounds,
three artists round-robin. Design does not consume a slice: the first
slice starts at 08:00, and the canvas locks at 11:00.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

OPEN_HOUR = 8
LOCK_HOUR = 11
SLICE_MINUTES = 15
ROUNDS = 4
ARTISTS = ("claude", "grok-artist", "openai")
DISPLAY = {
    "design": "Design",
    "claude": "Claude",
    "grok-artist": "Grok Artist",
    "openai": "OpenAI",
}
SLICE_COUNT = ROUNDS * len(ARTISTS)  # 12
PIXELS_PER_TURN = 64
PIXELS_PER_ARTIST_DAY = 256  # 4 turns × 64
GRID = 256

DEFAULT_ROSTER = list(ARTISTS)


def ny_date(now: datetime) -> str:
    return now.astimezone(NY).date().isoformat()


def day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day, OPEN_HOUR, 0, 0, tzinfo=NY)
    lock = datetime(d.year, d.month, d.day, LOCK_HOUR, 0, 0, tzinfo=NY)
    return start, lock


def slice_window(d: date, index: int) -> tuple[datetime, datetime]:
    if index < 0 or index >= SLICE_COUNT:
        raise ValueError(f"slice index {index} out of range")
    start, _ = day_bounds(d)
    begin = start + timedelta(minutes=SLICE_MINUTES * index)
    end = begin + timedelta(minutes=SLICE_MINUTES)
    return begin, end


def position(now: datetime) -> tuple[str, int | None]:
    """Return ('before'|'during'|'after', slice index or None)."""
    local = now.astimezone(NY)
    start, lock = day_bounds(local.date())
    if local < start:
        return "before", None
    if local >= lock:
        return "after", None
    index = int((local - start).total_seconds() // (SLICE_MINUTES * 60))
    if index < 0 or index >= SLICE_COUNT:
        return "after", None
    return "during", index


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
