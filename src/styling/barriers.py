"""A* barrier rendering — narrative walls / patrol lines drawn along the OSM
road network.

The schema (see config/dominus-columbia.yaml.original):

    barriers:
      - name: "The Wall"
        type: wall
        method: astar_road_network
        road_types: ["motorway", "trunk", ...]
        endpoints:
          east: {lat: 39.22, lon: -76.58}
          west: {lat: 39.05, lon: -77.20}
        corridor:
          lat_min: 38.92
          lat_max: 39.30
          penalty: 500000
        style:
          color: [200, 150, 90]
          width: 3
          shadow_width: 5

The algorithm:
  1. Fetch every OSM way with `highway` in `road_types` for the bounds.
  2. Build a node graph: {id → (lat, lon)} + {id → [(neighbor, dist_m, type)]}.
  3. Snap the configured endpoints to their nearest graph nodes.
  4. Run A* with cost = edge_length × type_multiplier + corridor_penalty.
  5. Convert the path's lat/lon sequence to canvas pixels and draw it.

Patrol Line (astar_offset method) and decorative styles (hash marks, dashed)
are deferred to follow-ups.
"""
from __future__ import annotations

import heapq
import math
from typing import TYPE_CHECKING

from PIL import Image, ImageChops, ImageDraw

from src.data.overpass import fetch_road_graph

if TYPE_CHECKING:
    from src.pipeline import MapBounds


# Per-road-type cost multipliers. Lower = more attractive to the path.
# Tuned so motorways cost 1×, residential ~5× — the path will detour through
# residential streets only when no arterial is available in the corridor.
_ROAD_TYPE_COST = {
    "motorway":     1.0,
    "trunk":        1.2,
    "primary":      1.5,
    "secondary":    2.2,
    "tertiary":     3.0,
    "unclassified": 4.0,
    "residential":  5.0,
    "service":      8.0,
}


# ---------------------------------------------------------------------------
# Graph build
# ---------------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two (lat, lon) pairs.

    Used both as edge weight (real-world segment length) and as the A*
    heuristic (always ≤ real path distance, so A* is admissible).
    """
    R = 6_371_000.0   # Earth radius in meters
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def build_road_graph(osm_data: dict) -> tuple[dict, dict]:
    """Convert raw Overpass output into a graph for pathfinding.

    Returns (nodes, adj):
      nodes: {node_id (int) → (lat, lon)}
      adj:   {node_id → list of (neighbor_id, edge_length_m, road_type)}

    Each OSM `way` is a sequence of node ids; we add edges between consecutive
    pairs. Nodes that appear in multiple ways become junction points
    automatically (same id → same dict entry).
    """
    elements = (osm_data or {}).get("elements", [])

    # First pass: collect node positions.
    nodes: dict[int, tuple[float, float]] = {}
    for el in elements:
        if el.get("type") == "node":
            nodes[el["id"]] = (float(el["lat"]), float(el["lon"]))

    # Second pass: walk each way's node sequence, emitting bidirectional edges.
    adj: dict[int, list[tuple[int, float, str]]] = {}
    for el in elements:
        if el.get("type") != "way":
            continue
        node_ids = el.get("nodes") or []
        if len(node_ids) < 2:
            continue
        rt = (el.get("tags") or {}).get("highway", "unclassified")
        for a_id, b_id in zip(node_ids, node_ids[1:]):
            a_pos = nodes.get(a_id)
            b_pos = nodes.get(b_id)
            if a_pos is None or b_pos is None:
                continue
            dist = _haversine_m(a_pos[0], a_pos[1], b_pos[0], b_pos[1])
            adj.setdefault(a_id, []).append((b_id, dist, rt))
            adj.setdefault(b_id, []).append((a_id, dist, rt))

    return nodes, adj


def find_nearest_node(
    nodes: dict, lat: float, lon: float
) -> int | None:
    """O(N) linear search for the closest graph node to a (lat, lon) point.

    Adequate for one-shot endpoint snapping (city-scale graphs are <100k
    nodes; takes ~5ms). For repeated queries a spatial index would pay off.
    Returns None if the graph is empty.
    """
    if not nodes:
        return None
    best_id = None
    best_dist = math.inf
    for nid, (nlat, nlon) in nodes.items():
        # Euclidean in degree-space is fine here because we only need to
        # find a *nearby* node — exact great-circle isn't worth the cosines.
        d = (nlat - lat) ** 2 + (nlon - lon) ** 2
        if d < best_dist:
            best_dist = d
            best_id = nid
    return best_id


def find_nearest_nodes(
    nodes: dict, lat: float, lon: float, k: int = 20
) -> list[int]:
    """Return up to `k` graph node ids ordered by distance from (lat, lon).

    Used as a snapping-fallback for A*: if the literally-nearest node
    sits on a tiny disconnected component (an isolated parking lot, a
    decommissioned spur), we try progressively further candidates until
    A* finds a path. ~5ms for a 1.5M-node graph since we just heap-track
    the top-k.
    """
    if not nodes:
        return []
    import heapq
    # Keep a max-heap of size k via negation. (-dist, nid)
    heap: list[tuple[float, int]] = []
    for nid, (nlat, nlon) in nodes.items():
        d = (nlat - lat) ** 2 + (nlon - lon) ** 2
        if len(heap) < k:
            heapq.heappush(heap, (-d, nid))
        elif -d > heap[0][0]:
            heapq.heapreplace(heap, (-d, nid))
    # Sort ascending by actual distance
    return [nid for _, nid in sorted(heap, key=lambda t: -t[0])]


# ---------------------------------------------------------------------------
# A* pathfinding
# ---------------------------------------------------------------------------

def astar(
    nodes: dict,
    adj: dict,
    start: int,
    goal: int,
    type_costs: dict[str, float] | None = None,
    corridor: dict | None = None,
) -> list[tuple[float, float]] | None:
    """A* shortest-path through the road graph, returning a (lat, lon) list.

    `type_costs` overrides the default road-type multipliers. Missing types
    fall through to the default table (so a YAML with road_types =
    ["motorway", "primary"] still works — other types are just expensive).

    `corridor` is an optional dict with:
      lat_min, lat_max:  the target latitude band
      penalty:           extra cost (in meters-equivalent) added to edges
                          whose endpoint lies outside the band

    Returns None if no path exists. For a connected graph between live
    endpoints this is rare, but worth handling.
    """
    if start == goal:
        return [nodes[start]]

    type_costs = {**_ROAD_TYPE_COST, **(type_costs or {})}

    if corridor:
        lat_min = float(corridor.get("lat_min", -90))
        lat_max = float(corridor.get("lat_max",  90))
        corridor_penalty = float(corridor.get("penalty", 0))
    else:
        lat_min, lat_max, corridor_penalty = -90.0, 90.0, 0.0

    goal_lat, goal_lon = nodes[goal]

    def heuristic(nid: int) -> float:
        lat, lon = nodes[nid]
        return _haversine_m(lat, lon, goal_lat, goal_lon)

    # Open set: a min-heap of (f_score, counter, node_id).
    # `counter` is a tiebreaker so heapq doesn't try to compare node ids when
    # f_scores collide (which would be valid here but defensive doesn't hurt).
    counter = 0
    open_heap: list[tuple[float, int, int]] = [(heuristic(start), 0, start)]
    came_from: dict[int, int] = {}
    g_score: dict[int, float] = {start: 0.0}
    closed: set[int] = set()

    while open_heap:
        _, _, current = heapq.heappop(open_heap)
        if current == goal:
            return _reconstruct_path(came_from, current, nodes)
        if current in closed:
            continue
        closed.add(current)

        for neighbor, edge_dist, road_type in adj.get(current, []):
            if neighbor in closed:
                continue
            n_lat, n_lon = nodes[neighbor]
            type_mult = type_costs.get(road_type, 5.0)
            cost = edge_dist * type_mult
            if not (lat_min <= n_lat <= lat_max):
                cost += corridor_penalty
            tentative = g_score[current] + cost
            if tentative < g_score.get(neighbor, math.inf):
                g_score[neighbor] = tentative
                came_from[neighbor] = current
                f = tentative + heuristic(neighbor)
                counter += 1
                heapq.heappush(open_heap, (f, counter, neighbor))

    return None


def _reconstruct_path(
    came_from: dict, current: int, nodes: dict
) -> list[tuple[float, float]]:
    """Walk parent pointers from goal back to start, reverse, return latlons."""
    path = [current]
    while current in came_from:
        current = came_from[current]
        path.append(current)
    path.reverse()
    return [nodes[nid] for nid in path]


def astar_offset(
    nodes: dict,
    adj: dict,
    start: int,
    goal: int,
    target_pts: list[tuple[float, float]],
    direction: str,
    corridor: dict,
    type_costs: dict[str, float] | None = None,
) -> list[tuple[float, float]] | None:
    """A* with a *corridor of attraction* defined by a target curve.

    Each candidate node is scored not just on edge length × type but also
    on its perpendicular deviation from the target curve. Used for derived
    barriers like a Patrol Line that tracks an existing Wall at a fixed
    offset — the corridor encourages the path to ride parallel to the
    reference even when the road network would otherwise wander.

    `target_pts` are pre-shifted (lat + offset, lon) points; `direction`
    determines which axis is "perpendicular" and which side is the wrong
    side for `south_hard_penalty`.
    """
    import numpy as np
    if start == goal:
        return [nodes[start]]
    if not target_pts:
        return None

    type_costs = {**_ROAD_TYPE_COST, **(type_costs or {})}
    deviation_penalty     = float(corridor.get("deviation_penalty", 0))
    south_hard_penalty    = float(corridor.get("south_hard_penalty", 0))
    max_deviation         = float(corridor.get("max_deviation", 999))
    max_deviation_penalty = float(corridor.get("max_deviation_penalty", 0))

    # Vectorize target curve for fast nearest-point lookup. For north/south
    # directions, deviation is along latitude; for east/west, longitude.
    target_arr = np.asarray(target_pts, dtype=np.float64)   # (N, 2): (lat, lon)
    target_lat = target_arr[:, 0]
    target_lon = target_arr[:, 1]
    perpendicular_is_lat = direction in ("north", "south")
    # "Wrong side" sign convention. For "north" offset (target above ref),
    # candidates SOUTH of target_lat are wrong. signed_dev = candidate - target.
    wrong_side_negative = direction in ("north", "east")

    def deviation_cost(lat: float, lon: float) -> float:
        # Find the reference target point closest in the AXIS PARALLEL TO
        # the reference line. For lat-shift targets (north/south), that's
        # the longitude axis; for lon-shift, the latitude axis.
        if perpendicular_is_lat:
            idx = int(np.argmin(np.abs(target_lon - lon)))
            t_lat, t_lon = target_lat[idx], target_lon[idx]
            signed_dev = lat - t_lat
        else:
            idx = int(np.argmin(np.abs(target_lat - lat)))
            t_lat, t_lon = target_lat[idx], target_lon[idx]
            signed_dev = lon - t_lon
        magnitude = abs(signed_dev)
        cost = deviation_penalty * magnitude
        wrong_side = (signed_dev < 0) if wrong_side_negative else (signed_dev > 0)
        if wrong_side:
            cost += south_hard_penalty
        if magnitude > max_deviation:
            cost += max_deviation_penalty
        return cost

    goal_lat, goal_lon = nodes[goal]

    def heuristic(nid: int) -> float:
        lat, lon = nodes[nid]
        return _haversine_m(lat, lon, goal_lat, goal_lon)

    counter = 0
    open_heap: list[tuple[float, int, int]] = [(heuristic(start), 0, start)]
    came_from: dict[int, int] = {}
    g_score: dict[int, float] = {start: 0.0}
    closed: set[int] = set()

    while open_heap:
        _, _, current = heapq.heappop(open_heap)
        if current == goal:
            return _reconstruct_path(came_from, current, nodes)
        if current in closed:
            continue
        closed.add(current)

        for neighbor, edge_dist, road_type in adj.get(current, []):
            if neighbor in closed:
                continue
            n_lat, n_lon = nodes[neighbor]
            type_mult = type_costs.get(road_type, 5.0)
            cost = edge_dist * type_mult + deviation_cost(n_lat, n_lon)
            tentative = g_score[current] + cost
            if tentative < g_score.get(neighbor, math.inf):
                g_score[neighbor] = tentative
                came_from[neighbor] = current
                f = tentative + heuristic(neighbor)
                counter += 1
                heapq.heappush(open_heap, (f, counter, neighbor))

    return None


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------

def compute_barrier_paths(
    cfg_barriers: list[dict],
    bounds: "MapBounds",
    cache_name_prefix: str = "barriers",
    water_mask=None,
    terrain_h: int | None = None,
    terrain_w: int | None = None,
) -> dict[str, list[tuple[float, float]]]:
    """For each barrier in the YAML list, fetch + path it. Returns
    {barrier_name → [(lat, lon), ...]}.

    Barriers are processed in YAML ORDER so a later barrier (e.g. The
    Patrol Line with `method: astar_offset`) can reference an earlier
    barrier (e.g. The Wall) by name via `reference_barrier`.

    Methods supported:
      astar_road_network  — A* through OSM road graph between two endpoints
      astar_offset        — A* offset from a previously-computed reference,
                             with deviation penalties keeping the path near
                             the offset target curve

    Skips barriers that fail to find a path (logs a warning) so one broken
    endpoint doesn't abort the whole render.
    """
    out: dict[str, list[tuple[float, float]]] = {}
    if not cfg_barriers:
        return out

    # All barriers in the same render share the same road graph if they
    # share the same `road_types` list. We cache by the sorted tuple so
    # multiple barriers on the same road set don't refetch.
    # Each entry is (nodes, adj, components_dict). `components_dict` is
    # computed lazily via _ensure_components — it's expensive (~1s for
    # 1.5M nodes) but only needed by astar_road_network for snap
    # disambiguation, and only computed once per (graph, render).
    graph_cache: dict[tuple[str, ...], dict] = {}

    def _fetch_graph(b: dict, name: str) -> dict | None:
        road_types = tuple(sorted(b.get("road_types") or
            ["motorway", "trunk", "primary", "secondary", "tertiary", "residential"]))
        if road_types in graph_cache:
            return graph_cache[road_types]
        cache_name = f"{cache_name_prefix}_{'_'.join(road_types)[:60]}"
        print(f"    fetching road graph for barrier {name!r}...")
        data = fetch_road_graph(bounds, list(road_types), cache_name)
        nodes, adj = build_road_graph(data or {})
        entry = {"nodes": nodes, "adj": adj, "components": None}
        graph_cache[road_types] = entry
        return entry

    for b in cfg_barriers:
        name = b.get("name") or "<unnamed barrier>"
        method = b.get("method", "astar_road_network")
        path: list[tuple[float, float]] | None = None

        if method == "astar_road_network":
            path = _compute_astar_road_network(b, name, _fetch_graph)
        elif method == "astar_offset":
            path = _compute_astar_offset(b, name, _fetch_graph, out)
        else:
            print(f"    [skip] barrier {name!r}: method {method!r} not supported")
            continue

        if not path:
            continue
        # Trim to longest land run if water_mask was provided. Keeps the
        # wall from rendering through sunken terrain (post-sea-level-rise
        # + sinking flood zones whose roads still exist in OSM).
        original_len = len(path)
        if water_mask is not None and terrain_h is not None and terrain_w is not None:
            path = _trim_path_to_land(path, water_mask, bounds, terrain_h, terrain_w)
            if len(path) < original_len:
                pct = 100 * (original_len - len(path)) / original_len
                print(f"    barrier {name!r}: trimmed {original_len - len(path)} "
                      f"underwater points ({pct:.1f}% of path)")
        print(f"    barrier {name!r}: {len(path)} points across "
              f"{_path_length_km(path):.1f} km")
        out[name] = path

    return out


def _connected_components(adj: dict, nodes: dict) -> dict[int, int]:
    """Compute a node_id → component_id mapping via union-find.

    Components are 0-indexed in size-descending order — i.e., the largest
    component is 0. Caller can pick "the main road network" by filtering
    to component_id == 0.
    """
    from collections import deque
    comp: dict[int, int] = {}
    next_label = 0
    sizes: list[tuple[int, int]] = []  # (label, size)
    for seed in nodes:
        if seed in comp:
            continue
        size = 0
        q = deque([seed])
        comp[seed] = next_label
        while q:
            u = q.popleft()
            size += 1
            for v, _, _ in adj.get(u, []):
                if v not in comp:
                    comp[v] = next_label
                    q.append(v)
        sizes.append((next_label, size))
        next_label += 1
    # Re-rank labels by size descending so callers can use 0 = "biggest".
    sizes.sort(key=lambda t: -t[1])
    relabel = {old: new for new, (old, _) in enumerate(sizes)}
    return {nid: relabel[old] for nid, old in comp.items()}


def find_nearest_node_in_component(
    nodes: dict, lat: float, lon: float, comp: dict[int, int], target_comp: int = 0
) -> int | None:
    """Nearest node restricted to a specific connected component.

    `target_comp = 0` (default) → the largest component, which for any
    real-world road graph is "the actual road network" (the rest is
    parking lots, gated communities, ferry-only islands, etc.).
    """
    if not nodes:
        return None
    best_id = None
    best_dist = math.inf
    for nid, (nlat, nlon) in nodes.items():
        if comp.get(nid) != target_comp:
            continue
        d = (nlat - lat) ** 2 + (nlon - lon) ** 2
        if d < best_dist:
            best_dist = d
            best_id = nid
    return best_id


def _ensure_components(graph_entry: dict) -> dict:
    """Lazily populate graph_entry["components"]; return the dict.

    Components labeling is ~1s for a 1.5M-node graph, so we only pay the
    cost once per (road_types) per render and only if a barrier actually
    needs main-component snapping (astar_road_network does; astar_offset
    snaps to nearest absolute, not connected, since the offset target
    curve drives the path geometry independently of component bounds).
    """
    if graph_entry["components"] is None:
        graph_entry["components"] = _connected_components(
            graph_entry["adj"], graph_entry["nodes"],
        )
    return graph_entry["components"]


def _compute_astar_road_network(
    b: dict, name: str, fetch_graph
) -> list[tuple[float, float]] | None:
    """A* through the OSM road graph between two named endpoints.

    Snaps endpoints to the nearest node in the LARGEST connected
    component of the road graph. The literal-nearest node is sometimes
    on a tiny disconnected island (an inaccessible parking lot near the
    coast, a decommissioned spur), in which case A* would return None.
    Forcing snaps to the main component avoids that pathology.
    """
    graph = fetch_graph(b, name)
    if graph is None:
        return None
    nodes, adj = graph["nodes"], graph["adj"]
    endpoints = b.get("endpoints") or {}
    keys = list(endpoints.keys())
    if len(keys) < 2:
        print(f"    [skip] barrier {name!r}: needs at least 2 endpoints")
        return None
    a_pt = endpoints[keys[0]]
    b_pt = endpoints[keys[1]]

    comp = _ensure_components(graph)
    a_id_main = find_nearest_node_in_component(
        nodes, float(a_pt["lat"]), float(a_pt["lon"]), comp, target_comp=0,
    )
    b_id_main = find_nearest_node_in_component(
        nodes, float(b_pt["lat"]), float(b_pt["lon"]), comp, target_comp=0,
    )
    a_id_lit = find_nearest_node(nodes, float(a_pt["lat"]), float(a_pt["lon"]))
    b_id_lit = find_nearest_node(nodes, float(b_pt["lat"]), float(b_pt["lon"]))
    if a_id_main is None or b_id_main is None:
        print(f"    [skip] barrier {name!r}: empty graph or no main component")
        return None

    if a_id_main != a_id_lit:
        a_loc = nodes[a_id_main]
        a_orig = nodes[a_id_lit] if a_id_lit else "?"
        print(f"    barrier {name!r}: literal-nearest a-snap was on a "
              f"disconnected island; main-component snap at {a_loc} "
              f"(literal was {a_orig})")
    if b_id_main != b_id_lit:
        b_loc = nodes[b_id_main]
        b_orig = nodes[b_id_lit] if b_id_lit else "?"
        print(f"    barrier {name!r}: literal-nearest b-snap was on a "
              f"disconnected island; main-component snap at {b_loc} "
              f"(literal was {b_orig})")

    return astar(nodes, adj, a_id_main, b_id_main, corridor=b.get("corridor"))


def _compute_astar_offset(
    b: dict,
    name: str,
    fetch_graph,
    prior_paths: dict[str, list[tuple[float, float]]],
) -> list[tuple[float, float]] | None:
    """A* offset from a previously-computed reference barrier.

    Schema:
      reference_barrier: "The Wall"
      offset_degrees: 0.10
      direction: north | south | east | west
      corridor:
        deviation_penalty:    cost added per degree of perpendicular distance
                              from the offset target curve
        south_hard_penalty:   extra cost when on the WRONG side of the
                              offset (south of a north-offset target, etc.)
        max_deviation:        degrees beyond which max_deviation_penalty
                              kicks in (hard-cap behavior)
        max_deviation_penalty: extra cost past max_deviation
    """
    ref_name = b.get("reference_barrier")
    if not ref_name:
        print(f"    [skip] barrier {name!r}: astar_offset needs reference_barrier")
        return None
    ref_path = prior_paths.get(ref_name)
    if not ref_path:
        print(f"    [skip] barrier {name!r}: reference_barrier {ref_name!r} not found "
              f"(must appear EARLIER in the barriers list)")
        return None

    direction = (b.get("direction") or "north").lower()
    offset = float(b.get("offset_degrees", 0.1))
    if direction == "north":   d_lat, d_lon =  offset, 0.0
    elif direction == "south": d_lat, d_lon = -offset, 0.0
    elif direction == "east":  d_lat, d_lon =  0.0,  offset
    elif direction == "west":  d_lat, d_lon =  0.0, -offset
    else:
        print(f"    [skip] barrier {name!r}: unknown direction {direction!r}")
        return None

    # Build the offset target curve: every reference point shifted by (d_lat, d_lon).
    target_pts = [(lat + d_lat, lon + d_lon) for (lat, lon) in ref_path]
    if not target_pts:
        return None

    graph = fetch_graph(b, name)
    if graph is None:
        return None
    nodes, adj = graph["nodes"], graph["adj"]

    # Endpoints: snap the offset of the FIRST and LAST reference points to
    # the nearest graph nodes. This way the patrol line tracks the wall's
    # extent automatically — no separate endpoints needed in the YAML.
    # Also snap to the main component so A* can find a connected route
    # (offset endpoints may land on disconnected islands too).
    start_lat, start_lon = target_pts[0]
    goal_lat,  goal_lon  = target_pts[-1]
    comp = _ensure_components(graph)
    a_id = find_nearest_node_in_component(nodes, start_lat, start_lon, comp, target_comp=0)
    b_id = find_nearest_node_in_component(nodes, goal_lat,  goal_lon,  comp, target_comp=0)
    if a_id is None or b_id is None:
        print(f"    [skip] barrier {name!r}: empty graph or no main component")
        return None

    return astar_offset(
        nodes, adj, a_id, b_id,
        target_pts=target_pts,
        direction=direction,
        corridor=b.get("corridor") or {},
    )


def parse_constraint(constraint: str, barriers_cfg: list[dict]) -> tuple[str, str] | None:
    """Resolve a constraint string like "north_of_wall" → (side, barrier_name).

    The string format is `<side>_of_<token>` where:
      - `side` ∈ {north, south, east, west}
      - `token` is a fragment that appears (case-insensitively) in the
         target barrier's `name` or matches its `type` exactly.

    Examples (with a barrier named "The Wall" of type "wall"):
      "north_of_wall"  → ("north", "The Wall")  # token "wall" matches name + type
      "south_of_patrol" → ("south", "The Patrol Line")  # if such barrier exists

    Returns None if the format doesn't parse or no barrier matches.
    """
    if not constraint or "_of_" not in constraint:
        return None
    side, _, token = constraint.lower().partition("_of_")
    if side not in ("north", "south", "east", "west") or not token:
        return None
    token = token.strip()
    # Match against barrier name (case-insensitive contains) or type (exact).
    for b in barriers_cfg:
        b_name = b.get("name") or ""
        b_type = b.get("type") or ""
        if token == b_type.lower() or token in b_name.lower():
            return side, b_name
    return None


# ----------------------------------------------------------------------
#  Compound constraints
# ----------------------------------------------------------------------

def collect_constraint_strings(tint_cfg: dict | None) -> tuple[list[str], str]:
    """Extract constraint strings + logic from a tint config.

    Schema accepted (legacy + compound):
      tint.constraint:        "north_of_wall"        # single string (legacy)
      tint.constraints:       [str, ...]              # compound list
      tint.constraint_logic:  "and" | "or"            # default "and"

    Both `constraint` and `constraints` MAY coexist — they're merged.
    Returns ([constraint_strings], "and"|"or") with empty list when no
    constraints are configured.
    """
    if not tint_cfg:
        return [], "and"
    items: list[str] = []
    legacy = tint_cfg.get("constraint")
    if isinstance(legacy, str) and legacy:
        items.append(legacy)
    extra = tint_cfg.get("constraints") or []
    if isinstance(extra, list):
        for c in extra:
            if isinstance(c, str) and c:
                items.append(c)
    logic = (tint_cfg.get("constraint_logic") or "and").lower()
    if logic not in ("and", "or"):
        logic = "and"
    return items, logic


def canonical_constraint_key(tint_cfg: dict | None) -> str | None:
    """Stable string key derived from a tint's constraint configuration.

    Used by the pipeline so each region's effective constraint mask
    can be cached + looked up regardless of which schema variant
    (legacy single-string vs. compound list) the YAML uses.

    Returns None when no constraints are configured.

    Examples:
      {constraint: "north_of_wall"}        →  "north_of_wall"
      {constraints: ["a","b"], logic: and} →  "AND:a;b"
      {constraints: ["b","a"], logic: and} →  "AND:a;b"   (sorted: same key)
      {constraints: ["a","b"], logic: or}  →  "OR:a;b"
    """
    items, logic = collect_constraint_strings(tint_cfg)
    if not items:
        return None
    if len(items) == 1:
        # Single constraint — preserve the legacy key (no AND/OR prefix).
        # This is what makes existing YAML keep working without invalidation.
        return items[0]
    # Compound: sort for determinism (AND/OR are commutative)
    return f"{logic.upper()}:{';'.join(sorted(items))}"


def resolve_constraint_mask(
    tint_cfg: dict | None,
    barriers_cfg: list[dict],
    barrier_paths: dict,
    bounds: "MapBounds",
    h: int,
    w: int,
    water_mask=None,
) -> "np.ndarray | None":
    """Compute a region tint's effective constraint mask from its config.

    Combines individual barrier_side_mask outputs according to the
    `constraint_logic` field. Missing barriers (those whose name doesn't
    resolve in barriers_cfg, or whose path isn't in barrier_paths) are
    silently dropped — the remaining ones combine; if all are missing,
    returns None.

    Returns None when no constraints are configured.
    """
    items, logic = collect_constraint_strings(tint_cfg)
    if not items:
        return None
    masks: list = []
    for cstr in items:
        parsed = parse_constraint(cstr, barriers_cfg)
        if not parsed:
            print(f"    [tint] constraint {cstr!r}: no matching barrier; ignoring")
            continue
        side, b_name = parsed
        path = barrier_paths.get(b_name)
        if not path:
            print(f"    [tint] constraint {cstr!r}: barrier "
                  f"{b_name!r} has no path; ignoring")
            continue
        m = barrier_side_mask(path, side, bounds, h, w, water_mask=water_mask)
        masks.append(m)
    if not masks:
        return None
    # Combine. Both AND and OR are associative so a left-fold suffices.
    result = masks[0]
    for m in masks[1:]:
        if logic == "or":
            result = result | m
        else:
            result = result & m
    return result


def barrier_side_mask(
    path: list[tuple[float, float]],
    side: str,
    bounds: "MapBounds",
    h: int,
    w: int,
    water_mask=None,
    strict: bool = False,
) -> "np.ndarray":
    """Rasterize a "north/south/east/west of polyline" mask at terrain
    resolution (h × w, no border). Returns a (h, w) boolean array.

    With `water_mask` provided (preferred): uses topological flood-fill.
    The path is stamped as a thick line and unioned with the water mask to
    form a "blocked" set. We then label the connected components of the
    complement (passable cells) and identify the component(s) reachable
    from the requested canvas edge. This is the wall's "side":

      - side="north" → land reachable from y=0 through passable cells
      - side="south" → land reachable from y=h-1
      - side="east"  → land reachable from x=w-1
      - side="west"  → land reachable from x=0

    When the path terminates in water at both ends — which `_trim_path_to_land`
    enforces via tangent + BFS extension — water and wall together form a
    continuous barrier and the canvas land splits into two disjoint regions.
    The mask correctly bisects the land with NO banding artifacts at the
    wall's endpoint latitudes/longitudes (the previous polygon approach
    introduced flat bands there).

    With `strict=True`: returns ONLY the flood-filled side. Use this for
    counting sources on a specific side — e.g. contamination's source-side
    vote, where blocked cells (path/water) should not vote.

    With `strict=False` (default): out-of-component cells default to True
    (pass-through). For tint and contamination consumers that already
    multiply by land_mask, this is equivalent to side-only.

    `water_mask=None` falls back to a finite polygon-to-canvas-corner
    approximation. Acceptable for tests / standalone use; production
    callers (pipeline stage 2, contamination) always pass it.
    """
    import numpy as np
    from PIL import Image, ImageDraw

    if not path or side not in ("north", "south", "east", "west"):
        return np.ones((h, w), dtype=bool)

    # Project path to terrain pixel coords. (x increases east, y increases south.)
    px = [bounds.to_pixel(lat, lon, h, w) for (lat, lon) in path]

    if water_mask is None:
        return _barrier_side_mask_polygon(px, side, h, w, strict)

    from scipy.ndimage import label

    # Stamp the path as a 2-pixel-wide line so 8-connected diagonal jumps in
    # the polyline are 4-connected after rasterization (scipy.ndimage.label
    # uses 4-connectivity by default; a 1-pixel diagonal would have a gap).
    stamp_img = Image.new("L", (w, h), 0)
    ImageDraw.Draw(stamp_img).line(px, fill=255, width=2)
    path_stamp = np.array(stamp_img, dtype=bool)

    blocked = water_mask | path_stamp
    passable = ~blocked

    # 4-connected flood-fill via connected-component labeling. Each label is
    # a topologically distinct passable region.
    labels, n_components = label(passable)
    if n_components == 0:
        return np.zeros((h, w), dtype=bool)

    # Identify which component(s) touch the requested canvas edge.
    if side == "north":
        edge_labels = set(int(L) for L in labels[0, :] if L > 0)
    elif side == "south":
        edge_labels = set(int(L) for L in labels[h - 1, :] if L > 0)
    elif side == "east":
        edge_labels = set(int(L) for L in labels[:, w - 1] if L > 0)
    else:  # west
        edge_labels = set(int(L) for L in labels[:, 0] if L > 0)

    if not edge_labels:
        # Reference edge is entirely blocked (e.g., entirely water). No
        # land is on the requested side — return empty mask.
        return np.zeros((h, w), dtype=bool)

    side_mask = np.isin(labels, list(edge_labels))

    if strict:
        return side_mask
    # Pass-through for blocked cells. Tints/contamination multiply by
    # land_mask anyway, so this just keeps the API symmetric with the
    # legacy polygon mask.
    return side_mask | blocked


def _barrier_side_mask_polygon(
    px: list[tuple[int, int]], side: str, h: int, w: int, strict: bool
) -> "np.ndarray":
    """Legacy polygon fallback used only when water_mask is unavailable.

    Closes the path through canvas corners; this introduces flat bands at
    endpoint coordinates (previously a known artifact). Production renders
    use the flood-fill path above which doesn't have this problem.
    """
    import numpy as np
    from PIL import Image, ImageDraw

    if side in ("south", "north"):
        polygon = list(px) + [(px[-1][0], h - 1), (px[0][0], h - 1)]
        target_side = "south"
    else:
        polygon = list(px) + [(w - 1, px[-1][1]), (w - 1, px[0][1])]
        target_side = "east"

    img = Image.new("L", (w, h), 0)
    ImageDraw.Draw(img).polygon(polygon, fill=255)
    strict_target = np.array(img, dtype=bool)
    if side == target_side:
        return strict_target
    other = ~strict_target
    if strict:
        return other
    return other


def band_mask(
    path_a: list[tuple[float, float]],
    path_b: list[tuple[float, float]],
    bounds: "MapBounds",
    h: int,
    w: int,
) -> "np.ndarray":
    """Boolean mask of the polygonal region BETWEEN two barrier paths.

    Built as the closed polygon `path_a + reverse(path_b)` — same
    geometry the buffer_zone hatching uses, but at terrain resolution
    (no border) for stage-2 tint application.

    Used by the `band` tint type so a region can be defined purely as
    "the strip between The Wall and The Patrol Line" instead of needing
    a manual `center` / `radius` config that would have to be hand-tuned
    every time the wall geometry shifts.

    Returns a (h, w) bool array, all-False if either path is empty or
    the polygon has fewer than 3 vertices (e.g., both paths are length-1).
    """
    import numpy as np
    from PIL import Image, ImageDraw

    if not path_a or not path_b:
        return np.zeros((h, w), dtype=bool)

    poly_a = [bounds.to_pixel(lat, lon, h, w) for (lat, lon) in path_a]
    poly_b = [bounds.to_pixel(lat, lon, h, w) for (lat, lon) in path_b]
    polygon = poly_a + list(reversed(poly_b))
    if len(polygon) < 3:
        return np.zeros((h, w), dtype=bool)

    img = Image.new("L", (w, h), 0)
    ImageDraw.Draw(img).polygon(polygon, fill=255)
    return np.array(img, dtype=bool)


def _path_length_km(path: list[tuple[float, float]]) -> float:
    if len(path) < 2:
        return 0.0
    return sum(
        _haversine_m(a[0], a[1], b[0], b[1])
        for a, b in zip(path, path[1:])
    ) / 1000.0


def _trim_path_to_land(
    path: list[tuple[float, float]],
    water_mask,
    bounds: "MapBounds",
    h: int,
    w: int,
) -> list[tuple[float, float]]:
    """Truncate a barrier path to its longest contiguous land sub-path,
    then extend each terminus along its tangent direction until hitting
    *shore water* (water connected to a canvas edge), or canvas edge.

    The combination guarantees the wall reaches the actual coastline at
    BOTH ends — necessary for the narrative requirement that a wall go
    "shore to shore with no gaps." Without this:
      - A* may terminate at a land node a few pixels inland (no extension)
      - Tangent extension may dead-end at a tiny inland lake (puddle)
    Both leave gaps that contamination/tints can leak through.

    A* routes along OSM road geometry; some of those roads may be
    underwater after sea-level rise + sinking — those segments are
    trimmed out. Tangent extension carries the wall the last few pixels
    to a coastal water cell. If tangent fails, BFS-to-edge-water searches
    in any direction.

    "Edge-water" is the union of water-mask connected components that
    touch any canvas edge — this excludes inland lakes and the tiny
    isolated puddles that floodgate the wall pathologically.

    `water_mask` is at terrain resolution (h × w, no border). Returns the
    unmodified path if water_mask is None or if no land run is ≥2 points.
    """
    if water_mask is None or len(path) < 2:
        return path

    import numpy as np
    # Pre-compute the "edge-water" mask once. BFS-to-water terminates only
    # on these cells; tiny inland lakes are not valid termination targets.
    from scipy.ndimage import label as scipy_label
    water_labels, _ = scipy_label(water_mask)
    edge_label_set = set()
    for arr in (water_labels[0, :], water_labels[h - 1, :],
                water_labels[:, 0], water_labels[:, w - 1]):
        for L in arr:
            if L > 0:
                edge_label_set.add(int(L))
    if edge_label_set:
        edge_water = np.isin(water_labels, list(edge_label_set))
    else:
        # No water touches any edge — fallback: accept any water (degenerate map).
        edge_water = water_mask

    runs: list[tuple[int, int]] = []   # (start_idx, end_idx_exclusive) for each land run
    in_land = False
    start = 0
    for i, (lat, lon) in enumerate(path):
        x, y = bounds.to_pixel(lat, lon, h, w)
        is_land = (0 <= x < w and 0 <= y < h and not water_mask[y, x])
        if is_land:
            if not in_land:
                start = i
                in_land = True
        else:
            if in_land:
                runs.append((start, i))
                in_land = False
    if in_land:
        runs.append((start, len(path)))

    if not runs:
        return path
    longest = max(runs, key=lambda r: r[1] - r[0])
    if longest[1] - longest[0] < 2:
        return path

    s, e = longest
    # Include boundary cells from the original path where available — these
    # are guaranteed water (since the run is bounded by water on both sides
    # within the original path).
    s_inc = max(0, s - 1)
    e_inc = min(len(path), e + 1)
    trimmed = list(path[s_inc:e_inc])

    # Tangent extension at each end, in case the trimmed terminus is still
    # on LAND (which happens when the original path's actual endpoint was
    # also on land — A* terminated short of the water). Two strategies in
    # order:
    #   (1) Walk in tangent direction (continues the wall's heading)
    #   (2) BFS to nearest water in any direction (fallback for endpoints
    #        whose tangent doesn't point toward water — e.g., a wall
    #        ending in inland Maryland where the nearest water is south,
    #        not along the wall's east-west axis)
    def _walk_to_water(pivot_xy, direction_xy, max_steps=200):
        px, py = pivot_xy
        dx, dy = direction_xy
        norm = (dx * dx + dy * dy) ** 0.5
        if norm < 1e-6:
            return []
        ux, uy = dx / norm, dy / norm
        cells = []
        for step in range(1, max_steps + 1):
            nx = int(round(px + ux * step))
            ny = int(round(py + uy * step))
            if not (0 <= nx < w and 0 <= ny < h):
                break
            cells.append((nx, ny))
            if edge_water[ny, nx]:
                return cells   # reached SHORE water — include it as terminus
        # Tangent didn't reach edge-water within max_steps; let BFS try.
        return []

    def _bfs_to_water(start_xy, max_radius=600):
        """8-connected BFS for nearest edge-connected water cell.

        Excludes inland puddles (tiny isolated lakes that aren't connected
        to any canvas edge). Otherwise the wall's "shore extension" can
        end in a 7-cell pond, leaving a bypass that contamination flows
        around.

        Returns the shortest pixel chain from start (exclusive) to the
        edge-water cell (inclusive), or [] if no edge-water within
        max_radius.
        """
        from collections import deque
        sx, sy = start_xy
        if not (0 <= sx < w and 0 <= sy < h):
            return []
        if edge_water[sy, sx]:
            return []   # already at shore
        parents = {(sx, sy): None}
        queue = deque([(sx, sy, 0)])
        while queue:
            x, y, dist = queue.popleft()
            if dist >= max_radius:
                continue
            for dx, dy in (
                (1, 0), (-1, 0), (0, 1), (0, -1),
                (1, 1), (1, -1), (-1, 1), (-1, -1),
            ):
                nx, ny = x + dx, y + dy
                if not (0 <= nx < w and 0 <= ny < h):
                    continue
                if (nx, ny) in parents:
                    continue
                parents[(nx, ny)] = (x, y)
                if edge_water[ny, nx]:
                    cells = [(nx, ny)]
                    cur = (x, y)
                    while cur is not None and cur != (sx, sy):
                        cells.append(cur)
                        cur = parents[cur]
                    return list(reversed(cells))
                queue.append((nx, ny, dist + 1))
        return []

    def _px_to_latlon(x, y):
        # Inverse of bounds.to_pixel; the +0.5 picks the cell center
        # so a tangent extension lands on a real lat/lon close to the
        # adjacent path point's coords.
        lon = bounds.lon_w + (x + 0.5) / w * (bounds.lon_e - bounds.lon_w)
        lat = bounds.lat_n - (y + 0.5) / h * (bounds.lat_n - bounds.lat_s)
        return (lat, lon)

    def _extend_to_water(pivot_xy, tangent_xy):
        """Try tangent first, then BFS. Returns ordered list of (x, y)
        cells from pivot_xy (exclusive) to a water cell (inclusive)."""
        ext = _walk_to_water(pivot_xy, tangent_xy)
        if ext:
            return ext
        return _bfs_to_water(pivot_xy)

    # Front-end extension: only if the FIRST trimmed point is still on land
    # (i.e., we couldn't include a boundary cell because the run starts at
    # index 0). Tangent direction points "outward" from path[0] — opposite
    # to the path's heading at the start.
    if len(trimmed) >= 2:
        first = trimmed[0]
        x0, y0 = bounds.to_pixel(first[0], first[1], h, w)
        if 0 <= x0 < w and 0 <= y0 < h and not water_mask[y0, x0]:
            second = trimmed[1]
            x1, y1 = bounds.to_pixel(second[0], second[1], h, w)
            ext = _extend_to_water((x0, y0), (x0 - x1, y0 - y1))
            if ext:
                # Prepend in reverse so the most-extended cell comes first
                # (the path enters from outside, then sweeps inward).
                trimmed = [_px_to_latlon(x, y) for (x, y) in reversed(ext)] + trimmed

    # Back-end extension: only if the LAST trimmed point is still on land.
    if len(trimmed) >= 2:
        last = trimmed[-1]
        xN, yN = bounds.to_pixel(last[0], last[1], h, w)
        if 0 <= xN < w and 0 <= yN < h and not water_mask[yN, xN]:
            penult = trimmed[-2]
            xP, yP = bounds.to_pixel(penult[0], penult[1], h, w)
            ext = _extend_to_water((xN, yN), (xN - xP, yN - yP))
            if ext:
                trimmed = trimmed + [_px_to_latlon(x, y) for (x, y) in ext]

    return trimmed


def draw_barriers(
    canvas: Image.Image,
    paths: dict[str, list[tuple[float, float]]],
    cfg_barriers: list[dict],
    bounds: "MapBounds",
    h: int,
    w: int,
    border: int,
    fonts: dict | None = None,
) -> int:
    """Render computed barrier paths onto `canvas` with their configured style.

    Each barrier is drawn as: a wider shadow stroke (RGBA, dark) underneath,
    then the colored line on top — matches the road-rendering convention so
    barriers sit visually with the roads but in their own hue.

    Decorative options per barrier `style:`
      dashed: true              — break stroke into dashes
      dash_length: 12           — pixel length of each dash (gap = same)
      hash_marks: true          — perpendicular tick marks (think fortification
                                   "wall with crenellations" cartographic style)
      hash_spacing: 8           — pixels along the path between ticks
      hash_length: 6            — pixel length of each tick from the line

    Optional `label:` block draws the barrier name along the path.
      text:     "THE WALL"
      position: midpoint | three_quarter | start | end  (default midpoint)
      offset:   [dx, dy] in pixels relative to the chosen point
      color:    [r, g, b]

    Stroke widths are CSS-design-target sizes (tuned for ~2700px canvases)
    and scale with the actual canvas dimensions, so the same YAML produces
    proportional barriers at preview vs full resolution.
    """
    if not paths or not cfg_barriers:
        return 0

    # Width scale: schema widths target ~2700px renders. Below that we shrink
    # proportionally; above that we keep growing so high-res renders don't
    # feel anemic. Floor at 1.0 so a small preview never goes BELOW design.
    width_scale = max(1.0, min(h, w) / 1800.0)

    # Index barrier configs by name for O(1) style lookup.
    by_name = {b.get("name"): b for b in cfg_barriers if b.get("name")}

    # Draw onto a transparent layer + alpha-composite, mirroring the
    # state-boundary rendering — preserves the canvas's existing contents
    # cleanly even with translucent strokes.
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    drawn = 0

    label_jobs: list[tuple[dict, list[tuple[int, int]], tuple[int, int, int]]] = []

    for name, path in paths.items():
        b = by_name.get(name) or {}
        style = b.get("style") or {}
        color = tuple(int(c) for c in style.get("color", (200, 150, 90)))
        width = max(1, int(round(int(style.get("width", 3)) * width_scale)))
        shadow_width = max(width + 2, int(round(
            int(style.get("shadow_width", 5)) * width_scale)))
        shadow_color = (30, 25, 18, 200)   # semi-opaque ink, matches road shadows
        dashed       = bool(style.get("dashed"))
        dash_length  = max(2, int(round(int(style.get("dash_length", 12)) * width_scale)))
        hash_marks   = bool(style.get("hash_marks"))
        hash_spacing = max(2, int(round(int(style.get("hash_spacing", 8)) * width_scale)))
        hash_length  = max(2, int(round(int(style.get("hash_length", 6))  * width_scale)))

        # Project lat/lon → canvas pixels via the same MapBounds helper used
        # everywhere else.
        pts = [bounds.to_canvas(lat, lon, h, w, border) for (lat, lon) in path]
        if len(pts) < 2:
            continue

        if dashed:
            # Dashed barriers use shadow-strokes-per-dash too — otherwise the
            # gaps would have no shadow and the dashes would look bare.
            for seg in _dash_segments(pts, dash_length):
                ld.line(seg, fill=shadow_color, width=shadow_width, joint="curve")
                ld.line(seg, fill=color + (255,), width=width, joint="curve")
        else:
            ld.line(pts, fill=shadow_color, width=shadow_width, joint="curve")
            ld.line(pts, fill=color + (255,), width=width, joint="curve")

        if hash_marks:
            for (hx0, hy0, hx1, hy1) in _hash_marks(pts, hash_spacing, hash_length):
                # Each tick gets its own miniature shadow + color stroke
                ld.line([(hx0, hy0), (hx1, hy1)], fill=shadow_color, width=max(width, 2))
                ld.line([(hx0, hy0), (hx1, hy1)], fill=color + (255,), width=max(1, width - 1))

        # Queue label rendering for later (does its own setup; we don't want
        # to entangle the stroke loop with text drawing).
        if b.get("label"):
            label_jobs.append((b["label"], pts, color))

        drawn += 1

    if drawn:
        if canvas.mode != "RGBA":
            canvas = canvas.convert("RGBA")
        canvas.alpha_composite(layer)

        # Labels render directly onto the (now-merged) canvas using the
        # standard label outlining. This gives them the same halo treatment
        # as settlement names so they read against busy terrain.
        if label_jobs and fonts is not None:
            from src.labels.markers import outline_text
            draw_on_canvas = ImageDraw.Draw(canvas)
            for (label_cfg, pts, default_color) in label_jobs:
                _place_barrier_label(
                    draw_on_canvas, canvas, label_cfg, pts, default_color, fonts,
                )
    return drawn


def _dash_segments(pts: list[tuple[int, int]], dash_length: int) -> list[list[tuple[int, int]]]:
    """Split a polyline into alternating drawn / skipped runs of `dash_length` px.

    We walk the path's cumulative length and emit a new sub-polyline every time
    we cross a dash boundary, alternating "drawing" and "gap" states.
    Returns just the drawn sub-polylines (each ≥2 points).
    """
    if len(pts) < 2 or dash_length <= 0:
        return [pts]

    out: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = [pts[0]]
    drawing = True
    accumulated = 0.0    # distance along path since last dash boundary

    for i in range(1, len(pts)):
        x0, y0 = current[-1] if drawing else pts[i - 1]
        x1, y1 = pts[i]
        seg_len = math.hypot(x1 - x0, y1 - y0)
        if seg_len <= 0:
            continue
        remaining = seg_len
        cx, cy = x0, y0
        while accumulated + remaining >= dash_length:
            # Travel until the next boundary
            travel = dash_length - accumulated
            t = travel / remaining if remaining else 0
            nx = cx + (x1 - cx) * t
            ny = cy + (y1 - cy) * t
            if drawing:
                current.append((int(round(nx)), int(round(ny))))
                if len(current) >= 2:
                    out.append(current)
                current = []
            # Toggle and advance the cursor along the segment
            drawing = not drawing
            cx, cy = nx, ny
            remaining -= travel
            accumulated = 0.0
            if drawing:
                current = [(int(round(cx)), int(round(cy)))]
        accumulated += remaining
        if drawing:
            current.append((x1, y1))

    if drawing and len(current) >= 2:
        out.append(current)
    return out


def _hash_marks(
    pts: list[tuple[int, int]], spacing: int, length: int
) -> list[tuple[int, int, int, int]]:
    """Generate perpendicular tick marks every `spacing` px along the path.

    Each tick is centered on the path and extends `length` px on each side.
    Returns (x0, y0, x1, y1) tuples in pixel coords.
    """
    if len(pts) < 2 or spacing <= 0:
        return []
    ticks: list[tuple[int, int, int, int]] = []
    accumulated = 0.0
    next_tick_at = spacing
    for i in range(1, len(pts)):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        dx, dy = x1 - x0, y1 - y0
        seg_len = math.hypot(dx, dy)
        if seg_len <= 0:
            continue
        # Perpendicular unit vector (rotate the tangent 90°)
        px, py = -dy / seg_len, dx / seg_len
        while accumulated + seg_len >= next_tick_at:
            t = (next_tick_at - accumulated) / seg_len
            cx = x0 + dx * t
            cy = y0 + dy * t
            ticks.append((
                int(round(cx - px * length)), int(round(cy - py * length)),
                int(round(cx + px * length)), int(round(cy + py * length)),
            ))
            next_tick_at += spacing
        accumulated += seg_len
    return ticks


def _place_barrier_label(
    draw,
    canvas: Image.Image,
    label_cfg: dict,
    pts: list[tuple[int, int]],
    default_color: tuple[int, int, int],
    fonts: dict,
) -> None:
    """Position a barrier label along its path at midpoint / start / end / etc.

    Position picks a single anchor point along the path (no curved type-on-
    path — that's a much harder problem). The label uses the standard
    halo-outlined text from src.labels.markers so it reads against busy
    terrain. `offset: [dx, dy]` nudges the label box.
    """
    text = label_cfg.get("text") or ""
    if not text:
        return
    position = (label_cfg.get("position") or "midpoint").lower()
    offset = label_cfg.get("offset") or [0, 0]
    try:
        ox, oy = int(offset[0]), int(offset[1])
    except (TypeError, ValueError, IndexError):
        ox, oy = 0, 0
    color = tuple(int(c) for c in (label_cfg.get("color") or default_color))

    # Find anchor point as a fraction of cumulative arc length.
    fracs = {"start": 0.0, "midpoint": 0.5, "three_quarter": 0.75, "end": 1.0}
    frac = fracs.get(position, 0.5)
    anchor = _point_at_fraction(pts, frac)
    if anchor is None:
        return
    px, py = anchor

    # Use the existing outlined-text helper for a halo. Pick the "settle"
    # font as a sensible default — could be overridden via label.font later.
    font_role = label_cfg.get("font") or "settle"
    font = fonts.get(font_role) or fonts.get("settle") or fonts.get("note")
    if font is None:
        return

    from src.labels.markers import outline_text
    outline_text(
        draw, (px + ox, py + oy), text, font, color,
        shadow=(30, 25, 18), width=2, anchor="ma",
    )


def _point_at_fraction(
    pts: list[tuple[int, int]], frac: float
) -> tuple[int, int] | None:
    """Linearly interpolate a point at `frac` of the polyline's arc length."""
    if not pts:
        return None
    if len(pts) == 1 or frac <= 0:
        return pts[0]
    if frac >= 1:
        return pts[-1]
    # Total length first
    seg_lens = [
        math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        for i in range(len(pts) - 1)
    ]
    total = sum(seg_lens)
    if total <= 0:
        return pts[0]
    target = frac * total
    accum = 0.0
    for i, sl in enumerate(seg_lens):
        if accum + sl >= target:
            t = (target - accum) / sl if sl > 0 else 0
            x = pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t
            y = pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t
            return int(round(x)), int(round(y))
        accum += sl
    return pts[-1]


def draw_buffer_zone(
    canvas: Image.Image,
    paths: dict[str, list[tuple[float, float]]],
    cfg_buffer: dict,
    bounds: "MapBounds",
    h: int,
    w: int,
    border: int,
    fonts: dict | None = None,
) -> bool:
    """Hatch the polygonal region between two named barriers, optionally
    sprinkling glyphs (e.g. biohazard symbols) along the centerline.

    Schema (from cfg.buffer_zone):
      enabled: true
      between: ["The Wall", "The Patrol Line"]
      hatching: { color: [200, 150, 60, 120], spacing: 14, direction: diagonal }
      glyphs:
        text: "☣"               # symbol(s) to sprinkle along centerline
        font: bold_14           # font role to render the glyph
        color: [180, 130, 50]   # rgb (alpha-included rgba also accepted)
        spacing: 80             # px between glyphs along centerline arc

    Geometry: closed polygon = path_a + reverse(path_b). We rasterize that
    polygon as a mask, then composite a hatch pattern (alternating diagonal
    stripes at `spacing` pixels) onto the canvas where the mask is true.

    Glyphs are placed at points sampled by averaging point_at_fraction on
    both paths — that traces the strip's centerline. They render with a
    dark halo for legibility on the parchment terrain.

    Returns True on success.
    """
    if not cfg_buffer or not cfg_buffer.get("enabled"):
        return False
    between = cfg_buffer.get("between") or []
    if len(between) != 2:
        print("    [skip] buffer_zone: `between` needs exactly 2 barrier names")
        return False
    a_name, b_name = between
    a_path = paths.get(a_name)
    b_path = paths.get(b_name)
    if not a_path or not b_path:
        missing = [n for n in (a_name, b_name) if not paths.get(n)]
        print(f"    [skip] buffer_zone: missing path(s) for {missing}")
        return False

    hatching = cfg_buffer.get("hatching") or {}
    color = hatching.get("color", [200, 150, 60, 120])
    color_rgba = tuple(int(c) for c in color[:4])
    if len(color_rgba) == 3:
        color_rgba = (*color_rgba, 120)
    spacing = max(2, int(hatching.get("spacing", 14)))
    direction = (hatching.get("direction") or "diagonal").lower()

    # Build the closed polygon in canvas pixel space.
    poly_a = [bounds.to_canvas(lat, lon, h, w, border) for (lat, lon) in a_path]
    poly_b = [bounds.to_canvas(lat, lon, h, w, border) for (lat, lon) in b_path]
    polygon = poly_a + list(reversed(poly_b))
    if len(polygon) < 3:
        return False

    # Step 1: create a transparent layer, draw the polygon mask filled with
    # the hatch color stamped over a stripe pattern.
    cw, ch = canvas.size
    mask_layer = Image.new("L", canvas.size, 0)
    md = ImageDraw.Draw(mask_layer)
    md.polygon(polygon, fill=255)

    # Step 2: build the stripe pattern (full-canvas) at `direction` angle.
    stripe_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(stripe_layer)
    if direction == "horizontal":
        # Horizontal stripes: every `spacing` rows
        for y in range(0, ch, spacing):
            sd.line([(0, y), (cw, y)], fill=color_rgba, width=1)
    elif direction == "vertical":
        for x in range(0, cw, spacing):
            sd.line([(x, 0), (x, ch)], fill=color_rgba, width=1)
    else:
        # diagonal: 45° stripes from top-left to bottom-right.
        # We sweep an offset c = y - x covering the full canvas range
        # [-cw, ch] in steps of `spacing`.
        diag_steps = (cw + ch) // spacing + 1
        for i in range(-cw // spacing, diag_steps + 1):
            c = i * spacing
            # Line from (max(0, -c), max(0, c)) to (min(cw, ch-c), min(ch, cw+c))
            x0 = max(0, -c)
            y0 = max(0, c)
            x1 = min(cw, ch - c)
            y1 = min(ch, cw + c)
            sd.line([(x0, y0), (x1, y1)], fill=color_rgba, width=1)

    # Step 3: clip the stripe pattern by the polygon mask. The stripe layer
    # already carries the hatch color's alpha; multiplying its alpha channel
    # by the mask drops everything outside the polygon while preserving the
    # transparent gaps BETWEEN stripes inside the polygon.
    _, _, _, stripe_alpha = stripe_layer.split()
    clipped_alpha = ImageChops.multiply(stripe_alpha, mask_layer)
    stripe_layer.putalpha(clipped_alpha)

    if canvas.mode != "RGBA":
        canvas = canvas.convert("RGBA")
    canvas.alpha_composite(stripe_layer)

    # Sprinkle glyphs along the centerline if configured.
    # Supports two schemas:
    #   symbols: {character: "☣", count: 6, font: <name>, font_size: 18,
    #             color: [r,g,b], placement: midline}
    #   glyphs:  {text: "☣", spacing: 80, font: <role>, color: [r,g,b]}
    # `symbols` is the legacy schema (count-based, font is file/face
    # name with explicit font_size). `glyphs` is the spacing-based
    # variant where `font` is a role from cfg.fonts.
    sym = cfg_buffer.get("symbols")
    gly = cfg_buffer.get("glyphs")
    if sym or gly:
        from PIL import ImageDraw as _ID, ImageFont as _IF
        from src.labels.markers import outline_text
        from src.pipeline import font_path
        glyph_text, n_glyphs, font, color_rgb = _resolve_glyph_params(
            sym, gly, fonts, poly_a, poly_b,
        )
        if glyph_text and font is not None and n_glyphs > 0:
            draw = _ID.Draw(canvas)
            for i in range(n_glyphs):
                # Centered fractions avoid placing glyphs right at the
                # path endpoints where the strip is narrowest.
                frac = (i + 0.5) / n_glyphs
                pa = _point_at_fraction(poly_a, frac)
                pb = _point_at_fraction(poly_b, frac)
                if pa is None or pb is None:
                    continue
                cx = int((pa[0] + pb[0]) / 2)
                cy = int((pa[1] + pb[1]) / 2)
                outline_text(
                    draw, (cx, cy), glyph_text, font, color_rgb,
                    shadow=(30, 25, 18), width=2, anchor="mm",
                )
    return True


def _resolve_glyph_params(sym, gly, fonts, poly_a, poly_b):
    """Resolve buffer-zone glyph params from either schema → (text, count, font, rgb).

    Returns (None, 0, None, None) for invalid configs so the caller can
    bail cleanly.
    """
    from PIL import ImageFont as _IF
    from src.pipeline import font_path
    if sym:
        text = sym.get("character") or ""
        count = max(1, int(sym.get("count", 6)))
        font_name = sym.get("font") or "DejaVuSans"
        font_size = int(sym.get("font_size", 18))
        # Treat `font` as a font-file name first (legacy schema), fall
        # back to a font role lookup for compatibility.
        font = None
        try:
            f_name = font_name if font_name.endswith(".ttf") else font_name + ".ttf"
            font = _IF.truetype(font_path(f_name), font_size)
        except Exception:
            if fonts:
                font = fonts.get(font_name) or fonts.get("settle")
        c = sym.get("color") or (180, 130, 50)
        rgb = tuple(int(v) for v in (c[:3] if len(c) > 3 else c))
        return text, count, font, rgb

    # glyphs schema (spacing-based)
    text = (gly or {}).get("text") or ""
    spacing = max(20, int((gly or {}).get("spacing", 80)))
    role = (gly or {}).get("font") or "bold_14"
    font = (fonts or {}).get(role) or (fonts or {}).get("settle")
    c = (gly or {}).get("color") or (180, 130, 50)
    rgb = tuple(int(v) for v in (c[:3] if len(c) > 3 else c))
    avg_len = (_polyline_length_px(poly_a) + _polyline_length_px(poly_b)) / 2.0
    count = max(1, int(avg_len / spacing))
    return text, count, font, rgb


def _polyline_length_px(pts: list[tuple[int, int]]) -> float:
    """Cumulative segment length of a pixel-space polyline."""
    if len(pts) < 2:
        return 0.0
    return sum(
        math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        for i in range(len(pts) - 1)
    )
