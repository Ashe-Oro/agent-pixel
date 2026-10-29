"""A small cozy-cabin scene for the local demo canvas.

The left window glow is fixed at x=100, y=136, color 10. The README curl
paints that pixel, so keep it inside the timber wall.
"""

from __future__ import annotations

from app.schedule import GRID

# id 0 is the unpainted night fill.
COZY_PALETTE = [
    {"id": 0, "hex": "#0c1224", "name": "night"},
    {"id": 1, "hex": "#1c3058", "name": "sky"},
    {"id": 2, "hex": "#4d6c96", "name": "dusk"},
    {"id": 3, "hex": "#f4e7b0", "name": "moon"},
    {"id": 4, "hex": "#102214", "name": "pine-dark"},
    {"id": 5, "hex": "#1e4a2a", "name": "pine"},
    {"id": 6, "hex": "#7eae5c", "name": "leaf"},
    {"id": 7, "hex": "#2c1810", "name": "shadow"},
    {"id": 8, "hex": "#6d3e24", "name": "wood"},
    {"id": 9, "hex": "#c4a06a", "name": "timber"},
    {"id": 10, "hex": "#ffd36a", "name": "window"},
    {"id": 11, "hex": "#ff7a3c", "name": "hearth"},
    {"id": 12, "hex": "#9aa0a8", "name": "smoke"},
    {"id": 13, "hex": "#d5deee", "name": "mist"},
    {"id": 14, "hex": "#3d342c", "name": "earth"},
    {"id": 15, "hex": "#efe6d6", "name": "snow"},
]

COZY_THEME = "Cozy cabin in the woods"
COZY_BRIEF = "Warm cabin, tall pines, soft chimney smoke, night sky"

# Interior of the left window. README paints this cell.
DEMO_WINDOW = (100, 136)


def blank_canvas() -> bytearray:
    return bytearray(GRID * GRID)


def _setp(buf: bytearray, x: int, y: int, color: int) -> None:
    if 0 <= x < GRID and 0 <= y < GRID:
        buf[y * GRID + x] = color


def _rect(buf: bytearray, x: int, y: int, w: int, h: int, color: int) -> None:
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            _setp(buf, xx, yy, color)


def _disc(buf: bytearray, cx: int, cy: int, radius: int, color: int) -> None:
    r2 = radius * radius
    for y in range(cy - radius, cy + radius + 1):
        for x in range(cx - radius, cx + radius + 1):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r2:
                _setp(buf, x, y, color)


def _tri(buf: bytearray, apex_x: int, apex_y: int, height: int, color: int) -> None:
    for i in range(height):
        y = apex_y + i
        half = i
        for x in range(apex_x - half, apex_x + half + 1):
            _setp(buf, x, y, color)


def draw_cabin(buf: bytearray) -> None:
    """Paint a hard-edged cabin into a blank (color 0) buffer."""
    _rect(buf, 0, 78, 256, 48, 1)
    _rect(buf, 0, 126, 256, 42, 2)

    _disc(buf, 196, 42, 16, 3)
    _disc(buf, 204, 40, 13, 0)
    for x, y in (
        (20, 18),
        (40, 36),
        (64, 14),
        (88, 28),
        (110, 16),
        (140, 24),
        (160, 12),
        (230, 22),
        (246, 40),
        (30, 52),
        (150, 48),
        (80, 8),
    ):
        _setp(buf, x, y, 3)
        _setp(buf, x + 1, y, 13)

    _rect(buf, 0, 168, 256, 88, 14)
    _rect(buf, 0, 168, 256, 3, 15)
    _rect(buf, 12, 196, 36, 5, 15)
    _rect(buf, 188, 206, 48, 4, 15)
    _rect(buf, 70, 228, 28, 4, 15)

    for apex_x, apex_y, height in ((18, 86, 64), (236, 90, 60), (206, 100, 52)):
        _tri(buf, apex_x, apex_y, height, 4)
        trunk_top = apex_y + height - 2
        if trunk_top < 168:
            _rect(buf, apex_x - 2, trunk_top, 5, 168 - trunk_top, 7)

    for apex_x, apex_y, height in ((46, 100, 86), (214, 104, 80)):
        _tri(buf, apex_x, apex_y, height, 5)
        _tri(buf, apex_x, apex_y + 18, height // 2, 6)
        trunk_top = min(apex_y + height - 4, 164)
        _rect(buf, apex_x - 2, trunk_top, 5, 170 - trunk_top, 8)

    # Walls, then roof so the eaves overlap the timber.
    _rect(buf, 88, 124, 80, 56, 9)
    _rect(buf, 88, 124, 5, 56, 8)
    _rect(buf, 163, 124, 5, 56, 8)
    for y in range(90, 128):
        t = (y - 90) / 38
        half = int(10 + t * 48)
        for x in range(128 - half, 128 + half):
            _setp(buf, x, y, 7 if y % 6 == 0 else 8)

    _rect(buf, 140, 72, 12, 30, 8)
    _rect(buf, 138, 68, 16, 6, 7)
    for index, (x, y) in enumerate(
        ((148, 62), (151, 56), (154, 50), (158, 44), (162, 38), (166, 32))
    ):
        _disc(buf, x, y, 2 if index < 3 else 3, 12)

    _rect(buf, 118, 146, 20, 34, 7)
    _rect(buf, 120, 148, 16, 30, 8)
    _setp(buf, 132, 164, 10)

    # Left window. Interior glow includes (100, 136).
    _rect(buf, 96, 132, 16, 16, 7)
    _rect(buf, 98, 134, 12, 12, 10)
    _rect(buf, 102, 138, 4, 4, 11)
    for x in range(98, 110):
        _setp(buf, x, 139, 8)
    for y in range(134, 146):
        _setp(buf, 103, y, 8)

    _rect(buf, 146, 132, 16, 16, 7)
    _rect(buf, 148, 134, 12, 12, 10)
    _rect(buf, 152, 138, 4, 4, 11)
    for x in range(148, 160):
        _setp(buf, x, 139, 8)
    for y in range(134, 146):
        _setp(buf, 153, y, 8)

    _rect(buf, 108, 180, 40, 3, 7)
    for y in range(183, 250):
        half = 10 + (y - 183) // 4
        for x in range(128 - half, 128 + half):
            if buf[y * GRID + x] == 14:
                _setp(buf, x, y, 7)
    _rect(buf, 112, 180, 32, 4, 11)
