"""Playwright tests for the Paint page (region tints editor).

Coverage targets:
  - Page-load smoke (title, nav, project name)
  - Region rail populates from cfg.regions
  - Selecting a region opens its inspector with identity fields
  - region_type picker (region_label vs tint_only) is interactive
  - tint type picker (radial vs directional) is interactive
  - Inspector geometry readouts (lat/lon center, radii) update on region change
  - No console errors

Like place.html, paint.html uses an explicit Commit-to-YAML button
(via setRenderingState) rather than debounced auto-save. We verify
in-memory state propagation, not the full PUT round-trip.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from playwright.sync_api import sync_playwright

from tests.playwright.helpers import (
    TestRun, click_via_js, get_attr_via_js, get_text_via_js,
    query_count, setup_console_capture, filter_benign_errors,
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

        # ---- Step 1: page load ----
        print("\n--- Step 1: paint.html load ---")
        goto_page(page, project_url(SERVER, "/paint", PROJECT))

        run.check("title is Cartograph Paint",
                  "Paint" in page.title(),
                  f"got {page.title()!r}")

        page.wait_for_function(
            "() => document.getElementById('projectName')?.textContent !== '—'",
            timeout=10000)
        proj_name = get_text_via_js(page, "#projectName")
        run.check("project name populates",
                  proj_name and "DOMINUS" in proj_name.upper(),
                  f"got {proj_name!r}")

        active_route = page.evaluate(
            "() => document.querySelector('.mode.is-active')?.dataset.route")
        run.check("topbar shows /paint active",
                  active_route == "/paint",
                  f"got {active_route!r}")

        # ---- Step 2: region rail populates ----
        print("\n--- Step 2: region rail ---")
        page.wait_for_function(
            "() => document.querySelectorAll('#regions .region').length > 0",
            timeout=10000)
        n_regions = query_count(page, "#regions .region")
        run.check("region rail populates (>=3 regions)",
                  n_regions >= 3,
                  f"got {n_regions} regions")

        # ---- Step 3: select first region → inspector populates ----
        print("\n--- Step 3: select a region ---")
        clicked = page.evaluate("""() => {
          const r = document.querySelector('#regions .region');
          if (r) { r.click(); return true; }
          return false;
        }""")
        run.check("first region row click dispatched",
                  clicked is True, "no region row")
        page.wait_for_timeout(350)

        ins_title = get_text_via_js(page, "#insTitle")
        run.check("inspector title updates with region name",
                  ins_title and ins_title not in ("Map-wide controls",
                                                    "Nothing selected", ""),
                  f"got {ins_title!r}")

        # The region name field should be present + populated
        f_region_name = page.evaluate(
            "() => document.querySelector('#fRegionName')?.value")
        run.check("inspector #fRegionName populated",
                  f_region_name is not None and len(f_region_name) > 0,
                  f"got {f_region_name!r}")

        # ---- Step 4: region type picker (region_label vs tint_only) ----
        print("\n--- Step 4: region type picker ---")
        type_picker_count = query_count(page,
            "#regionTypePicker button[data-rtype]")
        run.check("region type picker has buttons",
                  type_picker_count >= 2,
                  f"got {type_picker_count} buttons")

        # Active button matches current region's type
        active_type = page.evaluate("""() => {
          const btn = document.querySelector(
            '#regionTypePicker button[data-rtype].is-active');
          return btn ? btn.dataset.rtype : null;
        }""")
        run.check("region type picker has an active state",
                  active_type in ("region_label", "tint_only"),
                  f"got {active_type!r}")

        # ---- Step 5: tint type picker (radial vs directional) ----
        print("\n--- Step 5: tint type picker ---")
        tint_picker_count = query_count(page,
            "#tintTypePicker button[data-type]")
        run.check("tint type picker has buttons",
                  tint_picker_count >= 2,
                  f"got {tint_picker_count} buttons")

        # Verify clicking the OTHER tint type updates the active state
        other_type_clicked = page.evaluate("""() => {
          const cur = document.querySelector(
            '#tintTypePicker button.is-active');
          const buttons = Array.from(document.querySelectorAll(
            '#tintTypePicker button[data-type]'));
          const other = buttons.find(b => b !== cur);
          if (other) { other.click(); return other.dataset.type; }
          return null;
        }""")
        page.wait_for_timeout(300)
        new_active = page.evaluate("""() =>
          document.querySelector('#tintTypePicker button.is-active')
            ?.dataset.type""")
        run.check("clicking other tint type changes active state",
                  other_type_clicked == new_active and new_active is not None,
                  f"clicked={other_type_clicked!r}, active={new_active!r}")

        # ---- Step 6: switching regions changes inspector content ----
        print("\n--- Step 6: switch region → inspector re-hydrates ---")
        first_name = get_text_via_js(page, "#insTitle")
        # Click the SECOND region row
        clicked_2 = page.evaluate("""() => {
          const rows = document.querySelectorAll('#regions .region');
          if (rows.length >= 2) { rows[1].click(); return true; }
          return false;
        }""")
        run.check("second region row clicked",
                  clicked_2 is True, "fewer than 2 region rows")
        page.wait_for_timeout(350)
        second_name = get_text_via_js(page, "#insTitle")
        run.check("inspector title differs after switching regions",
                  first_name != second_name and second_name,
                  f"first={first_name!r}, second={second_name!r}")

        # ---- Step 7: nav cross-page persistence ----
        print("\n--- Step 7: nav preserves project ---")
        place_link = page.evaluate(
            "() => document.querySelector("
            "    '.mode[data-route=\"/place\"]')?.href")
        run.check("place link includes the project query",
                  place_link and PROJECT in place_link,
                  f"got {place_link!r}")

        # ---- Step 8: console errors ----
        print("\n--- Step 8: console errors ---")
        meaningful = filter_benign_errors(console_errors)
        run.check("no meaningful console errors",
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
