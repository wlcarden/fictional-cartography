"""Shared utilities for Playwright-based editor tests.

These helpers exist because Playwright's locator-level interactions
fight CSS transitions and `opacity:0` elements (which we use heavily
for fade-in animations + pickmode). The test_narrative_pickmode.py
suite hit and worked around these issues iteratively; this module
codifies the workarounds so future tests don't repeat the cycle.

Usage:
    from tests.playwright.helpers import (
        TestRun, click_via_js, get_attr_via_js,
        wait_for_save_complete, with_config_backup,
        setup_console_capture, BENIGN_CONSOLE_PATTERNS,
    )

    def main():
        run = TestRun()
        with with_config_backup("dominus-columbia"):
            with sync_playwright() as p:
                ...
                run.check("title is correct", title == "Expected", title)
        run.summary_and_exit()
"""
from __future__ import annotations

import contextlib
import shutil
import sys
from pathlib import Path
from typing import Any


# ----------------------------------------------------------------------
#  Test run accumulator — replaces ad-hoc PASSES/FAILS lists
# ----------------------------------------------------------------------

class TestRun:
    """Accumulator for assertion outcomes across a Playwright script.

    Pattern: tests call `.check(name, condition, detail="")` for each
    assertion. At the end, `.summary_and_exit()` prints results and
    exits with status 0 (pass) or 1 (fail). Diagnostic detail is
    captured per-failure for easy root-cause inspection.
    """

    def __init__(self):
        self.passes: list[str] = []
        self.fails: list[tuple[str, str]] = []

    def check(self, name: str, condition: bool, detail: Any = "") -> bool:
        """Record one assertion. Returns the boolean condition for chaining."""
        if condition:
            self.passes.append(name)
            print(f"  PASS  {name}")
        else:
            self.fails.append((name, str(detail)))
            print(f"  FAIL  {name} :: {detail}")
        return bool(condition)

    def summary_and_exit(self) -> None:
        print()
        print("=" * 60)
        print(f"PASSED: {len(self.passes)}")
        print(f"FAILED: {len(self.fails)}")
        if self.fails:
            print("\nFailures:")
            for name, detail in self.fails:
                print(f"  - {name}")
                if detail:
                    print(f"      {detail}")
            sys.exit(1)
        print("\nAll checks passed.")
        sys.exit(0)


# ----------------------------------------------------------------------
#  JS-evaluated fallbacks for Playwright's stability heuristics
# ----------------------------------------------------------------------

def click_via_js(page, selector: str) -> bool:
    """Click an element via document.querySelector(...).click().

    Bypasses Playwright's transition-stability check, which fights any
    element with hover transitions or animated parents. Returns True
    if a match was found and clicked, False otherwise.

    Example: click_via_js(page, '#saveBtn')
    """
    return page.evaluate(
        f"() => {{ const el = document.querySelector({_jsstr(selector)}); "
        f"if (el) {{ el.click(); return true; }} return false; }}"
    )


def click_text_via_js(page, parent_selector: str, text: str) -> bool:
    """Find a child of `parent_selector` whose textContent contains `text`,
    then click it. Useful for layer rows / list items.

    Example: click_text_via_js(page, '.layer-row', 'The Wall')
    """
    return page.evaluate(
        f"() => {{ const target = Array.from(document.querySelectorAll("
        f"  {_jsstr(parent_selector)})).find(r => "
        f"  r.textContent.includes({_jsstr(text)})); "
        f"if (target) {{ target.click(); return true; }} return false; }}"
    )


def get_attr_via_js(page, selector: str, attr: str) -> str | None:
    """Read an attribute via evaluate — bypasses Playwright's visibility
    filter that times out on opacity:0 elements (e.g., road-bloom polylines).
    """
    return page.evaluate(
        f"() => {{ const el = document.querySelector({_jsstr(selector)}); "
        f"return el ? el.getAttribute({_jsstr(attr)}) : null; }}"
    )


def get_text_via_js(page, selector: str) -> str | None:
    """Read textContent via evaluate."""
    return page.evaluate(
        f"() => {{ const el = document.querySelector({_jsstr(selector)}); "
        f"return el ? el.textContent : null; }}"
    )


def query_count(page, selector: str) -> int:
    """Count elements matching a selector via querySelectorAll."""
    return int(page.evaluate(
        f"() => document.querySelectorAll({_jsstr(selector)}).length"
    ))


def _jsstr(s: str) -> str:
    """Format a Python string for safe JS interpolation. Just JSON-encodes
    so quotes/backslashes are escaped properly."""
    import json
    return json.dumps(s)


# ----------------------------------------------------------------------
#  Save lifecycle
# ----------------------------------------------------------------------

def wait_for_save_complete(page, timeout: int = 5000) -> bool:
    """Wait until the save-status pill returns to 'idle' or 'saved'.

    The save-status auto-clears 1.2s after a successful PUT in the
    current implementation, so this normally completes in < 2s.
    Returns True if the pill settled, False on timeout.
    """
    try:
        page.wait_for_function(
            "() => { const ss = document.querySelector('#saveStatus');"
            " if (!ss) return false;"
            " const s = ss.dataset.state;"
            " return s === 'saved' || s === 'idle'; }",
            timeout=timeout,
        )
        return True
    except Exception:
        return False


def get_save_state(page) -> str | None:
    """Snapshot of #saveStatus[data-state]."""
    return get_attr_via_js(page, "#saveStatus", "data-state")


# ----------------------------------------------------------------------
#  Console capture
# ----------------------------------------------------------------------

# Patterns that count as benign noise — filtered out of the final
# error assertion. Add to this list judiciously.
BENIGN_CONSOLE_PATTERNS = (
    "favicon",          # /favicon.ico 404 is fine
    "404",              # specifically the favicon 404; other 404s caught by "fetch failed"
    "fonts.gstatic",    # Google Fonts CDN occasionally errors
    "fonts.googleapis", # ditto
)


def setup_console_capture(page) -> list[str]:
    """Wire console-error and pageerror listeners. Returns a list that
    will be appended to as errors occur during the test.
    """
    errors: list[str] = []
    page.on("console", lambda msg:
        errors.append(msg.text)
        if msg.type in ("error",) else None)
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    return errors


def filter_benign_errors(errors: list[str]) -> list[str]:
    """Drop benign-noise errors. Returns the remaining 'meaningful' list."""
    return [e for e in errors
             if not any(p in e.lower() for p in BENIGN_CONSOLE_PATTERNS)]


# ----------------------------------------------------------------------
#  Config backup/restore — non-destructive testing
# ----------------------------------------------------------------------

@contextlib.contextmanager
def with_config_backup(config_name: str, backup_dir: str = "/tmp"):
    """Backup config/<name>.yaml at __enter__; restore at __exit__.

    Works for both pass and fail paths so a failed test never leaves
    the YAML in an edited state. The backup goes to /tmp (or any
    user-specified dir).
    """
    src = Path("config") / f"{config_name}.yaml"
    bak = Path(backup_dir) / f"{config_name}.yaml.bak"
    if not src.exists():
        raise FileNotFoundError(f"config not found: {src}")
    shutil.copy2(src, bak)
    try:
        yield
    finally:
        shutil.copy2(bak, src)
        print(f"\n[restored {src} from backup]")


# ----------------------------------------------------------------------
#  Common navigation/setup
# ----------------------------------------------------------------------

def goto_page(page, url: str, wait_for_selector: str = ".rail",
                timeout: int = 20000) -> None:
    """Navigate + wait for a sentinel selector that signals page-ready.

    Uses 'domcontentloaded' instead of 'networkidle' because the
    background road-vector fetch keeps the network non-idle for
    several seconds — the page is interactive long before then.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    page.wait_for_selector(wait_for_selector, timeout=timeout)


def project_url(server: str, page_path: str, project: str) -> str:
    """Build a URL like 'http://127.0.0.1:5080/place?project=dominus-columbia'."""
    return f"{server.rstrip('/')}{page_path}?project={project}"
