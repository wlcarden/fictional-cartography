"""Overpass API queries with disk caching, and parsers for the responses we use."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Sequence

from src.pipeline import MapBounds, cache_dir


# Mirrors are tried in order. Each one is a separate Overpass instance with
# its own queue, so a 504 on one does not necessarily mean the next is busy.
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
RETRYABLE_STATUS = {429, 502, 503, 504}
MAX_ATTEMPTS = 4


def query(
    query_text: str, cache_name: str, force_refresh: bool = False
) -> dict | None:
    """Run an Overpass query, caching the JSON result by cache_name.

    Retries on 429/502/503/504 with exponential backoff and rotates through
    mirrors so a busy primary server doesn't block the whole render.
    """
    cache_path = cache_dir("overpass") / f"{cache_name}.json"
    if cache_path.exists() and not force_refresh:
        with open(cache_path) as f:
            return json.load(f)
    print(f"  overpass: querying {cache_name}")
    payload = urllib.parse.urlencode({"data": query_text}).encode()

    last_err: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        url = OVERPASS_MIRRORS[attempt % len(OVERPASS_MIRRORS)]
        req = urllib.request.Request(url, data=payload)
        req.add_header(
            "User-Agent",
            # OSM/Overpass policy expects a contact URL or repo for User-Agent.
            "fictional-cartography/0.1 (+https://github.com/wlcarden/fictional-cartography)",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read())
            cache_path.write_text(json.dumps(result))
            n = len(result.get("elements", []))
            print(f"  overpass: {cache_name} -> {n} elements (via {url.split('/')[2]})")
            return result
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code not in RETRYABLE_STATUS:
                raise
            wait = 2 ** attempt * 5
            print(f"  overpass: HTTP {e.code} from {url.split('/')[2]}; "
                  f"retrying in {wait}s (attempt {attempt + 1}/{MAX_ATTEMPTS})")
            time.sleep(wait)
        except urllib.error.URLError as e:
            last_err = e
            wait = 2 ** attempt * 5
            print(f"  overpass: {e.reason} from {url.split('/')[2]}; "
                  f"retrying in {wait}s (attempt {attempt + 1}/{MAX_ATTEMPTS})")
            time.sleep(wait)
    raise RuntimeError(
        f"Overpass query {cache_name!r} failed after {MAX_ATTEMPTS} attempts: {last_err}"
    )


def _network_label(net: str) -> str:
    """Render an OSM network code as a short prefix for display/labeling.
    'US:I' -> 'I', 'US:NJ' -> 'NJ', 'US:NJ:CR' -> 'NJ' (last hop wins)."""
    return net.split(":")[-1]


def fetch_routes(
    bounds: MapBounds, networks: Sequence[str], cache_name: str
) -> dict | None:
    """Fetch road-route relations matching any of the configured OSM network codes.

    Modern OSM data carries the road class in the `network` tag (e.g. "US:I" for
    Interstate, "US:US" for US Highway, "US:NJ" for state routes). We escape any
    colons in the values for the regex (they're not regex metacharacters in the
    Overpass dialect, but staying explicit avoids surprises).
    """
    pattern = "|".join(net.replace(":", r"\:") for net in networks)
    bbox = f"{bounds.lat_s},{bounds.lon_w},{bounds.lat_n},{bounds.lon_e}"
    q = f"""[out:json][timeout:120];
relation["type"="route"]["route"="road"]
  ["network"~"^({pattern})$"]
  ({bbox});
out body;>;out skel qt;"""
    return query(q, cache_name)


def fetch_landuse(bounds: MapBounds, cache_name: str = "landuse") -> dict | None:
    """Fetch residential/commercial/industrial/retail land-use polygons."""
    bbox = f"{bounds.lat_s},{bounds.lon_w},{bounds.lat_n},{bounds.lon_e}"
    q = f"""[out:json][timeout:180];
way["landuse"~"residential|commercial|industrial|retail"]
  ({bbox});
out body;>;out skel qt;"""
    return query(q, cache_name)


def fetch_admin_boundary(
    name: str, admin_level: int = 4, within_state: str | None = None
) -> dict | None:
    """Fetch a named administrative boundary as a multipolygon GeoJSON-shape.

    Query: `relation[boundary=administrative][admin_level=N][name=NAME]`.
    Default admin_level=4 corresponds to US states; admin_level=6 is a US
    county. Returns {"type": "Polygon"|"MultiPolygon", "coordinates": [...]}
    matching the format `cache/boundaries/state_boundaries.json` already uses.

    `within_state` disambiguates names that repeat across states. Many US
    county names collide (there's a "Sussex County" in NJ, DE, and VA); a
    bare name query returns whichever relation OSM lists first. When
    `within_state` is given we wrap the relation lookup in an Overpass area
    filter so only the county geographically inside that state matches.

    The relation comes back with hundreds of `outer`-role way members.
    We extract each way's inline geometry (via `out geom`) and run a
    greedy endpoint-matching stitcher to assemble closed rings. The result
    isn't always topologically perfect for complex coastal states, but
    it's accurate enough for a rasterized mask (PIL polygon fill uses
    even-odd rule which tolerates self-intersection).

    Returns None on any fetch error so the caller can fall back gracefully.
    """
    safe = name.replace('"', '\\"').replace('\\', '\\\\')
    cache_safe = "".join(c if c.isalnum() else "_" for c in name).strip("_").lower()
    if within_state:
        state_safe = within_state.replace('"', '\\"').replace('\\', '\\\\')
        state_cache = "".join(
            c if c.isalnum() else "_" for c in within_state
        ).strip("_").lower()
        cache_name = f"admin_{admin_level}_{cache_safe}_in_{state_cache}"
        # Constrain the relation to those inside the named state's area.
        # `area[...]->.st` materializes the state polygon as a named area
        # set; `(area.st)` then filters the county relation to members
        # geographically within it.
        q = f"""[out:json][timeout:120];
area["boundary"="administrative"]["admin_level"="4"]["name"="{state_safe}"]->.st;
relation["boundary"="administrative"]["admin_level"="{admin_level}"]["name"="{safe}"](area.st);
out geom;"""
    else:
        cache_name = f"admin_{admin_level}_{cache_safe}"
        q = f"""[out:json][timeout:120];
relation["boundary"="administrative"]["admin_level"="{admin_level}"]["name"="{safe}"];
out geom;"""
    data = query(q, cache_name)
    if not data:
        return None

    elements = data.get("elements", [])
    if not elements:
        return None
    relation = elements[0]
    outer_ways: list[list[tuple[float, float]]] = []
    for member in relation.get("members", []):
        if member.get("role") != "outer":
            continue
        geom = member.get("geometry") or []
        # `out geom` gives us each way's points as [{lat, lon}, ...].
        # Convert to (lat, lon) tuples; skip degenerate ways.
        pts = [(float(p["lat"]), float(p["lon"])) for p in geom if "lat" in p]
        if len(pts) >= 2:
            outer_ways.append(pts)
    if not outer_ways:
        return None

    rings = _stitch_rings(outer_ways)
    # Convert to GeoJSON layout (each ring is [[lon, lat], ...]) and wrap
    # as MultiPolygon so callers get a uniform shape.
    coords = [
        [[[lon, lat] for (lat, lon) in ring]]   # MultiPolygon: list of polygons,
        for ring in rings if len(ring) >= 4      # each polygon = [outer_ring, *holes]
    ]
    if not coords:
        return None
    return {"type": "MultiPolygon", "coordinates": coords}


def _stitch_rings(
    ways: list[list[tuple[float, float]]],
    epsilon: float = 1e-7,
) -> list[list[tuple[float, float]]]:
    """Greedy endpoint-matching: chain ways together into closed rings.

    OSM relation members come in arbitrary order. We pick a way, then
    repeatedly find another whose endpoint matches one of ours and append
    (reversing if necessary). Ring closes when both endpoints meet, or
    when no further match exists (in which case we accept the open chain
    and start a new ring with the next remaining way).

    Float equality via `epsilon` so node positions that are byte-identical
    in OSM but pass through float arithmetic still match. State boundary
    nodes are usually identical without rounding, but defensive doesn't hurt.
    """
    def near(a: tuple[float, float], b: tuple[float, float]) -> bool:
        return abs(a[0] - b[0]) < epsilon and abs(a[1] - b[1]) < epsilon

    remaining = [list(w) for w in ways]
    rings: list[list[tuple[float, float]]] = []

    while remaining:
        chain = remaining.pop(0)
        progress = True
        while progress and remaining:
            progress = False
            tail = chain[-1]
            head = chain[0]
            for i, w in enumerate(remaining):
                if near(w[0], tail):
                    chain.extend(w[1:])
                    remaining.pop(i); progress = True; break
                if near(w[-1], tail):
                    chain.extend(reversed(w[:-1]))
                    remaining.pop(i); progress = True; break
                if near(w[-1], head):
                    chain = w[:-1] + chain
                    remaining.pop(i); progress = True; break
                if near(w[0], head):
                    chain = list(reversed(w[1:])) + chain
                    remaining.pop(i); progress = True; break
        # Close the ring if it isn't already
        if chain and not near(chain[0], chain[-1]):
            chain.append(chain[0])
        rings.append(chain)
    return rings


def fetch_osm_nodes(
    bounds: MapBounds, node_filter: str, cache_name: str
) -> dict | None:
    """Run an arbitrary `node[...]` Overpass query and return the JSON.

    `node_filter` is the OSM filter expression that goes BETWEEN `node` and
    the bbox — e.g. for WMATA stations it would be:

        '["railway"="station"]["network"="WMATA"]'

    The schema wraps this in `node{filter}({bbox}); out body;` so the result
    is a list of node elements with lat/lon and tags. Used by contamination
    spread (multi-source Dijkstra seeds) and any future POI-driven feature.
    """
    if not node_filter:
        return {"elements": []}
    bbox = f"{bounds.lat_s},{bounds.lon_w},{bounds.lat_n},{bounds.lon_e}"
    q = f"""[out:json][timeout:120];
node{node_filter}
  ({bbox});
out body;"""
    return query(q, cache_name)


def fetch_road_graph(
    bounds: MapBounds, road_types: Sequence[str], cache_name: str
) -> dict | None:
    """Fetch every OSM way with `highway` ∈ road_types in the bounds.

    Returns the raw Overpass body — a parser later turns it into a connected
    node/edge graph for A* pathfinding (see src/styling/barriers.py).

    `road_types` is a list of OSM `highway` values, ordered by preference for
    the path: ["motorway", "trunk", "primary", "secondary", "tertiary",
    "residential"]. The pathfinder uses ordering to compute per-class costs.
    """
    if not road_types:
        return {"elements": []}
    pattern = "|".join(road_types)
    bbox = f"{bounds.lat_s},{bounds.lon_w},{bounds.lat_n},{bounds.lon_e}"
    q = f"""[out:json][timeout:180];
way["highway"~"^({pattern})$"]
  ({bbox});
out body;>;out skel qt;"""
    return query(q, cache_name)


def parse_routes(
    data: dict | None,
) -> dict[tuple[str, str], list[list[tuple[float, float]]]]:
    """Parse route relations into {(network, ref): [[(lat, lon), ...], ...]}.

    Both `network` (e.g. "US:I") and `ref` (e.g. "95") come from OSM tags; the
    composite key keeps state and federal highways distinct even when they share
    a number. Routes lacking a network tag are bucketed under "" so the caller
    can still display or filter them.
    """
    if not data:
        return {}
    nodes: dict[int, tuple[float, float]] = {}
    ways_by_id: dict[int, list[int]] = {}
    relations: list[dict] = []
    for e in data.get("elements", []):
        t = e["type"]
        if t == "node":
            nodes[e["id"]] = (e["lat"], e["lon"])
        elif t == "way":
            ways_by_id[e["id"]] = e.get("nodes", [])
        elif t == "relation":
            relations.append(e)

    routes: dict[tuple[str, str], list[list[tuple[float, float]]]] = {}
    for rel in relations:
        tags = rel.get("tags", {})
        ref = tags.get("ref", "")
        network = tags.get("network", "")
        if not ref:
            continue
        segments: list[list[tuple[float, float]]] = []
        for member in rel.get("members", []):
            if member["type"] == "way" and member["ref"] in ways_by_id:
                seg = [nodes[nid] for nid in ways_by_id[member["ref"]] if nid in nodes]
                if seg:
                    segments.append(seg)
        if segments:
            routes.setdefault((network, ref), []).extend(segments)
    return routes


def parse_polygons(data: dict | None) -> list[list[tuple[float, float]]]:
    """Extract closed-way polygon vertex lists from an Overpass response."""
    if not data:
        return []
    nodes: dict[int, tuple[float, float]] = {}
    ways: list[dict] = []
    for e in data.get("elements", []):
        t = e["type"]
        if t == "node":
            nodes[e["id"]] = (e["lat"], e["lon"])
        elif t == "way":
            ways.append(e)
    polys: list[list[tuple[float, float]]] = []
    for way in ways:
        nids = way.get("nodes", [])
        coords = [nodes[n] for n in nids if n in nodes]
        if len(coords) >= 3:
            polys.append(coords)
    return polys
