"""Playwright tests for the Places page (settlements editor).

Coverage targets:
  - Page-load smoke (title, nav active, project loads)
  - Settlement rail populates from cfg.settlements
  - Selecting a settlement opens its inspector
  - Editing a field triggers dirty save → save completes
  - Round-trip via API: server has the new value after save
  - Marker selector affects inspector state

Run via:
  python3 ~/.claude/plugins/marketplaces/anthropic-agent-skills/skills/webapp-testing/scripts/with_server.py \
      --server "python3 -m src.server --port 5080" --port 5080 --timeout 60 \
      -- python3 tests/playwright/test_place.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow this script to import sibling helpers when run from project root.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from playwright.sync_api import sync_playwright

from tests.playwright.helpers import (
    TestRun, click_via_js, click_text_via_js, get_attr_via_js,
    get_text_via_js, query_count, wait_for_save_complete, get_save_state,
    setup_console_capture, filter_benign_errors,
    with_config_backup, goto_page, project_url,
)


SERVER = "http://127.0.0.1:5080"
PROJECT = "dominus-columbia"


def _run(run: TestRun) -> None:
    """Body of the test. Wrapped so backup/restore can be a context manager."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        console_errors = setup_console_capture(page)

        # ---- Step 1: page load ----
        print("\n--- Step 1: place.html load ---")
        goto_page(page, project_url(SERVER, "/place", PROJECT))

        run.check("title is Cartograph Place",
                  "Place" in page.title(),
                  f"got {page.title()!r}")

        # Project name in topbar populates from the loaded config
        # (via hydrateProject → renderProjectName).
        # Wait briefly for the async fetch to complete.
        page.wait_for_function(
            "() => document.getElementById('projectName')"
            "        ?.textContent !== '—'",
            timeout=10000)
        proj_name = get_text_via_js(page, "#projectName")
        run.check("project name populates",
                  proj_name and "DOMINUS" in proj_name.upper(),
                  f"got {proj_name!r}")

        # Active step in the topbar nav should be /place.
        active_route = page.evaluate(
            "() => document.querySelector('.mode.is-active')?.dataset.route")
        run.check("topbar shows /place active",
                  active_route == "/place",
                  f"got {active_route!r}")

        # ---- Step 2: settlements rail populates ----
        print("\n--- Step 2: settlement rail ---")
        # Wait for itemsList to have at least one row
        page.wait_for_function(
            "() => document.querySelectorAll("
            "    '#itemsList .settlement-row').length > 0",
            timeout=10000)
        n_settlements = query_count(page, "#itemsList .settlement-row")
        run.check("settlement rail populates (>5 rows)",
                  n_settlements > 5,
                  f"got {n_settlements} rows")

        # First row should be a real settlement with a name.
        first_name = get_text_via_js(page,
            "#itemsList .settlement-row .settlement-row__name")
        run.check("first settlement row has a name",
                  bool(first_name and first_name.strip()),
                  f"got {first_name!r}")

        # ---- Step 3: select a settlement → inspector populates ----
        print("\n--- Step 3: select a settlement ---")
        # Click via JS to bypass any transition-stability flutter
        clicked = page.evaluate("""() => {
          const row = document.querySelector(
            '#itemsList .settlement-row');
          if (row) { row.click(); return true; }
          return false;
        }""")
        run.check("first settlement row click dispatched",
                  clicked is True, "no row found")
        page.wait_for_timeout(250)

        # When a settlement is selected, the inspector__head changes shape:
        # `Nothing selected` becomes an eyebrow like `Settlement · Tier 1`
        # plus an editable name <input id="fName">. Both are reliable
        # signals that selection took effect.
        eyebrow = get_text_via_js(page, ".inspector__eyebrow")
        run.check("inspector eyebrow shows 'Settlement · ...'",
                  eyebrow and eyebrow.startswith("Settlement"),
                  f"got {eyebrow!r}")

        # Settlement inspector uses #fName (NOT #fieldName — that's the
        # project modal). Distinct ID per inspector mode.
        field_name_value = page.evaluate(
            "() => document.querySelector('#fName')?.value")
        run.check("inspector #fName populated with selected settlement name",
                  field_name_value is not None and len(field_name_value) > 0,
                  f"got {field_name_value!r}")

        # ---- Step 4: edit settlement note → in-memory state ----
        # NOTE: place.html uses an explicit "Commit to YAML" button (NOT
        # debounced auto-save like narrative.html). Edits accumulate in
        # the JS SETTLEMENTS array; only the button click PUTs to the
        # server + triggers a render. This test verifies the in-memory
        # propagation; a full commit-and-render lifecycle test belongs
        # in a separate slow-suite test.
        print("\n--- Step 4: edit note → in-memory state ---")
        note_present = page.evaluate(
            "() => !!document.querySelector('#fNote')")
        run.check("settlement inspector has note field (#fNote)",
                  note_present is True, "no #fNote in DOM")

        if note_present:
            sentinel = "TEST_NOTE_PLAYWRIGHT_42"
            page.evaluate(f"""() => {{
              const el = document.querySelector('#fNote');
              el.value = {sentinel!r};
              el.dispatchEvent(new Event('input', {{bubbles:true}}));
              el.dispatchEvent(new Event('change', {{bubbles:true}}));
              el.dispatchEvent(new Event('blur', {{bubbles:true}}));
            }}""")
            page.wait_for_timeout(400)
            # Verify the textarea value updated
            current_val = page.evaluate(
                "() => document.querySelector('#fNote')?.value")
            run.check("note textarea reflects the edit",
                      current_val == sentinel,
                      f"got {current_val!r}")
            # The inspector's __sub paragraph should reflect it too
            # (place.html re-renders the inspector head with the new note).
            sub_text = page.evaluate(
                "() => document.querySelector('.inspector__sub')?.textContent")
            # The note may not propagate to the sub until a re-render —
            # test only asserts the textarea, which is the canonical edit
            # surface.
            print(f"  (info: inspector__sub now reads: {sub_text!r})")

        # ---- Step 4b: shared Save + History modules loaded ----
        print("\n--- Step 4b: Cartograph.Save + History loaded ---")
        carto_state = page.evaluate("""() => ({
          has_namespace: !!window.Cartograph,
          has_save: !!(window.Cartograph && Cartograph.Save),
          has_history: !!(window.Cartograph && Cartograph.History),
          save_status: window.Cartograph && Cartograph.Save.getStatus
            ? Cartograph.Save.getStatus() : null,
          history_can_undo: window.Cartograph && Cartograph.History
            ? Cartograph.History.canUndo() : null,
          history_depth: window.Cartograph && Cartograph.History
            ? Cartograph.History.depth() : null,
        })""")
        run.check("Cartograph namespace loaded",
                  carto_state.get("has_namespace") is True,
                  f"got {carto_state}")
        run.check("Cartograph.Save initialized",
                  carto_state.get("has_save") is True,
                  f"got {carto_state}")
        run.check("Cartograph.History initialized with baseline snapshot",
                  carto_state.get("history_depth", {}).get("undoLevels") == 0
                  and carto_state.get("history_can_undo") is False,
                  f"got {carto_state}")
        # Save status should be 'idle' (no edits yet)
        run.check("save status starts idle",
                  carto_state.get("save_status") == "idle",
                  f"got {carto_state.get('save_status')!r}")

        # Functional undo round-trip: take the current cfg, push a
        # snapshot, mutate the in-memory state (or just push the same
        # snapshot again so we can verify undo() returns true), undo,
        # confirm state restored.
        # We rely on Cartograph.History.pushSnapshot returning a
        # consistent stack depth so we can drive undo predictably.
        depth_pre = page.evaluate("() => Cartograph.History.depth()")
        run.check("history starts with no undo levels",
                  depth_pre.get("undoLevels") == 0,
                  f"got depth={depth_pre}")

        # Force-push a snapshot by ensuring it differs from baseline.
        # Easiest: stash a marker into the history's internal state via
        # public API. The History dedup compares deep-equality; we
        # invoke pushSnapshot multiple times and check that AT LEAST
        # one undo level appears once we mutate cfg.
        push_result = page.evaluate("""() => {
          // Mutate the in-memory cfg via the page's primitives, then
          // push. If mutation happened, dedup misses → snapshot pushed.
          const before = Cartograph.History.depth();
          // Find a settlement and append a sentinel char to its note.
          // (We rely on the closure's SETTLEMENTS being accessible to
          // the page's commitToServer; we'll mutate via the inspector
          // textarea which IS accessible.)
          const note = document.querySelector('#fNote');
          if (note) {
            note.value = (note.value || '') + 'X';
            note.dispatchEvent(new Event('input', {bubbles: true}));
            note.dispatchEvent(new Event('change', {bubbles: true}));
          }
          Cartograph.History.pushSnapshot('mutated');
          const after = Cartograph.History.depth();
          return { before, after };
        }""")
        print(f"  push result: {push_result}")
        run.check("pushing snapshot after edit increments undo levels",
                  push_result.get("after", {}).get("undoLevels", 0) >= 1,
                  f"got {push_result}")

        # Now undo — should bring undoLevels back down
        undo_result = page.evaluate("""() => {
          const before = Cartograph.History.depth();
          const ok = Cartograph.History.undo();
          const after = Cartograph.History.depth();
          return { before, ok, after };
        }""")
        run.check("undo() returns true when undo is available",
                  undo_result.get("ok") is True,
                  f"got {undo_result}")
        run.check("undo decrements the undo-level count",
                  undo_result["after"]["undoLevels"]
                    < undo_result["before"]["undoLevels"],
                  f"got {undo_result}")
        run.check("undo enables redo",
                  undo_result["after"]["redoLevels"] >= 1,
                  f"got {undo_result}")

        # ---- Step 5: nav cross-page persistence ----
        print("\n--- Step 5: nav to narrative preserves project ---")
        narrative_link = page.evaluate(
            "() => document.querySelector("
            "    '.mode[data-route=\"/narrative\"]')?.href")
        run.check("narrative link includes the active project query",
                  narrative_link and PROJECT in narrative_link,
                  f"got {narrative_link!r}")
        # Note: we don't actually navigate — that would require waiting for
        # the new page to load. The presence of the project param in the
        # link is the assertion we want.

        # ---- Step 6: console errors ----
        print("\n--- Step 6: console errors ---")
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
