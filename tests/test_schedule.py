from datetime import date, timedelta

from app.schedule import (
    RedoWindow,
    artist_on_slice,
    configured_slice_count,
    day_bounds,
    label_hhmm,
    lock_label,
    parse_ny_local,
    position,
    redo_seconds,
    rotate,
    round_on_slice,
    slice_window,
    tile_rounds,
    walk_session,
)


def test_default_rounds_cover_eight_to_eight_fifty_four():
    assert tile_rounds() == 6
    assert configured_slice_count() == 18
    day = date(2026, 10, 28)
    start, lock = day_bounds(day)
    assert label_hhmm(start) == "08:00"
    assert label_hhmm(lock) == "08:54"
    assert lock_label() == "08:54"
    first_begin, first_end = slice_window(day, 0)
    last_begin, last_end = slice_window(day, 17)
    assert first_begin == start
    assert label_hhmm(first_end) == "08:03"
    assert label_hhmm(last_begin) == "08:51"
    assert last_end == lock
    order = ["claude", "grok-artist", "openai"]
    sequence = [artist_on_slice(order, index) for index in range(18)]
    assert sequence == order * 6
    assert [round_on_slice(index) for index in (0, 3, 15, 17)] == [1, 2, 6, 6]


def test_position_boundaries():
    assert position(parse_ny_local("2026-10-28T07:59:59")) == ("before", None)
    assert position(parse_ny_local("2026-10-28T08:00:00")) == ("during", 0)
    assert position(parse_ny_local("2026-10-28T08:02:59")) == ("during", 0)
    assert position(parse_ny_local("2026-10-28T08:03:00")) == ("during", 1)
    assert position(parse_ny_local("2026-10-28T08:53:59")) == ("during", 17)
    assert position(parse_ny_local("2026-10-28T08:54:00")) == ("after", None)


def test_twelve_slice_history_still_locks_at_eight_thirty_six():
    day = date(2026, 10, 28)
    start, lock = day_bounds(day, slice_count=12)
    assert label_hhmm(lock) == "08:36"
    assert lock_label(12) == "08:36"
    _, last_end = slice_window(day, 11, slice_count=12)
    assert last_end == lock
    clock = walk_session(start, {}, parse_ny_local("2026-10-28T08:35:59"), slice_count=12)
    assert clock.phase == "during"
    assert clock.index == 11
    assert len(clock.slices) == 12
    done = walk_session(start, {}, parse_ny_local("2026-10-28T08:36:00"), slice_count=12)
    assert done.phase == "after"
    assert position(parse_ny_local("2026-10-28T08:36:00"), slice_count=12) == ("after", None)


def test_tile_rounds_respects_bounds(monkeypatch):
    monkeypatch.setenv("TILE_ROUNDS", "2")
    assert tile_rounds() == 2
    assert configured_slice_count() == 6
    assert lock_label() == "08:18"
    monkeypatch.setenv("TILE_ROUNDS", "24")
    assert tile_rounds() == 24
    assert configured_slice_count() == 72
    for raw in ("0", "25", "-3", "nope", "", "6.5"):
        monkeypatch.setenv("TILE_ROUNDS", raw)
        assert tile_rounds() == 6, raw
        assert configured_slice_count() == 18


def test_review_gate_holds_until_a_note_or_the_timeout():
    start = parse_ny_local("2026-10-28T08:00:00")
    acted = {0: parse_ny_local("2026-10-28T08:00:10")}
    held = walk_session(start, acted, parse_ny_local("2026-10-28T08:00:20"), gate_seconds=75)
    assert held.phase == "holding"
    assert held.holding_index == 1
    assert held.index is None
    assert len(held.slices) == configured_slice_count()
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
    # A 12-slice canvas still holds the same way; the gate does not grow the board.
    legacy = walk_session(
        start,
        acted,
        parse_ny_local("2026-10-28T08:00:20"),
        gate_seconds=75,
        slice_count=12,
    )
    assert legacy.phase == "holding"
    assert len(legacy.slices) == 12


def test_redo_shifts_the_following_gate_and_the_lock():
    assert redo_seconds() == 120
    start = parse_ny_local("2026-10-28T08:00:00")
    acted = {0: parse_ny_local("2026-10-28T08:00:10")}
    veto = parse_ny_local("2026-10-28T08:00:20")
    plain = walk_session(start, acted, parse_ny_local("2026-10-28T08:00:30"), gate_seconds=75)
    assert plain.phase == "holding"
    assert plain.holding_index == 1
    live = walk_session(
        start,
        acted,
        parse_ny_local("2026-10-28T08:00:30"),
        gate_seconds=75,
        redos={0: RedoWindow(veto, None, 120)},
    )
    assert live.phase == "during"
    assert live.index == 0
    assert live.slices[0].redo is True
    assert live.deadline == veto + timedelta(seconds=120)
    redo_close = parse_ny_local("2026-10-28T08:02:20")
    held = walk_session(
        start,
        acted,
        parse_ny_local("2026-10-28T08:02:30"),
        gate_seconds=75,
        redos={0: RedoWindow(veto, redo_close, 120)},
    )
    assert held.phase == "holding"
    assert held.holding_index == 1
    assert held.slices[1].opens == redo_close + timedelta(seconds=75)
    stale = walk_session(
        start,
        acted,
        parse_ny_local("2026-10-28T08:02:30"),
        gate_seconds=75,
        note_at={1: parse_ny_local("2026-10-28T08:00:15")},
        redos={0: RedoWindow(veto, redo_close, 120)},
    )
    assert stale.phase == "holding"
    released = walk_session(
        start,
        acted,
        redo_close,
        gate_seconds=75,
        note_at={1: parse_ny_local("2026-10-28T08:01:00")},
        redos={0: RedoWindow(veto, redo_close, 120)},
    )
    assert released.phase == "during"
    assert released.index == 1
    later = parse_ny_local("2026-10-28T12:00:00")
    no_redo = walk_session(start, acted, later, gate_seconds=75)
    with_redo = walk_session(
        start,
        acted,
        later,
        gate_seconds=75,
        redos={0: RedoWindow(veto, redo_close, 120)},
    )
    assert with_redo.phase == "after"
    assert no_redo.slices[1].opens == acted[0] + timedelta(seconds=75)
    assert with_redo.lock_at - no_redo.lock_at == redo_close - acted[0]


def test_rotation_by_day_index():
    order = ["claude", "grok-artist", "openai"]
    assert rotate(order, 1, True) == order
    assert rotate(order, 2, True) == ["grok-artist", "openai", "claude"]
    assert rotate(order, 3, True) == ["openai", "claude", "grok-artist"]
    assert rotate(order, 2, False) == order
