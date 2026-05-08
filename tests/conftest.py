"""Shared fixtures for the pipeline test suite.

Synthetic inputs are tiny (100×100 DEMs, hand-built road graphs) so the
whole suite finishes in seconds. Real fixtures (SRTM tiles, OSM road
data) are *not* exercised here — those would test the network layer
rather than the masking / spread / sinking logic we care about.
"""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pytest

from src.pipeline import MapBounds


# ----------------------------------------------------------------------
#  Bounds + DEM fixtures
# ----------------------------------------------------------------------

@pytest.fixture
def small_bounds() -> MapBounds:
    """A 1°×1° square — easy lat/lon math for synthetic tests."""
    return MapBounds(lat_n=39.0, lat_s=38.0, lon_w=-77.0, lon_e=-76.0)


@pytest.fixture
def grid_size() -> tuple[int, int]:
    """100×100 — small enough that flood-fills are instantaneous."""
    return (100, 100)


@pytest.fixture
def flat_dem(grid_size) -> np.ndarray:
    """A flat 100m-elevation DEM. No water by default at sea_level=0."""
    h, w = grid_size
    return np.full((h, w), 100.0, dtype=np.float32)


@pytest.fixture
def dem_with_central_lake(grid_size) -> np.ndarray:
    """100×100 land with a central 30×30 underwater rectangle.

    Used to test: contamination must not tunnel across the lake;
    barriers crossing the lake should trim out the underwater portion.
    """
    h, w = grid_size
    dem = np.full((h, w), 100.0, dtype=np.float32)
    dem[35:65, 35:65] = -10.0  # below sea level → water
    return dem


@pytest.fixture
def dem_with_horizontal_strait(grid_size) -> np.ndarray:
    """100×100 land split by a horizontal water strait at rows 45-55.

    Used to test: a barrier going north→south can be land-water-land
    (so trim picks the longer of the two land runs); a flood-fill from
    the top vs bottom edge produces disjoint components if a polyline
    spans the strait.
    """
    h, w = grid_size
    dem = np.full((h, w), 100.0, dtype=np.float32)
    dem[45:55, :] = -10.0
    return dem


@pytest.fixture
def water_mask_horizontal_strait(dem_with_horizontal_strait) -> np.ndarray:
    """Pre-computed water mask for the horizontal-strait DEM."""
    return dem_with_horizontal_strait <= 0


@pytest.fixture
def water_mask_central_lake(dem_with_central_lake) -> np.ndarray:
    return dem_with_central_lake <= 0


# ----------------------------------------------------------------------
#  Road graph fixtures
# ----------------------------------------------------------------------

@pytest.fixture
def two_island_graph():
    """Two disconnected components: a 4-node loop (the "main") and a
    2-node spur. Tests that endpoint snapping picks the main component
    when the literal-nearest node is on the spur.

    Layout (node_id : (lat, lon)):
        Main loop (square at 38.5N):
            1: (38.50, -76.80)  ←→  2: (38.50, -76.70)
                ↕                       ↕
            4: (38.40, -76.80)  ←→  3: (38.40, -76.70)
        Spur (isolated, near node 1):
            10: (38.51, -76.81)  ←→  11: (38.52, -76.82)
    """
    nodes = {
        1: (38.50, -76.80),
        2: (38.50, -76.70),
        3: (38.40, -76.70),
        4: (38.40, -76.80),
        10: (38.51, -76.81),
        11: (38.52, -76.82),
    }
    adj: dict = {
        1: [(2, 1000, "primary"), (4, 1000, "primary")],
        2: [(1, 1000, "primary"), (3, 1000, "primary")],
        3: [(2, 1000, "primary"), (4, 1000, "primary")],
        4: [(1, 1000, "primary"), (3, 1000, "primary")],
        10: [(11, 200, "primary")],
        11: [(10, 200, "primary")],
    }
    return nodes, adj


@pytest.fixture
def simple_path_north_south(small_bounds) -> list[tuple[float, float]]:
    """A polyline running roughly north-to-south across the bounds.

    Useful for testing barrier_side_mask: with a horizontal strait
    crossing the bounds, this path will be land-water-land, exercising
    the trim + tangent extension.
    """
    return [
        (38.95, -76.50),  # near top edge
        (38.50, -76.50),  # middle
        (38.05, -76.50),  # near bottom edge
    ]
