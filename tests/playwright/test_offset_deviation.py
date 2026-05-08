"""Playwright tests for the astar_offset deviation visualization.

When the user selects an astar_offset barrier in the rail, the canvas
overlays the reference path (ghost), the offset target curve (dashed),
and the max_deviation band (translucent polygon). The inspector exposes
sliders for offset, direction, and the four corridor penalties, plus a
"Compute Path" button that re-runs A* server-side and updates the
in-memory STATE.paths.

These tests verify:
  - Selecting an astar_offset layer triggers the deviation overlay
  - Adjusting max_deviation tightens/loosens the band (DOM-level)
  - The Compute Path button is wired
  - No JS errors during slider interactions
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from playwright.sync_api import sync_playwright

from tests.playwright.helpers import (
    TestRun, get_text_via_js, get_attr_via_js, query_count,
    setup_console_capture, filter_benign_errors,
    with_config_backup, goto_page, project_url,
)


SERVER = "http://127.0.0.1:5080"
PROJECT = "dominus-columbia"


def _run(run: TestRun) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        console_errors = setup_console_capture(page)

        # ---- Step 1: load narrative page ----
        print("\n--- Step 1: narrative.html load ---")
        goto_page(page, project_url(SERVER, "/narrative", PROJECT))
        page.wait_for_function(
            "() => document.querySelectorAll('.layer-row').length >= 5",
            timeout=20000)
        # Wait for the barrier-paths fetch to finish — without this,
        # STATE.paths is null and the offset visualization (which needs
        # the reference barrier path) can't render. The narrative page
        # exposes __cartoState() that reports has_paths.
        page.wait_for_function(
            "() => window.__cartoState && window.__cartoState().has_paths",
            timeout=30000)

        # ---- Step 2: NO offset overlay when no astar_offset layer is selected ----
        print("\n--- Step 2: no overlay before offset barrier selected ---")
        # Select The Wall (astar_road_network, not astar_offset)
        clicked = page.evaluate("""() => {
          const rows = document.querySelectorAll('.layer-row');
          for (const r of rows) {
            if (r.textContent.includes('The Wall')) { r.click(); return true; }
          }
          return false;
        }""")
        run.check("Wall layer found and clicked",
                  clicked is True, "no Wall row")
        page.wait_for_timeout(350)
        n_offset = query_count(page, "#overlaySvg g.offset-deviation")
        run.check("no offset-deviation overlay when Wall selected",
                  n_offset == 0,
                  f"got {n_offset} offset overlays")

        # ---- Step 3: select The Patrol Line → offset overlay appears ----
        print("\n--- Step 3: Patrol Line selection triggers overlay ---")
        clicked2 = page.evaluate("""() => {
          const rows = document.querySelectorAll('.layer-row');
          for (const r of rows) {
            if (r.textContent.includes('Patrol Line')) {
              r.click(); return true;
            }
          }
          return false;
        }""")
        run.check("Patrol Line layer found and clicked",
                  clicked2 is True, "no Patrol Line row")
        page.wait_for_timeout(600)

        # Diagnostic — probe the in-memory state to see what's selected
        diag = page.evaluate("""() => {
          const probe = window.__cartoState ? window.__cartoState() : null;
          // Inspect the inspector to confirm Patrol Line is selected
          const insTitle = document.querySelector('#insTitle')?.textContent;
          const insBody = document.querySelector('#insBody');
          const offsetBtn = document.getElementById('computeOffsetBtn');
          const overlayChildren = Array.from(
            document.querySelectorAll('#overlaySvg > g')).map(g => g.className.baseVal);
          return {
            probe,
            insTitle,
            has_offset_btn: !!offsetBtn,
            overlay_groups: overlayChildren,
          };
        }""")
        print(f"  diag: {diag}")

        n_offset = query_count(page, "#overlaySvg g.offset-deviation")
        run.check("offset-deviation overlay rendered",
                  n_offset >= 1,
                  f"got {n_offset} offset overlays")

        # The overlay contains 3 elements: band polygon, ref polyline, target polyline
        n_band = query_count(page,
            "#overlaySvg g.offset-deviation polygon")
        run.check("max-deviation band polygon present",
                  n_band == 1, f"got {n_band} band polygons")

        n_polylines = query_count(page,
            "#overlaySvg g.offset-deviation polyline")
        run.check("ghost reference + target polyline rendered",
                  n_polylines == 2,
                  f"got {n_polylines} polylines (expected 2)")

        # ---- Step 4: corridor sliders editable ----
        print("\n--- Step 4: corridor sliders editable ---")
        # Verify the corridor inputs exist
        max_dev_field = page.evaluate(
            "() => document.querySelector("
            "  '[data-path*=\"corridor.max_deviation\"]')?.value")
        run.check("max_deviation input exists + has a numeric value",
                  max_dev_field and float(max_dev_field) > 0,
                  f"got {max_dev_field!r}")

        # Capture band polygon geometry via evaluate (works around
        # Playwright's visibility filter on opacity/transition elements).
        band_before = page.evaluate("""() => {
          const p = document.querySelector(
            '#overlaySvg g.offset-deviation polygon');
          if (!p) return null;
          // Read points from the attribute. Element may have opacity-0
          // which trips Playwright's locator API but evaluate is fine.
          return p.getAttribute('points') ||
                 (p.points && p.points.numberOfItems > 0
                   ? `pts:${p.points.numberOfItems}` : null);
        }""")
        run.check("band polygon has geometry",
                  band_before is not None and len(band_before) > 4,
                  f"got {band_before!r}")

        # Edit max_deviation to a NEW value (not the current one) so the
        # band is forced to redraw with different coordinates. We compute
        # the new value as roughly 4x current so the change is visible
        # at any starting state.
        edit_diag = page.evaluate("""() => {
          const el = document.querySelector(
            '[data-path*=\"corridor.max_deviation\"]');
          if (!el) return { found: false };
          const before = parseFloat(el.value) || 0.1;
          // Pick a target that's definitively different — 0.05 if current
          // is large, 0.4 if current is small. Either way, redraw should
          // produce different points.
          const target = before > 0.2 ? 0.05 : 0.40;
          el.value = String(target);
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
          return {
            found: true,
            data_path: el.dataset.path,
            value_before: before, value_after: target,
          };
        }""")
        print(f"  edit diag: {edit_diag}")
        page.wait_for_timeout(400)
        band_after = page.evaluate("""() => {
          const p = document.querySelector(
            '#overlaySvg g.offset-deviation polygon');
          if (!p) return null;
          return p.getAttribute('points') ||
                 (p.points && p.points.numberOfItems > 0
                   ? `pts:${p.points.numberOfItems}` : null);
        }""")
        before_repr = (band_before or "")[:40]
        after_repr = (band_after or "")[:40]
        run.check("band polygon redraws after max_deviation edit",
                  band_after is not None and band_after != band_before,
                  f"before={before_repr!r}, after={after_repr!r}")

        # ---- Step 5: Compute Path button is wired ----
        print("\n--- Step 5: Compute Path button present ---")
        compute_btn = page.evaluate(
            "() => !!document.getElementById('computeOffsetBtn')")
        run.check("Compute Path button is rendered",
                  compute_btn is True, "missing #computeOffsetBtn")

        # Don't actually click it during test — A* re-run on Patrol Line
        # would take 5-10s. Verify the click handler is wired (data-barrier-idx
        # attribute presence is a good proxy).
        idx_attr = get_attr_via_js(page,
            "#computeOffsetBtn", "data-barrier-idx")
        run.check("Compute button has barrier_idx attribute",
                  idx_attr is not None and idx_attr.isdigit(),
                  f"got {idx_attr!r}")

        # ---- Step 6: switching back to non-offset clears overlay ----
        print("\n--- Step 6: deselect → overlay clears ---")
        page.evaluate("""() => {
          const rows = document.querySelectorAll('.layer-row');
          for (const r of rows) {
            if (r.textContent.includes('Submerged')) {
              r.click(); return true;
            }
          }
          return false;
        }""")
        page.wait_for_timeout(350)
        n_offset_final = query_count(page,
            "#overlaySvg g.offset-deviation")
        run.check("offset overlay cleared when non-offset layer selected",
                  n_offset_final == 0,
                  f"got {n_offset_final} (expected 0)")

        # ---- Step 7: console errors ----
        print("\n--- Step 7: console errors ---")
        meaningful = filter_benign_errors(console_errors)
        run.check("no meaningful console errors during interactions",
                  len(meaningful) == 0,
                  f"errors: {meaningful}")

        browser.close()


def main() -> int:
    run = TestRun()
    with with_config_backup(PROJECT):
        _run(run)
    run.summary_and_exit()
    return 0


if __name__ == "__main__":
    main()
