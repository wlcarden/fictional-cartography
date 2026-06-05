# Feature backlog & known gaps

A living log of things we had to do **manually** — in code, by hand-editing YAML, or through a workaround — because the pipeline or web editor didn't cover them. Append here the moment you hit a gap; promote an entry to a real change when you implement it, and move it to **Recently resolved**.

Entries are grouped by effort:

- **UI gaps** — the config field exists and the pipeline honors it, but no editor page can set it. Small (wire an existing knob into a page + a test).
- **Pipeline / config gaps** — not configurable at all; needs a schema field + pipeline wiring + UI + docs + test. Medium.
- **Workflow / automation** — manual steps or judgement calls a heuristic could automate. Open-ended.
- **Suspected bugs** — quirks noticed in passing, not yet confirmed/triaged.

Each entry records: **Hit while** (the context that surfaced it), **Workaround** (what we did instead), **Proposed** (the fix).

---

## Open — UI gaps (config works, editor can't set it)

### Furniture + legend colours, and `--grayscale`

- **Hit while:** Building the B&W print edition (`config/sussex-county-bw.yaml`).
- **Workaround:** Hand-edited YAML. The pipeline now honours background/text colour knobs for the cartouche (`decoration.cartouche.{bg_color,border_color,title_color,subtitle_color,divider_color}`), legend (`legend.{bg_color,border_color,title_color,text_color}`), compass (`decoration.compass.{line_color,fill_color,text_color}`), scale bar (`decoration.scale_bar.{dark_color,light_color,border_color,label_color}`), and credit (`decoration.credit.color`) — but none are in the Decoration editor page. The `--grayscale` export flag is CLI-only (not in the Render page or `/api/render`).
- **Proposed:** Add colour pickers for each furniture element to the Decoration page; add a Grayscale toggle to the Render page + `grayscale` to the render endpoints. Consider a one-click "B&W print" preset that bundles the light-furniture + grayscale recipe.

### `legend.position`

- **Hit while:** Sussex County map — needed to move the legend out of the southern cluster.
- **Workaround:** Set `legend.position: top-left` by hand-editing the YAML. The Decoration page's Legend section only edits entries, not position.
- **Proposed:** Add a position selector (bottom-left / top-left / top-right / bottom-right) to the Decoration page's Legend fieldset, hydrating + writing `legend.position`.

### County boundaries (`state_boundaries.county_of` + county names)

- **Hit while:** Sussex County map — the whole point was a county outline.
- **Workaround:** Hand-edited `state_boundaries.county_of: New Jersey` + `states: ["Sussex County"]`. The Decoration page's State boundaries section only handles state names and has no `county_of` control; it can't drive a county fetch.
- **Proposed:** Extend the Decoration boundaries UI with a "county of <state>" mode (or an admin-level toggle) that sets `county_of` and lets you add county names. Bonus: a name field with validation/feedback when the Overpass fetch returns nothing.

---

## Open — pipeline / config gaps (not configurable at all)

### Boundary line width

- **Hit while:** Sussex pass 1 — the county outline read a bit thin against busy hillshade.
- **Workaround:** Lived with it; leaned on a high-contrast amber color instead. `draw_state_boundaries` takes `width: int = 2` but the pipeline call (`src/pipeline.py`) passes no width, so it's effectively hardcoded to 2; no YAML knob.
- **Proposed:** Add `state_boundaries.width` (and maybe per-entry width / a dash pattern), thread it through the pipeline call into `draw_state_boundaries`.

### Title cartouche position + scale

- **Hit while:** Sussex legend de-confliction — reading the cartouche code to compute clearance.
- **Workaround:** Hardcoded `CARTOUCHE_BOTTOM = 142` in the legend-clearance logic, mirroring the cartouche's fixed geometry. `decoration.cartouche` only exposes `enabled`; the cartouche is locked to top-center at `y=22` with a fixed 120px box height.
- **Proposed:** Add `decoration.cartouche.position` (top-center / top-left / top-right) and scale the box geometry with the canvas. See also the suspected-bug note below about high-scale exports.

### Per-boundary / richer boundary styling

- **Hit while:** Sussex — wanted the focus county to read clearly without garish fill.
- **Workaround:** Used `highlight_color` (one color) + `other_color`. No per-entry color, no dashed/dotted style, no fill-with-low-alpha option.
- **Proposed:** Allow per-entry style overrides under `state_boundaries` (color, width, dash, optional fill alpha).

---

## Open — workflow / automation

### OSM data cache is keyed by config name, not by bounds

- **Hit while:** First render of `config/sussex-county-bw.yaml` took ~5 minutes.
- **Detail:** The roads/landuse Overpass cache uses `routes_{config_stem}` / `landuse_{config_stem}` as the cache key. A variant config with identical bounds (e.g. the B&W edition of an existing map) re-fetches 7+ MB of roads and 2 MB of landuse from Overpass even though the data is byte-identical to the sibling config's already-cached fetch.
- **Proposed:** Key the OSM data cache by a hash of (bbox + query), not the config name, so sibling/variant configs share fetches. Big win for any "print edition" or A/B variant workflow.

### Dense-cluster label de-confliction is manual

- **Hit while:** Sussex passes 2–4 — six settlements packed into the southern ~4 miles.
- **Workaround:** Hand-tuned `label_side` per settlement and relocated the legend. **Update:** most of the tangle turned out to be the hardcoded-legend-reservation bug (see Recently resolved) — once the placer knew the legend had moved, it spread the cluster cleanly on its own, with leader lines, no manual offsets. So the placer is better than it looked; the remaining need is narrower.
- **Proposed:** A clustering/auto-spread pass for genuinely dense groups (N markers within a small radius) that fans preferred sides radially from the centroid before scoring. Lower priority now that the reservation bug is fixed.

---

## Suspected bugs (verify before acting)

### Scale-bar + compass placer reservations are hardcoded to default positions

- **Hit while:** Fixing the legend-reservation bug (same root cause).
- **Detail:** The placer reserves furniture boxes so labels don't sit on them. The legend reservation now follows `legend.position` (fixed), but the **scale-bar** reservation (`cw//2 ± 200`, bottom-center) and **compass** reservation (bottom-right corner) are still hardcoded to their default positions. If a config sets `decoration.scale_bar.position` or `decoration.compass.position` away from the default, the reservation won't follow — labels could overlap the moved furniture, and a phantom box stays at the old spot. No live map triggers it yet (Sussex keeps both at defaults).
- **Proposed:** Compute the scale-bar and compass reservation boxes from their configured positions, mirroring the `_legend_placement` fix. Probably extract a small `_furniture_box(kind, position, ...)` helper.

### Cartouche vertical geometry doesn't scale with canvas

- **Hit while:** Reading `draw_title_cartouche` during legend-clearance work.
- **Detail:** Box width scales (it's `max(title_w, subtitle_w) + 40`, and fonts scale via `font_scale`), but the vertical geometry is fixed pixels: anchor `y=22`, box height 120. On a high-`--scale` export the title font grows (`font_scale = max(1, min(h,w)/1800)`) while the box height stays 120px — likely vertical overflow.
- **Next:** Render an existing cartouche map at `--scale 4` and inspect the title block. If it overflows, scale the box geometry (and `y`) by `font_scale`.

---

## Recently resolved

_Kept briefly for continuity; prune when stale._

- **`decoration` block missing from the stage-4 cache key** (this session) — the final-stage cache key listed `legend`, `title`, `credit`, `fonts`… but never the `decoration` block, even though all its furniture (compass / scale_bar / credit / ornaments / cartouche) renders in stage 4. So furniture edits — colour OR position — silently returned a stale cached `final`; they only ever appeared to work when a stage-1–3 change happened to cascade the cache. Fixed by adding `decoration=cfg.get("decoration", {})` to the key. Latent since the decoration block was introduced.
- **Furniture + legend colours now config-driven** (this session) — added `bg_color`/`text_color`/etc. knobs for the cartouche, legend, compass, scale bar, and credit (wired through `_color_kwargs`; each falls back to the drawing function's default, so existing maps are unchanged). Plus a reusable `--grayscale` export flag (true luminance conversion at save). Enables the B&W print edition `config/sussex-county-bw.yaml`. (UI exposure still pending — see Open/UI gaps.)
- **Legend placer reservation didn't follow `legend.position`** (this session) — the placer reserved the legend's _default_ bottom-left corner regardless of where the legend was configured. After moving Sussex's legend to top-left, the phantom bottom-left reservation squeezed the southern settlement cluster while the real (top-left) legend went unprotected. Fixed via `_legend_placement`, a single helper shared by the reservation and the draw so they always agree; the default branch keeps the exact legacy box for pixel-identical legacy renders. This was the actual cause of the "tangled cluster," not label-placer weakness.
- **Boundary auto-fetch on render** (commit `4c3c155`) — boundaries were the one data layer that only read the cache instead of fetching on demand; a fresh clone silently lost any non-pre-cached outline. Now fetches on first render like SRTM/OSM, with `state_boundaries.county_of` for county disambiguation.
- **County boundary fetching** (commit `4c3c155`) — `fetch_admin_boundary(within_state=...)` + `ensure_county_boundary()` added; counties (admin_level 6) are now fetchable with state disambiguation.
- **Credit line drew on top of the border** (earlier session) — credit now offsets from the inner border (`decoration.credit.offset_from_border`) instead of the canvas edge.
- **`pip install -e .` registered src/ subdirs as top-level modules** (publish prep) — fixed with explicit `[tool.setuptools.packages.find]` in pyproject.toml.
