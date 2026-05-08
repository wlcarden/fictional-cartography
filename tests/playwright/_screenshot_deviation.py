"""Capture a screenshot of the deviation visualization for visual review."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from playwright.sync_api import sync_playwright
from tests.playwright.helpers import goto_page, project_url

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_context(viewport={"width": 1600, "height": 1000}).new_page()
    goto_page(page, project_url("http://127.0.0.1:5080", "/narrative",
                                  "dominus-columbia"))
    page.wait_for_function(
        "() => document.querySelectorAll('.layer-row').length >= 5",
        timeout=20000)
    page.wait_for_function(
        "() => window.__cartoState && window.__cartoState().has_paths",
        timeout=30000)
    # Click Patrol Line
    page.evaluate("""() => {
      for (const r of document.querySelectorAll('.layer-row')) {
        if (r.textContent.includes('Patrol Line')) { r.click(); break; }
      }
    }""")
    page.wait_for_timeout(700)
    page.screenshot(path="/tmp/deviation_viz.png")
    print("saved /tmp/deviation_viz.png")
    browser.close()
