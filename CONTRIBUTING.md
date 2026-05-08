# Contributing to Cartograph

Thanks for considering a contribution. This document covers development setup, the test layout, and conventions the codebase follows.

## Development setup

```bash
git clone https://github.com/wlcarden/fictional-cartography.git
cd fictional-cartography
pip install -e ".[dev]"

# Optional but recommended for editor work:
pip install playwright
python -m playwright install chromium
```

The `[dev]` extras (Playwright + pytest) aren't yet declared in `pyproject.toml` — install them manually for now:

```bash
pip install pytest playwright
python -m playwright install chromium
```

## Project layout

```
src/                  Python pipeline + Flask web server
  pipeline.py         Render orchestrator (4 cached stages)
  cli.py              `cartograph` CLI entry point
  server.py           Flask backend for the web editor
  terrain/            SRTM mosaic, hillshade, sea-level modifications
  data/               Overpass road/landuse fetch, US Census boundaries
  styling/            Roads, regional tints, paper texture, barriers
  labels/             Collision-aware label placement
  furniture/          Compass, scale bar, legend, ornaments, cartouche

web/                  HTML/JS editor pages (one per route)
  setup.html          Step 01 — project, bounds, sea level, colors, fonts
  paint.html          Step 02 — terrain styling + regions ("/paint" but labeled "Terrain")
  place.html          Step 03 — settlements, edges, infrastructure, water labels
  routes.html         Step 04 — OSM road networks + styles
  narrative.html      Step 05 — barriers, buffer zones, sinking, contamination
  decoration.html     Step 06 — paper, frame, legend, compass, scale, credit, ornaments
  render.html         Step 07 — export workflow with presets + history
  _shared.js          Shared Cartograph.{Save, History} modules + undo/redo

tests/playwright/     Playwright end-to-end tests (one file per page area)
config/               YAML map definitions
docs/                 Detailed guides (web-editor.md, config-reference.md, etc.)
```

## Tests

The codebase uses two test runners:

### pytest (unit-style)

```bash
pytest
```

Discovers tests under `tests/` (excluding `tests/playwright/` which is a separate runner). Currently lightweight — most coverage is at the integration layer.

### Playwright (end-to-end editor tests)

The Playwright suites are stand-alone Python scripts (not pytest test files) that drive a real headless Chromium against a live Flask server. Each suite covers one editor page area.

```bash
# Run the full sweep:
./run_all_tests.sh

# Run an individual suite (server must be running):
python -m src.server &
python tests/playwright/test_decoration_extended.py
```

Current suites:

| Suite                              | Assertions | Covers                                                                |
| ---------------------------------- | ---------- | --------------------------------------------------------------------- |
| `test_setup.py`                    | 16         | Setup page (identity, bounds, sea level)                              |
| `test_paint.py`                    | 15         | Paint/Terrain page (region inspector + tint type picker)              |
| `test_place.py`                    | 21         | Places page (settlement rail, inspector, in-memory edits)             |
| `test_routes_decoration_render.py` | 12         | Smoke + console-error checks for routes/decoration/render             |
| `test_render_export.py`            | 36         | Render-page export workflow (presets, format-row visibility, history) |
| `test_decoration_extended.py`      | 41         | Decoration extras (compass, scale-bar, credit, ornaments, cartouche)  |
| `test_global_settings.py`          | 21         | Global YAML editors (terrain extras, colors, fonts round-trip)        |

162 assertions total, all passing as of the most recent change. New features should land with a corresponding suite or extend an existing one.

### Test-writing conventions

The Playwright tests use a small custom harness (`tests/playwright/helpers.py`) rather than pytest because the early development cycle hit too many `Locator.click()` stability issues with CSS-transitioning UI elements. The patterns:

- **Read DOM, don't read JS state.** State variables in the editor pages are closure-bound (`let SCALE = 1` inside an IIFE-style `<script>`). `page.evaluate("SCALE")` returns `undefined`. Read from the DOM mirror (input values, `is-active` classes, checkbox state) — that's the canonical truth the user sees anyway.
- **Use `click_via_js` for transitioning elements.** `Locator.click()` waits for stability heuristics that CSS hover transitions never satisfy. The `click_via_js(page, selector)` helper bypasses Playwright's wait and dispatches a synthetic click event.
- **Wrap with `with_config_backup(PROJECT)`.** Every test that PUTs to `/api/config/<name>` must restore the original YAML on exit. The helper takes a snapshot before and restores it after — even on test failure.
- **Use `setup_console_capture` + `filter_benign_errors`.** Every test should end with a "no meaningful console errors" assertion. The benign-pattern list filters known-OK warnings (favicon 404s, etc.).

## Code style

### Python

- Python 3.10+; use modern syntax (`X | None`, `match`, etc.)
- Docstrings on public functions describe **what** and **why**, not just **what**. Surprises and non-obvious decisions earn their own paragraph.
- Pipeline modules use `from __future__ import annotations` for forward refs.
- 4-space indents, no tabs.
- Type hints on function signatures where they aid readability — not as religion.

### HTML/JS (editor pages)

- Vanilla JS only. No framework dependencies. The editor is intentionally low-stakes for a hobby project's lifespan.
- Each page is a self-contained `<script>` IIFE-ish blob. Module pattern: `const { foo, bar } = (() => { ... return { foo, bar }; })();`
- Shared editor library at `web/_shared.js` exposes `window.Cartograph.{Save, History}` plus the undo/redo affordance pill. Every editor page loads it via `<script src="/_shared.js"></script>`.
- 2-space indents in HTML; 2-space in JS.

### Configs (YAML)

- New top-level keys need a default in the pipeline. Configs without the key should render identically to before the key was added (backward compat).
- The `decoration` block is the established pattern for adding new "global furniture" knobs — extend that rather than adding new top-level blocks for incremental cosmetic features.

## Adding a new feature

The typical pattern:

1. **Define the YAML schema** — pick the key path, write defaults that match the current rendering.
2. **Wire the pipeline** — read the key in `pipeline.py`, dispatch to the relevant module. Defaults match prior behavior so existing configs render unchanged.
3. **Update the relevant editor page** — add controls, hydrate from cfg, write back via the page's `read*FromControls()` function.
4. **Write a Playwright test** — new feature gets at least: presence assertion, defaults-match-pipeline-behavior assertion, edit-and-PUT-round-trip assertion, no-console-errors assertion.
5. **Run the full sweep** — `./run_all_tests.sh`. All 7 suites must pass.
6. **Update docs** — add the new keys to `docs/config-reference.md` and the relevant section of `docs/web-editor.md`.

## Caching model (read this if you touch the pipeline)

The render pipeline is split into 4 cached stages keyed by config-derived fingerprints:

1. `terrain_dem` — SRTM mosaic, crop, downsample
2. `terrain_styled` — water mask, hillshade, base colors, noise, urbanization, regional tints, paper, vignette
3. `canvas_with_roads` — paper-bordered canvas with roads drawn
4. `final` — settlements, regions, edge indicators, water labels, barriers, buffer zones, contamination, all furniture

A stage's cache key includes its inputs from the YAML. **Bug to watch for:** if you add a new YAML key that affects stage N's output, you must include it in stage N's cache key — otherwise the renderer returns stale cached output when the user changes only that key. The `stage_key()` helper in `src/cache.py` handles fingerprinting.

## Pull requests

- One feature/fix per PR. Small PRs land faster.
- Include the test output (the `PASSED: N / FAILED: 0` summary lines).
- Include a screenshot or before/after if your change is visual.
- Link to any GitHub issue you're closing.

## Reporting bugs

When reporting a render bug, please include:

- The config that triggered it (or a minimal repro config).
- The CLI command or editor action that caused it.
- The actual vs expected behavior.
- A screenshot if it's visual.
- Output of `pip list | grep -E 'numpy|scipy|Pillow|PyYAML|Flask|playwright'`.

For data-fetch bugs (Overpass timeouts, SRTM 404s), include the cache file paths under `cache/overpass/` and `cache/srtm/` and whether deleting them resolves the issue.

## License

By contributing, you agree your contributions will be licensed under the [MIT License](LICENSE).
