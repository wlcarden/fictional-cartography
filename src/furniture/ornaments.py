"""Corner ornaments — fleur-de-lis glyphs at each corner of the canvas."""
from __future__ import annotations

from PIL import ImageDraw, ImageFont

from src.labels.markers import outline_text


def draw_corner_ornaments(
    draw: ImageDraw.ImageDraw,
    canvas_w: int,
    canvas_h: int,
    font: ImageFont.FreeTypeFont,
    glyph: str = "⚜",  # FLEUR-DE-LIS U+269C
    color: tuple[int, int, int] = (85, 65, 45),
    shadow: tuple[int, int, int] = (50, 42, 32),
    inset_x: int = 35,
    inset_top: int = 30,
    inset_bottom: int = 45,
) -> None:
    """Draw a fleur at each of the four corners of the canvas."""
    corners = [
        (inset_x, inset_top),
        (canvas_w - inset_x, inset_top),
        (inset_x, canvas_h - inset_bottom),
        (canvas_w - inset_x, canvas_h - inset_bottom),
    ]
    for fx, fy in corners:
        outline_text(
            draw, (fx - 12, fy), glyph, font, color, shadow=shadow, width=2
        )
