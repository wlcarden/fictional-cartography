"""Pipeline orchestrator and shared utilities for fictional cartography.

Loads a YAML map config and produces a rendered PNG/JPG by orchestrating the
data acquisition, terrain coloring, styling, label placement, and furniture
modules.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import gaussian_filter


# Maps a human-readable label_side from the YAML config to the angle
# the LabelPlacer uses internally. The placer computes:
#     cx = anchor_x + dist * cos(angle)
#     cy = anchor_y - dist * sin(angle)
# so with PIL's positive-y-down convention, angle 90 puts the label *above*
# the marker and angle 270 puts it below. (Earlier versions of this map had
# above/below swapped — Port Barren in jersea-wastes.yaml is the only label
# that hit that bug in practice.)
SIDE_TO_ANGLE: dict[str, int] = {
    # cardinals
    "right":  0,   "e":  0,
    "above": 90,   "n": 90,
    "left":  180,  "w": 180,
    "below": 270,  "s": 270,
    # diagonals (geographic phrasing + compass abbreviations + screen-space)
    "above-right":  45,   "ne":  45, "top-right":     45,
    "above-left":  135,   "nw": 135, "top-left":     135,
    "below-left":  225,   "sw": 225, "bottom-left":  225,
    "below-right": 315,   "se": 315, "bottom-right": 315,
}

DEFAULT_FONT_DIR = "/usr/share/fonts/truetype/dejavu"
CANVAS_GRAIN_SEED = 99


@dataclass(frozen=True)
class MapBounds:
    """Geographic rectangle in degrees, lat positive north, lon positive east."""

    lat_n: float
    lat_s: float
    lon_w: float
    lon_e: float

    @property
    def lat_span(self) -> float:
        return self.lat_n - self.lat_s

    @property
    def lon_span(self) -> float:
        return self.lon_e - self.lon_w

    def to_pixel(self, lat: float, lon: float, h: int, w: int) -> tuple[int, int]:
        x = int((lon - self.lon_w) / self.lon_span * w)
        y = int((self.lat_n - lat) / self.lat_span * h)
        return x, y

    def to_canvas(
        self, lat: float, lon: float, h: int, w: int, border: int
    ) -> tuple[int, int]:
        x, y = self.to_pixel(lat, lon, h, w)
        return x + border, y + border


def parse_aspect_ratio(s: str | None) -> tuple[int, int] | None:
    if not s:
        return None
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid aspect_ratio {s!r}; expected 'W:H' like '3:4'")
    return int(parts[0]), int(parts[1])


def load_config(path: str | Path) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)


def resolve_color(
    value: Any,
    color_map: dict[str, Any],
    default: tuple[int, int, int] | None = None,
) -> tuple[int, int, int] | None:
    if value is None:
        return default
    if isinstance(value, str):
        if value not in color_map:
            raise KeyError(
                f"Color name {value!r} not found in config colors map "
                f"(known: {sorted(color_map.keys())})"
            )
        rgb = color_map[value]
    else:
        rgb = value
    if len(rgb) != 3:
        raise ValueError(f"Expected 3-component color, got {rgb!r}")
    return (int(rgb[0]), int(rgb[1]), int(rgb[2]))


def project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parent.parent


def cache_dir(subdir: str | None = None) -> Path:
    root = project_root() / "cache"
    if subdir:
        root = root / subdir
    root.mkdir(parents=True, exist_ok=True)
    return root


def font_path(name: str, font_dir: str = DEFAULT_FONT_DIR) -> str:
    return f"{font_dir}/{name}"


def load_fonts(
    scale: float = 1.0,
    font_dir: str = DEFAULT_FONT_DIR,
    overrides: dict | None = None,
) -> dict[str, ImageFont.FreeTypeFont]:
    """Load every font used by the renderer, scaled by `scale` (>=1.0 enlarges).

    `overrides` is a per-role dict from the YAML's `fonts:` block; each value
    may set `file` (filename, looked up in `font_dir`), `size` (integer base
    size before `scale`), or both. Missing entries fall through to the
    built-in defaults below — so configs only need to mention roles they
    actually want to change.

    Example YAML:
        fonts:
          title:  {file: "Garamond-Bold.ttf", size: 80}
          region: {file: "MyFont-Italic.ttf"}
          scale:  {size: 16}
    """
    overrides = overrides or {}

    def f(role: str, default_name: str, default_size: int) -> ImageFont.FreeTypeFont:
        ov = overrides.get(role) or {}
        name = ov.get("file", default_name)
        size = ov.get("size", default_size)
        return ImageFont.truetype(font_path(name, font_dir),
                                  max(8, int(round(float(size) * scale))))

    fonts = {
        "title":         f("title",         "DejaVuSerif-Bold.ttf",   72),
        "sub":           f("sub",           "DejaVuSerif-Italic.ttf", 36),
        "major":         f("major",         "DejaVuSerif-Bold.ttf",   38),
        "settle":        f("settle",        "DejaVuSans-Bold.ttf",    28),
        "minor":         f("minor",         "DejaVuSans-Bold.ttf",    20),
        "region":        f("region",        "DejaVuSerif-Italic.ttf", 32),
        "note":          f("note",          "DejaVuSans.ttf",         20),
        "water":         f("water",         "DejaVuSerif-Italic.ttf", 26),
        "credit":        f("credit",        "DejaVuSerif-Italic.ttf", 14),
        "legend_title":  f("legend_title",  "DejaVuSerif-Bold.ttf",   18),
        "legend":        f("legend",        "DejaVuSans.ttf",         16),
        "legend_italic": f("legend_italic", "DejaVuSerif-Italic.ttf", 14),
        "compass":       f("compass",       "DejaVuSerif-Bold.ttf",   22),
        "scale":         f("scale",         "DejaVuSans.ttf",         14),
        "fleur":         f("fleur",         "DejaVuSans.ttf",         32),
    }
    # Honor extra roles defined in cfg.fonts that aren't in the default
    # role list (e.g. `bold_13`, `italic_16`, `regular_12` that barrier
    # labels reference). Without this loop, those lookups silently
    # resolve to None and the barrier label is dropped (was
    # _place_barrier_label's no-op early-return). Each custom role MUST
    # specify both `file` and `size` since there's no default to fall
    # back on.
    for role, ov in overrides.items():
        if role in fonts or not isinstance(ov, dict):
            continue
        name = ov.get("file")
        size = ov.get("size")
        if not name or size is None:
            print(f"  [fonts] custom role {role!r}: needs both `file` and "
                  f"`size`; skipping")
            continue
        try:
            fonts[role] = ImageFont.truetype(
                font_path(name, font_dir),
                max(8, int(round(float(size) * scale))),
            )
        except Exception as e:
            print(f"  [fonts] custom role {role!r}: load failed ({e})")
    return fonts


def _legend_placement(
    legend_cfg: dict, n_entries: int, cartouche_on: bool,
    cw: int, ch: int, border: int,
) -> tuple[int, int, tuple[int, int, int, int]]:
    """Compute the legend plaque's top-left anchor and its reservation box.

    Shared by the placer reservation (so settlement labels avoid the legend
    wherever it actually sits) and the draw call (so the two agree). This is
    the fix for a class of bug where the reservation was hardcoded to the
    legend's *default* corner and didn't follow `legend.position` — leaving
    a phantom reserved zone at the old spot and the real legend unprotected.

    Mirrors draw_legend's layout math: 285px width, 38px title row +
    n*28px entries + 14px pad, with a 14px pad bleed on every side. Top
    placements clear the title cartouche band (y≈142) when one is present.
    Returns (lx, ly, reserve_box).
    """
    legend_w = 285
    legend_h = 38 + n_entries * 28 + 14
    margin = 15
    cartouche_bottom = 142
    top_y = (cartouche_bottom + margin) if cartouche_on else (border + margin + 14)
    pos = str(legend_cfg.get("position", "bottom-left"))
    if pos == "top-left":
        lx, ly = border + margin, top_y
        box = (lx - margin, ly - margin, lx + legend_w, ly + legend_h)
    elif pos == "top-right":
        lx, ly = cw - border - legend_w - margin, top_y
        box = (lx - margin, ly - margin, lx + legend_w, ly + legend_h)
    elif pos == "bottom-right":
        lx, ly = cw - border - legend_w - margin, ch - border - legend_h - margin
        box = (lx - margin, ly - margin, lx + legend_w, ly + legend_h)
    else:  # bottom-left (default)
        # Anchor AND reservation box are the exact historical values, so
        # existing maps (default legend) render pixel-identically.
        lx, ly = border + 15, ch - border - 200
        box = (border + 5, ch - border - 260, border + 320, ch - border - 5)
    return lx, ly, box


def _build_road_highlight_styles(base_styles: dict) -> dict:
    """Derive a thinner, brighter "highlight" style dict for re-stamping
    major roads over contamination. Only `interstate` and `us_highway`
    are highlighted; minor roads stay buried under the stain so the
    visual hierarchy still feels apocalyptic.

    Width drops to 1, shadow_width to 0 (no shadow pass — the existing
    road outline underneath provides depth), color shifts toward warm
    bone for legibility against the dark contamination.
    """
    out: dict = {}
    for name in ("interstate", "us_highway"):
        if name not in base_styles:
            continue
        cfg = dict(base_styles[name])
        # Bump color toward bone/parchment for visibility on dark bg.
        cfg["color"] = [220, 200, 155]
        cfg["width"] = 1
        cfg["shadow_width"] = 0
        out[name] = cfg
    return out


def apply_canvas_grain(
    canvas: Image.Image,
    fine_sigma: float = 8.0,
    smooth_strength: float = 15.0,
    smooth_blur_sigma: float = 20.0,
    seed: int = CANVAS_GRAIN_SEED,
) -> Image.Image:
    """Add paper-style noise across the canvas (border included)."""
    pa = np.array(canvas).astype(np.float32)
    h, w = pa.shape[:2]
    rng = np.random.RandomState(seed)
    pa[:, :, :3] += rng.normal(0, fine_sigma, (h, w, 3)).astype(np.float32)
    smooth = gaussian_filter(
        rng.normal(0, smooth_strength, (h, w, 3)).astype(np.float32),
        sigma=(smooth_blur_sigma, smooth_blur_sigma, 0),
    )
    pa[:, :, :3] += smooth
    return Image.fromarray(np.clip(pa, 0, 255).astype(np.uint8), "RGBA")


def render_map(
    config_path: str | Path,
    output_path: str | Path | None = None,
    scale_factor: int = 1,
    output_format: str = "png",
    jpeg_quality: int = 88,
    reference_mode: bool = False,
    dpi: int | None = None,
    embed_metadata: bool = False,
) -> Path:
    """Render a map from a YAML config to disk. Returns the output path.

    Internally split into 4 cached stages:

      1. terrain_dem        — SRTM mosaic, crop, downsample. Slowest.
      2. terrain_styled     — water mask, hillshade, base colors, drift, scorch,
                              urbanization, regional tints, wash, paper, vignette.
      3. canvas_with_roads  — paper-bordered canvas with roads drawn.
      4. final              — settlements, regions, edge indicators, water labels,
                              and all cartographic furniture.

    Each stage's cache key cascades from its predecessor's; editing a setting
    that only affects stage 4 (e.g. moving a settlement) skips stages 1-3.
    """
    from src.cache import StageCache, stage_key
    from src.terrain.srtm import load_terrain
    from src.terrain.modifications import water_mask as compute_water_mask
    from src.terrain.hillshade import hillshade, base_colors_land, base_colors_water
    from src.data.overpass import fetch_routes, fetch_landuse, parse_routes, parse_polygons
    from src.data.urban_density import rasterize_polygons
    from src.styling.tints import apply_all_region_tints
    from src.styling.texture import (
        apply_drift_noise, apply_scorch, apply_urbanization,
        apply_wasteland_wash, composite_land_water, darken_coastline,
        apply_paper_grain, apply_vignette,
    )
    from src.styling.roads import draw_roads
    from src.labels.placer import LabelPlacer
    from src.labels.markers import (
        outline_text, place_settlement, place_region, place_water,
    )
    from src.furniture.compass import draw_compass_rose
    from src.furniture.scale_bar import draw_scale_bar
    from src.furniture.legend import draw_legend
    from src.furniture.cartouche import draw_title_cartouche, draw_credit
    from src.furniture.ornaments import draw_corner_ornaments

    cfg_path = Path(config_path)
    cfg = load_config(cfg_path)
    if reference_mode:
        cfg.setdefault("state_boundaries", {})["enabled"] = True
        cfg.setdefault("reference_cities", {})["enabled"] = True

    # Barrier paths are needed by stage 2 (tint constraints) AND stage 4
    # (contamination wall-blocking + barrier rendering). Compute lazily so
    # stage hits don't pay the cost — first caller triggers fetch+A*.
    _barriers_state = {"computed": False, "paths": {}}
    def _lazy_barrier_paths():
        if not _barriers_state["computed"]:
            cfg_b = cfg.get("barriers") or []
            if cfg_b:
                from src.styling.barriers import compute_barrier_paths
                # Pass the post-sinking water mask so A* paths can be
                # trimmed to land-only — drops segments routed through
                # roads that exist in OSM but have been flooded — and so
                # tangent/BFS extensions can carry endpoints to a water
                # cell, guaranteeing the wall goes shore-to-shore.
                _barriers_state["paths"] = compute_barrier_paths(
                    cfg_b, bounds, cache_name_prefix=f"barriers_{cfg_path.stem}",
                    water_mask=water, terrain_h=h, terrain_w=w,
                )
            _barriers_state["computed"] = True
        return _barriers_state["paths"]

    name = cfg_path.stem
    cache = StageCache(name)
    color_map: dict[str, Any] = cfg.get("colors", {})
    bounds_cfg = cfg["bounds"]
    requested = MapBounds(
        lat_n=float(bounds_cfg["lat_n"]),
        lat_s=float(bounds_cfg["lat_s"]),
        lon_w=float(bounds_cfg["lon_w"]),
        lon_e=float(bounds_cfg["lon_e"]),
    )
    aspect = parse_aspect_ratio(bounds_cfg.get("aspect_ratio"))

    terrain_cfg = cfg.get("terrain", {})
    sea_level = float(terrain_cfg.get("sea_level_rise", 0))
    config_downsample = int(terrain_cfg.get("downsample", 4))
    effective_downsample = max(1, round(config_downsample / max(scale_factor, 1)))
    print(f"Effective downsample: {effective_downsample}x")

    # ============================================================
    # STAGE 1 — terrain_dem (SRTM mosaic + crop + downsample)
    # ============================================================
    s1_key = stage_key(
        "v1",
        bounds=[requested.lat_n, requested.lat_s, requested.lon_w, requested.lon_e],
        aspect=aspect,
        downsample=effective_downsample,
    )
    if cache.hit("terrain_dem", s1_key):
        meta = cache.get_meta("terrain_dem")
        b = meta["bounds"]
        bounds = MapBounds(
            lat_n=b["lat_n"], lat_s=b["lat_s"],
            lon_w=b["lon_w"], lon_e=b["lon_e"],
        )
        dem = cache.load_npy("terrain_dem")
        h, w = dem.shape
        print(f"  [hit]  terrain_dem      ({h}×{w})")
    else:
        print("  [miss] terrain_dem      — fetching SRTM tiles...")
        dem, bounds = load_terrain(
            requested, aspect=aspect, downsample_factor=effective_downsample,
        )
        h, w = dem.shape
        cache.save_npy(
            "terrain_dem", s1_key, dem,
            bounds={"lat_n": bounds.lat_n, "lat_s": bounds.lat_s,
                    "lon_w": bounds.lon_w, "lon_e": bounds.lon_e},
            h=h, w=w,
        )

    # ============================================================
    # STAGE 2 — terrain_styled (water mask, base colors, drift, scorch,
    # urbanization, regional tints, wash, composite, paper, vignette)
    # ============================================================
    s2_key = stage_key(
        # v5: water palette bumped to bluer slate-tones; coastline darken
        # tightened (dilation 2->1, factor 0.75->0.78). v4 was paper
        # grain resolution-scaling; v3 tint-after-darken; v2 flood-fill
        # side-mask; v1 original.
        "v5",
        s1_key,
        terrain=terrain_cfg,
        urbanization=cfg.get("urbanization", {}),
        sinking=cfg.get("sinking", {}),
        # Only the tint sub-blocks of regions feed terrain styling; label-side
        # changes flow into stage 4 instead.
        region_tints=[
            (r.get("name", ""), r.get("tint")) for r in cfg.get("regions", [])
        ],
        # Barriers feed tint constraints (e.g. "north_of_wall" clips a tint
        # to one side of The Wall), so changing barrier geometry must
        # invalidate stage 2 even though barriers themselves render in s4.
        barriers=cfg.get("barriers", []),
        sea_level=sea_level,
    )
    if cache.hit("terrain_styled", s2_key):
        terrain_img, water = cache.load_image_and_mask("terrain_styled")
        print("  [hit]  terrain_styled")
    else:
        print("  [miss] terrain_styled   — applying tints, texture, vignette...")
        water = compute_water_mask(dem, sea_level)

        # Sinking: a named region (e.g. Virginia) gets unioned into the water
        # mask BEFORE styling, so palette/hillshade/coastline-darken treat it
        # as ocean automatically. Honors cfg.sinking.{enabled,region,source}.
        cfg_sink = cfg.get("sinking") or {}
        if cfg_sink.get("enabled"):
            from src.terrain.modifications import apply_sinking
            water, n_sunk = apply_sinking(water, cfg_sink, bounds, project_root())
            if n_sunk:
                print(f"    [sinking] sank {n_sunk:,} cells "
                      f"({n_sunk/water.size*100:.1f}% of map) "
                      f"into region {cfg_sink.get('region')!r}")

        land_mask = ~water
        print(f"    water: {water.sum()/water.size*100:.1f}% of cells")

        palette = terrain_cfg.get("palette", {})
        shade_cfg = terrain_cfg.get("shade", {})
        water_palette = terrain_cfg.get("water", {})
        shade = hillshade(
            dem, sea_level,
            sun_altitude=float(shade_cfg.get("sun_altitude", 35)),
            sun_azimuth=float(shade_cfg.get("sun_azimuth", 315)),
        )
        land_rgb = base_colors_land(
            dem, sea_level,
            palette_low=tuple(palette.get("low",  (0.80, 0.70, 0.48))),
            palette_mid=tuple(palette.get("mid",  (0.72, 0.65, 0.44))),
            palette_high=tuple(palette.get("high", (0.56, 0.54, 0.48))),
            mid_break=float(palette.get("mid_break", 0.3)),
            shade=shade,
            shade_floor=float(shade_cfg.get("floor", 0.3)),
        )
        water_rgb = base_colors_water(
            dem, sea_level,
            shallow=tuple(water_palette.get("shallow", (0.58, 0.60, 0.58))),
            deep=tuple(water_palette.get("deep", (0.36, 0.40, 0.44))),
            depth_range=float(water_palette.get("depth_range", 50)),
        )

        noise_cfg = terrain_cfg.get("noise", {})
        # sigma / strength can be scalar (uniform across channels — original
        # behavior) OR a 3-list / 3-dict for per-channel control. The
        # texture.py functions handle both shapes; we just pass through
        # without coercing to float here.
        apply_drift_noise(
            land_rgb, land_mask,
            sigma=noise_cfg.get("drift_sigma", 60),
            strength=noise_cfg.get("drift_strength", 0.06),
        )
        apply_scorch(
            land_rgb, land_mask,
            sigma=noise_cfg.get("scorch_sigma", 25),
            strength=noise_cfg.get("scorch_strength", 0.08),
        )

        urb_cfg = cfg.get("urbanization", {})
        if urb_cfg.get("enabled", True):
            print("    fetching landuse for urban density...")
            landuse_data = fetch_landuse(bounds, cache_name=f"landuse_{name}")
            polys = parse_polygons(landuse_data)
            print(f"    {len(polys)} landuse polygons")
            density = rasterize_polygons(polys, bounds, h, w)
            urb_color = tuple(urb_cfg.get("color", (0.48, 0.46, 0.44)))
            apply_urbanization(
                land_rgb, density, land_mask,
                color=urb_color,
                blend_strength=float(urb_cfg.get("blend_strength", 0.60)),
            )

        # Build constraint masks for any region tint that uses them.
        # Supports both legacy single-string (`constraint: "north_of_wall"`)
        # and compound list (`constraints: [...] + constraint_logic: and|or`).
        # The pipeline computes one mask per UNIQUE constraint config
        # (deduplicated via canonical_constraint_key) and stores it in
        # constraint_masks under that canonical key. apply_all_region_tints
        # later looks up using the same key.
        constraint_masks: dict[str, "np.ndarray"] = {}
        # Map canonical key → raw tint cfg used to compute it (the FIRST
        # region whose constraint produces this canonical key wins; subsequent
        # regions with the same canonical config reuse the cached mask).
        unique_tint_cfgs: dict[str, dict] = {}
        for r in cfg.get("regions", []):
            t = r.get("tint") or {}
            key = None
            try:
                from src.styling.barriers import canonical_constraint_key
                key = canonical_constraint_key(t)
            except Exception:
                pass
            if key and key not in unique_tint_cfgs:
                unique_tint_cfgs[key] = t

        has_band_tint = any(
            (r.get("tint") or {}).get("type") == "band"
            for r in cfg.get("regions", [])
        )
        barrier_paths_for_tints: dict = {}
        if unique_tint_cfgs or has_band_tint:
            from src.styling.barriers import resolve_constraint_mask
            cfg_b = cfg.get("barriers") or []
            barrier_paths_for_tints = _lazy_barrier_paths()
            for key, tint_cfg in unique_tint_cfgs.items():
                mask = resolve_constraint_mask(
                    tint_cfg, cfg_b, barrier_paths_for_tints,
                    bounds, h, w, water_mask=water,
                )
                if mask is None:
                    print(f"    [tint] constraint {key!r}: unresolvable; "
                          f"region tints using it will not be clipped")
                    continue
                constraint_masks[key] = mask
                print(f"    [tint] constraint {key!r} resolved")
                print(f"    [tint] constraint {cstr!r} → {side} of {b_name!r}")

        # Wasteland wash is a mild GLOBAL desaturation — fine to apply
        # before the composite. It's not coast-specific, so it doesn't
        # care about the darken_coastline ordering.
        wash_cfg = terrain_cfg.get("wasteland_wash", {})
        apply_wasteland_wash(
            land_rgb, land_mask,
            desaturate=float(wash_cfg.get("desaturate", 0.12)),
            warm_push_r=float(wash_cfg.get("warm_push_r", 0.015)),
            cool_push_b=float(wash_cfg.get("cool_push_b", -0.010)),
        )

        rgb = composite_land_water(land_rgb, water_rgb, land_mask)
        coast_cfg = terrain_cfg.get("coastline_darken", {})
        darken_coastline(
            rgb, water,
            dilation=int(coast_cfg.get("dilation", 3)),
            factor=float(coast_cfg.get("factor", 0.70)),
        )

        # Region tints applied AFTER coastline_darken (was: before, which
        # caused coastal tints like the Cheapskate Beaches yellow to be
        # subsequently dimmed by the 0.70 darken factor along the shore).
        # apply_all_region_tints multiplies by land_mask internally, so
        # operating on the post-composite `rgb` is safe — only land cells
        # get tinted.
        apply_all_region_tints(
            rgb, cfg.get("regions", []), bounds, land_mask,
            constraint_masks=constraint_masks,
            barrier_paths=barrier_paths_for_tints,
        )

        rgb_255 = (rgb * 255.0).astype(np.float32)
        apply_paper_grain(rgb_255)
        vig = terrain_cfg.get("vignette", {})
        apply_vignette(
            rgb_255,
            strength=float(vig.get("strength", 0.40)),
            floor=float(vig.get("floor", 0.50)),
        )
        terrain_img = Image.fromarray(np.clip(rgb_255, 0, 255).astype(np.uint8), "RGB")
        cache.save_image_and_mask("terrain_styled", s2_key, terrain_img, water)

    # ============================================================
    # STAGE 3 — canvas_with_roads (paper border + roads)
    # ============================================================
    canvas_cfg = cfg.get("canvas", {})
    border = int(canvas_cfg.get("border", 100))
    paper_color = tuple(canvas_cfg.get("paper_color", (210, 190, 150)))
    cw, ch = w + border * 2, h + border * 2
    # roads_cfg lifted out of the s3-miss branch so stage 4's road
    # highlight pass over contamination can also reference it on s3 hit.
    roads_cfg = cfg.get("roads")

    s3_key = stage_key(
        "v1", s2_key,
        canvas=canvas_cfg,
        roads=cfg.get("roads"),
    )
    if cache.hit("canvas_roads", s3_key):
        canvas = cache.load_image("canvas_roads").convert("RGBA")
        print("  [hit]  canvas_roads")
    else:
        print("  [miss] canvas_roads     — drawing border + roads...")
        canvas = Image.new("RGBA", (cw, ch), paper_color + (255,))
        canvas = apply_canvas_grain(canvas)
        canvas.paste(terrain_img.convert("RGBA"), (border, border))
        draw = ImageDraw.Draw(canvas)

        for line in canvas_cfg.get(
            "border_lines",
            [
                {"offset": 8, "color": [50, 40, 30], "width": 3},
                {"offset": 15, "color": [70, 55, 40], "width": 1},
                {"offset": 20, "color": [90, 72, 50], "width": 2},
            ],
        ):
            off = int(line["offset"])
            col = tuple(line["color"])
            wid = int(line.get("width", 1))
            draw.rectangle([off, off, cw - 1 - off, ch - 1 - off], outline=col, width=wid)
        draw.rectangle(
            [border - 4, border - 4, border + w + 3, border + h + 3],
            outline=(60, 48, 35), width=2,
        )

        if roads_cfg:
            print("    fetching + drawing roads...")
            networks = roads_cfg.get("networks", ["US:I", "US:US"])
            routes_data = fetch_routes(
                bounds, networks, cache_name=f"routes_{name}"
            )
            routes = parse_routes(routes_data)
            styles = roads_cfg.get("styles", {})
            wm_for_roads = water if roads_cfg.get("mask_by_water", True) else None
            n_drawn = draw_roads(
                draw, routes, styles, bounds, h, w, border,
                water_mask=wm_for_roads,
                shadow_color=tuple(roads_cfg.get("shadow_color", (45, 38, 28))),
            )
            print(f"    drew {n_drawn} routes")

        cache.save_image("canvas_roads", s3_key, canvas, h=h, w=w, border=border)

    # ============================================================
    # STAGE 4 — final (settlements, regions, edge labels, infra, water,
    # cartographic furniture)
    # ============================================================
    # Note: scale_factor is intentionally NOT hashed here. Stage 1 hashes
    # `downsample = config_downsample / scale_factor`, so scale changes
    # propagate s1 → s2 → s3 → s4 automatically. Including scale_factor
    # explicitly was redundant (and made the key noisier in logs).
    s4_key = stage_key(
        # v4: compass rose redesigned (filled kite arms + ticks + ornate
        # hub); legend redesigned (inset frame + divider + diamonds);
        # road highlight pass over contamination; settlement label
        # cleanup. v3 was cartouche/buffer/submerged improvements;
        # v2 was flood-fill contamination; v1 was original.
        "v4", s3_key,
        settlements=cfg.get("settlements", []),
        regions_label=[
            (r.get("name"), r.get("type"), r.get("center"),
             r.get("color"), r.get("subtitle"), r.get("rotation"))
            for r in cfg.get("regions", [])
        ],
        edge_indicators=cfg.get("edge_indicators", []),
        infrastructure=cfg.get("infrastructure", []),
        water_labels=cfg.get("water_labels", []),
        state_boundaries=cfg.get("state_boundaries", {}),
        reference_cities=cfg.get("reference_cities", {}),
        barriers=cfg.get("barriers", []),
        buffer_zone=cfg.get("buffer_zone", {}),
        contamination=cfg.get("contamination", {}),
        legend=cfg.get("legend", {}),
        title=cfg.get("name", ""),
        subtitle=cfg.get("subtitle", ""),
        credit=cfg.get("credit", ""),
        fonts=cfg.get("fonts", {}),
    )
    if cache.hit("final", s4_key):
        final = cache.load_image("final")
        print("  [hit]  final")
    else:
        print("  [miss] final            — placing labels + furniture...")
        # Operate on a copy so we don't mutate stage 3's cached canvas.
        final = canvas.copy()
        draw = ImageDraw.Draw(final)

        # Font sizes tuned for ~2700px canvases; scale up when the canvas grows
        # so text stays the same physical size. Per-role overrides come from
        # the YAML's `fonts:` block (e.g. {title: {file: "Garamond-Bold.ttf"}}).
        font_scale = max(1.0, min(h, w) / 1800.0)
        fonts = load_fonts(scale=font_scale, overrides=cfg.get("fonts"))

        margin = 15
        placer = LabelPlacer(
            draw,
            margin_left=border + margin,
            margin_top=border + margin,
            margin_right=cw - border - margin,
            margin_bottom=ch - border - margin,
        )
        title_text = cfg.get("name", "")
        tbb = draw.textbbox((0, 0), title_text, font=fonts["title"])
        title_w = tbb[2] - tbb[0]
        cart_w = title_w + 60
        cart_x = (cw - cart_w) // 2
        placer.reserve((cart_x, 2, cart_x + cart_w, 150))
        # Reserve the legend box at its CONFIGURED position (not a hardcoded
        # corner) so the placer keeps labels off the legend wherever it sits
        # — and, crucially, doesn't reserve a phantom zone where the legend
        # no longer is. Shared with the draw call below via _legend_placement.
        _legend_cfg = cfg.get("legend", {})
        _legend_entries = _legend_cfg.get("entries", [])
        if _legend_entries:
            _cart_on = bool(
                cfg.get("decoration", {}).get("cartouche", {}).get("enabled", True)
                and title_text
            )
            _, _, _legend_box = _legend_placement(
                _legend_cfg, len(_legend_entries), _cart_on, cw, ch, border
            )
            placer.reserve(_legend_box)
        # Compass reservation: enlarged to match the resolution-aware
        # compass_r computed below (max(55, min(cw,ch)/22)). Reserve a
        # square ~3x compass_r so labels can't crowd against it.
        _compass_r_est = max(55, int(min(cw, ch) / 22))
        _comp_box = _compass_r_est * 3
        placer.reserve((
            cw - border - _comp_box, ch - border - _comp_box - 60,
            cw - border - 5, ch - border - 30,
        ))
        placer.reserve((cw // 2 - 200, ch - border - 50, cw // 2 + 200, ch - border - 5))
        placer.reserve((cw // 2 - 200, ch - 40, cw // 2 + 200, ch - 5))

        parchment_color = resolve_color("parch", color_map, default=(200, 190, 170))
        shadow_color = resolve_color("shadow", color_map, default=(30, 25, 18))

        # Boundaries + reference cities first — they sit UNDER labels so a
        # settlement marker on top of a state border still reads clearly.
        from src.styling.boundaries import (
            draw_state_boundaries, draw_reference_cities,
        )
        # Auto-fetch any referenced boundary that isn't cached yet, so the
        # map reproduces on a fresh clone (same "fetch on first render" model
        # as SRTM/OSM). `county_of` disambiguates county names across states.
        sb_cfg = cfg.get("state_boundaries", {})
        if sb_cfg.get("enabled") and sb_cfg.get("states"):
            from src.data.boundaries import ensure_boundaries
            n_fetched = ensure_boundaries(
                sb_cfg.get("states", []), county_of=sb_cfg.get("county_of")
            )
            if n_fetched:
                print(f"    fetched {n_fetched} missing boundary polygon(s)")
        n_boundaries = draw_state_boundaries(
            final, sb_cfg,
            bounds, h, w, border,
        )
        if n_boundaries:
            print(f"    drew {n_boundaries} state boundary outline(s)")
        # ImageDraw target may have lost its binding after alpha_composite —
        # rebind so subsequent draw.* calls land on the updated `final`.
        draw = ImageDraw.Draw(final)

        n_cities = draw_reference_cities(
            final, draw, cfg.get("reference_cities", {}),
            bounds, h, w, border,
            font=fonts["note"],
            halo=shadow_color,
        )
        if n_cities:
            print(f"    drew {n_cities} reference cit{'y' if n_cities == 1 else 'ies'}")

        # Barriers (A* through OSM road graph): The Wall, Patrol Line, etc.
        # Drawn before labels so a settlement marker can sit on top cleanly.
        # Path computation is expensive on cache miss — heavy graph fetch +
        # search — so the OSM JSON is cached on disk per (road_types) tuple.
        cfg_barriers = cfg.get("barriers") or []
        if cfg_barriers:
            from src.styling.barriers import draw_barriers, draw_buffer_zone
            # Reuse paths from the lazy memo — if stage 2 already computed
            # them for tint constraints this is free; otherwise this is
            # where they're computed.
            barrier_paths = _lazy_barrier_paths()
            # Buffer zone hatching first (between two barriers) so the
            # barrier strokes draw on top of the hatching, not under it.
            cfg_buffer = cfg.get("buffer_zone") or {}
            if cfg_buffer.get("enabled"):
                if draw_buffer_zone(
                    final, barrier_paths, cfg_buffer, bounds, h, w, border,
                    fonts=fonts,
                ):
                    print(f"    drew buffer zone between {cfg_buffer.get('between')}")
                draw = ImageDraw.Draw(final)
            n_barriers = draw_barriers(
                final, barrier_paths, cfg_barriers, bounds, h, w, border,
                fonts=fonts,
            )
            if n_barriers:
                print(f"    drew {n_barriers} barrier(s)")
            draw = ImageDraw.Draw(final)

        # Contamination spread (multi-source distance-transform overlay).
        # Goes AFTER barriers so The Wall (or any barrier listed in
        # spread.blocked_by) clips the spread to the source-side half-plane,
        # but BEFORE settlement labels so markers stay on top.
        cfg_contam = cfg.get("contamination") or {}
        if cfg_contam.get("enabled"):
            from src.styling.contamination import (
                compute_intensity, draw_contamination,
            )
            result = compute_intensity(
                cfg_contam, bounds, h, w, border,
                water_mask=water,
                barrier_paths=_lazy_barrier_paths(),
                cache_name_prefix=f"contamination_{name}",
            )
            if result is not None:
                intensity, _ = result
                if draw_contamination(final, intensity, cfg_contam):
                    print(f"    drew contamination overlay")
            draw = ImageDraw.Draw(final)

            # Re-stamp major roads on top of contamination so the highway
            # network remains visible through the plague stain. Only the
            # most-important routes get re-stamped — settling for less
            # density than stage 3 keeps the contamination visually
            # dominant while still preserving navigability.
            if roads_cfg:
                wm_for_roads = water if roads_cfg.get("mask_by_water", True) else None
                highlight_styles = _build_road_highlight_styles(
                    roads_cfg.get("styles", {})
                )
                if highlight_styles:
                    routes_data2 = fetch_routes(
                        bounds, roads_cfg.get("networks", ["US:I", "US:US"]),
                        cache_name=f"routes_{name}",
                    )
                    routes2 = parse_routes(routes_data2)
                    n_h = draw_roads(
                        draw, routes2, highlight_styles, bounds, h, w, border,
                        water_mask=wm_for_roads,
                        shadow_color=tuple(roads_cfg.get(
                            "shadow_color", (45, 38, 28))),
                    )
                    if n_h:
                        print(f"    re-stamped {n_h} road highlights "
                              f"over contamination")
                    draw = ImageDraw.Draw(final)

        settlements = sorted(
            cfg.get("settlements", []),
            key=lambda s: -int(s.get("radius", 0)),
        )
        for s in settlements:
            lat, lon = float(s["lat"]), float(s["lon"])
            cx, cy = bounds.to_canvas(lat, lon, h, w, border)
            radius = int(s.get("radius", 10))
            font_key = s.get("font") or ("major" if radius >= 14 else "settle")
            name_font = fonts[font_key] if font_key in fonts else fonts["settle"]
            side = s.get("label_side", "right")
            # Optional manual label nudge: list-of-int offsets in pixels.
            # Sentinel values like ['-width-10', -6] from the Dominus YAML
            # aren't supported yet — only numeric pairs.
            raw_off = s.get("label_offset")
            label_offset = None
            if raw_off is not None and len(raw_off) == 2:
                try:
                    label_offset = (int(raw_off[0]), int(raw_off[1]))
                except (TypeError, ValueError):
                    label_offset = None  # silently drop unsupported sentinels
            place_settlement(
                draw, placer, cx, cy,
                name=s["name"],
                color=resolve_color(s.get("color"), color_map, default=(230, 190, 80)),
                radius=radius,
                name_font=name_font,
                note=s.get("note"),
                note_font=fonts["note"],
                note_color=parchment_color,
                preferred_angle=SIDE_TO_ANGLE.get(side, 0),
                margin_left=border, margin_top=border,
                margin_right=cw - border, margin_bottom=ch - border,
                shadow=shadow_color,
                marker=s.get("marker", "circle"),
                label_offset=label_offset,
                rotation=float(s.get("rotation") or 0),
                canvas=final,
            )

        for r in cfg.get("regions", []):
            if r.get("type") != "region_label":
                continue
            center = r.get("center")
            if not center:
                continue
            lat, lon = float(center[0]), float(center[1])
            cx, cy = bounds.to_canvas(lat, lon, h, w, border)
            place_region(
                draw, placer, cx, cy,
                name=r["name"],
                color=resolve_color(r.get("color"), color_map, default=(200, 190, 170)),
                name_font=fonts["region"],
                sub=r.get("subtitle"),
                sub_font=fonts["note"],
                margin_left=border, margin_top=border,
                margin_right=cw - border, margin_bottom=ch - border,
                shadow=shadow_color,
                rotation=float(r.get("rotation") or 0),
                canvas=final,
            )

        for ei in cfg.get("edge_indicators", []):
            edge = ei.get("edge", "west")
            lat = float(ei["lat"])
            if edge == "west":
                lon = bounds.lon_w + 0.03
            elif edge == "east":
                lon = bounds.lon_e - 0.03
            else:
                lon = (bounds.lon_w + bounds.lon_e) / 2
            cx, cy = bounds.to_canvas(lat, lon, h, w, border)
            col = resolve_color(ei.get("color"), color_map, default=(220, 120, 80))
            outline_text(
                draw, (cx, cy), ei["text"], fonts["settle"], col,
                shadow=shadow_color, width=3,
            )
            if ei.get("note"):
                outline_text(
                    draw, (cx, cy + 30), ei["note"], fonts["note"],
                    (200, 150, 120), shadow=shadow_color, width=2,
                )
            placer.reserve((cx - 4, cy - 4, cx + 350, cy + 60))

        for inf in cfg.get("infrastructure", []):
            lat, lon = float(inf["lat"]), float(inf["lon"])
            cx, cy = bounds.to_canvas(lat, lon, h, w, border)
            outline_text(
                draw, (cx, cy), inf["name"], fonts["note"],
                resolve_color("white", color_map, default=(235, 225, 200)),
                shadow=shadow_color, width=2,
            )
            if inf.get("note"):
                outline_text(
                    draw, (cx, cy + 24), inf["note"], fonts["credit"],
                    parchment_color, shadow=shadow_color, width=1,
                )
            placer.reserve((cx - 4, cy - 4, cx + 200, cy + 40))

        water_color = resolve_color("water", color_map, default=(140, 160, 175))
        for wl in cfg.get("water_labels", []):
            lat, lon = float(wl["lat"]), float(wl["lon"])
            cx, cy = bounds.to_canvas(lat, lon, h, w, border)
            place_water(
                draw, placer, cx, cy, wl["name"],
                color=water_color,
                font=fonts["water"],
                margin_left=border, margin_top=border,
                margin_right=cw - border, margin_bottom=ch - border,
                shadow=shadow_color,
                rotation=float(wl.get("rotation") or 0),
                canvas=final,
            )

        # ─── Cartographic furniture (config-driven) ──────────────────
        # All five elements (ornaments, cartouche, compass, scale bar,
        # credit) read from the optional `decoration:` block. Each element
        # has an `enabled` toggle and per-element knobs; sensible defaults
        # match the prior hard-coded behavior so existing configs keep
        # rendering identically without a schema bump.
        deco_cfg = cfg.get("decoration") or {}

        # Corner ornaments — glyph + color + size + insets are all
        # config-driven. Defaults match the prior hard-coded behavior so
        # existing configs render identically without a `decoration:` block.
        ornaments_cfg = deco_cfg.get("ornaments") or {}
        if ornaments_cfg.get("enabled", True):
            # Optional size override — re-instantiate the fleur font at the
            # requested base size (then scaled by font_scale to keep it
            # physically consistent across canvas sizes). When `size` is
            # absent/zero, fall back to the pre-loaded fonts["fleur"].
            orn_font = fonts["fleur"]
            size_override = ornaments_cfg.get("size")
            if size_override:
                fleur_role = (cfg.get("fonts") or {}).get("fleur") or {}
                fleur_file = fleur_role.get("file") or "DejaVuSans.ttf"
                try:
                    orn_font = ImageFont.truetype(
                        font_path(fleur_file),
                        max(8, int(round(float(size_override) * font_scale))),
                    )
                except (OSError, ValueError):
                    # Fall back silently — orn_font already points at the
                    # default fleur. We don't want a bad config knob to
                    # crash the whole render.
                    pass

            # Default color/shadow tuples match the prior call signature.
            # Lists from YAML get cast to tuples here so downstream code
            # can hash them (PIL's text-stroke routines expect tuples).
            def _rgb(seq, default):
                try:
                    return tuple(int(v) for v in (seq or default)[:3])
                except (TypeError, ValueError):
                    return default
            color = _rgb(ornaments_cfg.get("color"), (85, 65, 45))
            # Shadow auto-darkens from the main color so we don't need a
            # second config knob; explicit override still honored when set.
            default_shadow = tuple(max(0, int(c * 0.55)) for c in color)
            shadow = _rgb(ornaments_cfg.get("shadow"), default_shadow)

            draw_corner_ornaments(
                draw, cw, ch, orn_font,
                glyph=str(ornaments_cfg.get("glyph") or "⚜"),
                color=color,
                shadow=shadow,
                inset_x=int(ornaments_cfg.get("inset_x", 35)),
                inset_top=int(ornaments_cfg.get("inset_top", 30)),
                inset_bottom=int(ornaments_cfg.get("inset_bottom", 45)),
            )

        # Title cartouche (top-center is the only style the renderer supports
        # today; future positions hook in here.)
        cartouche_cfg = deco_cfg.get("cartouche") or {}
        if cartouche_cfg.get("enabled", True):
            final, draw = draw_title_cartouche(
                final, title_text, cfg.get("subtitle"),
                fonts["title"], fonts["sub"], cw,
            )

        # Compass rose
        compass_cfg = deco_cfg.get("compass") or {}
        if compass_cfg.get("enabled", True):
            # Radius scales with canvas — period-cartographic compass roses
            # occupy ~5-7% of canvas. Floor at 55 px so small previews
            # don't get a sub-pixel compass. Config knob `radius_pct` is
            # a percentage (5–10) of min(cw, ch) when supplied.
            radius_pct = float(compass_cfg.get("radius_pct", 0) or 0)
            if radius_pct > 0:
                compass_r = max(55, int(min(cw, ch) * radius_pct / 100))
            else:
                compass_r = max(55, int(min(cw, ch) / 22))
            # Position selector — bottom-right matches the previous default.
            position = compass_cfg.get("position", "bottom-right")
            offset_x = int(compass_cfg.get("offset_x", 20))
            offset_y = int(compass_cfg.get("offset_y", 80))
            if position == "bottom-left":
                compass_cx = border + compass_r + offset_x
                compass_cy = ch - border - compass_r - offset_y
            elif position == "top-right":
                compass_cx = cw - border - compass_r - offset_x
                compass_cy = border + compass_r + offset_y
            elif position == "top-left":
                compass_cx = border + compass_r + offset_x
                compass_cy = border + compass_r + offset_y
            else:  # bottom-right (default)
                compass_cx = cw - border - compass_r - offset_x
                compass_cy = ch - border - compass_r - offset_y
            final, draw = draw_compass_rose(
                final, compass_cx, compass_cy, compass_r, fonts["compass"],
            )

        # Scale bar
        scale_cfg = deco_cfg.get("scale_bar") or {}
        if scale_cfg.get("enabled", True):
            draw_scale_bar(
                draw, bounds, w, cw, ch, fonts["scale"],
                bar_miles=int(scale_cfg.get("bar_miles", 20)),
                segments=int(scale_cfg.get("segments", 4)),
                position=scale_cfg.get("position", "bottom-center"),
                border=border,
                offset_from_border=int(scale_cfg.get("offset_from_border", 60)),
            )

        # Legend (kept under cfg.legend for backward compat — not nested
        # under cfg.decoration, since legend entries are already a top-level
        # YAML block in every existing config). Position is config-driven; the
        # anchor is computed by _legend_placement, the SAME helper the placer
        # reservation uses above, so the reserved box and the drawn plaque
        # always agree.
        legend_cfg = cfg.get("legend", {})
        legend_entries = legend_cfg.get("entries", [])
        if legend_entries:
            cartouche_on = bool(
                (deco_cfg.get("cartouche") or {}).get("enabled", True) and title_text
            )
            lx, ly, _ = _legend_placement(
                legend_cfg, len(legend_entries), cartouche_on, cw, ch, border
            )
            final, draw = draw_legend(
                final, lx, ly, legend_entries, color_map,
                title_font=fonts["legend_title"],
                entry_font=fonts["legend"],
                italic_entry_font=fonts["legend_italic"],
            )

        # Credit line — text comes from cfg.credit (top-level, existing
        # convention) but visibility + spacing are config-driven via
        # `decoration.credit`. The default offset_from_border of 28 px
        # places the line cleanly inside the inner frame line.
        credit_cfg = deco_cfg.get("credit") or {}
        credit = (credit_cfg.get("text") or cfg.get("credit") or "").strip()
        if credit and credit_cfg.get("enabled", True):
            draw_credit(
                draw, credit, fonts["credit"], cw, ch,
                border=border,
                offset_from_border=int(credit_cfg.get("offset_from_border", 28)),
                divider=bool(credit_cfg.get("divider", True)),
            )

        cache.save_image("final", s4_key, final, h=ch, w=cw, labels=len(placer.boxes))

    # === Save ===
    # Format → file extension mapping. We accept several aliases (jpg/jpeg,
    # tif/tiff) and normalize them to a canonical extension on disk.
    fmt = output_format.lower()
    ext_by_fmt = {
        "png":  "png",
        "jpg":  "jpg",
        "jpeg": "jpg",
        "webp": "webp",
        "tif":  "tiff",
        "tiff": "tiff",
        "pdf":  "pdf",
    }
    if fmt not in ext_by_fmt:
        raise ValueError(
            f"unknown output_format {output_format!r}; "
            f"expected one of {sorted(set(ext_by_fmt))}"
        )
    ext = ext_by_fmt[fmt]

    if output_path is None:
        out_dir = project_root() / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{cfg_path.stem}.{ext}"
    else:
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

    # Build the kwargs Pillow expects for each format. DPI applies to
    # raster formats that store physical-size hints (PNG, TIFF, JPEG, PDF).
    final_rgb = final.convert("RGB")
    save_kwargs: dict[str, Any] = {}
    if dpi is not None and dpi > 0:
        save_kwargs["dpi"] = (int(dpi), int(dpi))

    # Embedded metadata: name/subtitle/credit + render timestamp +
    # Cartograph version. Different formats use different metadata APIs
    # so we pick the channel each format actually honors.
    metadata = _build_export_metadata(cfg, embed_metadata)

    if fmt in ("jpg", "jpeg"):
        save_kwargs.update(format="JPEG", quality=jpeg_quality, optimize=True)
        if metadata:
            # JPEG carries metadata in EXIF — Pillow lets us write a small
            # EXIF block via the `exif` kwarg on save.
            save_kwargs["exif"] = _encode_exif(metadata)
        final_rgb.save(out_path, **save_kwargs)
    elif fmt == "webp":
        save_kwargs.update(format="WEBP", quality=jpeg_quality, method=6)
        if metadata:
            save_kwargs["exif"] = _encode_exif(metadata)
        final_rgb.save(out_path, **save_kwargs)
    elif fmt in ("tif", "tiff"):
        # TIFF is lossless; quality is irrelevant. LZW compresses ~50%
        # without changing pixels.
        save_kwargs.update(format="TIFF", compression="tiff_lzw")
        if metadata:
            # TIFF stores `Description` in tag 270 (ImageDescription) and
            # `Artist` in 315. Pillow writes via the `description` shortcut.
            save_kwargs["description"] = metadata.get("description", "")
        final_rgb.save(out_path, **save_kwargs)
    elif fmt == "pdf":
        # Single-page raster PDF. Title/Author land in the PDF info dict.
        save_kwargs.update(format="PDF")
        if metadata:
            save_kwargs["title"] = metadata.get("title", "")
            save_kwargs["author"] = metadata.get("author", "")
            save_kwargs["subject"] = metadata.get("description", "")
            save_kwargs["producer"] = metadata.get("producer", "fictional-cartography")
        final_rgb.save(out_path, **save_kwargs)
    else:  # png
        save_kwargs.update(format="PNG")
        if metadata:
            # PNG carries metadata in tEXt chunks via PngInfo.
            save_kwargs["pnginfo"] = _encode_pnginfo(metadata)
        final_rgb.save(out_path, **save_kwargs)

    print(f"\nSaved {out_path} ({cw} x {ch})")
    return out_path


def _build_export_metadata(cfg: dict[str, Any], enabled: bool) -> dict[str, str]:
    """Assemble the small metadata payload we embed in exported files.

    Returns an empty dict when `enabled` is False so callers can `if not meta`
    to short-circuit the format-specific encode steps.
    """
    if not enabled:
        return {}
    import datetime

    name = (cfg.get("name") or "").strip()
    subtitle = (cfg.get("subtitle") or "").strip()
    credit = (cfg.get("credit") or "").strip()
    title = name + (f" — {subtitle}" if subtitle else "")
    parts = []
    if subtitle:
        parts.append(subtitle)
    parts.append(f"Rendered {datetime.datetime.now().isoformat(timespec='seconds')}")
    parts.append("fictional-cartography")
    description = " · ".join(parts)
    return {
        "title": title or (Path(cfg.get("__path__", "map")).stem if cfg.get("__path__") else "map"),
        "author": credit,
        "description": description,
        "producer": "fictional-cartography",
    }


def _encode_exif(meta: dict[str, str]) -> bytes:
    """Encode a small EXIF block from metadata dict.

    JPEG/WebP both accept the `exif=` save kwarg. We populate ImageDescription
    (tag 270), Artist (315), and Software (305) — the only fields most viewers
    surface.
    """
    from PIL import Image as _Img
    exif = _Img.Exif()
    if meta.get("description"):
        exif[270] = meta["description"]
    if meta.get("author"):
        exif[315] = meta["author"]
    if meta.get("title"):
        exif[270] = exif.get(270) or meta["title"]
    exif[305] = "fictional-cartography"
    return exif.tobytes()


def _encode_pnginfo(meta: dict[str, str]):
    """Encode a PngInfo block of tEXt chunks."""
    from PIL.PngImagePlugin import PngInfo
    pinfo = PngInfo()
    if meta.get("title"):       pinfo.add_text("Title", meta["title"])
    if meta.get("author"):      pinfo.add_text("Author", meta["author"])
    if meta.get("description"): pinfo.add_text("Description", meta["description"])
    pinfo.add_text("Software", "fictional-cartography")
    return pinfo


def fetch_data(config_path: str | Path) -> None:
    """Pre-fetch SRTM and Overpass data for a config without rendering."""
    from src.terrain.srtm import load_terrain
    from src.data.overpass import fetch_routes, fetch_landuse

    cfg_path = Path(config_path)
    cfg = load_config(cfg_path)
    bounds_cfg = cfg["bounds"]
    bounds = MapBounds(
        lat_n=float(bounds_cfg["lat_n"]),
        lat_s=float(bounds_cfg["lat_s"]),
        lon_w=float(bounds_cfg["lon_w"]),
        lon_e=float(bounds_cfg["lon_e"]),
    )
    aspect = parse_aspect_ratio(bounds_cfg.get("aspect_ratio"))
    ds = int(cfg.get("terrain", {}).get("downsample", 4))

    print("Fetching SRTM tiles...")
    load_terrain(bounds, aspect=aspect, downsample_factor=ds)

    roads_cfg = cfg.get("roads")
    if roads_cfg:
        print("Fetching routes...")
        fetch_routes(
            bounds, roads_cfg.get("networks", ["US:I", "US:US"]),
            cache_name=f"routes_{cfg_path.stem}",
        )

    if cfg.get("urbanization", {}).get("enabled", True):
        print("Fetching landuse polygons...")
        fetch_landuse(bounds, cache_name=f"landuse_{cfg_path.stem}")

    print("Done.")
