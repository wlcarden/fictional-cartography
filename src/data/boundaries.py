"""State / administrative boundary loading.

The cache file is a dict {state_name: GeoJSON geometry}. Geometries are
Polygon or MultiPolygon with [lon, lat] vertex order per GeoJSON convention;
this module returns (lat, lon) lists to match the rest of the codebase.

If a requested state isn't in the cache, `ensure_state_boundary()` fetches
it from Overpass and writes it back into the cache file so future runs are
fast.
"""
from __future__ import annotations

import json

from src.pipeline import cache_dir


CACHE_FILE = "state_boundaries.json"


def load_state_boundaries() -> dict[str, list[list[tuple[float, float]]]]:
    """Return {state_name: [polygon, ...]} where each polygon is [(lat, lon), ...].

    A MultiPolygon is flattened to a list of polygons. Returns an empty dict
    if the cache file isn't present.
    """
    path = cache_dir("boundaries") / CACHE_FILE
    if not path.exists():
        return {}
    with open(path) as f:
        raw = json.load(f)

    out: dict[str, list[list[tuple[float, float]]]] = {}
    for name, geom in raw.items():
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])
        polys: list[list[tuple[float, float]]] = []
        if gtype == "Polygon":
            # coordinates: [outer_ring, hole1, hole2, ...]; we keep only the outer
            if coords:
                polys.append([(lat, lon) for lon, lat in coords[0]])
        elif gtype == "MultiPolygon":
            for poly in coords:
                if poly:
                    polys.append([(lat, lon) for lon, lat in poly[0]])
        out[name] = polys
    return out


def ensure_state_boundary(name: str) -> list[list[tuple[float, float]]]:
    """Return the polygon list for `name`, fetching from Overpass if needed.

    First checks the on-disk cache `state_boundaries.json`. If absent,
    queries Overpass via fetch_admin_boundary and persists the result back
    into the cache. Returns [] if the polygon can't be obtained.

    Used by the sinking feature (and any future region-mask consumer) to
    get a polygon by state name without forcing the user to populate the
    cache manually.
    """
    library = load_state_boundaries()
    if name in library:
        return library[name]

    # Cache miss → fetch via Overpass.
    print(f"  boundaries: fetching admin boundary for {name!r}...")
    from src.data.overpass import fetch_admin_boundary
    geom = fetch_admin_boundary(name)
    if geom is None:
        print(f"  boundaries: no admin boundary returned for {name!r}")
        return []

    # Persist back into the shared cache file.
    path = cache_dir("boundaries") / CACHE_FILE
    if path.exists():
        with open(path) as f:
            raw = json.load(f)
    else:
        raw = {}
    raw[name] = geom
    with open(path, "w") as f:
        json.dump(raw, f, indent=2)
    print(f"  boundaries: cached {name!r} ({geom.get('type')}, "
          f"{len(geom.get('coordinates', []))} polygon(s))")

    # Re-parse via load to get the canonical [(lat, lon), ...] shape.
    return load_state_boundaries().get(name, [])
