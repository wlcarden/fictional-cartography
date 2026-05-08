"""Rasterize OSM landuse polygons to a smooth urban-density field."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import gaussian_filter

from src.pipeline import MapBounds


def rasterize_polygons(
    polygons: list[list[tuple[float, float]]],
    bounds: MapBounds,
    h: int,
    w: int,
    sigma: float = 8.0,
) -> np.ndarray:
    """Project polygons into pixel space, fill them, blur, and normalize to [0, 1]."""
    if not polygons:
        return np.zeros((h, w), dtype=np.float32)

    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    for coords in polygons:
        px = []
        for lat, lon in coords:
            x = int((lon - bounds.lon_w) / bounds.lon_span * w)
            y = int((bounds.lat_n - lat) / bounds.lat_span * h)
            px.append((x, y))
        if len(px) >= 3:
            draw.polygon(px, fill=255)

    density = np.array(img, dtype=np.float32) / 255.0
    density = gaussian_filter(density, sigma=sigma)
    mx = float(density.max())
    if mx > 0:
        density /= mx
    return density
