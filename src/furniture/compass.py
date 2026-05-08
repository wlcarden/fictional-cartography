"""Compass rose: outer circle, N/S/E/W arrows with N arrowhead,
intercardinal arms, and a decorative inner ring."""
from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageFont

from src.labels.markers import outline_text


def draw_compass_rose(
    canvas: Image.Image,
    cx: int,
    cy: int,
    radius: int,
    label_font: ImageFont.FreeTypeFont,
    line_color: tuple[int, int, int, int] = (140, 115, 78, 220),
    fill_color: tuple[int, int, int, int] = (110, 90, 60, 180),
    text_color: tuple[int, int, int] = (140, 115, 78),
) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """Draw an ornate compass rose centered at (cx, cy).

    Layered structure (back-to-front):
      1. Outer ring (thick)
      2. Intermediate ring at 0.78 * radius (thin)
      3. Inner hub disk + small ring around hub
      4. 8 cardinal/intercardinal arrow arms — N/E/S/W full-length filled
         "kite" arrows (poly), NE/NW/SE/SW shorter filled-arrow secondaries
      5. North arrow with bold arrowhead
      6. Cardinal labels (N/E/S/W) outside the outer ring
      7. Subtle 16-tick degree marks around the outer ring

    Returns (canvas, draw) — caller may need a fresh `draw` since we
    composited onto a new layer.
    """
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    # --- Rings ---
    # Outer ring
    od.ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius],
        outline=line_color, width=3,
    )
    # Intermediate ring (0.78 * radius)
    inter_r = int(radius * 0.78)
    od.ellipse(
        [cx - inter_r, cy - inter_r, cx + inter_r, cy + inter_r],
        outline=line_color, width=1,
    )

    # --- Cardinal arms (filled-kite arrows: long thin diamonds) ---
    # Each cardinal arm extends from hub to outer ring with a thin tail
    # on the opposite side. Drawing as two halves (light + shadow) gives
    # the engraved look of period compass roses.
    light_color = (180, 150, 110, 230)   # highlighted half (NE-facing)
    shadow_color = (90, 70, 45, 230)     # shaded half (SW-facing)

    cardinal_angles = [0, 90, 180, 270]   # N, E, S, W (clockwise from up)
    arm_outer = radius - 8
    arm_width = max(4, int(radius * 0.08))    # half-width at midpoint

    for angle in cardinal_angles:
        # Tip and tail in canvas pixel coords (0° points up = north)
        rad_tip = math.radians(angle - 90)
        rad_left = math.radians(angle - 90 - 90)
        tip = (cx + int(arm_outer * math.cos(rad_tip)),
               cy + int(arm_outer * math.sin(rad_tip)))
        tail = (cx - int(arm_outer * 0.18 * math.cos(rad_tip)),
                cy - int(arm_outer * 0.18 * math.sin(rad_tip)))
        # The two side points define the diamond width; one is the
        # "light" side, the other "shadow"
        side_l = (cx + int(arm_width * math.cos(rad_left)),
                  cy + int(arm_width * math.sin(rad_left)))
        side_r = (cx - int(arm_width * math.cos(rad_left)),
                  cy - int(arm_width * math.sin(rad_left)))
        # Two half-diamonds: tip→side_l→tail (one tone) and tip→side_r→tail (other tone)
        # Pick which half is light vs shadow based on cardinal direction:
        # for N (angle=0), light side faces east (+x), shadow side faces west (-x)
        # We map that consistently for each cardinal.
        if angle in (0, 90):
            light_poly = [tip, side_l, tail]
            shadow_poly = [tip, side_r, tail]
        else:
            light_poly = [tip, side_r, tail]
            shadow_poly = [tip, side_l, tail]
        od.polygon(light_poly, fill=light_color)
        od.polygon(shadow_poly, fill=shadow_color)
        # Edge stroke for definition
        od.line([tip, side_l, tail, side_r, tip],
                fill=line_color, width=1)

    # --- Intercardinal arms (shorter kites: NE/NW/SE/SW) ---
    inter_outer = int(radius * 0.62)
    inter_width = max(3, int(radius * 0.05))
    for angle in (45, 135, 225, 315):
        rad_tip = math.radians(angle - 90)
        rad_left = math.radians(angle - 90 - 90)
        tip = (cx + int(inter_outer * math.cos(rad_tip)),
               cy + int(inter_outer * math.sin(rad_tip)))
        tail = (cx - int(inter_outer * 0.20 * math.cos(rad_tip)),
                cy - int(inter_outer * 0.20 * math.sin(rad_tip)))
        side_l = (cx + int(inter_width * math.cos(rad_left)),
                  cy + int(inter_width * math.sin(rad_left)))
        side_r = (cx - int(inter_width * math.cos(rad_left)),
                  cy - int(inter_width * math.sin(rad_left)))
        if angle in (45, 135):
            light_poly = [tip, side_l, tail]
            shadow_poly = [tip, side_r, tail]
        else:
            light_poly = [tip, side_r, tail]
            shadow_poly = [tip, side_l, tail]
        od.polygon(light_poly, fill=light_color)
        od.polygon(shadow_poly, fill=shadow_color)
        od.line([tip, side_l, tail, side_r, tip],
                fill=line_color, width=1)

    # --- Hub: filled disk + small ring around it ---
    hub_r = max(6, int(radius * 0.12))
    od.ellipse(
        [cx - hub_r, cy - hub_r, cx + hub_r, cy + hub_r],
        fill=fill_color, outline=line_color, width=2,
    )
    # Tiny inner dot
    inner_dot = max(2, hub_r // 3)
    od.ellipse(
        [cx - inner_dot, cy - inner_dot, cx + inner_dot, cy + inner_dot],
        fill=line_color,
    )

    # --- North arrowhead emphasis ---
    # Draw a bold arrowhead at the tip of the N cardinal arm to mark
    # which direction is north. Sized proportionally to radius.
    n_tip_y = cy - (radius - 8)
    ah = max(8, int(radius * 0.13))
    od.polygon(
        [(cx, n_tip_y - ah),
         (cx - ah * 0.6, n_tip_y + ah * 0.4),
         (cx, n_tip_y + ah * 0.1),
         (cx + ah * 0.6, n_tip_y + ah * 0.4)],
        fill=(180, 150, 110, 250), outline=line_color,
    )

    # --- Tick marks around the outer ring (16 ticks: 4 cardinal + 4
    # intercardinal already covered, plus 8 secondary at multiples of 22.5°) ---
    for tick_angle in range(0, 360, 22):
        if tick_angle % 45 == 0:
            continue   # cardinal/intercardinal already drawn as arms
        rad = math.radians(tick_angle - 90)
        x_outer = cx + int(radius * math.cos(rad))
        y_outer = cy + int(radius * math.sin(rad))
        x_inner = cx + int((radius - 6) * math.cos(rad))
        y_inner = cy + int((radius - 6) * math.sin(rad))
        od.line([(x_inner, y_inner), (x_outer, y_outer)],
                fill=line_color, width=1)

    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)

    # --- Cardinal labels outside the outer ring ---
    for angle, label in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
        rad = math.radians(angle - 90)
        dist = radius + 18
        lx = cx + int(dist * math.cos(rad))
        ly = cy + int(dist * math.sin(rad))
        bb = draw.textbbox((0, 0), label, font=label_font)
        lw = bb[2] - bb[0]
        lh = bb[3] - bb[1]
        outline_text(
            draw, (lx - lw // 2, ly - lh // 2), label, label_font,
            text_color, width=2,
        )

    return canvas, draw
