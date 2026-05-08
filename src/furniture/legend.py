"""Legend box: title, plus a list of marker/line/text sample entries.

The legend reads as an inset-framed plaque to match the title cartouche.
Two-rectangle frame (outer fill + inner inset rule), title in serif on
its own row with a horizontal divider beneath, entries below at uniform
line-height.
"""
from __future__ import annotations

from typing import Sequence

from PIL import Image, ImageDraw, ImageFont

from src.labels.markers import outline_text
from src.pipeline import resolve_color


def draw_legend(
    canvas: Image.Image,
    x: int,
    y: int,
    entries: Sequence[dict],
    color_map: dict,
    title_font: ImageFont.FreeTypeFont,
    entry_font: ImageFont.FreeTypeFont,
    italic_entry_font: ImageFont.FreeTypeFont | None = None,
    width: int = 285,
    title: str = "MAP KEY",
    title_color: tuple[int, int, int] = (235, 220, 185),
    text_color: tuple[int, int, int] = (210, 200, 175),
    bg_color: tuple[int, int, int, int] = (90, 75, 55, 215),
    border_color: tuple[int, int, int, int] = (170, 150, 115, 220),
    inner_border_color: tuple[int, int, int, int] = (120, 100, 75, 200),
    line_height: int = 28,
    italic_color: tuple[int, int, int] = (140, 175, 150),
    divider_color: tuple[int, int, int, int] = (170, 150, 115, 200),
) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """Render a legend plaque with title, divider rule, and entry rows.

    Title row gets ~36 px tall with the divider sitting at title row's
    baseline + 8 px gap. Entries below at `line_height` spacing — bumped
    from the previous 22 to 28 px so symbols and labels don't crowd.

    Frame: outer fill + 2px inner border + 1px inset rule, matching the
    cartouche's "engraved plaque" feel.
    """
    n = len(entries)
    title_row_h = 38
    body_h = n * line_height
    pad = 14
    h = title_row_h + body_h + pad

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    # Outer rectangle (background fill)
    od.rectangle(
        [x - pad, y - pad, x + width, y + h],
        fill=bg_color,
    )
    # Outer border (slightly inset)
    od.rectangle(
        [x - pad + 2, y - pad + 2, x + width - 2, y + h - 2],
        outline=border_color, width=2,
    )
    # Inner border line (deep inset for engraved look)
    od.rectangle(
        [x - pad + 6, y - pad + 6, x + width - 6, y + h - 6],
        outline=inner_border_color, width=1,
    )
    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)

    # Title
    title_bb = draw.textbbox((0, 0), title, font=title_font)
    title_w = title_bb[2] - title_bb[0]
    title_x = x + (width - title_w) // 2 - 4   # -4 = roughly centered minus inset
    outline_text(draw, (title_x, y), title, title_font, title_color, width=2)

    # Divider rule under the title — centered, with diamond ornaments at
    # both ends. Mirrors the cartouche frame's divider vocabulary.
    div_y = y + title_row_h - 8
    div_left = x + 18
    div_right = x + width - 18
    diamond_size = 4
    cx_l = div_left + diamond_size + 2
    cx_r = div_right - diamond_size - 2
    draw.polygon(
        [(cx_l - diamond_size, div_y), (cx_l, div_y - diamond_size),
         (cx_l + diamond_size, div_y), (cx_l, div_y + diamond_size)],
        fill=divider_color[:3] if len(divider_color) > 3 else divider_color,
    )
    draw.polygon(
        [(cx_r - diamond_size, div_y), (cx_r, div_y - diamond_size),
         (cx_r + diamond_size, div_y), (cx_r, div_y + diamond_size)],
        fill=divider_color[:3] if len(divider_color) > 3 else divider_color,
    )
    draw.line(
        [(cx_l + diamond_size + 4, div_y),
         (cx_r - diamond_size - 4, div_y)],
        fill=divider_color[:3] if len(divider_color) > 3 else divider_color,
        width=1,
    )

    # Entries
    ly = y + title_row_h + 4
    sym_x = x + 4
    label_x = x + 28

    for entry in entries:
        etype = entry.get("type", "circle")
        label = entry.get("label", "")
        color = resolve_color(entry.get("color"), color_map, default=(200, 200, 200))
        sym_cy = ly + line_height // 2 - 4
        if etype == "circle":
            draw.ellipse(
                [sym_x, sym_cy, sym_x + 12, sym_cy + 12],
                fill=color, outline=(50, 40, 30), width=1,
            )
            draw.text((label_x, ly), label, fill=text_color, font=entry_font)
        elif etype == "line":
            draw.line(
                [(sym_x, sym_cy + 6), (sym_x + 12, sym_cy + 6)],
                fill=color, width=3,
            )
            draw.text((label_x, ly), label, fill=text_color, font=entry_font)
        elif etype == "italic_text":
            font = italic_entry_font or entry_font
            draw.text((sym_x, ly), "Region", fill=italic_color, font=font)
            draw.text((label_x + 38, ly), label, fill=text_color, font=entry_font)
        else:
            draw.text((label_x - 24, ly), label, fill=text_color, font=entry_font)
        ly += line_height

    return canvas, draw
