"""Hillshade computation and three-stop elevation gradient base coloring."""
from __future__ import annotations

import numpy as np


def hillshade(
    dem: np.ndarray,
    sea_level: float,
    sun_altitude: float = 35.0,
    sun_azimuth: float = 315.0,
) -> np.ndarray:
    """Standard Lambertian hillshade in [0, 1].

    Cells at/below sea_level are pushed 5 m below it before the gradient,
    which produces a sharp coastal cliff shadow regardless of the actual
    bathymetry. NaN cells are treated as 0 elevation.
    """
    d = dem.copy()
    d[np.isnan(d)] = 0.0
    d[dem <= sea_level] = sea_level - 5.0

    dy, dx = np.gradient(d)
    slope = np.arctan(np.sqrt(dx * dx + dy * dy))
    aspect = np.arctan2(-dy, dx)

    alt_r = np.radians(sun_altitude)
    az_r = np.radians(sun_azimuth)
    shade = (
        np.sin(alt_r) * np.cos(slope)
        + np.cos(alt_r) * np.sin(slope) * np.cos(az_r - aspect)
    )
    return np.clip(shade, 0.0, 1.0)


def base_colors_land(
    dem: np.ndarray,
    sea_level: float,
    palette_low: tuple[float, float, float],
    palette_mid: tuple[float, float, float],
    palette_high: tuple[float, float, float],
    mid_break: float,
    shade: np.ndarray,
    shade_floor: float,
) -> np.ndarray:
    """Three-stop elevation gradient with shade applied. Returns (h, w, 3) float32 in [0, 1].

    Below `mid_break` (a fraction of the elevation range): low->mid.
    Above:                                                 mid->high.
    `shade_floor` is the minimum shade multiplier (deeper shadows = lower floor).
    """
    max_elev = float(np.nanmax(dem))
    elev_t = np.clip((dem - sea_level) / max(max_elev - sea_level, 1e-6), 0.0, 1.0)
    shade_mod = shade_floor + (1.0 - shade_floor) * shade

    t_low = np.clip(elev_t / max(mid_break, 1e-6), 0.0, 1.0)
    t_hi = np.clip((elev_t - mid_break) / max(1.0 - mid_break, 1e-6), 0.0, 1.0)

    out = np.empty(dem.shape + (3,), dtype=np.float32)
    for i, (lo, mi, hi) in enumerate(
        zip(palette_low, palette_mid, palette_high)
    ):
        chan = ((lo * (1 - t_low) + mi * t_low) * (1 - t_hi) + hi * t_hi) * shade_mod
        out[..., i] = chan
    return out


def base_colors_water(
    dem: np.ndarray,
    sea_level: float,
    shallow: tuple[float, float, float],
    deep: tuple[float, float, float],
    depth_range: float,
) -> np.ndarray:
    """Depth-shaded water color. Returns (h, w, 3) float32 in [0, 1].

    NaN cells (no DEM data, treated as ocean) get depth_t = 0.75.
    """
    depth_t = np.clip((sea_level - dem) / max(depth_range, 1e-6), 0.0, 1.0)
    depth_t = np.where(np.isnan(dem), 0.75, depth_t)
    out = np.empty(dem.shape + (3,), dtype=np.float32)
    for i in range(3):
        out[..., i] = shallow[i] - depth_t * (shallow[i] - deep[i])
    return out
