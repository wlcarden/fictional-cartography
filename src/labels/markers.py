"""Settlement markers, leaders, region labels, water labels, edge indicators."""
from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageFont

from src.labels.placer import LabelPlacer


def outline_text(
    draw: ImageDraw.ImageDraw,
    pos: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    shadow: tuple[int, int, int] = (30, 25, 18),
    width: int = 3,
    anchor: str = "la",
    rotation: float = 0.0,
    canvas: Image.Image | None = None,
) -> None:
    """Draw text with a circular shadow halo for readability over textured terrain.

    `anchor` follows PIL's two-letter convention. Default 'la' (left-ascender)
    means `pos` is the top-left of the glyph cell — left-aligned text.
    Use 'ma' (middle-ascender) for centered text where `pos.x` is the
    horizontal center.

    When `rotation` is nonzero, `canvas` MUST be provided (the underlying
    Image, not just the Draw). The text + halo are rendered onto a transparent
    intermediate layer, rotated counter-clockwise around the text center, and
    alpha-composited back. `pos` is interpreted as the rotation pivot — the
    geometric center of the un-rotated glyph block — regardless of `anchor`.
    """
    if rotation:
        if canvas is None:
            # Fail loud rather than silently dropping the rotation request.
            raise ValueError("outline_text(rotation=...) requires the canvas Image")
        _draw_rotated_haloed_text(canvas, pos, text, font, fill, shadow, width, rotation)
        return

    x, y = pos
    for dx in range(-width, width + 1):
        for dy in range(-width, width + 1):
            if dx * dx + dy * dy <= width * width:
                draw.text((x + dx, y + dy), text, fill=shadow, font=font, anchor=anchor)
    draw.text((x, y), text, fill=fill, font=font, anchor=anchor)


def _draw_rotated_haloed_text(
    canvas: Image.Image,
    center: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    shadow: tuple[int, int, int],
    halo_width: int,
    rotation: float,
) -> None:
    """Render `text` with a circular halo on a transparent layer, rotate it
    counter-clockwise by `rotation` degrees, and alpha-composite onto `canvas`
    so the rotated text is centered at `center`.
    """
    # Measure glyph bounds. Use a temporary Draw on a 1×1 image just for
    # textbbox — PIL doesn't expose a static measurement API for multi-line
    # behavior, but our text is single-line so we can use anchor='lt'.
    bbox = font.getbbox(text)
    text_w = max(1, bbox[2] - bbox[0])
    text_h = max(1, bbox[3] - bbox[1])

    # Pad the layer so the halo fits and rotation expansion has room.
    pad = halo_width + 4
    layer_w = text_w + 2 * pad
    layer_h = text_h + 2 * pad
    layer = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)

    # Anchor the text at the layer's center using PIL's 'mm' (middle-middle).
    cx, cy = layer_w // 2, layer_h // 2
    for dx in range(-halo_width, halo_width + 1):
        for dy in range(-halo_width, halo_width + 1):
            if dx * dx + dy * dy <= halo_width * halo_width:
                ld.text((cx + dx, cy + dy), text, fill=shadow, font=font, anchor="mm")
    ld.text((cx, cy), text, fill=fill, font=font, anchor="mm")

    rotated = layer.rotate(rotation, resample=Image.Resampling.BICUBIC, expand=True)
    rw, rh = rotated.size
    px = int(round(center[0] - rw / 2))
    py = int(round(center[1] - rh / 2))

    if canvas.mode != "RGBA":
        # alpha_composite requires RGBA on both sides; defensive in case the
        # caller passes an RGB final image (rare but possible).
        canvas = canvas.convert("RGBA")
    canvas.alpha_composite(rotated, (px, py))


def draw_marker_dot(
    draw: ImageDraw.ImageDraw,
    placer: LabelPlacer,
    x: int,
    y: int,
    color: tuple[int, int, int],
    radius: int,
    shadow: tuple[int, int, int] = (30, 25, 18),
    marker: str = "circle",
) -> None:
    """Render one of several marker glyphs at (x, y).

    Supported `marker` values:
      circle      — concentric ringed dot with shadow halo (default)
      circled_x   — ringed circle with an X drawn through it
      x           — bare X with shadow halo
      diamond     — rotated square outline filled with color, darker center
      dot         — small filled dot, smaller than `radius`, used for sub-features
      none        — draws nothing; caller should use `radius=0` for the same effect
    """
    if radius <= 0 or marker == "none":
        return

    if marker == "circle":
        draw.ellipse(
            [x - radius - 2, y - radius - 2, x + radius + 2, y + radius + 2],
            fill=shadow,
        )
        draw.ellipse(
            [x - radius, y - radius, x + radius, y + radius],
            fill=None, outline=color, width=3,
        )
        inn = max(1, radius - 4)
        draw.ellipse(
            [x - inn, y - inn, x + inn, y + inn],
            fill=tuple(max(0, c - 60) for c in color),
        )
        draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=color)

    elif marker == "circled_x":
        draw.ellipse(
            [x - radius - 2, y - radius - 2, x + radius + 2, y + radius + 2],
            fill=shadow,
        )
        draw.ellipse(
            [x - radius, y - radius, x + radius, y + radius],
            fill=None, outline=color, width=3,
        )
        # X arms extend ~65% of the radius so they sit cleanly inside the ring
        d = max(2, int(radius * 0.65))
        # Dark center jewel under the X
        inn = max(1, radius - 4)
        draw.ellipse(
            [x - inn, y - inn, x + inn, y + inn],
            fill=tuple(max(0, c - 60) for c in color),
        )
        # Shadow strokes for the X
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            draw.line([(x - d + dx, y - d + dy), (x + d + dx, y + d + dy)],
                      fill=shadow, width=3)
            draw.line([(x - d + dx, y + d + dy), (x + d + dx, y - d + dy)],
                      fill=shadow, width=3)
        # Color strokes on top
        draw.line([(x - d, y - d), (x + d, y + d)], fill=color, width=2)
        draw.line([(x - d, y + d), (x + d, y - d)], fill=color, width=2)

    elif marker == "x":
        d = max(2, int(radius * 0.85))
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            draw.line([(x - d + dx, y - d + dy), (x + d + dx, y + d + dy)],
                      fill=shadow, width=4)
            draw.line([(x - d + dx, y + d + dy), (x + d + dx, y - d + dy)],
                      fill=shadow, width=4)
        draw.line([(x - d, y - d), (x + d, y + d)], fill=color, width=3)
        draw.line([(x - d, y + d), (x + d, y - d)], fill=color, width=3)

    elif marker == "diamond":
        outer = [
            (x, y - radius - 2), (x + radius + 2, y),
            (x, y + radius + 2), (x - radius - 2, y),
        ]
        draw.polygon(outer, fill=shadow)
        inner = [
            (x, y - radius), (x + radius, y),
            (x, y + radius), (x - radius, y),
        ]
        draw.polygon(inner, fill=color, outline=tuple(max(0, c - 80) for c in color))
        d = max(1, radius - 4)
        dark = [(x, y - d), (x + d, y), (x, y + d), (x - d, y)]
        draw.polygon(dark, fill=tuple(max(0, c - 60) for c in color))

    elif marker == "dot":
        # Small filled dot — half the radius, used for low-significance
        # features like submerged ruins. A pale parchment-colored ring
        # surrounds the dot so it reads as a "feature beneath the
        # water" against the dark sunken-region color (without the ring,
        # the dot blends into the slate water and disappears at preview
        # resolution). Two halo layers: outer dark for contrast against
        # the bright parch ring, inner parch ring for the "highlighted
        # underwater feature" feel.
        r_eff = max(2, radius // 2)
        # Outer dark halo (contrast)
        draw.ellipse(
            [x - r_eff - 3, y - r_eff - 3, x + r_eff + 3, y + r_eff + 3],
            fill=shadow,
        )
        # Pale parchment ring (the highlight)
        draw.ellipse(
            [x - r_eff - 2, y - r_eff - 2, x + r_eff + 2, y + r_eff + 2],
            outline=(220, 205, 175), width=1,
        )
        # The dot itself
        draw.ellipse([x - r_eff, y - r_eff, x + r_eff, y + r_eff], fill=color)

    else:
        # Unknown marker name → fall back to circle so renders never silently break
        draw_marker_dot(draw, placer, x, y, color, radius, shadow=shadow, marker="circle")
        return

    placer.reserve((x - radius - 4, y - radius - 4, x + radius + 4, y + radius + 4))


def draw_leader(
    draw: ImageDraw.ImageDraw,
    anchor_x: int,
    anchor_y: int,
    label_x: int,
    label_y: int,
    label_w: int,
    label_h: int,
    color: tuple[int, int, int],
    radius: int,
    max_distance: int = 200,
) -> None:
    """Thin line from marker edge to nearest label edge. Skipped if too far or radius=0."""
    if radius <= 0:
        return
    lcx = label_x + label_w / 2
    lcy = label_y + label_h / 2
    dist = math.hypot(lcx - anchor_x, lcy - anchor_y)
    if dist > max_distance:
        return
    angle = math.atan2(lcy - anchor_y, lcx - anchor_x)
    mx = anchor_x + int(radius * math.cos(angle))
    my = anchor_y + int(radius * math.sin(angle))
    if label_x > anchor_x:
        ex, ey = label_x - 3, label_y + label_h // 2
    elif label_x + label_w < anchor_x:
        ex, ey = label_x + label_w + 3, label_y + label_h // 2
    elif label_y > anchor_y:
        ex, ey = label_x + label_w // 2, label_y - 3
    else:
        ex, ey = label_x + label_w // 2, label_y + label_h + 3
    draw.line([(mx, my), (ex, ey)], fill=color, width=2)


def place_settlement(
    draw: ImageDraw.ImageDraw,
    placer: LabelPlacer,
    canvas_x: int,
    canvas_y: int,
    name: str,
    color: tuple[int, int, int],
    radius: int,
    name_font: ImageFont.FreeTypeFont,
    note: str | None,
    note_font: ImageFont.FreeTypeFont,
    note_color: tuple[int, int, int],
    preferred_angle: int,
    margin_left: int,
    margin_top: int,
    margin_right: int,
    margin_bottom: int,
    shadow: tuple[int, int, int] = (30, 25, 18),
    marker: str = "circle",
    label_offset: tuple[int, int] | None = None,
    rotation: float = 0.0,
    canvas: Image.Image | None = None,
) -> None:
    """Render a settlement marker + label; skip if anchor is far outside bounds.

    Caller is responsible for converting (lat, lon) -> (canvas_x, canvas_y).
    `marker` selects the glyph (see draw_marker_dot for options).
    `label_offset`, if provided, bypasses the auto-placer and positions the
    label's top-left at (canvas_x + dx, canvas_y + dy). The label box is still
    reserved with the placer so subsequent labels avoid it.

    `rotation` (degrees, counter-clockwise) rotates each label line around the
    axis-aligned label box's center. Reservation stays axis-aligned, so
    heavily-rotated labels may overlap rotated neighbors slightly. Pass the
    underlying `canvas` Image when rotation != 0.
    """
    if not (
        margin_left - 30 < canvas_x < margin_right + 30
        and margin_top - 30 < canvas_y < margin_bottom + 30
    ):
        return
    draw_marker_dot(draw, placer, canvas_x, canvas_y, color, radius, shadow=shadow, marker=marker)
    lines: list[tuple[str, ImageFont.FreeTypeFont]] = [(name, name_font)]
    if note:
        lines.append((note, note_font))

    # Compute the stacked-line bounding box so both placement modes have it.
    bw = max(placer.text_size(t, f)[0] for t, f in lines)
    bh = sum(placer.text_size(t, f)[1] + 4 for t, f in lines) - 4

    if label_offset is not None:
        dx, dy = label_offset
        lx = canvas_x + int(dx)
        ly = canvas_y + int(dy)
        # Reserve manually since we skipped placer.place(...)
        pad = 8
        placer.reserve((lx - pad, ly - pad, lx + bw + pad, ly + bh + pad))
    else:
        lx, ly = placer.place(
            canvas_x, canvas_y, lines, preferred_angle=preferred_angle
        )
    # Leader lines connect the marker to the label's nearest edge — geometry
    # only makes sense for axis-aligned labels, so skip when rotated.
    if not rotation:
        draw_leader(draw, canvas_x, canvas_y, lx, ly, bw, bh, color, radius)
    # Center each line around the box's horizontal midline. Both lines share
    # the same axis so a wider note doesn't drag a shorter name off-center.
    center_x = lx + bw // 2
    cy = ly
    for text, f in lines:
        c = color if f is name_font else note_color
        if rotation:
            # For rotation, the pivot must be the line's GEOMETRIC CENTER, not
            # the top — otherwise rotated lines wander away from the box.
            line_h = placer.text_size(text, f)[1]
            pivot = (center_x, cy + line_h // 2)
            outline_text(
                draw, pivot, text, f, c,
                shadow=shadow, width=3 if f is name_font else 2,
                anchor="ma", rotation=rotation, canvas=canvas,
            )
        else:
            outline_text(
                draw, (center_x, cy), text, f, c,
                shadow=shadow, width=3 if f is name_font else 2,
                anchor="ma",
            )
        cy += placer.text_size(text, f)[1] + 4


def place_region(
    draw: ImageDraw.ImageDraw,
    placer: LabelPlacer,
    canvas_x: int,
    canvas_y: int,
    name: str,
    color: tuple[int, int, int],
    name_font: ImageFont.FreeTypeFont,
    sub: str | None,
    sub_font: ImageFont.FreeTypeFont,
    margin_left: int,
    margin_top: int,
    margin_right: int,
    margin_bottom: int,
    shadow: tuple[int, int, int] = (30, 25, 18),
    rotation: float = 0.0,
    canvas: Image.Image | None = None,
) -> None:
    if not (margin_left < canvas_x < margin_right and margin_top < canvas_y < margin_bottom):
        return
    lines: list[tuple[str, ImageFont.FreeTypeFont]] = [(name, name_font)]
    if sub:
        lines.append((sub, sub_font))
    lx, ly = placer.place(canvas_x, canvas_y, lines, preferred_angle=0)
    bw = max(placer.text_size(t, f)[0] for t, f in lines)
    center_x = lx + bw // 2
    cy = ly
    for text, f in lines:
        c = color if f is name_font else tuple(max(0, ch - 30) for ch in color)
        if rotation:
            line_h = placer.text_size(text, f)[1]
            pivot = (center_x, cy + line_h // 2)
            outline_text(
                draw, pivot, text, f, c,
                shadow=shadow, width=3 if f is name_font else 2,
                anchor="ma", rotation=rotation, canvas=canvas,
            )
        else:
            outline_text(
                draw, (center_x, cy), text, f, c,
                shadow=shadow, width=3 if f is name_font else 2,
                anchor="ma",
            )
        cy += placer.text_size(text, f)[1] + 4


def place_water(
    draw: ImageDraw.ImageDraw,
    placer: LabelPlacer,
    canvas_x: int,
    canvas_y: int,
    name: str,
    color: tuple[int, int, int],
    font: ImageFont.FreeTypeFont,
    margin_left: int,
    margin_top: int,
    margin_right: int,
    margin_bottom: int,
    shadow: tuple[int, int, int] = (30, 25, 18),
    rotation: float = 0.0,
    canvas: Image.Image | None = None,
) -> None:
    if not (margin_left < canvas_x < margin_right and margin_top < canvas_y < margin_bottom):
        return
    lx, ly = placer.place(
        canvas_x, canvas_y, [(name, font)], preferred_angle=270
    )
    tw = placer.text_size(name, font)[0]
    if rotation:
        line_h = placer.text_size(name, font)[1]
        pivot = (lx + tw // 2, ly + line_h // 2)
        outline_text(
            draw, pivot, name, font, color,
            shadow=shadow, width=2, anchor="ma",
            rotation=rotation, canvas=canvas,
        )
    else:
        outline_text(
            draw, (lx + tw // 2, ly), name, font, color,
            shadow=shadow, width=2, anchor="ma",
        )
