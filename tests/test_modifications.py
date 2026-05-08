"""Tests for terrain.modifications: water_mask + apply_sinking.

apply_sinking unions a polygon (e.g., entire state of Virginia) into
the water mask before stage 2 styling. The bug surface here is small
but real: NaN handling, polygon outside bounds, MultiPolygon parsing.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from src.terrain.modifications import (
    water_mask,
    apply_sinking,
    _parse_geojson_polygons,
    _rasterize_polygons,
)


class TestWaterMask:
    """water_mask is the single function that defines what's water."""

    def test_below_sea_level_is_water(self):
        dem = np.array([[10.0, -5.0], [-2.0, 100.0]], dtype=np.float32)
        mask = water_mask(dem, sea_level=0)
        assert mask[0, 0] == False  # 10m above
        assert mask[0, 1] == True   # -5m below
        assert mask[1, 0] == True   # -2m below
        assert mask[1, 1] == False  # 100m above

    def test_at_sea_level_is_water(self):
        dem = np.array([[0.0, 0.5]], dtype=np.float32)
        mask = water_mask(dem, sea_level=0)
        assert mask[0, 0] == True  # exactly 0 → water (<=)
        assert mask[0, 1] == False

    def test_nan_treated_as_water(self):
        """NaN means "no SRTM data" — historically this is ocean (no land
        elevation reading). Test ensures the convention is preserved."""
        dem = np.array([[10.0, np.nan], [50.0, np.nan]], dtype=np.float32)
        mask = water_mask(dem, sea_level=0)
        assert mask[0, 1] == True
        assert mask[1, 1] == True
        assert mask[0, 0] == False

    def test_sea_level_rise_floods_low_land(self):
        """Sea level rise = configurable scenario (post-apocalyptic etc.)"""
        dem = np.array([[5.0, 15.0, 25.0]], dtype=np.float32)
        # Default sea level 0: nothing is water
        assert not water_mask(dem, 0).any()
        # Rise to 10m: only the 5m cell submerged
        m1 = water_mask(dem, 10)
        assert m1.tolist() == [[True, False, False]]
        # Rise to 20m: 5m and 15m cells submerged
        m2 = water_mask(dem, 20)
        assert m2.tolist() == [[True, True, False]]


class TestParseGeojsonPolygons:
    """The schema accepts both Polygon and MultiPolygon GeoJSON."""

    def test_simple_polygon(self):
        geom = {
            "type": "Polygon",
            "coordinates": [[
                [-77.0, 38.0], [-76.0, 38.0],
                [-76.0, 39.0], [-77.0, 39.0], [-77.0, 38.0],
            ]],
        }
        polys = _parse_geojson_polygons(geom)
        assert len(polys) == 1
        # GeoJSON is (lon, lat); function returns (lat, lon)
        assert polys[0][0] == (38.0, -77.0)

    def test_multipolygon_yields_multiple(self):
        geom = {
            "type": "MultiPolygon",
            "coordinates": [
                [[[-77.0, 38.0], [-76.0, 38.0], [-76.0, 39.0], [-77.0, 38.0]]],
                [[[-76.5, 38.5], [-76.0, 38.5], [-76.0, 39.0], [-76.5, 38.5]]],
            ],
        }
        polys = _parse_geojson_polygons(geom)
        assert len(polys) == 2

    def test_unsupported_type_returns_empty(self):
        polys = _parse_geojson_polygons({"type": "LineString", "coordinates": []})
        assert polys == []

    def test_empty_polygon_returns_empty(self):
        polys = _parse_geojson_polygons({"type": "Polygon", "coordinates": []})
        assert polys == []


class TestRasterizePolygons:
    """The polygon rasterization step produces the mask that gets unioned
    into the water mask."""

    def test_polygon_inside_bounds_filled(self, small_bounds):
        # Square in the middle of the bounds (lat 38.3-38.7, lon -76.7 - -76.3)
        poly = [(38.3, -76.7), (38.3, -76.3), (38.7, -76.3), (38.7, -76.7)]
        mask = _rasterize_polygons([poly], small_bounds, h=100, w=100)
        assert mask.dtype == np.bool_
        assert mask.sum() > 0
        # Center cell at (lat=38.5, lon=-76.5) should be inside
        # → pixel (50, 50)
        assert mask[50, 50]
        # Corner (lat=38.05, lon=-76.05) should be outside
        assert not mask[95, 95]

    def test_empty_polygon_list_returns_zeros(self, small_bounds):
        mask = _rasterize_polygons([], small_bounds, 50, 50)
        assert mask.dtype == np.bool_
        assert not mask.any()

    def test_degenerate_polygon_no_crash(self, small_bounds):
        # Two points → can't form a polygon, function should skip
        polys = [[(38.3, -76.5), (38.7, -76.5)]]
        mask = _rasterize_polygons(polys, small_bounds, 50, 50)
        assert not mask.any()


class TestApplySinking:
    """The integration target: apply_sinking floods a region into water."""

    def test_disabled_returns_unchanged(self, small_bounds):
        water_in = np.zeros((50, 50), dtype=bool)
        out, n = apply_sinking(water_in, {"enabled": False}, small_bounds,
                                Path("."))
        assert n == 0
        # Same array object — no copy
        assert out is water_in

    def test_unsupported_method_returns_unchanged(self, small_bounds):
        water_in = np.zeros((50, 50), dtype=bool)
        cfg = {"enabled": True, "method": "subsidence_unimplemented",
               "region": "Test"}
        out, n = apply_sinking(water_in, cfg, small_bounds, Path("."))
        assert n == 0
        assert out is water_in

    def test_geojson_source_floods_polygon(self, small_bounds, tmp_path):
        """An explicit GeoJSON source path correctly floods its polygon."""
        # Create a small Polygon that covers ~half the canvas
        geojson = {
            "type": "Polygon",
            "coordinates": [[
                [-77.0, 38.0], [-76.0, 38.0],
                [-76.0, 38.5], [-77.0, 38.5], [-77.0, 38.0],
            ]],
        }
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        gj_path = cfg_dir / "test_region.json"
        gj_path.write_text(json.dumps(geojson))

        water_in = np.zeros((50, 50), dtype=bool)
        cfg = {
            "enabled": True, "method": "nan_mask",
            "source": "test_region.json",
        }
        out, n = apply_sinking(water_in, cfg, small_bounds, tmp_path)
        # Bottom half (lat 38.0-38.5) should be flooded; top half not.
        # In pixel space, lat=38.5 is row 25, lat=38.0 is row 50.
        # So rows 25-49 should be water, 0-24 should not.
        assert n > 0, "expected some cells flooded"
        assert out[40, 25], "bottom-half cell expected to be water"
        assert not out[10, 25], "top-half cell expected to remain land"

    def test_missing_source_file_returns_unchanged(self, small_bounds, tmp_path):
        water_in = np.zeros((50, 50), dtype=bool)
        cfg = {
            "enabled": True, "method": "nan_mask",
            "source": "missing.json",
        }
        out, n = apply_sinking(water_in, cfg, small_bounds, tmp_path)
        # Function logs a warning and returns unchanged; no crash.
        assert n == 0

    def test_in_place_mutation(self, small_bounds, tmp_path):
        """apply_sinking mutates the water mask in-place via np.logical_or
        with out=. This contract is depended on by the pipeline."""
        geojson = {
            "type": "Polygon",
            "coordinates": [[
                [-76.7, 38.3], [-76.3, 38.3],
                [-76.3, 38.7], [-76.7, 38.7], [-76.7, 38.3],
            ]],
        }
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        gj_path = cfg_dir / "small.json"
        gj_path.write_text(json.dumps(geojson))

        water_in = np.zeros((50, 50), dtype=bool)
        original_id = id(water_in)
        out, _ = apply_sinking(water_in, {
            "enabled": True, "method": "nan_mask", "source": "small.json"
        }, small_bounds, tmp_path)
        # Same array (in-place via np.logical_or out=)
        assert id(out) == original_id
