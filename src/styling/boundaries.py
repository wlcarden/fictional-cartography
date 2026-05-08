"""State / county boundary outlines and reference-city markers.

Rendered in stage 4 (alongside labels) so tweaking colors doesn't bust the
canvas-roads cache. Boundaries are drawn as polygon OUTLINES — fills would
swamp the terrain — using the YAML's `highlight_color` for the named state
and `other_color` for siblings.

Reference cities are simple ringed-dot markers with an italic name label.
They're cartographic orientation aids, not narrative placemarks, so they
intentionally read quieter than settlements (no leader lines, no notes).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

from src.data.boundaries import load_state_boundaries

if TYPE_CHECKING:
    from src.pipeline import MapBounds


def _coerce_color(value, default):
    """Return a 4-tuple RGBA. Accepts 3- or 4-tuples; pads with alpha=255."""
    if not isinstance(value, (list, tuple)):
        return default
    out = [int(c) for c in value[:4]]
    if len(out) == 3:
        out.append(255)
    if len(out) != 4:
        return default
    return tuple(max(0, min(255, c)) for c in out)


def draw_state_boundaries(
    canvas,
    cfg: dict,
    bounds: "MapBounds",
    h: int,
    w: int,
    border: int,
    width: int = 2,
) -> int:
    """Outline configured state polygons onto `canvas` (in place).

    `cfg` is the YAML's `state_boundaries` block. Reads `enabled`, `states`,
    `highlight`, `highlight_color`, `other_color`. Returns the number of
    polygons drawn (zero when disabled or the cache is missing).

    Drawn into a transparent RGBA layer first so the YAML's alpha channel
    actually blends — `ImageDraw.line` on the main canvas would clamp to
    opaque otherwise.
    """
    if not cfg or not cfg.get("enabled"):
        return 0
    states_to_draw = cfg.get("states") or []
    if not states_to_draw:
        return 0

    library = load_state_boundaries()
    if not library:
        return 0

    highlight_name = cfg.get("highlight")
    highlight_color = _coerce_color(cfg.get("highlight_color"), (255, 210, 70, 200))
    other_color    = _coerce_color(cfg.get("other_color"),     (170, 170, 190, 70))

    # Render onto a transparent layer so the alpha channel from the YAML
    # actually composites — direct draw onto an RGBA canvas works, but routing
    # through a layer keeps the API consistent with future rotation/effects.
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)

    drawn = 0
    for state_name in states_to_draw:
        polys = library.get(state_name)
        if not polys:
            continue
        color = highlight_color if state_name == highlight_name else other_color
        for poly in polys:
            # Project each vertex to canvas coords. Skip rings with <2 points.
            pts = [bounds.to_canvas(lat, lon, h, w, border) for (lat, lon) in poly]
            if len(pts) < 2:
                continue
            # Close the ring so the outline is continuous.
            if pts[0] != pts[-1]:
                pts.append(pts[0])
            ld.line(pts, fill=color, width=width)
            drawn += 1

    canvas.alpha_composite(layer)
    return drawn


def draw_reference_cities(
    canvas,
    draw: ImageDraw.ImageDraw,
    cfg: dict,
    bounds: "MapBounds",
    h: int,
    w: int,
    border: int,
    font: ImageFont.FreeTypeFont,
    color: tuple[int, int, int] = (210, 200, 170),
    halo: tuple[int, int, int] = (30, 25, 18),
) -> int:
    """Draw small markers + italic labels for each reference city.

    `cfg` is the YAML's `reference_cities` block. Reads `enabled` and
    `cities` (list of {name, lat, lon}). Cities outside the canvas extents
    are silently skipped.

    Returns the number of cities drawn.
    """
    if not cfg or not cfg.get("enabled"):
        return 0
    cities = cfg.get("cities") or []
    if not cities:
        return 0

    cw, ch = canvas.size
    drawn = 0
    for c in cities:
        try:
            lat = float(c["lat"])
            lon = float(c["lon"])
            name = str(c["name"])
        except (KeyError, TypeError, ValueError):
            continue
        x, y = bounds.to_canvas(lat, lon, h, w, border)
        # Cull cities entirely outside the bordered canvas — not even their
        # markers should bleed into paper margins.
        if not (border < x < cw - border and border < y < ch - border):
            continue

        # Marker: shadow disc + ring + bright pip.
        r = 4
        draw.ellipse([x - r - 1, y - r - 1, x + r + 1, y + r + 1], fill=halo)
        draw.ellipse([x - r, y - r, x + r, y + r],
                     outline=color, width=1, fill=None)
        draw.ellipse([x - 1, y - 1, x + 1, y + 1], fill=color)

        # Label: small italic, anchored to upper-left of the dot. Keep it
        # offset enough that the marker isn't covered.
        from src.labels.markers import outline_text
        outline_text(
            draw, (x + r + 4, y - r - 2), name, font, color,
            shadow=halo, width=1, anchor="la",
        )
        drawn += 1
    return drawn
