"""Capture screenshots of every editor page for docs/screenshots/.

Drives a real Chromium against the local Flask server and saves a PNG
per editor page at a consistent 1600×1000 viewport. Each shot waits for
the page's primary content to be visible before capturing.

Usage:
  python -m src.server &                                # start server
  python tests/playwright/_capture_screenshots.py      # capture

Output: docs/screenshots/{setup,terrain,places,routes,narrative,decoration,render}.png

The leading underscore on the filename keeps pytest from collecting this
as a test (it's a one-off utility script).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from playwright.sync_api import sync_playwright


SERVER = "http://127.0.0.1:5080"
PROJECT = "dominus-columbia"
OUT_DIR = Path(__file__).parent.parent.parent / "docs" / "screenshots"

# (route, output filename, content selector to wait for)
PAGES = [
    ("/setup",      "setup.png",      "#fName"),
    ("/paint",      "terrain.png",    "#rngSeaLevel"),
    ("/place",      "places.png",     "#itemsList"),
    ("/routes",     "routes.png",     ".inspector"),
    ("/narrative",  "narrative.png",  "#layersRail, #layerList, .layer-row"),
    ("/decoration", "decoration.png", "#chkCompassEnabled"),
    ("/render",     "render.png",     "#presetPicker"),
]


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()

        for route, fname, sel in PAGES:
            url = f"{SERVER}{route}?project={PROJECT}"
            print(f"  capturing {route:13s} → {fname}")
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            try:
                # Wait for the primary content to render. The narrative page
                # has a slow barrier-paths fetch we don't want to wait on,
                # so the timeout is short and a missing selector falls back
                # to capturing whatever's already visible.
                page.wait_for_selector(sel, timeout=8000)
            except Exception:
                pass
            # Brief settle for any fade-in transitions.
            page.wait_for_timeout(800)
            out = OUT_DIR / fname
            page.screenshot(path=str(out), full_page=False)
            size = out.stat().st_size
            print(f"    saved {fname} ({size:,} bytes)")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
