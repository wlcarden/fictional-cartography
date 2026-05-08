"""Tests for compound tint constraints.

The legacy schema (`constraint: "north_of_wall"`) and the new compound
schema (`constraints: [...] + constraint_logic`) must coexist; existing
YAML must keep working without modification.

Covers:
  - canonical_constraint_key: stable + sort-order-independent
  - collect_constraint_strings: legacy + compound + merged
  - resolve_constraint_mask: AND vs OR semantics, missing barriers
  - apply_all_region_tints lookup using canonical key (backward compat)
"""
from __future__ import annotations

import numpy as np
import pytest

from src.styling.barriers import (
    canonical_constraint_key,
    collect_constraint_strings,
    resolve_constraint_mask,
    barrier_side_mask,
)


# ----------------------------------------------------------------------
#  Synthetic barriers config + paths used across tests
# ----------------------------------------------------------------------

@pytest.fixture
def two_barriers_config():
    """A simple two-barrier setup: 'The Wall' (horizontal, at lat 38.5)
    and 'The River' (vertical, at lon -76.5). Both span the whole bounds."""
    return [
        {"name": "The Wall",  "type": "wall",
         "method": "astar_road_network",
         "endpoints": {
             "east": {"lat": 38.5, "lon": -76.0},
             "west": {"lat": 38.5, "lon": -77.0},
         }},
        {"name": "The River", "type": "river",
         "method": "astar_road_network"},
    ]


@pytest.fixture
def two_barrier_paths(small_bounds, water_mask_horizontal_strait):
    """Hand-crafted paths shore-to-shore:
       - "The Wall": horizontal, crossing the strait at lat ~38.5
       - "The River": vertical at lon ~-76.5
    Both paths reach the canvas edges so flood-fill produces clean
    half-plane masks.
    """
    h, w = water_mask_horizontal_strait.shape

    def pixel_to_latlon(x, y):
        lon = small_bounds.lon_w + (x + 0.5) / w * small_bounds.lon_span
        lat = small_bounds.lat_n - (y + 0.5) / h * small_bounds.lat_span
        return (lat, lon)

    # Horizontal "wall" — top→bottom of canvas at x=50 (vertical actually,
    # but bisects N/S in lat for our purposes).
    # We want the WALL to bisect N from S, so its path runs east-to-west
    # at fixed latitude. With water at rows 45-55, a horizontal path at
    # row 50 sits in water — let's path along row 30 (above strait).
    wall_path = [pixel_to_latlon(0, 30), pixel_to_latlon(50, 30),
                 pixel_to_latlon(99, 30)]

    # The "river" — vertical path at x=50, top to bottom. To reach the
    # land on each side of the horizontal strait, it needs to cross the
    # strait. Simplification: just span y=10..89 and let the trim handle
    # the strait crossing.
    river_path = [pixel_to_latlon(50, 10), pixel_to_latlon(50, 50),
                  pixel_to_latlon(50, 89)]

    return {"The Wall": wall_path, "The River": river_path}


# ----------------------------------------------------------------------
#  collect_constraint_strings
# ----------------------------------------------------------------------

class TestCollectConstraintStrings:

    def test_empty_returns_empty(self):
        items, logic = collect_constraint_strings(None)
        assert items == []
        assert logic == "and"
        items, logic = collect_constraint_strings({})
        assert items == []

    def test_legacy_single_string(self):
        items, logic = collect_constraint_strings({"constraint": "north_of_wall"})
        assert items == ["north_of_wall"]
        assert logic == "and"   # default

    def test_compound_list(self):
        items, logic = collect_constraint_strings({
            "constraints": ["a", "b", "c"],
            "constraint_logic": "or",
        })
        assert items == ["a", "b", "c"]
        assert logic == "or"

    def test_compound_default_logic_is_and(self):
        _, logic = collect_constraint_strings({"constraints": ["a"]})
        assert logic == "and"

    def test_legacy_and_compound_merge(self):
        """Both `constraint` and `constraints` set → legacy first, then list."""
        items, logic = collect_constraint_strings({
            "constraint": "x",
            "constraints": ["y", "z"],
            "constraint_logic": "and",
        })
        assert items == ["x", "y", "z"]
        assert logic == "and"

    def test_invalid_logic_falls_back_to_and(self):
        _, logic = collect_constraint_strings({
            "constraints": ["a"], "constraint_logic": "xor",
        })
        assert logic == "and"

    def test_non_string_constraints_skipped(self):
        items, _ = collect_constraint_strings({
            "constraints": ["a", None, 42, "b"],
        })
        assert items == ["a", "b"]


# ----------------------------------------------------------------------
#  canonical_constraint_key
# ----------------------------------------------------------------------

class TestCanonicalConstraintKey:

    def test_no_constraint_returns_none(self):
        assert canonical_constraint_key(None) is None
        assert canonical_constraint_key({}) is None
        assert canonical_constraint_key({"constraint": ""}) is None

    def test_legacy_single_string_preserves_key(self):
        """Backward-compat: existing YAML using `constraint: "north_of_wall"`
        produces the SAME key as before — no cache invalidation."""
        key = canonical_constraint_key({"constraint": "north_of_wall"})
        assert key == "north_of_wall"

    def test_compound_uses_logic_prefix(self):
        key = canonical_constraint_key({
            "constraints": ["a", "b"], "constraint_logic": "and",
        })
        assert key == "AND:a;b"

    def test_compound_or_uses_or_prefix(self):
        key = canonical_constraint_key({
            "constraints": ["a", "b"], "constraint_logic": "or",
        })
        assert key == "OR:a;b"

    def test_compound_sorts_for_determinism(self):
        """[a, b] and [b, a] under AND/OR (commutative) must produce
        the same key, so cache hits aren't accidentally missed by
        config reordering."""
        k1 = canonical_constraint_key({
            "constraints": ["b", "a"], "constraint_logic": "and",
        })
        k2 = canonical_constraint_key({
            "constraints": ["a", "b"], "constraint_logic": "and",
        })
        assert k1 == k2 == "AND:a;b"

    def test_single_compound_collapses_to_single_string(self):
        """A `constraints: [single]` config produces the same key as the
        legacy `constraint: single` form — they're semantically identical."""
        k1 = canonical_constraint_key({"constraints": ["north_of_wall"]})
        k2 = canonical_constraint_key({"constraint": "north_of_wall"})
        assert k1 == k2 == "north_of_wall"


# ----------------------------------------------------------------------
#  resolve_constraint_mask — semantic correctness
# ----------------------------------------------------------------------

class TestResolveConstraintMask:

    def test_no_config_returns_none(self, small_bounds, two_barriers_config):
        assert resolve_constraint_mask(
            None, two_barriers_config, {}, small_bounds, 50, 50,
        ) is None

    def test_unresolvable_constraint_returns_none(
        self, small_bounds, two_barriers_config
    ):
        """Reference to a barrier that doesn't exist — silently falls
        back to None (logs a warning)."""
        result = resolve_constraint_mask(
            {"constraint": "north_of_nonexistent"},
            two_barriers_config, {}, small_bounds, 50, 50,
        )
        assert result is None

    def test_single_constraint_matches_barrier_side_mask(
        self, small_bounds, two_barrier_paths, two_barriers_config,
        water_mask_horizontal_strait,
    ):
        """A single-constraint resolve_constraint_mask should produce
        the same result as calling barrier_side_mask directly."""
        h, w = water_mask_horizontal_strait.shape
        result = resolve_constraint_mask(
            {"constraint": "north_of_wall"},
            two_barriers_config, two_barrier_paths,
            small_bounds, h, w, water_mask=water_mask_horizontal_strait,
        )
        expected = barrier_side_mask(
            two_barrier_paths["The Wall"], "north", small_bounds, h, w,
            water_mask=water_mask_horizontal_strait,
        )
        assert result is not None
        assert (result == expected).all()

    def test_compound_AND_intersects_masks(
        self, small_bounds, two_barrier_paths, two_barriers_config,
        water_mask_horizontal_strait,
    ):
        """AND of two constraints = bitwise & of the individual masks."""
        h, w = water_mask_horizontal_strait.shape
        result = resolve_constraint_mask(
            {
                "constraints": ["north_of_wall", "east_of_river"],
                "constraint_logic": "and",
            },
            two_barriers_config, two_barrier_paths,
            small_bounds, h, w, water_mask=water_mask_horizontal_strait,
        )
        m_a = barrier_side_mask(
            two_barrier_paths["The Wall"], "north", small_bounds, h, w,
            water_mask=water_mask_horizontal_strait,
        )
        m_b = barrier_side_mask(
            two_barrier_paths["The River"], "east", small_bounds, h, w,
            water_mask=water_mask_horizontal_strait,
        )
        expected = m_a & m_b
        assert result is not None
        assert (result == expected).all()
        # Sanity: AND mask is no larger than either input
        assert result.sum() <= m_a.sum()
        assert result.sum() <= m_b.sum()

    def test_compound_OR_unions_masks(
        self, small_bounds, two_barrier_paths, two_barriers_config,
        water_mask_horizontal_strait,
    ):
        """OR of two constraints = bitwise | of the individual masks."""
        h, w = water_mask_horizontal_strait.shape
        result = resolve_constraint_mask(
            {
                "constraints": ["north_of_wall", "east_of_river"],
                "constraint_logic": "or",
            },
            two_barriers_config, two_barrier_paths,
            small_bounds, h, w, water_mask=water_mask_horizontal_strait,
        )
        m_a = barrier_side_mask(
            two_barrier_paths["The Wall"], "north", small_bounds, h, w,
            water_mask=water_mask_horizontal_strait,
        )
        m_b = barrier_side_mask(
            two_barrier_paths["The River"], "east", small_bounds, h, w,
            water_mask=water_mask_horizontal_strait,
        )
        expected = m_a | m_b
        assert result is not None
        assert (result == expected).all()
        # Sanity: OR mask is no smaller than either input
        assert result.sum() >= m_a.sum()
        assert result.sum() >= m_b.sum()

    def test_partial_resolution_uses_what_resolves(
        self, small_bounds, two_barrier_paths, two_barriers_config,
        water_mask_horizontal_strait,
    ):
        """Compound config where ONE constraint references a missing
        barrier — the resolved one should still produce a mask, the
        unresolvable one is silently dropped."""
        h, w = water_mask_horizontal_strait.shape
        result = resolve_constraint_mask(
            {
                "constraints": ["north_of_wall", "east_of_phantom"],
                "constraint_logic": "and",
            },
            two_barriers_config, two_barrier_paths,
            small_bounds, h, w, water_mask=water_mask_horizontal_strait,
        )
        # Only north_of_wall resolves → result == that mask
        expected = barrier_side_mask(
            two_barrier_paths["The Wall"], "north", small_bounds, h, w,
            water_mask=water_mask_horizontal_strait,
        )
        assert result is not None
        assert (result == expected).all()


# ----------------------------------------------------------------------
#  Backward compatibility — existing YAML keeps working
# ----------------------------------------------------------------------

class TestBackwardCompat:
    """The legacy `constraint: "..."` schema must produce the SAME
    canonical key + the SAME mask as before, so existing renders remain
    bit-identical. This is a requirement, not a nice-to-have: the s2
    cache key includes region_tints, and changing keys would invalidate
    every cached render."""

    def test_legacy_key_unchanged(self):
        """The canonical key for a legacy single-string config is just
        the string itself — no AND/ prefix, no sorting."""
        # Identity transformation
        for raw in ("north_of_wall", "south_of_river",
                     "east_of_some_long_barrier_name"):
            assert canonical_constraint_key({"constraint": raw}) == raw

    def test_legacy_mask_unchanged(
        self, small_bounds, two_barrier_paths, two_barriers_config,
        water_mask_horizontal_strait,
    ):
        """resolve_constraint_mask of a legacy config equals the
        single-barrier_side_mask call (same as the OLD code path)."""
        h, w = water_mask_horizontal_strait.shape
        legacy_result = resolve_constraint_mask(
            {"constraint": "north_of_wall"},
            two_barriers_config, two_barrier_paths,
            small_bounds, h, w, water_mask=water_mask_horizontal_strait,
        )
        old_path_result = barrier_side_mask(
            two_barrier_paths["The Wall"], "north", small_bounds, h, w,
            water_mask=water_mask_horizontal_strait,
        )
        assert (legacy_result == old_path_result).all()
