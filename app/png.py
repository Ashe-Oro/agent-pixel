"""Crisp palette-index PNG previews. Nearest-neighbor only — no blending."""

from __future__ import annotations

import io

from PIL import Image

from app.schedule import GRID


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    raw = value.removeprefix("#")
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)


def _color_table(palette: list[dict]) -> dict[int, tuple[int, int, int]]:
    return {int(swatch["id"]): hex_to_rgb(swatch["hex"]) for swatch in palette}


def render_png(
    canvas: bytes,
    palette: list[dict],
    scale: int = 3,
    crop: tuple[int, int, int, int] | None = None,
) -> bytes:
    if len(canvas) != GRID * GRID:
        raise ValueError("canvas must be 256×256 palette indices")
    scale = max(1, min(int(scale), 8))
    colors = _color_table(palette)
    fallback = (255, 0, 128)
    image = Image.new("RGB", (GRID, GRID))
    pixels = image.load()
    for index, color_id in enumerate(canvas):
        pixels[index % GRID, index // GRID] = colors.get(color_id, fallback)
    if crop is not None:
        x, y, w, h = crop
        image = image.crop((x, y, x + w, y + h))
    if scale != 1:
        image = image.resize(
            (image.width * scale, image.height * scale),
            Image.Resampling.NEAREST,
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
