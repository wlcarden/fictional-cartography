"""Playwright tests for the Setup page (bounds, sea level, aspect ratio).

Coverage targets:
  - Page-load smoke
  - Atlas title populates from cfg.name
  - Bounds inputs (lat/lon N/S/W/E) populate from cfg.bounds
  - Sea level input populates from cfg.terrain.sea_level_rise
  - Aspect ratio dropdown populates
  - Editing a value updates the input (in-memory at least)
  - No console errors

Setup uses the same Commit-to-YAML pattern as paint/place.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from playwright.sync_api import sync_playwright

from tests.playwright.helpers import (
    TestRun, get_text_via_js, query_count,
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

        # ---- Step 1: page load ----
        print("\n--- Step 1: setup.html load ---")
        # setup.html uses .inspector (no .rail layout); pass an explicit
        # sentinel selector to goto_page.
        goto_page(page, project_url(SERVER, "/setup", PROJECT),
                  wait_for_selector=".inspector")

        run.check("title is Cartograph Setup",
                  "Setup" in page.title(),
                  f"got {page.title()!r}")

        active_route = page.evaluate(
            "() => document.querySelector('.mode.is-active')?.dataset.route")
        run.check("topbar shows /setup active",
                  active_route == "/setup",
                  f"got {active_route!r}")

        # ---- Step 2: atlas title (h2) populates ----
        print("\n--- Step 2: atlas title from config ---")
        page.wait_for_function(
            "() => { const t = document.getElementById('atlasTitle');"
            " return t && t.textContent && t.textContent !== '—'; }",
            timeout=10000)
        atlas_title = get_text_via_js(page, "#atlasTitle")
        run.check("atlas title populates from cfg",
                  atlas_title and "DOMINUS" in atlas_title.upper(),
                  f"got {atlas_title!r}")

        # ---- Step 3: bounds inputs populated ----
        print("\n--- Step 3: bounds inputs from config ---")
        bounds = page.evaluate("""() => ({
          lat_n: document.querySelector('#fLatN')?.value,
          lat_s: document.querySelector('#fLatS')?.value,
          lon_w: document.querySelector('#fLonW')?.value,
          lon_e: document.querySelector('#fLonE')?.value,
        })""")
        run.check("lat_n input populated",
                  bounds.get("lat_n") and float(bounds["lat_n"]) > 30,
                  f"got {bounds!r}")
        run.check("lat_s input populated",
                  bounds.get("lat_s") and float(bounds["lat_s"]) > 30,
                  f"got {bounds!r}")
        run.check("lon_w input populated",
                  bounds.get("lon_w") and float(bounds["lon_w"]) < 0,
                  f"got {bounds!r}")
        run.check("lon_e input populated",
                  bounds.get("lon_e") and float(bounds["lon_e"]) < 0,
                  f"got {bounds!r}")

        # Verify N > S and W < E (basic invariants for valid bounds)
        run.check("lat_n > lat_s",
                  float(bounds["lat_n"]) > float(bounds["lat_s"]),
                  f"got {bounds!r}")
        run.check("lon_w < lon_e",
                  float(bounds["lon_w"]) < float(bounds["lon_e"]),
                  f"got {bounds!r}")

        # ---- Step 4: aspect ratio dropdown ----
        print("\n--- Step 4: aspect ratio dropdown ---")
        n_aspect_opts = query_count(page, "#fAspect option")
        run.check("aspect ratio dropdown has options",
                  n_aspect_opts >= 2,
                  f"got {n_aspect_opts} options")
        aspect_value = page.evaluate(
            "() => document.querySelector('#fAspect')?.value")
        run.check("aspect ratio has a current value",
                  aspect_value, f"got {aspect_value!r}")

        # ---- Step 5: sea level input ----
        print("\n--- Step 5: sea level input ---")
        sea_level = page.evaluate(
            "() => document.querySelector('#fSeaLevel')?.value")
        run.check("sea level input populated",
                  sea_level is not None and sea_level != "",
                  f"got {sea_level!r}")
        # Should be numeric
        try:
            sl = float(sea_level)
            run.check("sea level is numeric in valid range",
                      0 <= sl <= 200,
                      f"got {sl}")
        except (ValueError, TypeError):
            run.check("sea level is numeric in valid range",
                      False,
                      f"got non-numeric {sea_level!r}")

        # ---- Step 6: editing bounds updates input ----
        print("\n--- Step 6: edit lat_n value ---")
        page.evaluate("""() => {
          const el = document.querySelector('#fLatN');
          el.value = '45.500';
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
        }""")
        page.wait_for_timeout(200)
        new_val = page.evaluate(
            "() => document.querySelector('#fLatN')?.value")
        run.check("lat_n input accepts edits",
                  new_val == "45.500" or float(new_val) == 45.5,
                  f"got {new_val!r}")

        # ---- Step 7: nav preserves project ----
        print("\n--- Step 7: nav preserves project ---")
        next_link = page.evaluate(
            "() => document.querySelector("
            "    '.mode[data-route=\"/paint\"]')?.href")
        run.check("paint link includes project query",
                  next_link and PROJECT in next_link,
                  f"got {next_link!r}")

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
