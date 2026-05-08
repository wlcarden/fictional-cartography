"""Road rendering on the canvas with optional water masking and a shadow pass."""
from __future__ import annotations

from typing import Sequence

import numpy as np
from PIL import ImageDraw

from src.pipeline import MapBounds


# Default OSM `network` value associated with each built-in style key.
# Override per-style in the YAML by adding `network: "..."` (or `prefix: "..."`
# for legacy ref-based matching).
_DEFAULT_NETWORK_FOR_STYLE = {
    "interstate": "US:I",
    "us_highway": "US:US",
}


def _network_for_style(style_name: str, style_cfg: dict) -> str:
    if "network" in style_cfg:
        return style_cfg["network"]
    return _DEFAULT_NETWORK_FOR_STYLE.get(style_name, "")


def _prefix_for_style(style_name: str, style_cfg: dict) -> str:
    return style_cfg.get("prefix", "")


def _classify_route(
    network: str, ref: str, styles: dict[str, dict]
) -> tuple[str, dict] | None:
    """Match a (network, ref) route against the configured styles.

    Network match wins over prefix match. If multiple styles match, the longest
    network/prefix wins so e.g. 'US:NJ:CR' beats 'US:NJ'.

    A style may also restrict to specific `refs` (a list of OSM ref values like
    ["95", "295", "395"]). If `refs` is non-empty and the route's ref isn't in
    it, the style won't match — useful for picking out a handful of named
    highways from a network that contains hundreds of routes.
    """
    def _refs_pass(cfg: dict) -> bool:
        wanted = cfg.get("refs")
        if not wanted:           # missing or [] = no filter, all refs accepted
            return True
        # Cast to str so int "95" in YAML still matches OSM's string "95".
        return str(ref) in {str(r) for r in wanted}

    matches: list[tuple[int, int, str, dict]] = []
    for name, cfg in styles.items():
        net = _network_for_style(name, cfg)
        if net and network == net:
            if _refs_pass(cfg):
                matches.append((1, len(net), name, cfg))
        elif net and network.startswith(net + ":"):
            if _refs_pass(cfg):
                matches.append((0, len(net), name, cfg))
        else:
            prefix = _prefix_for_style(name, cfg)
            if prefix and ref.startswith(prefix) and _refs_pass(cfg):
                matches.append((0, len(prefix), name, cfg))
    if not matches:
        return None
    matches.sort(reverse=True)  # exact-network beats prefix; longer beats shorter
    _, _, name, cfg = matches[0]
    return name, cfg


def draw_roads(
    draw: ImageDraw.ImageDraw,
    routes: dict[tuple[str, str], list[list[tuple[float, float]]]],
    styles: dict[str, dict],
    bounds: MapBounds,
    terrain_h: int,
    terrain_w: int,
    border: int,
    water_mask: np.ndarray | None,
    shadow_color: tuple[int, int, int],
) -> int:
    """Render named routes in two passes (shadows below, color above).

    `routes` is the parse_routes output: {(network, ref): [[(lat, lon), ...], ...]}.
    `styles` keys (e.g. "interstate", "us_highway") map to
    {color, width, shadow_width, network?, prefix?}. The classifier prefers
    exact `network` match; if a style only sets `prefix`, it falls back to
    matching the route's ref by prefix.
    `water_mask` is the same h/w as the terrain image (NOT the canvas); when
    provided, segments straddling water are skipped so roads end at the new coast.

    Returns the number of distinct routes drawn.
    """
    classified: list[tuple[str, list[list[tuple[float, float]]], tuple[int, int, int], int, int]] = []
    for (network, ref), segs in routes.items():
        match = _classify_route(network, ref, styles)
        if match is None:
            continue
        _, cfg = match
        color = tuple(cfg["color"])
        width = int(cfg.get("width", 2))
        shadow_width = int(cfg.get("shadow_width", 1))
        classified.append((f"{network} {ref}", segs, color, width, shadow_width))

    canvas_w = terrain_w + 2 * border
    canvas_h = terrain_h + 2 * border

    def underwater(lat: float, lon: float) -> bool:
        if water_mask is None:
            return False
        h, w = water_mask.shape
        r = int((bounds.lat_n - lat) / bounds.lat_span * h)
        c = int((lon - bounds.lon_w) / bounds.lon_span * w)
        if 0 <= r < h and 0 <= c < w:
            return bool(water_mask[r, c])
        return True  # off-map points treated as water (don't draw)

    def to_canvas(lat: float, lon: float) -> tuple[int, int]:
        x = border + int((lon - bounds.lon_w) / bounds.lon_span * terrain_w)
        y = border + int((bounds.lat_n - lat) / bounds.lat_span * terrain_h)
        return x, y

    margin = 200
    for shadow_pass in (True, False):
        for ref, segs, color, width, sw in classified:
            for seg in segs:
                if len(seg) < 2:
                    continue
                for i in range(len(seg) - 1):
                    la1, lo1 = seg[i]
                    la2, lo2 = seg[i + 1]
                    if underwater(la1, lo1) or underwater(la2, lo2):
                        continue
                    x1, y1 = to_canvas(la1, lo1)
                    x2, y2 = to_canvas(la2, lo2)
                    if (
                        (x1 < -margin or x1 > canvas_w + margin or
                         y1 < -margin or y1 > canvas_h + margin)
                        and (x2 < -margin or x2 > canvas_w + margin or
                             y2 < -margin or y2 > canvas_h + margin)
                    ):
                        continue
                    if shadow_pass:
                        draw.line(
                            [(x1, y1), (x2, y2)],
                            fill=shadow_color,
                            width=width + sw * 2,
                        )
                    else:
                        draw.line(
                            [(x1, y1), (x2, y2)], fill=color, width=width
                        )
    return len(classified)
