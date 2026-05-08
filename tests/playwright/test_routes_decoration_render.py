"""Combined Playwright tests for the smaller editor pages.

These three pages have the smallest surface area and the lowest
regression risk. We bundle them together to avoid three test files
each duplicating the page-load smoke pattern.

Coverage targets per page:
  - routes.html:      page-load + has roads inspector
  - decoration.html:  page-load + frame/legend inspector
  - render.html:      page-load + render-trigger UI

Each page section runs in its own browser context to keep state isolated.
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


def _check_page_load(run: TestRun, page, page_path: str, page_label: str,
                     expected_route: str) -> None:
    """Common page-load smoke for a page that's expected to render."""
    goto_page(page, project_url(SERVER, page_path, PROJECT),
              wait_for_selector=".inspector")
    run.check(f"{page_label}: title contains label",
              page_label.lower() in page.title().lower(),
              f"got {page.title()!r}")
    active = page.evaluate(
        "() => document.querySelector('.mode.is-active')?.dataset.route")
    run.check(f"{page_label}: nav active is {expected_route}",
              active == expected_route, f"got {active!r}")
    return None


def _run_routes(run: TestRun, browser) -> None:
    print("\n=== routes.html ===")
    context = browser.new_context(viewport={"width": 1600, "height": 1000})
    page = context.new_page()
    errors = setup_console_capture(page)
    _check_page_load(run, page, "/routes", "Routes", "/routes")
    # Inspector should have its title
    title = get_text_via_js(page, ".inspector__title")
    run.check("routes inspector title is 'Roads'",
              title == "Roads", f"got {title!r}")
    meaningful = filter_benign_errors(errors)
    run.check("routes: no meaningful console errors",
              len(meaningful) == 0, f"errors: {meaningful}")
    context.close()


def _run_decoration(run: TestRun, browser) -> None:
    print("\n=== decoration.html ===")
    context = browser.new_context(viewport={"width": 1600, "height": 1000})
    page = context.new_page()
    errors = setup_console_capture(page)
    _check_page_load(run, page, "/decoration", "Decoration", "/decoration")
    title = get_text_via_js(page, ".inspector__title")
    run.check("decoration inspector title set",
              title and len(title) > 0, f"got {title!r}")
    meaningful = filter_benign_errors(errors)
    run.check("decoration: no meaningful console errors",
              len(meaningful) == 0, f"errors: {meaningful}")
    context.close()


def _run_render(run: TestRun, browser) -> None:
    print("\n=== render.html ===")
    context = browser.new_context(viewport={"width": 1600, "height": 1000})
    page = context.new_page()
    errors = setup_console_capture(page)
    _check_page_load(run, page, "/render", "Render", "/render")
    title = get_text_via_js(page, ".inspector__title")
    run.check("render inspector title set",
              title and len(title) > 0, f"got {title!r}")
    meaningful = filter_benign_errors(errors)
    run.check("render: no meaningful console errors",
              len(meaningful) == 0, f"errors: {meaningful}")
    context.close()


def _run(run: TestRun) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            _run_routes(run, browser)
            _run_decoration(run, browser)
            _run_render(run, browser)
        finally:
            browser.close()


def main() -> int:
    run = TestRun()
    with with_config_backup(PROJECT):
        _run(run)
    run.summary_and_exit()
    return 0


if __name__ == "__main__":
    main()
