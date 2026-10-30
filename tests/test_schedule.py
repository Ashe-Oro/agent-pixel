from datetime import date

from app.schedule import (
    SLICE_COUNT,
    artist_on_slice,
    day_bounds,
    label_hhmm,
    parse_ny_local,
    position,
    rotate,
    round_on_slice,
    slice_window,
    walk_session,
)


def test_twelve_slices_cover_eight_to_eight_thirty_six():
    day = date(2026, 10, 28)
    start, lock = day_bounds(day)
    assert label_hhmm(start) == "08:00"
    assert label_hhmm(lock) == "08:36"
    first_begin, first_end = slice_window(day, 0)
    last_begin, last_end = slice_window(day, 11)
    assert first_begin == start
    assert label_hhmm(first_end) == "08:03"
    assert label_hhmm(last_begin) == "08:33"
    assert last_end == lock
    order = ["claude", "grok-artist", "openai"]
    sequence = [artist_on_slice(order, index) for index in range(12)]
    assert sequence == order * 4
    assert [round_on_slice(index) for index in range(12)] == [1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4]


def test_position_boundaries():
    assert position(parse_ny_local("2026-10-28T07:59:59")) == ("before", None)
    assert position(parse_ny_local("2026-10-28T08:00:00")) == ("during", 0)
    assert position(parse_ny_local("2026-10-28T08:02:59")) == ("during", 0)
    assert position(parse_ny_local("2026-10-28T08:03:00")) == ("during", 1)
    assert position(parse_ny_local("2026-10-28T08:35:59")) == ("during", 11)
    assert position(parse_ny_local("2026-10-28T08:36:00")) == ("after", None)


def test_review_gate_holds_until_a_note_or_the_timeout():
    start = parse_ny_local("2026-10-28T08:00:00")
    acted = {0: parse_ny_local("2026-10-28T08:00:10")}
    held = walk_session(start, acted, parse_ny_local("2026-10-28T08:00:20"), gate_seconds=75)
    assert held.phase == "holding"
    assert held.holding_index == 1
    assert held.index is None
    assert len(held.slices) == SLICE_COUNT
    opened = walk_session(start, acted, parse_ny_local("2026-10-28T08:01:25"), gate_seconds=75)
    assert opened.phase == "during"
    assert opened.index == 1
    noted = walk_session(
        start,
        acted,
        parse_ny_local("2026-10-28T08:00:40"),
        gate_seconds=75,
        note_at={1: parse_ny_local("2026-10-28T08:00:30")},
    )
    assert noted.phase == "during"
    assert noted.index == 1
    early = walk_session(
        start,
        acted,
        parse_ny_local("2026-10-28T08:00:10"),
        gate_seconds=75,
        note_at={1: parse_ny_local("2026-10-28T08:00:05")},
    )
    assert early.phase == "during"
    assert early.index == 1
    immediate = walk_session(start, acted, parse_ny_local("2026-10-28T08:00:10"), gate_seconds=0)
    assert immediate.phase == "during"
    assert immediate.index == 1


def test_rotation_by_day_index():
    order = ["claude", "grok-artist", "openai"]
    assert rotate(order, 1, True) == order
    assert rotate(order, 2, True) == ["grok-artist", "openai", "claude"]
    assert rotate(order, 3, True) == ["openai", "claude", "grok-artist"]
    assert rotate(order, 2, False) == order
