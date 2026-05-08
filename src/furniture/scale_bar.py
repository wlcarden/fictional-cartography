"""Mile-based scale bar with alternating segments and labels."""
from __future__ import annotations

import math

from PIL import ImageDraw, ImageFont

from src.pipeline import MapBounds


def draw_scale_bar(
    draw: ImageDraw.ImageDraw,
    bounds: MapBounds,
    terrain_w: int,
    canvas_w: int,
    canvas_h: int,
    font: ImageFont.FreeTypeFont,
    bar_miles: int = 20,
    segments: int = 4,
    bar_y_from_bottom: int | None = None,
    bar_height: int = 8,
    dark_color: tuple[int, int, int] = (80, 65, 45),
    light_color: tuple[int, int, int] = (180, 165, 135),
    border_color: tuple[int, int, int] = (60, 48, 35),
    label_color: tuple[int, int, int] = (120, 105, 78),
    border: int = 0,
    offset_from_border: int = 60,
    position: str = "bottom-center",
) -> None:
    """Scale bar in miles, positioned by config.

    Width is computed from the geographic bounds: the bar covers ``bar_miles``
    miles at the map's mid-latitude. Renders in-place on ``draw``.

    ``position`` is one of ``bottom-center`` / ``bottom-left`` /
    ``bottom-right``. The bar is offset from the inner edge of the canvas
    border by ``offset_from_border`` px, so it sits cleanly inside the
    printed frame regardless of border thickness.

    ``bar_y_from_bottom`` (legacy) overrides the border-relative placement
    if explicitly passed.
    """
    mid_lat = (bounds.lat_n + bounds.lat_s) / 2
    km_per_deg_lon = 111.32 * math.cos(math.radians(mid_lat))
    total_km = bounds.lon_span * km_per_deg_lon
    total_miles = total_km * 0.621371
    if total_miles <= 0:
        return
    px_per_mile = terrain_w / total_miles

    bar_px = int(bar_miles * px_per_mile)
    seg_px = bar_px // segments

    # Horizontal placement
    if position == "bottom-left":
        bar_x = border + offset_from_border
    elif position == "bottom-right":
        bar_x = canvas_w - border - offset_from_border - bar_px
    else:  # bottom-center (default)
        bar_x = canvas_w // 2 - bar_px // 2

    # Vertical placement: legacy bar_y_from_bottom keeps the old behavior
    # for callers that pass it explicitly; otherwise we offset from the
    # inner border so the bar sits above (and clear of) the credit line.
    if bar_y_from_bottom is not None:
        bar_y = canvas_h - bar_y_from_bottom
    else:
        bar_y = canvas_h - border - offset_from_border

    for i in range(segments):
        sx = bar_x + i * seg_px
        fill = dark_color if i % 2 == 0 else light_color
        draw.rectangle(
            [sx, bar_y, sx + seg_px, bar_y + bar_height],
            fill=fill, outline=border_color,
        )

    draw.text((bar_x, bar_y - 16), "0", fill=label_color, font=font)
    end_label = f"{bar_miles} miles"
    eb = draw.textbbox((0, 0), end_label, font=font)
    draw.text(
        (bar_x + bar_px - eb[2] + eb[0], bar_y - 16),
        end_label, fill=label_color, font=font,
    )
    mid_label = f"{bar_miles // 2}"
    mb = draw.textbbox((0, 0), mid_label, font=font)
    draw.text(
        (bar_x + bar_px // 2 - mb[2] // 2, bar_y - 16),
        mid_label, fill=label_color, font=font,
    )
