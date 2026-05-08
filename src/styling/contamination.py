"""Contamination spread — multi-source distance-transform overlay with
edge noise. Renders narrative things like "The Omni Plague spreading from
WMATA stations" as a dark stain over the map.

Schema (from cfg.contamination):
    name: "Omni Plague"
    enabled: true
    method: multi_source_dijkstra
    sources:
      type: osm_query
      query: 'node["railway"="station"]["network"="WMATA"]'
      epicenter: {lat: 38.90, lon: -77.02}
      weighting: distance_from_epicenter
      weight_scale: 100
    spread:
      max_distance: 1200          # in cells (≈ pixels at terrain resolution)
      blocked_by: ["water"]       # currently honors "water"; "The Wall" is
                                  # a planned extension (needs side-of-line
                                  # mask + custom BFS)
    edge_noise:
      seed: 666
      octaves: [{scale: 2, weight: 0.30}, ...]
    overlay:
      max_opacity: 0.70
      color: [0.05, 0.03, 0.07]   # dark inky purple
      noise_variation: 0.02

Algorithm:
  1. Fetch the source nodes from Overpass.
  2. Compute per-source weight = exp(-haversine_to_epicenter / weight_scale)
     so closer-to-epicenter stations spread further.
  3. Per-source EDT (scipy distance_transform_edt) gives distance from each
     source. Take the element-wise min of (dist - weight × weight_scale)
     across sources to get effective distance.
  4. Block by water: set effective distance to infinity in water cells.
  5. Apply multi-octave gaussian-filtered noise to perturb the boundary.
  6. Convert distance → intensity = max(0, 1 - dist / max_distance).
  7. Render as alpha-composited color overlay.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image
from scipy.ndimage import distance_transform_edt, gaussian_filter, maximum_filter

from src.data.overpass import fetch_osm_nodes

if TYPE_CHECKING:
    from src.pipeline import MapBounds


def compute_intensity(
    cfg: dict,
    bounds: "MapBounds",
    h: int,
    w: int,
    border: int,
    water_mask: np.ndarray | None = None,
    barrier_paths: dict[str, list[tuple[float, float]]] | None = None,
    cache_name_prefix: str = "contamination",
) -> tuple[np.ndarray, list[tuple[float, float]]] | None:
    """Compute the contamination intensity field as a (canvas_h, canvas_w)
    float32 array in [0, 1], plus the source positions for downstream use
    (e.g. drawing markers on top).

    Returns None if `cfg.enabled` is false or if no sources were found.

    The returned array spans the FULL CANVAS (terrain + paper border), with
    border cells naturally at zero intensity since they're far from any
    source. This lets the caller alpha-composite directly onto the
    bordered final canvas without offset arithmetic.
    """
    if not cfg or not cfg.get("enabled"):
        return None

    cw, ch = w + 2 * border, h + 2 * border

    # ---- 1. Fetch sources --------------------------------------------------
    sources_cfg = cfg.get("sources") or {}
    sources_type = sources_cfg.get("type", "osm_query")
    if sources_type != "osm_query":
        print(f"    [contamination] sources.type={sources_type!r} not supported")
        return None
    raw_query = sources_cfg.get("query", "")
    # Strip the leading "node" if the user included it — fetch_osm_nodes
    # adds it back. Tolerates both "node[k=v]" and "[k=v]" inputs.
    node_filter = raw_query.strip()
    if node_filter.startswith("node"):
        node_filter = node_filter[4:]
    if not node_filter:
        print("    [contamination] empty source query")
        return None

    name = cfg.get("name") or "contamination"
    safe_name = "".join(c if c.isalnum() else "_" for c in name).strip("_")[:40]
    osm_data = fetch_osm_nodes(bounds, node_filter, f"{cache_name_prefix}_{safe_name}")
    elements = (osm_data or {}).get("elements", [])
    sources_latlon = [
        (float(el["lat"]), float(el["lon"]))
        for el in elements
        if el.get("type") == "node" and "lat" in el and "lon" in el
    ]
    # Cull to sources actually visible on the canvas (with a small overscan
    # so contamination from a near-edge source still bleeds onto the map).
    overscan = 0.05  # degrees
    sources_latlon = [
        (lat, lon) for (lat, lon) in sources_latlon
        if (bounds.lat_s - overscan) <= lat <= (bounds.lat_n + overscan)
        and (bounds.lon_w - overscan) <= lon <= (bounds.lon_e + overscan)
    ]
    if not sources_latlon:
        print(f"    [contamination] no sources matched filter in bounds")
        return None
    print(f"    [contamination] {len(sources_latlon)} source nodes")

    # ---- 2. Per-source weights from epicenter distance --------------------
    epicenter = sources_cfg.get("epicenter") or {}
    epi_lat = float(epicenter.get("lat", (bounds.lat_n + bounds.lat_s) / 2))
    epi_lon = float(epicenter.get("lon", (bounds.lon_w + bounds.lon_e) / 2))
    weight_scale = float(sources_cfg.get("weight_scale", 100))
    weighting = sources_cfg.get("weighting", "distance_from_epicenter")

    if weighting == "distance_from_epicenter":
        # exp(-d_km / weight_scale): closest-to-epicenter source has weight≈1
        # and distant sources have weight→0. weight_scale is in km.
        weights = []
        for (slat, slon) in sources_latlon:
            d_km = _haversine_km(slat, slon, epi_lat, epi_lon)
            weights.append(math.exp(-d_km / weight_scale))
    else:
        weights = [1.0] * len(sources_latlon)

    # ---- 3. Project sources + spread through land via max-pool dilation ----
    # Critical correctness property: contamination should not "tunnel
    # through water". A source on one side of a bay must NOT contaminate
    # land on the other side at small Euclidean distance — the spread has
    # to flow across land cells to reach there.
    #
    # Algorithm: iterative max-pool with linear falloff, restricted to a
    # land mask. Each iteration, every cell becomes max(self,
    # max-of-3×3-neighbors − 1/max_distance). Water cells stay at 0
    # because we zero them every step — they can't carry intensity, so
    # they can't propagate it either.
    #
    # Sources are seeded with their distance-from-epicenter weight as
    # initial intensity. Higher-weight (close-to-epicenter) sources start
    # higher and reach further proportionally; same falloff rate.
    spread = cfg.get("spread") or {}
    max_distance = float(spread.get("max_distance", 1200))
    max_distance = max(max_distance, 1.0)

    source_px = [
        bounds.to_canvas(lat, lon, h, w, border)
        for (lat, lon) in sources_latlon
    ]

    # Build the LAND mask (where contamination is allowed to propagate).
    # Always restrict to the bordered terrain area so contamination can't
    # bleed into the paper margin. Water blocking subtracts further.
    blocked_by = spread.get("blocked_by") or []
    land = np.zeros((ch, cw), dtype=bool)
    land[border:border + h, border:border + w] = True
    if "water" in blocked_by and water_mask is not None:
        canvas_water = np.zeros((ch, cw), dtype=bool)
        canvas_water[border:border + h, border:border + w] = water_mask
        land &= ~canvas_water

    # Seed intensities at source pixels (only on LAND cells — sources that
    # somehow snapped to a water cell get dropped from contamination).
    intensity = np.zeros((ch, cw), dtype=np.float32)
    seeded = 0
    for (sx, sy), wt in zip(source_px, weights):
        if not (0 <= sx < cw and 0 <= sy < ch):
            continue
        if not land[sy, sx]:
            continue
        seed = float(wt)   # weight ∈ (0, 1]; higher = stronger initial signal
        if intensity[sy, sx] < seed:
            intensity[sy, sx] = seed
        seeded += 1

    if seeded == 0:
        # All sources on water (or out of bounds). No contamination to draw.
        return np.zeros((ch, cw), dtype=np.float32), sources_latlon

    falloff = 1.0 / max_distance
    # Iterate until either intensity stops growing or we hit max_distance
    # steps. The early-exit on convergence skips wasted work when the
    # spread fills the reachable land before the step cap.
    for step in range(int(max_distance) + 5):
        # 3×3 max with constant=0 boundary (so canvas edges don't wrap).
        neighbor_max = maximum_filter(intensity, size=3, mode="constant", cval=0.0)
        propagated = neighbor_max - falloff
        new_intensity = np.maximum(intensity, propagated)
        new_intensity[~land] = 0.0
        new_intensity = np.maximum(new_intensity, 0.0)
        # Convergence: stop when no cell gained meaningful intensity.
        if np.max(new_intensity - intensity) < 0.5 * falloff:
            intensity = new_intensity
            break
        intensity = new_intensity

    # Named barriers in `blocked_by` (anything other than "water") clip the
    # spread to the side of the barrier containing the source cluster. We
    # determine "source side" by majority vote over the source pixels:
    # whichever flood-component has more source nodes wins. Cells on the
    # OTHER side get intensity = 0 (the wall holds the contamination back).
    #
    # barrier_side_mask uses topological flood-fill bounded by water + path
    # stamp. With the wall extended shore-to-shore (via _trim_path_to_land's
    # tangent + BFS extensions), the canvas land splits cleanly into two
    # components — no banding artifacts at endpoint coordinates, and no
    # "leak" around the wall's tip because water acts as a hard boundary.
    from src.styling.barriers import barrier_side_mask
    # Land mask in terrain coords for the side-mask water input.
    water_mask_terrain = water_mask if water_mask is not None else None
    for blocker in blocked_by:
        if blocker == "water":
            continue
        path = (barrier_paths or {}).get(blocker)
        if not path:
            print(f"    [contamination] blocked_by {blocker!r}: barrier not found; ignoring")
            continue
        # Source-side vote uses STRICT mode so blocked cells (water/path)
        # don't tip the vote either way — only land cells reachable from
        # the north canvas edge count as "north."
        north_strict_only = barrier_side_mask(
            path, "north", bounds, h, w,
            water_mask=water_mask_terrain, strict=True,
        )
        n_north = sum(
            1 for (sx, sy) in source_px
            if 0 <= (sy - border) < h and 0 <= (sx - border) < w
            and north_strict_only[sy - border, sx - border]
        )
        n_total = len(source_px)
        if n_north >= (n_total - n_north):
            kept_side = "north"
        else:
            kept_side = "south"
        kept_mask_terrain = barrier_side_mask(
            path, kept_side, bounds, h, w,
            water_mask=water_mask_terrain, strict=False,
        )
        kept_mask_canvas = np.zeros((ch, cw), dtype=bool)
        kept_mask_canvas[border:border + h, border:border + w] = kept_mask_terrain
        intensity[~kept_mask_canvas] = 0.0
        print(f"    [contamination] blocked by {blocker!r}: kept {kept_side} land "
              f"component ({n_north}/{n_total} sources in north)")

    # ---- 5. Edge noise (multiplicative perturbation of intensity) ---------
    # Multi-octave gaussian-filtered white noise. Perturbs the intensity
    # field's *boundary* — small noise where intensity is uniform (interior
    # or pure exterior), bigger visible effect where intensity transitions
    # from high to zero.
    en = cfg.get("edge_noise") or {}
    seed = int(en.get("seed", 666))
    octaves = en.get("octaves") or []
    if octaves:
        rng = np.random.RandomState(seed)
        noise_field = np.zeros((ch, cw), dtype=np.float32)
        for oct in octaves:
            scale = float(oct.get("scale", 5))
            weight = float(oct.get("weight", 0.1))
            n = rng.normal(0, 1, (ch, cw)).astype(np.float32)
            blurred = gaussian_filter(n, sigma=max(0.5, scale))
            noise_field += blurred * weight
        std = noise_field.std() or 1.0
        noise_field /= std       # normalize to roughly [-1, 1]

        overlay_cfg = cfg.get("overlay") or {}
        noise_variation = float(overlay_cfg.get("noise_variation", 0.02))
        # Apply as a multiplicative kick, then clip. noise_variation=0.02
        # means ~2% wiggle around the original intensity.
        intensity = np.clip(intensity * (1.0 + noise_field * noise_variation * 5.0),
                            0.0, 1.0)

    return intensity.astype(np.float32), sources_latlon


def draw_contamination(
    canvas: Image.Image,
    intensity: np.ndarray,
    cfg: dict,
) -> bool:
    """Composite a contamination-color overlay onto `canvas`.

    `intensity` is a (canvas_h, canvas_w) float32 array in [0, 1] from
    compute_intensity. Each pixel's alpha is intensity × max_opacity.
    """
    if intensity is None or intensity.size == 0:
        return False
    overlay_cfg = cfg.get("overlay") or {}
    color01 = overlay_cfg.get("color", [0.05, 0.03, 0.07])
    max_opacity = float(overlay_cfg.get("max_opacity", 0.70))
    # Convert [0,1] color floats → 0-255 RGB
    cr, cg, cb = (int(round(max(0, min(1, c)) * 255)) for c in color01[:3])

    ch, cw = intensity.shape
    if (cw, ch) != canvas.size:
        print(f"    [contamination] size mismatch: intensity {intensity.shape} "
              f"vs canvas {canvas.size}; skipping")
        return False

    # Build an RGBA layer: solid color + alpha = intensity × max_opacity
    alpha = (np.clip(intensity, 0, 1) * max_opacity * 255).astype(np.uint8)
    rgba = np.zeros((ch, cw, 4), dtype=np.uint8)
    rgba[..., 0] = cr
    rgba[..., 1] = cg
    rgba[..., 2] = cb
    rgba[..., 3] = alpha
    layer = Image.fromarray(rgba, mode="RGBA")

    if canvas.mode != "RGBA":
        canvas = canvas.convert("RGBA")
    canvas.alpha_composite(layer)
    return True


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers."""
    R_KM = 6_371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_KM * math.asin(math.sqrt(a))
