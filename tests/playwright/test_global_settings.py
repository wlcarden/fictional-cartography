"""Playwright tests for the global YAML editors closed in this round.

Covers four previously-YAML-only blocks:
  - terrain.downsample (paint.html Terrain panel — single new slider)
  - urbanization.* (paint.html Terrain panel — new fieldset)
  - cfg.colors (setup.html — named-color palette editor)
  - cfg.fonts (setup.html — per-role font picker)

Each section: presence + default-from-config + edit + commit-round-trip.

Run via:
  python3 ~/.claude/plugins/marketplaces/anthropic-agent-skills/skills/webapp-testing/scripts/with_server.py \
      --server "python3 -m src.server --port 5080" --port 5080 --timeout 60 \
      -- python3 tests/playwright/test_global_settings.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from playwright.sync_api import sync_playwright

from tests.playwright.helpers import (
    TestRun, setup_console_capture, filter_benign_errors,
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

        # ===========================================================
        #  Part 1 — paint.html: terrain.downsample + urbanization.*
        # ===========================================================
        print("\n=== paint.html: terrain extras ===")
        goto_page(page, project_url(SERVER, "/paint", PROJECT),
                  wait_for_selector=".inspector")
        page.wait_for_function(
            "() => document.getElementById('rngDownsample') "
            "  && document.getElementById('chkUrbanEnabled') "
            "  && document.getElementById('rngUrbanStrength')",
            timeout=8000)
        run.check("downsample slider + urbanization controls present", True, "")

        # Defaults — pre-hydrate values come from cfg
        page.wait_for_function(
            "() => PROJECT_CONFIG !== null",
            timeout=8000)
        page.wait_for_timeout(300)

        ds = page.evaluate("() => Number(document.getElementById('rngDownsample').value)")
        # dominus-columbia.yaml sets terrain.downsample=4
        run.check("downsample hydrates from cfg.terrain.downsample (4 in dominus)",
                  ds == 4, f"got {ds!r}")

        urb_state = page.evaluate("""() => ({
          enabled:   document.getElementById('chkUrbanEnabled').checked,
          strength:  Number(document.getElementById('rngUrbanStrength').value),
          color: [
            Number(document.getElementById('rngUrbanColor_r').value),
            Number(document.getElementById('rngUrbanColor_g').value),
            Number(document.getElementById('rngUrbanColor_b').value),
          ],
        })""")
        # dominus-columbia.yaml sets urbanization.enabled=true
        run.check("urbanization.enabled hydrates from YAML",
                  urb_state["enabled"] is True,
                  f"got {urb_state!r}")
        # urbanization.blend_strength is 0.55 in dominus
        run.check("urbanization.blend_strength=0.55 (matches dominus YAML)",
                  abs(urb_state["strength"] - 0.55) < 1e-6,
                  f"got {urb_state['strength']!r}")

        # Edit + commit + GET round-trip
        edit_result = page.evaluate("""async () => {
          // Bump downsample to 8 (preview-mode)
          const ds = document.getElementById('rngDownsample');
          ds.value = '8';
          ds.dispatchEvent(new Event('input', {bubbles: true}));
          // Disable urbanization
          const ue = document.getElementById('chkUrbanEnabled');
          ue.checked = false;
          ue.dispatchEvent(new Event('change', {bubbles: true}));
          // Bump strength
          const us = document.getElementById('rngUrbanStrength');
          us.value = '0.75';
          us.dispatchEvent(new Event('input', {bubbles: true}));
          // Recolor R channel
          const ur = document.getElementById('rngUrbanColor_r');
          ur.value = '0.90';
          ur.dispatchEvent(new Event('input', {bubbles: true}));

          const merged = {
            ...PROJECT_CONFIG,
            terrain:      readTerrainFromControls(),
            urbanization: readUrbanizationFromControls(),
          };
          const put = await fetch('/api/config/dominus-columbia', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(merged),
          });
          const got = await fetch('/api/config/dominus-columbia').then(r => r.json());
          return {
            ok: put.ok,
            downsample: got.terrain && got.terrain.downsample,
            urb_enabled: got.urbanization && got.urbanization.enabled,
            urb_strength: got.urbanization && got.urbanization.blend_strength,
            urb_color_r: got.urbanization && got.urbanization.color
                          && got.urbanization.color[0],
          };
        }""")
        run.check("PUT terrain+urbanization round-trip succeeded",
                  edit_result["ok"] is True, f"got {edit_result!r}")
        run.check("server-stored terrain.downsample=8",
                  edit_result["downsample"] == 8,
                  f"got {edit_result['downsample']!r}")
        run.check("server-stored urbanization.enabled=False",
                  edit_result["urb_enabled"] is False,
                  f"got {edit_result['urb_enabled']!r}")
        run.check("server-stored urbanization.blend_strength=0.75",
                  abs((edit_result["urb_strength"] or 0) - 0.75) < 1e-3,
                  f"got {edit_result['urb_strength']!r}")
        run.check("server-stored urbanization.color[0]=0.90",
                  abs((edit_result["urb_color_r"] or 0) - 0.90) < 1e-3,
                  f"got {edit_result['urb_color_r']!r}")

        # ===========================================================
        #  Part 2 — setup.html: cfg.colors + cfg.fonts
        # ===========================================================
        print("\n=== setup.html: colors + fonts ===")
        goto_page(page, project_url(SERVER, "/setup", PROJECT),
                  wait_for_selector=".inspector")
        page.wait_for_function(
            "() => document.getElementById('colorPaletteList') "
            "  && document.getElementById('fontsList') "
            "  && document.getElementById('btnAddColor') "
            "  && document.getElementById('btnAddFont')",
            timeout=8000)
        run.check("color palette + fonts editors present", True, "")

        # Wait for hydrate to populate from PROJECT_CONFIG
        page.wait_for_function(
            "() => document.querySelectorAll('#colorPaletteList > div').length > 0 "
            "  && document.querySelectorAll('#fontsList > div').length > 0",
            timeout=8000)

        # Defaults — dominus-columbia has 11 colors + several font roles
        n_colors = page.evaluate(
            "() => COLOR_PALETTE.length")
        run.check("colors hydrate from cfg.colors (>= 5 entries)",
                  n_colors >= 5, f"got {n_colors!r}")
        n_fonts = page.evaluate("() => FONTS.length")
        run.check("fonts hydrate from cfg.fonts (>= 5 entries)",
                  n_fonts >= 5, f"got {n_fonts!r}")

        # Verify a known color round-trips. dominus has shadow=[30,25,18].
        shadow_present = page.evaluate("""() => {
          const sh = COLOR_PALETTE.find(c => c.name === 'shadow');
          return sh ? sh.color : null;
        }""")
        run.check("'shadow' color hydrates from YAML as [30,25,18]",
                  shadow_present == [30, 25, 18],
                  f"got {shadow_present!r}")

        # Add a new color via the button + edit it
        result_colors = page.evaluate("""async () => {
          const before = COLOR_PALETTE.length;
          document.getElementById('btnAddColor').click();
          // The new row's name input was focused — type into it
          const lastRow = document.querySelector(
            '#colorPaletteList > div:last-child');
          const nameInput = lastRow.querySelector('input[data-field="name"]');
          nameInput.value = 'rust';
          nameInput.dispatchEvent(new Event('input', {bubbles: true}));
          // Set R/G/B to a distinctive red
          const setRGB = (field, v) => {
            const el = lastRow.querySelector(`input[data-field="${field}"]`);
            el.value = String(v);
            el.dispatchEvent(new Event('input', {bubbles: true}));
          };
          setRGB('r', 200); setRGB('g', 80); setRGB('b', 50);

          const merged = readSetupFromControls();
          const put = await fetch('/api/config/dominus-columbia', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(merged),
          });
          const got = await fetch('/api/config/dominus-columbia').then(r => r.json());
          return {
            ok: put.ok,
            after_count: COLOR_PALETTE.length,
            before,
            rust_in_yaml: got.colors && got.colors.rust,
          };
        }""")
        run.check("'+ Add color' creates a new row",
                  result_colors["after_count"] == result_colors["before"] + 1,
                  f"got before={result_colors['before']} after={result_colors['after_count']}")
        run.check("PUT colors round-trip succeeded",
                  result_colors["ok"] is True, f"got {result_colors!r}")
        run.check("server-stored cfg.colors.rust=[200,80,50]",
                  result_colors["rust_in_yaml"] == [200, 80, 50],
                  f"got {result_colors['rust_in_yaml']!r}")

        # Edit a font row + add a custom role + commit
        result_fonts = page.evaluate("""async () => {
          // Bump the size of the first font role
          const firstRow = document.querySelector('#fontsList > div:first-child');
          const sizeInput = firstRow.querySelector('input[data-field="size"]');
          const oldRole = firstRow.querySelector('input[data-field="role"]').value;
          sizeInput.value = '99';
          sizeInput.dispatchEvent(new Event('input', {bubbles: true}));

          // Add a new role via the input + button
          document.getElementById('fNewFontRole').value = 'test_role_99';
          document.getElementById('btnAddFont').click();

          const merged = readSetupFromControls();
          const put = await fetch('/api/config/dominus-columbia', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(merged),
          });
          const got = await fetch('/api/config/dominus-columbia').then(r => r.json());
          return {
            ok: put.ok,
            old_role: oldRole,
            old_role_size: got.fonts && got.fonts[oldRole] && got.fonts[oldRole].size,
            new_role_present: !!(got.fonts && got.fonts['test_role_99']),
            new_role_def: got.fonts && got.fonts['test_role_99'],
          };
        }""")
        run.check("PUT fonts round-trip succeeded",
                  result_fonts["ok"] is True, f"got {result_fonts!r}")
        run.check("server-stored fonts[<first>].size=99",
                  result_fonts["old_role_size"] == 99,
                  f"got role={result_fonts['old_role']!r} size={result_fonts['old_role_size']!r}")
        run.check("server-stored fonts.test_role_99 exists",
                  result_fonts["new_role_present"] is True,
                  f"got {result_fonts['new_role_def']!r}")
        run.check("test_role_99 has default file + size",
                  result_fonts["new_role_def"]
                    and result_fonts["new_role_def"].get("file") == "DejaVuSerif.ttf"
                    and result_fonts["new_role_def"].get("size") == 14,
                  f"got {result_fonts['new_role_def']!r}")

        # ===========================================================
        #  Console errors across both pages
        # ===========================================================
        print("\n=== console errors (both pages combined) ===")
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
