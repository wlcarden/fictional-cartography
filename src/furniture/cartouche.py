"""Title cartouche (semi-transparent box around title + subtitle) and credit line."""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from src.labels.markers import outline_text


def draw_title_cartouche(
    canvas: Image.Image,
    title: str,
    subtitle: str | None,
    title_font: ImageFont.FreeTypeFont,
    sub_font: ImageFont.FreeTypeFont,
    canvas_w: int,
    y: int = 22,
    bg_color: tuple[int, int, int, int] = (110, 95, 70, 210),
    border_color: tuple[int, int, int, int] = (170, 150, 115, 200),
    title_color: tuple[int, int, int] = (235, 220, 185),
    sub_color: tuple[int, int, int] = (200, 190, 165),
    divider_color: tuple[int, int, int] = (180, 160, 130),
) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """Centered title block with optional subtitle, divider, and small
    ornamental diamond accents flanking the divider rule.

    The diamond ornaments give the cartouche the "period-cartographic"
    feel the reference image has — a small visual cue that this is a
    formal title block, not just text on a colored bar.
    """
    draw = ImageDraw.Draw(canvas)
    tbb = draw.textbbox((0, 0), title, font=title_font)
    ttw = tbb[2] - tbb[0]
    if subtitle:
        sbb = draw.textbbox((0, 0), subtitle, font=sub_font)
        stw = sbb[2] - sbb[0]
    else:
        stw = 0

    box_w = max(ttw, stw) + 40
    box_x = (canvas_w - box_w) // 2

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle([box_x, y - 20, box_x + box_w, y + 120], fill=bg_color)
    od.rectangle(
        [box_x + 3, y - 17, box_x + box_w - 3, y + 117],
        outline=border_color, width=2,
    )
    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)

    title_x = (canvas_w - ttw) // 2
    outline_text(draw, (title_x, y), title, title_font, title_color, width=4)

    if subtitle:
        # Divider rule with diamond ornaments flanking it. Diamond
        # geometry: 4-point polygon, ~6 px half-size at base. The rule
        # is interrupted on both ends so the diamonds feel inset rather
        # than glued onto the rule.
        rule_y = y + 78
        rule_left = box_x + 40
        rule_right = box_x + box_w - 40
        diamond_size = 6
        gap = diamond_size + 4
        # Left diamond
        cx_l = rule_left + diamond_size + 2
        draw.polygon(
            [(cx_l - diamond_size, rule_y), (cx_l, rule_y - diamond_size),
             (cx_l + diamond_size, rule_y), (cx_l, rule_y + diamond_size)],
            fill=divider_color,
        )
        # Right diamond
        cx_r = rule_right - diamond_size - 2
        draw.polygon(
            [(cx_r - diamond_size, rule_y), (cx_r, rule_y - diamond_size),
             (cx_r + diamond_size, rule_y), (cx_r, rule_y + diamond_size)],
            fill=divider_color,
        )
        # Rule, with gaps for the diamonds
        draw.line(
            [(cx_l + gap, rule_y), (cx_r - gap, rule_y)],
            fill=divider_color, width=2,
        )

        draw.text(((canvas_w - stw) // 2, y + 86), subtitle, fill=sub_color, font=sub_font)

    return canvas, draw


def draw_credit(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    canvas_w: int,
    canvas_h: int,
    color: tuple[int, int, int] = (120, 105, 78),
    y_from_bottom: int | None = None,
    divider: bool = True,
    border: int = 0,
    offset_from_border: int = 28,
) -> None:
    """Centered credit line, positioned relative to the inner border line.

    Positioning: by default we offset from the inner edge of the canvas
    border so the credit always sits inside the printed frame. Pass
    ``y_from_bottom`` to override and use the legacy "from canvas edge"
    placement (kept for backward compat, but the default is the correct
    "inside the frame" placement).

    The previous default (22 px from canvas edge) drew on top of the
    border lines whenever ``border > ~20``. Using ``border + offset``
    keeps the credit line inside the frame at any border thickness.
    """
    bb = draw.textbbox((0, 0), text, font=font)
    text_w = bb[2] - bb[0]
    x = (canvas_w - text_w) // 2
    if y_from_bottom is not None:
        y = canvas_h - y_from_bottom
    else:
        y = canvas_h - border - offset_from_border
    draw.text((x, y), text, fill=color, font=font)
    if divider:
        draw.line(
            [(x - 30, y - 6), (x + text_w + 30, y - 6)],
            fill=color, width=1,
        )
