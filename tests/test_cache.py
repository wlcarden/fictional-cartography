"""Tests for the stage cache key generator + StageCache class.

The cache invalidation contract is delicate: every visual semantic
change must bump the version string OR the hashed inputs. Wrong keys =
stale renders that look correct but use old logic.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from src.cache import stage_key, StageCache


class TestStageKeyDeterminism:
    """Same inputs → same key. Different inputs → different keys."""

    def test_identical_inputs_produce_identical_keys(self):
        k1 = stage_key("v1", a=1, b=[2, 3], c={"d": 4})
        k2 = stage_key("v1", a=1, b=[2, 3], c={"d": 4})
        assert k1 == k2

    def test_kwargs_order_doesnt_matter(self):
        """Keyword args sort internally so insertion order is irrelevant."""
        k1 = stage_key("v1", a=1, b=2, c=3)
        k2 = stage_key("v1", c=3, a=1, b=2)
        assert k1 == k2

    def test_positional_order_does_matter(self):
        """Positional args ARE order-sensitive."""
        k1 = stage_key("v1", "first", "second")
        k2 = stage_key("v1", "second", "first")
        assert k1 != k2

    def test_value_change_changes_key(self):
        k1 = stage_key("v1", x=10)
        k2 = stage_key("v1", x=11)
        assert k1 != k2

    def test_version_bump_changes_key(self):
        """The whole point of version strings: bumping them invalidates."""
        k1 = stage_key("v1", payload={"a": 1})
        k2 = stage_key("v2", payload={"a": 1})
        assert k1 != k2

    def test_nested_dict_change_changes_key(self):
        k1 = stage_key("v1", terrain={"sea_level": 10, "shade": {"sun_alt": 35}})
        k2 = stage_key("v1", terrain={"sea_level": 10, "shade": {"sun_alt": 36}})
        assert k1 != k2

    def test_list_order_matters(self):
        """Lists are ordered: [1,2,3] != [3,2,1]."""
        k1 = stage_key("v1", arr=[1, 2, 3])
        k2 = stage_key("v1", arr=[3, 2, 1])
        assert k1 != k2

    def test_empty_inputs_produce_stable_key(self):
        k1 = stage_key()
        k2 = stage_key()
        assert k1 == k2
        # Should be a sha1 hex (40 chars)
        assert len(k1) == 40

    def test_returns_hex_digest(self):
        """Sanity: result is a hex string."""
        k = stage_key("v1", x=5)
        assert isinstance(k, str)
        assert len(k) == 40
        int(k, 16)  # parses as hex without error


class TestStageKeyCommonPatterns:
    """Verify the actual usage patterns from src/pipeline.py work."""

    def test_pipeline_s2_pattern(self):
        """Reproduce a real-world s2_key call from pipeline.py."""
        s1_key = "abc123"
        terrain_cfg = {"sea_level_rise": 0, "shade": {"sun_altitude": 35}}
        regions = [{"name": "test", "tint": {"r": 0.1}}]
        sinking = {"enabled": False}
        barriers = []
        sea_level = 0

        k1 = stage_key(
            "v5", s1_key,
            terrain=terrain_cfg,
            urbanization={},
            sinking=sinking,
            region_tints=[(r["name"], r.get("tint")) for r in regions],
            barriers=barriers,
            sea_level=sea_level,
        )
        # Same inputs → same key
        k2 = stage_key(
            "v5", s1_key,
            terrain=terrain_cfg, urbanization={}, sinking=sinking,
            region_tints=[(r["name"], r.get("tint")) for r in regions],
            barriers=barriers, sea_level=sea_level,
        )
        assert k1 == k2

    def test_changing_sea_level_changes_s2_key(self):
        common = {
            "terrain": {"sea_level_rise": 0},
            "urbanization": {}, "sinking": {}, "region_tints": [],
            "barriers": [],
        }
        k1 = stage_key("v5", "abc", **common, sea_level=0)
        k2 = stage_key("v5", "abc", **common, sea_level=15)
        assert k1 != k2, "sea level change must invalidate s2"

    def test_changing_barrier_geometry_changes_s2_key(self):
        """Barriers feed tint constraints, so geometry edits must
        invalidate stage 2."""
        b1 = [{"name": "Wall", "endpoints": {"east": {"lat": 39.0}}}]
        b2 = [{"name": "Wall", "endpoints": {"east": {"lat": 39.5}}}]
        k1 = stage_key("v5", "abc", barriers=b1)
        k2 = stage_key("v5", "abc", barriers=b2)
        assert k1 != k2


class TestStageCache:
    """The StageCache class wraps disk persistence with hit/miss helpers."""

    def test_save_then_hit(self, tmp_path, monkeypatch):
        """Round-trip: save then verify hit() returns True for the same key."""
        # Redirect cache_dir to tmp
        from src import pipeline
        monkeypatch.setattr(pipeline, "project_root", lambda: tmp_path)

        cache = StageCache("test_config")
        key = stage_key("v1", x=1)
        # Save a tiny numpy array
        arr = np.zeros((10, 10), dtype=np.uint8)
        cache.save_npy("terrain_dem", key, arr)
        # Should be a cache hit for the same key
        assert cache.hit("terrain_dem", key) is True
        # Should be a miss for a different key
        assert cache.hit("terrain_dem", "different_key") is False

    def test_load_npy_round_trips(self, tmp_path, monkeypatch):
        from src import pipeline
        monkeypatch.setattr(pipeline, "project_root", lambda: tmp_path)
        cache = StageCache("test_config")
        key = stage_key("v1", payload="test")
        arr = np.array([[1, 2], [3, 4]], dtype=np.float32)
        cache.save_npy("terrain_dem", key, arr, custom_meta="hello")
        loaded = cache.load_npy("terrain_dem")
        assert (loaded == arr).all()

    def test_get_meta_returns_custom_fields(self, tmp_path, monkeypatch):
        from src import pipeline
        monkeypatch.setattr(pipeline, "project_root", lambda: tmp_path)
        cache = StageCache("test_config")
        key = stage_key("v1", x=99)
        arr = np.zeros((5, 5))
        cache.save_npy("terrain_dem", key, arr,
                        bounds={"lat_n": 40}, custom="value")
        meta = cache.get_meta("terrain_dem")
        # `key` field is the cache key; bounds + custom were our extras.
        assert meta.get("custom") == "value"
        assert meta.get("bounds", {}).get("lat_n") == 40

    def test_miss_when_meta_missing(self, tmp_path, monkeypatch):
        from src import pipeline
        monkeypatch.setattr(pipeline, "project_root", lambda: tmp_path)
        cache = StageCache("nonexistent_config")
        # No save_npy called → meta file doesn't exist
        assert cache.hit("terrain_dem", "any_key") is False
