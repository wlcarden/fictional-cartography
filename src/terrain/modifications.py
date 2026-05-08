"""DEM modifications: sea-level rise, water mask derivation, sinking regions."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image, ImageDraw

if TYPE_CHECKING:
    from src.pipeline import MapBounds


def water_mask(dem: np.ndarray, sea_level: float) -> np.ndarray:
    """True where the cell is at or below sea_level, or has no data (NaN)."""
    return (dem <= sea_level) | np.isnan(dem)


def apply_sinking(
    water: np.ndarray,
    cfg: dict,
    bounds: "MapBounds",
    project_root: Path,
) -> tuple[np.ndarray, int]:
    """Modify `water` in-place by OR-ing in the polygon mask of a sunk region.

    Schema (from cfg.sinking):
      enabled: true
      region:  "Virginia"          # name lookup in boundary cache (auto-fetch)
      source:  "virginia_border.json"  # OPTIONAL override; relative path to a
                                         # GeoJSON file under config/
      method:  "nan_mask"          # currently the only supported method;
                                     # makes the region behave like water

    Returns (water, n_cells_added). n_cells_added is for logging — how many
    new "water" cells the sinking introduced. If sinking is disabled or no
    polygon can be loaded, water is returned unchanged.

    The polygon source is resolved in priority order:
      1. cfg.sinking.source (file path, relative to project root) — if set
      2. boundary cache via ensure_state_boundary(region)         — fallback

    Multiple polygons (MultiPolygon) are unioned via OR.
    """
    if not cfg or not cfg.get("enabled"):
        return water, 0

    method = cfg.get("method", "nan_mask")
    if method != "nan_mask":
        print(f"    [sinking] method {method!r} not supported")
        return water, 0

    polygons = _resolve_sinking_polygons(cfg, project_root)
    if not polygons:
        print(f"    [sinking] no polygon for region={cfg.get('region')!r}; skipping")
        return water, 0

    h, w = water.shape
    sinking_mask = _rasterize_polygons(polygons, bounds, h, w)
    n_added = int(np.count_nonzero(sinking_mask & ~water))
    np.logical_or(water, sinking_mask, out=water)
    return water, n_added


def _resolve_sinking_polygons(
    cfg: dict, project_root: Path
) -> list[list[tuple[float, float]]]:
    """Return the list of polygons (in [(lat, lon), ...] form) to sink.

    Tries the explicit `source:` file first, falls back to the boundary
    cache lookup by `region:` name.
    """
    source = cfg.get("source")
    if source:
        # Resolve `source` relative to the project's config/ directory.
        # Accept absolute paths too. Reject anything that escapes config/.
        path = Path(source)
        if not path.is_absolute():
            path = project_root / "config" / source
        if not path.exists():
            print(f"    [sinking] source file not found: {path}")
        else:
            try:
                with open(path) as f:
                    geom = json.load(f)
                # Accept either {type, coordinates} or a list of polygons
                # (lenient — saves users from needing to wrap in GeoJSON).
                return _parse_geojson_polygons(geom)
            except (OSError, json.JSONDecodeError) as e:
                print(f"    [sinking] failed to read {path}: {e}")

    region = cfg.get("region")
    if not region:
        return []
    from src.data.boundaries import ensure_state_boundary
    return ensure_state_boundary(region)


def _parse_geojson_polygons(geom: dict) -> list[list[tuple[float, float]]]:
    """Pull (lat, lon) outer rings from a GeoJSON Polygon/MultiPolygon."""
    gtype = geom.get("type")
    coords = geom.get("coordinates", [])
    polys: list[list[tuple[float, float]]] = []
    if gtype == "Polygon" and coords:
        polys.append([(lat, lon) for lon, lat in coords[0]])
    elif gtype == "MultiPolygon":
        for poly in coords:
            if poly:
                polys.append([(lat, lon) for lon, lat in poly[0]])
    return polys


def _rasterize_polygons(
    polygons: list[list[tuple[float, float]]],
    bounds: "MapBounds",
    h: int,
    w: int,
) -> np.ndarray:
    """Rasterize lat/lon polygons into a (h, w) boolean mask aligned with the
    DEM. Polygons are filled (even-odd rule via PIL) and unioned via OR.
    """
    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    for poly in polygons:
        # Project each vertex to pixel coords. We project to TERRAIN coords
        # (no border offset), since this mask aligns with the DEM (h × w).
        pts = [bounds.to_pixel(lat, lon, h, w) for (lat, lon) in poly]
        if len(pts) >= 3:
            draw.polygon(pts, fill=255)
    return np.array(img, dtype=bool)
