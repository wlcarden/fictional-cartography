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


def ensure_county_boundary(
    county: str, state: str
) -> list[list[tuple[float, float]]]:
    """Return the polygon list for a county, fetching from Overpass if needed.

    Counties live at admin_level=6 in OSM. Because county names repeat across
    states ("Sussex County" exists in NJ, DE, and VA), the fetch is scoped to
    the containing `state` via an Overpass area filter.

    The result is cached in the same `state_boundaries.json` library under the
    bare county name, so the name-based renderer (`draw_state_boundaries`)
    picks it up with no awareness that it's a county rather than a state.
    Reference it from a config's `state_boundaries.states` list by name.

    Returns [] if the polygon can't be obtained.
    """
    library = load_state_boundaries()
    if county in library:
        return library[county]

    print(f"  boundaries: fetching county {county!r} within {state!r}...")
    from src.data.overpass import fetch_admin_boundary
    geom = fetch_admin_boundary(county, admin_level=6, within_state=state)
    if geom is None:
        print(f"  boundaries: no county boundary returned for {county!r}")
        return []

    path = cache_dir("boundaries") / CACHE_FILE
    if path.exists():
        with open(path) as f:
            raw = json.load(f)
    else:
        raw = {}
    raw[county] = geom
    with open(path, "w") as f:
        json.dump(raw, f, indent=2)
    print(f"  boundaries: cached {county!r} ({geom.get('type')}, "
          f"{len(geom.get('coordinates', []))} polygon(s))")

    return load_state_boundaries().get(county, [])


def ensure_boundaries(
    names: list[str], county_of: str | None = None
) -> int:
    """Ensure every named boundary is in the cache, fetching any that aren't.

    This is the render-time hook that makes boundary maps reproducible on a
    fresh clone — mirroring how SRTM tiles and OSM roads auto-fetch on first
    render. The draw path (`draw_state_boundaries`) only reads the cache, so
    without this a referenced-but-uncached boundary silently doesn't draw.

    When `county_of` is set, missing names are fetched as counties
    (admin_level 6) within that state; otherwise as states (admin_level 4).
    A map referencing a county must therefore set `state_boundaries.county_of`
    so the fetch can disambiguate (e.g. "Sussex County" exists in NJ/DE/VA).

    Returns the count of boundaries newly fetched this call (0 when all were
    already cached — the common warm-cache case).
    """
    fetched = 0
    for name in names:
        if name in load_state_boundaries():
            continue
        if county_of:
            polys = ensure_county_boundary(name, county_of)
        else:
            polys = ensure_state_boundary(name)
        if polys:
            fetched += 1
    return fetched
