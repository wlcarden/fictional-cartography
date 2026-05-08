"""Regional tints: radial and directional masks plus color/desaturation effects."""
from __future__ import annotations

from typing import Sequence

import numpy as np
from scipy.ndimage import gaussian_filter

from src.pipeline import MapBounds


# Sigma applied to every region mask before tint/desat — softens sharp edges
# and matches the reference render's character. Adjustable per call site.
DEFAULT_MASK_SIGMA = 15.0

# Default rate at which a directional bias falls off with distance from the
# source's bias direction. Matches the reference's `* 3` factor.
DEFAULT_DIR_BIAS_FACTOR = 3.0


def _coord_grids(bounds: MapBounds, h: int, w: int) -> tuple[np.ndarray, np.ndarray]:
    """2-D arrays of (lat, lon) for every pixel in an h x w terrain image."""
    lats = np.linspace(bounds.lat_n, bounds.lat_s, h, dtype=np.float32)[:, None]
    lons = np.linspace(bounds.lon_w, bounds.lon_e, w, dtype=np.float32)[None, :]
    lats_2d = np.broadcast_to(lats, (h, w))
    lons_2d = np.broadcast_to(lons, (h, w))
    return lats_2d, lons_2d


def radial_mask(
    bounds: MapBounds,
    h: int,
    w: int,
    center: tuple[float, float],
    inner_radius: float,
    outer_radius: float,
) -> np.ndarray:
    """Linear falloff from 1 inside `inner_radius` to 0 beyond `outer_radius`.

    Distances are in degrees, with longitudinal distance corrected by
    cos(latitude) so the mask is approximately circular at mid-latitudes.
    """
    lats_2d, lons_2d = _coord_grids(bounds, h, w)
    clat, clon = center
    dlat = lats_2d - clat
    dlon = (lons_2d - clon) * np.cos(np.radians(clat))
    dist = np.sqrt(dlat * dlat + dlon * dlon)
    span = max(outer_radius - inner_radius, 1e-6)
    return np.clip(1 - (dist - inner_radius) / span, 0, 1).astype(np.float32)


def directional_mask(
    bounds: MapBounds,
    h: int,
    w: int,
    source: tuple[float, float],
    direction: str,
    range_: float,
    bias_factor: float = DEFAULT_DIR_BIAS_FACTOR,
) -> np.ndarray:
    """Falloff from `source` along a cardinal/intercardinal direction.

    Multiplies a range-based radial falloff by a directional bias that's
    1 along the named direction and 0 against it.
    """
    lats_2d, lons_2d = _coord_grids(bounds, h, w)
    slat, slon = source
    dlat = lats_2d - slat
    dlon = (lons_2d - slon) * np.cos(np.radians(slat))
    # Squash latitude so falloff is more horizontal — visually-circular at mid-lat
    dist = np.sqrt((dlat * 0.5) ** 2 + dlon * dlon)
    range_falloff = np.clip(1 - dist / max(range_, 1e-6), 0, 1)

    d = direction.lower()
    if d == "south":
        bias = -dlat
    elif d == "north":
        bias = dlat
    elif d == "east":
        bias = dlon
    elif d == "west":
        bias = -dlon
    elif d == "southeast":
        bias = -dlat  # reference's "southeast" is a southern bias only; keep that
    elif d == "northeast":
        bias = dlat
    elif d == "southwest":
        bias = -dlat
    elif d == "northwest":
        bias = dlat
    else:
        raise ValueError(
            f"Unknown direction {direction!r}; "
            "expected one of: north, south, east, west, northeast, "
            "northwest, southeast, southwest"
        )
    bias = np.clip(bias * bias_factor, 0, 1)
    return (range_falloff * bias).astype(np.float32)


def apply_tint(
    rgb: np.ndarray,
    mask: np.ndarray,
    land_mask: np.ndarray,
    dr: float,
    dg: float,
    db: float,
    sigma: float = DEFAULT_MASK_SIGMA,
) -> None:
    """Add an RGB shift under (mask & land_mask), in-place. rgb is float [0, 1]."""
    m = gaussian_filter((mask * land_mask).astype(np.float32), sigma=sigma)
    rgb[..., 0] = np.clip(rgb[..., 0] + m * dr, 0, 1)
    rgb[..., 1] = np.clip(rgb[..., 1] + m * dg, 0, 1)
    rgb[..., 2] = np.clip(rgb[..., 2] + m * db, 0, 1)


def apply_desaturation(
    rgb: np.ndarray,
    mask: np.ndarray,
    land_mask: np.ndarray,
    blend: float,
    sigma: float = DEFAULT_MASK_SIGMA,
) -> None:
    """Blend RGB toward Rec.601 luma under (mask & land_mask), in-place."""
    m = gaussian_filter((mask * land_mask).astype(np.float32), sigma=sigma)
    grey = 0.3 * rgb[..., 0] + 0.59 * rgb[..., 1] + 0.11 * rgb[..., 2]
    factor = m * blend
    inv = 1 - factor
    rgb[..., 0] = rgb[..., 0] * inv + grey * factor
    rgb[..., 1] = rgb[..., 1] * inv + grey * factor
    rgb[..., 2] = rgb[..., 2] * inv + grey * factor


def apply_all_region_tints(
    rgb: np.ndarray,
    regions: Sequence[dict],
    bounds: MapBounds,
    land_mask: np.ndarray,
    constraint_masks: dict | None = None,
    barrier_paths: dict | None = None,
) -> None:
    """Apply every region's `tint` config to rgb, in-place.

    Each region dict may contain a `tint` sub-dict with:
      type:           "radial" (default), "directional", or "band"
      center / inner_radius / outer_radius:    radial params
      source / direction / range:              directional params
      between: [name_a, name_b]:                band params (two barrier names)
      strength:       multiplier on the mask, default 1.0; applied to both
                      desat and tint by default
      desat_strength: optional override for desat mask scale
      tint_strength:  optional override for tint mask scale
      desaturate:     0..1 blend toward grey
      adjustments:    {r, g, b} additive shift in [-1, 1]
      constraint:     optional string like "north_of_wall" — the tint mask
                      gets multiplied by `constraint_masks[constraint]`,
                      which the caller (pipeline) builds from barrier paths.
                      Allows region tints to be clipped by a barrier so e.g.
                      "The Red Embankment" only colors land north of The Wall.

    `constraint_masks` is `{constraint_string → (h, w) boolean array}`. Any
    constraint name not in the dict is silently ignored (logs a warning).

    `barrier_paths` is `{barrier_name → [(lat, lon), ...]}`, required for
    `type: band` tints. Bands resolve to the polygon between the two named
    barrier paths — the same geometry the buffer_zone hatching uses, but
    applied as a terrain-level RGB shift instead of a stripe overlay.
    """
    h, w = rgb.shape[:2]
    constraint_masks = constraint_masks or {}
    barrier_paths = barrier_paths or {}
    for region in regions:
        cfg = region.get("tint")
        if not cfg:
            continue
        ttype = cfg.get("type", "radial")
        if ttype == "directional":
            base = directional_mask(
                bounds, h, w,
                source=tuple(cfg["source"]),
                direction=cfg["direction"],
                range_=cfg["range"],
            )
        elif ttype == "band":
            between = cfg.get("between") or []
            if len(between) != 2:
                print(f"    [tint] band {region.get('name')!r}: "
                      f"`between` needs exactly 2 barrier names; skipping")
                continue
            a_path = barrier_paths.get(between[0])
            b_path = barrier_paths.get(between[1])
            if not a_path or not b_path:
                missing = [n for n in between if not barrier_paths.get(n)]
                print(f"    [tint] band {region.get('name')!r}: "
                      f"missing barrier path(s) {missing}; skipping")
                continue
            from src.styling.barriers import band_mask as _band_mask
            base = _band_mask(a_path, b_path, bounds, h, w).astype(np.float32)
        else:
            base = radial_mask(
                bounds, h, w,
                center=tuple(cfg["center"]),
                inner_radius=float(cfg["inner_radius"]),
                outer_radius=float(cfg["outer_radius"]),
            )

        # Constraint clipping: multiply the tint mask by a (possibly
        # compound) half-plane mask derived from one or more barriers.
        # The caller pre-resolves these via canonical_constraint_key
        # (which handles both legacy single-string and compound list
        # configs uniformly).
        from src.styling.barriers import canonical_constraint_key
        ckey = canonical_constraint_key(cfg)
        if ckey:
            cmask = constraint_masks.get(ckey)
            if cmask is not None:
                base = base * cmask.astype(np.float32)
            else:
                print(f"    [tint] constraint {ckey!r} on region "
                      f"{region.get('name')!r}: no matching mask; ignoring")

        strength = float(cfg.get("strength", 1.0))
        desat_strength = float(cfg.get("desat_strength", strength))
        tint_strength = float(cfg.get("tint_strength", strength))

        desat = float(cfg.get("desaturate", 0.0))
        if desat > 0:
            apply_desaturation(rgb, base * desat_strength, land_mask, blend=desat)

        adj = cfg.get("adjustments", {})
        dr = float(adj.get("r", 0.0))
        dg = float(adj.get("g", 0.0))
        db = float(adj.get("b", 0.0))
        if dr or dg or db:
            apply_tint(rgb, base * tint_strength, land_mask, dr, dg, db)
