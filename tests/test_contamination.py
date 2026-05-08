"""Tests for contamination spread.

The big bug class here was "water tunneling" — the original Euclidean
distance transform let contamination jump across water bodies because
distance was computed in the abstract Cartesian sense rather than the
flood-fill-through-land sense. The new BFS-through-land via max-pool
dilation is the fix; this test suite is the regression guard.
"""
from __future__ import annotations

import numpy as np
import pytest
from unittest.mock import patch

from src.styling.contamination import compute_intensity


# Reusable mock for fetch_osm_nodes — all contamination tests want to
# bypass the real Overpass call and inject synthetic source coords.
def _mock_overpass(sources_lat_lon):
    """Return a mock that produces an OSM-shaped response with the given
    list of (lat, lon) sources."""
    elements = [
        {"type": "node", "id": i, "lat": la, "lon": lo}
        for i, (la, lo) in enumerate(sources_lat_lon)
    ]
    return {"elements": elements}


class TestContaminationBasics:
    """Sanity: enabled/disabled, missing fields, empty sources."""

    def test_disabled_returns_none(self, small_bounds):
        out = compute_intensity({"enabled": False}, small_bounds, 50, 50, 0)
        assert out is None

    def test_empty_query_returns_none(self, small_bounds):
        with patch("src.styling.contamination.fetch_osm_nodes",
                    return_value={"elements": []}):
            out = compute_intensity({
                "enabled": True,
                "sources": {"type": "osm_query", "query": ""},
            }, small_bounds, 50, 50, 0)
        assert out is None

    def test_empty_sources_returns_none(self, small_bounds):
        """Query returns no nodes → no contamination."""
        with patch("src.styling.contamination.fetch_osm_nodes",
                    return_value={"elements": []}):
            out = compute_intensity({
                "enabled": True,
                "sources": {"type": "osm_query", "query": 'node["x"="y"]'},
            }, small_bounds, 50, 50, 0)
        # When 0 sources match within bounds, function returns None
        assert out is None


class TestContaminationSpread:
    """The actual flood-fill behavior."""

    def test_single_source_spreads_to_neighbors(self, small_bounds):
        """A single source produces a non-empty intensity field."""
        # Source at the center of the bounds — lat 38.5, lon -76.5
        with patch("src.styling.contamination.fetch_osm_nodes",
                    return_value=_mock_overpass([(38.5, -76.5)])):
            cfg = {
                "enabled": True,
                "name": "Test",
                "sources": {
                    "type": "osm_query", "query": 'node["x"="y"]',
                    "epicenter": {"lat": 38.5, "lon": -76.5},
                    "weight_scale": 100,
                    "weighting": "distance_from_epicenter",
                },
                "spread": {"max_distance": 100},
                "overlay": {"color": [0, 0, 0], "max_opacity": 0.5},
            }
            out = compute_intensity(cfg, small_bounds, 50, 50, 0)
        assert out is not None
        intensity, sources = out
        assert intensity.shape == (50, 50)  # h + 2*border = 50
        assert intensity.max() > 0, "expected some non-zero intensity"
        assert len(sources) == 1


class TestNoWaterTunneling:
    """Regression test for the original Euclidean-distance bug.

    Setup: a horizontal water strait (rows 45-55) bisects the canvas.
    Source on the TOP land. Contamination should NOT reach the BOTTOM
    land because the BFS-through-land respects water as an obstacle.
    The original Euclidean version would have leaked across.
    """

    def test_contamination_blocked_by_water(
        self, small_bounds, water_mask_horizontal_strait
    ):
        # Source near top of bounds (above the strait)
        # In our 100×100 grid, strait is rows 45-55 (lat 38.55 - 38.45).
        # Place source at row 20 = lat ~38.80
        # Cell on bottom land at row 80 = lat ~38.20
        #   Euclidean distance: 60 cells → would contaminate at max_distance > 60
        #   BFS-through-land: must walk AROUND the strait → ∞ within bounds
        with patch("src.styling.contamination.fetch_osm_nodes",
                    return_value=_mock_overpass([(38.80, -76.50)])):
            cfg = {
                "enabled": True,
                "name": "BlockTest",
                "sources": {
                    "type": "osm_query", "query": 'node["x"="y"]',
                    "epicenter": {"lat": 38.80, "lon": -76.50},
                    "weight_scale": 100,
                    "weighting": "uniform",
                },
                "spread": {"max_distance": 200, "blocked_by": ["water"]},
                "overlay": {"color": [0, 0, 0], "max_opacity": 0.5},
            }
            out = compute_intensity(cfg, small_bounds, 100, 100, 0,
                                     water_mask=water_mask_horizontal_strait)
        assert out is not None
        intensity, _ = out
        # Top land should have intensity > 0 near the source
        # With border=0, source at (lat=38.80) is canvas y=20
        # Cell at canvas y=10 (still top land) should be contaminated
        top_cell = intensity[10, 50]
        bottom_cell = intensity[80, 50]
        assert top_cell > 0, f"top-land near source should be contaminated, got {top_cell}"
        # Bottom cell — across the water strait — must NOT be contaminated.
        # This is the regression assertion for the tunneling bug.
        assert bottom_cell == 0, (
            f"bottom-land across water strait must NOT be contaminated; "
            f"got intensity={bottom_cell}. This is the water-tunneling "
            f"regression test."
        )

    def test_water_cells_have_zero_intensity(
        self, small_bounds, water_mask_horizontal_strait
    ):
        """Water cells must always have intensity=0 — the spread can
        approach but never enter water."""
        with patch("src.styling.contamination.fetch_osm_nodes",
                    return_value=_mock_overpass([(38.80, -76.50)])):
            cfg = {
                "enabled": True, "name": "WaterTest",
                "sources": {
                    "type": "osm_query", "query": 'node["x"="y"]',
                    "epicenter": {"lat": 38.5, "lon": -76.5},
                    "weight_scale": 100, "weighting": "uniform",
                },
                "spread": {"max_distance": 1000, "blocked_by": ["water"]},
                "overlay": {"color": [0, 0, 0], "max_opacity": 0.5},
            }
            out = compute_intensity(cfg, small_bounds, 100, 100, 0,
                                     water_mask=water_mask_horizontal_strait)
        assert out is not None
        intensity, _ = out
        # All water cells (rows 45-54) must have intensity == 0
        water_intensity = intensity[45:55, :]
        assert (water_intensity == 0).all(), (
            f"water cells must have zero intensity; max found was "
            f"{water_intensity.max():.4f}"
        )

    def test_max_distance_bounded(
        self, small_bounds, water_mask_horizontal_strait
    ):
        """The spread should not exceed max_distance from source. Use a
        small max_distance and verify cells far from source are zero."""
        with patch("src.styling.contamination.fetch_osm_nodes",
                    return_value=_mock_overpass([(38.80, -76.50)])):
            cfg = {
                "enabled": True, "name": "BoundedTest",
                "sources": {
                    "type": "osm_query", "query": 'node["x"="y"]',
                    "epicenter": {"lat": 38.80, "lon": -76.50},
                    "weight_scale": 100, "weighting": "uniform",
                },
                "spread": {"max_distance": 5, "blocked_by": ["water"]},
                "overlay": {"color": [0, 0, 0], "max_opacity": 0.5},
            }
            out = compute_intensity(cfg, small_bounds, 100, 100, 0,
                                     water_mask=water_mask_horizontal_strait)
        assert out is not None
        intensity, _ = out
        # Source at (lat=38.80) → canvas y=20. With max_distance=5, cells
        # > ~5 rows away should be zero.
        far_cell = intensity[40, 50]  # 20 rows from source
        assert far_cell == 0, (
            f"cell 20 rows from source must be zero with max_distance=5, "
            f"got {far_cell}"
        )
