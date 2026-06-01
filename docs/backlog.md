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

### Dense-cluster label de-confliction is manual

- **Hit while:** Sussex passes 2–4 — six settlements packed into the southern ~4 miles.
- **Workaround:** Hand-tuned `label_side` per settlement and relocated the legend to free space. The collision placer handles overlaps but doesn't _spread_ a tight cluster; dense groups still need manual side hints.
- **Proposed:** A clustering/auto-spread pass that, when N markers fall within a small radius, fans their preferred sides radially from the cluster centroid before the placer scores positions.

---

## Suspected bugs (verify before acting)

### Cartouche vertical geometry doesn't scale with canvas

- **Hit while:** Reading `draw_title_cartouche` during legend-clearance work.
- **Detail:** Box width scales (it's `max(title_w, subtitle_w) + 40`, and fonts scale via `font_scale`), but the vertical geometry is fixed pixels: anchor `y=22`, box height 120. On a high-`--scale` export the title font grows (`font_scale = max(1, min(h,w)/1800)`) while the box height stays 120px — likely vertical overflow.
- **Next:** Render an existing cartouche map at `--scale 4` and inspect the title block. If it overflows, scale the box geometry (and `y`) by `font_scale`.

---

## Recently resolved

_Kept briefly for continuity; prune when stale._

- **Boundary auto-fetch on render** (commit `4c3c155`) — boundaries were the one data layer that only read the cache instead of fetching on demand; a fresh clone silently lost any non-pre-cached outline. Now fetches on first render like SRTM/OSM, with `state_boundaries.county_of` for county disambiguation.
- **County boundary fetching** (commit `4c3c155`) — `fetch_admin_boundary(within_state=...)` + `ensure_county_boundary()` added; counties (admin_level 6) are now fetchable with state disambiguation.
- **Credit line drew on top of the border** (earlier session) — credit now offsets from the inner border (`decoration.credit.offset_from_border`) instead of the canvas edge.
- **`pip install -e .` registered src/ subdirs as top-level modules** (publish prep) — fixed with explicit `[tool.setuptools.packages.find]` in pyproject.toml.
