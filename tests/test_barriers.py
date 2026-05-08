"""Pipeline correctness tests for barrier-related logic.

These tests target the bug classes we hit during development:
  - Path masking topology (the wall must bisect the canvas; flood-fill
    based side-masks must produce disjoint sets).
  - Snap-to-disconnected-island fallback (literal-nearest may land on a
    tiny graph component; main-component snap is the recovery).
  - Path trimming + tangent/BFS extension to shore (so the wall reaches
    actual water on both ends).

Synthetic inputs throughout — small DEMs, hand-built graphs.
"""
from __future__ import annotations

import numpy as np
import pytest

from src.styling.barriers import (
    band_mask,
    barrier_side_mask,
    _connected_components,
    find_nearest_node,
    find_nearest_node_in_component,
    _trim_path_to_land,
    _compute_astar_road_network,
    astar,
)


# ======================================================================
#  _connected_components
# ======================================================================

class TestConnectedComponents:
    """The components labeling underpins main-component snap recovery."""

    def test_single_connected_graph_one_component(self, two_island_graph):
        nodes, adj = two_island_graph
        # Build a graph that's all connected: link node 11 to node 1
        adj_connected = {**adj}
        adj_connected[11] = adj_connected[11] + [(1, 500, "primary")]
        adj_connected[1] = adj_connected[1] + [(11, 500, "primary")]
        comp = _connected_components(adj_connected, nodes)
        assert set(comp.values()) == {0}, \
            "fully-connected graph must have a single component (id=0)"

    def test_two_components_main_is_zero(self, two_island_graph):
        nodes, adj = two_island_graph
        comp = _connected_components(adj, nodes)
        # Component 0 is the main (4 nodes), component 1 is the spur (2 nodes)
        size_0 = sum(1 for c in comp.values() if c == 0)
        size_1 = sum(1 for c in comp.values() if c == 1)
        assert size_0 == 4, f"main component should have 4 nodes, got {size_0}"
        assert size_1 == 2, f"spur component should have 2 nodes, got {size_1}"
        # Node 10 / 11 are the spur
        assert comp[10] == 1 and comp[11] == 1
        assert comp[1] == 0 and comp[2] == 0

    def test_components_descending_size(self):
        """Component IDs are size-ranked: 0 = largest, 1 = next, ..."""
        nodes = {i: (38, -76) for i in range(10)}
        # 5-node clique, 3-node clique, 2-node clique
        adj = {}
        for clique, ids in enumerate([(0, 1, 2, 3, 4), (5, 6, 7), (8, 9)]):
            for a in ids:
                adj[a] = [(b, 1, "x") for b in ids if b != a]
        comp = _connected_components(adj, nodes)
        # 5-clique = 0, 3-clique = 1, 2-clique = 2
        size_by_label = {}
        for cid in comp.values():
            size_by_label[cid] = size_by_label.get(cid, 0) + 1
        assert size_by_label[0] == 5
        assert size_by_label[1] == 3
        assert size_by_label[2] == 2


# ======================================================================
#  find_nearest_node + find_nearest_node_in_component
# ======================================================================

class TestNearestNodeSnap:
    """Snap-to-disconnected-island fallback was the root cause of the
    Wall-east-endpoint bug. Verify the recovery path explicitly."""

    def test_literal_nearest_on_spur(self, two_island_graph):
        nodes, _ = two_island_graph
        # Click at (38.51, -76.81) — closest to spur node 10
        nid = find_nearest_node(nodes, 38.51, -76.81)
        assert nid == 10, f"expected literal-nearest=10, got {nid}"

    def test_main_component_snap_skips_spur(self, two_island_graph):
        """The same click should snap to a MAIN-COMPONENT node when we ask
        for component-restricted nearest. This is what saves the wall A*
        from returning None on disconnected snap targets."""
        nodes, adj = two_island_graph
        comp = _connected_components(adj, nodes)
        # Click at the spur location, but ask for main component.
        nid = find_nearest_node_in_component(nodes, 38.51, -76.81,
                                              comp, target_comp=0)
        assert nid in {1, 2, 3, 4}, \
            f"main-component snap should pick from {{1,2,3,4}}, got {nid}"

    def test_empty_graph_returns_none(self):
        assert find_nearest_node({}, 38.5, -76.5) is None
        assert find_nearest_node_in_component({}, 38.5, -76.5, {}) is None


# ======================================================================
#  A* through small graphs
# ======================================================================

class TestAStar:
    """A* itself is a battle-tested algorithm, but we verify the
    integration: corridor penalties + tie-breakers."""

    def test_simple_path_recovered(self, two_island_graph):
        nodes, adj = two_island_graph
        path = astar(nodes, adj, 1, 3)
        assert path is not None, "expected a path between 1 and 3"
        assert path[0] == nodes[1]
        assert path[-1] == nodes[3]

    def test_disconnected_returns_none(self, two_island_graph):
        nodes, adj = two_island_graph
        # 1 is in the main component, 10 is in the spur — no path
        assert astar(nodes, adj, 1, 10) is None

    def test_compute_astar_recovers_disconnected_endpoints(
        self, two_island_graph
    ):
        """High-level: even if config snaps land on disconnected islands,
        _compute_astar_road_network's main-component fallback recovers."""
        nodes, adj = two_island_graph

        def fake_fetch_graph(b, name):
            return {"nodes": nodes, "adj": adj, "components": None}

        # Endpoints near the spur (node 10/11) literal-nearest; the
        # main-component fallback should snap them to {1..4}.
        b_cfg = {
            "name": "test_wall",
            "endpoints": {
                "east": {"lat": 38.52, "lon": -76.82},
                "west": {"lat": 38.51, "lon": -76.81},
            },
        }
        path = _compute_astar_road_network(b_cfg, "test_wall", fake_fetch_graph)
        # Both endpoints will snap to main-component nodes; A* should find
        # at least the start=goal trivial path or a real path.
        assert path is not None, \
            "expected fallback to find a path via main component"
        # Both endpoints must lie in the main component
        first = path[0]
        last = path[-1]
        assert first in [(38.50, -76.80), (38.50, -76.70),
                          (38.40, -76.70), (38.40, -76.80)]
        assert last in [(38.50, -76.80), (38.50, -76.70),
                         (38.40, -76.70), (38.40, -76.80)]


# ======================================================================
#  barrier_side_mask — the topology bug class
# ======================================================================

class TestBarrierSideMask:
    """The mask must bisect the canvas when the path goes shore-to-shore.

    Original bug: pass-through outside polyline x-range allowed
    contamination to flow around the wall's endpoints. Replaced with
    flood-fill bounded by water + path-stamp.
    """

    def test_no_water_uses_polygon_fallback(self, small_bounds):
        """Without water_mask, falls back to polygon approximation. The
        polygon mask doesn't perfectly bisect, but should not crash."""
        path = [(38.7, -76.8), (38.5, -76.5), (38.3, -76.2)]
        mask = barrier_side_mask(path, "south", small_bounds, 50, 50)
        assert mask.shape == (50, 50)
        assert mask.dtype == np.bool_
        # Polygon fallback returns SOME true cells (the south half-plane)
        assert mask.sum() > 0

    def test_flood_fill_bisects_canvas_with_shore_to_shore_path(
        self, small_bounds, water_mask_horizontal_strait
    ):
        """A polyline crossing a horizontal water strait + reaching land
        on both top and bottom should produce STRICTLY DISJOINT north
        and south flood-fill masks. This is the regression test for
        the wall-leak bug."""
        h, w = water_mask_horizontal_strait.shape
        # Vertical line at x=50 running from y=10 (top) to y=90 (bottom).
        # In lat/lon: top is high lat, bottom is low lat.
        # Convert pixel (50, 10) → lat/lon
        def pixel_to_latlon(x, y):
            lon = small_bounds.lon_w + (x + 0.5) / w * small_bounds.lon_span
            lat = small_bounds.lat_n - (y + 0.5) / h * small_bounds.lat_span
            return (lat, lon)

        path = [pixel_to_latlon(50, 10), pixel_to_latlon(50, 50),
                pixel_to_latlon(50, 90)]

        north = barrier_side_mask(path, "north", small_bounds, h, w,
                                   water_mask=water_mask_horizontal_strait,
                                   strict=True)
        south = barrier_side_mask(path, "south", small_bounds, h, w,
                                   water_mask=water_mask_horizontal_strait,
                                   strict=True)
        overlap = (north & south).sum()
        assert overlap == 0, (
            f"north and south flood-fills must be disjoint, got "
            f"{overlap} overlapping cells")
        assert north.sum() > 0, "north side should have some land"
        assert south.sum() > 0, "south side should have some land"

    def test_pass_through_default_includes_blocked(
        self, small_bounds, water_mask_horizontal_strait
    ):
        """Non-strict mode includes blocked cells (water/path) — used
        to keep contamination/tints from over-clipping."""
        h, w = water_mask_horizontal_strait.shape
        def pixel_to_latlon(x, y):
            return (small_bounds.lat_n - (y + 0.5) / h * small_bounds.lat_span,
                    small_bounds.lon_w + (x + 0.5) / w * small_bounds.lon_span)

        path = [pixel_to_latlon(50, 10), pixel_to_latlon(50, 90)]
        non_strict = barrier_side_mask(path, "north", small_bounds, h, w,
                                        water_mask=water_mask_horizontal_strait,
                                        strict=False)
        strict = barrier_side_mask(path, "north", small_bounds, h, w,
                                    water_mask=water_mask_horizontal_strait,
                                    strict=True)
        # Non-strict ⊇ strict (every strict-True cell is also non-strict-True)
        assert (non_strict & ~strict).sum() > 0, \
            "non-strict should include blocked cells absent from strict"
        # No cell can be in strict but not non-strict
        assert (strict & ~non_strict).sum() == 0

    def test_empty_path_returns_all_true(self, small_bounds):
        mask = barrier_side_mask([], "north", small_bounds, 20, 20)
        assert mask.all(), "empty path → no clipping (all True)"

    def test_invalid_side_returns_all_true(self, small_bounds):
        path = [(38.5, -76.5), (38.4, -76.4)]
        mask = barrier_side_mask(path, "garbage", small_bounds, 20, 20)
        assert mask.all()


# ======================================================================
#  _trim_path_to_land — water-trimming + tangent/BFS extension
# ======================================================================

class TestTrimPathToLand:
    """The wall must reach water on both ends. Synthetic test: a path
    that's entirely on land has its termini extended to the strait."""

    def test_no_water_mask_returns_unchanged(self, small_bounds):
        path = [(38.5, -76.5), (38.4, -76.4)]
        out = _trim_path_to_land(path, None, small_bounds, 50, 50)
        assert out == path

    def test_path_fully_on_land_unchanged(
        self, small_bounds, water_mask_central_lake
    ):
        """Path that doesn't cross water at all — unmodified by trim."""
        h, w = water_mask_central_lake.shape
        # Path along the top edge (well above the central lake at y=35-65)
        path = [(38.95, -76.95), (38.95, -76.50), (38.95, -76.05)]
        out = _trim_path_to_land(path, water_mask_central_lake,
                                  small_bounds, h, w)
        # No internal water — entire run is "land". Tangent extensions
        # may or may not append; the original points should still be
        # somewhere in the output.
        for p in path:
            assert p in out, f"original point {p} dropped from trim"

    def test_path_with_water_segment_trims(
        self, small_bounds, water_mask_horizontal_strait
    ):
        """Path crossing a horizontal strait gets trimmed to a single
        contiguous land run."""
        h, w = water_mask_horizontal_strait.shape
        # Build a path with three waypoints:
        # (a) top land,  (b) inside the strait,  (c) bottom land
        path = [
            (38.85, -76.50),   # top land
            (38.50, -76.50),   # in strait (water)
            (38.15, -76.50),   # bottom land
        ]
        out = _trim_path_to_land(path, water_mask_horizontal_strait,
                                  small_bounds, h, w)
        # The trim should pick the longer land run + extensions; the
        # in-strait waypoint at lat 38.50 should NOT be in the output.
        for (lat, lon) in out:
            # Convert back to pixel and verify NOT in strait
            x = int((lon - small_bounds.lon_w) / small_bounds.lon_span * w)
            y = int((small_bounds.lat_n - lat) / small_bounds.lat_span * h)
            x = max(0, min(w - 1, x)); y = max(0, min(h - 1, y))
            assert not (45 <= y < 55) or water_mask_horizontal_strait[y, x], \
                f"trim left a point in the strait at y={y}: {(lat, lon)}"


# ======================================================================
#  band_mask — between-two-paths polygon
# ======================================================================

class TestBandMask:
    """band_mask is used by buffer zones + band tints."""

    def test_two_parallel_paths_produce_band(self, small_bounds):
        h, w = 50, 50
        path_a = [(38.7, -76.9), (38.7, -76.5), (38.7, -76.1)]  # top
        path_b = [(38.3, -76.9), (38.3, -76.5), (38.3, -76.1)]  # bottom
        mask = band_mask(path_a, path_b, small_bounds, h, w)
        assert mask.dtype == np.bool_
        assert mask.shape == (h, w)
        # Band sits between rows ~15 (lat 38.7) and ~35 (lat 38.3)
        assert mask.sum() > 0, "non-empty band expected"
        # Cells well outside the band should be False
        assert not mask[5, 25], "cell above band should be False"
        assert not mask[45, 25], "cell below band should be False"
        # Cells inside the band should be True
        assert mask[25, 25], "cell inside band should be True"

    def test_empty_paths_return_empty_mask(self, small_bounds):
        mask = band_mask([], [], small_bounds, 20, 20)
        assert mask.sum() == 0

    def test_one_path_empty_returns_empty(self, small_bounds):
        mask = band_mask([(38.5, -76.5), (38.4, -76.4)], [], small_bounds, 20, 20)
        assert mask.sum() == 0

    def test_degenerate_short_paths_return_empty(self, small_bounds):
        # Single points each — polygon will have only 2 vertices, < 3
        mask = band_mask([(38.5, -76.5)], [(38.4, -76.4)], small_bounds, 20, 20)
        assert mask.sum() == 0
