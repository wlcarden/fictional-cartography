"""Playwright tests for the extended Decoration tab.

Coverage targets:
  - New controls (compass / scale_bar / credit / ornaments / cartouche)
    are present in the DOM
  - Defaults match the pipeline's prior hard-coded behavior so existing
    configs render identically without a `decoration:` block
  - Edits flow through readDecorationFromControls() into a valid YAML
    structure (verified by round-tripping through commitToServer + GET)
  - Toggling a checkbox round-trips correctly (enabled=false in YAML)

The render itself is *not* exercised end-to-end here — that's covered by
the pipeline-side smoke test we ran during implementation. This test
focuses on the UI plumbing + config round-trip.

Run via:
  python3 ~/.claude/plugins/marketplaces/anthropic-agent-skills/skills/webapp-testing/scripts/with_server.py \
      --server "python3 -m src.server --port 5080" --port 5080 --timeout 60 \
      -- python3 tests/playwright/test_decoration_extended.py
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


def _read_decoration_state(page):
    """Snapshot the decoration UI's editable controls.

    Mirror of the JS state objects (COMPASS, SCALE_BAR, CREDIT_DECO,
    ORNAMENTS, CARTOUCHE_ENABLED). Read straight from the DOM so we
    don't depend on closure-scoped JS variables.
    """
    return page.evaluate("""() => {
      const $ = (id) => document.getElementById(id);
      return {
        compass: {
          enabled:    $('chkCompassEnabled').checked,
          position:   $('selCompassPosition').value,
          radius_pct: Number($('rngCompassRadius').value),
          offset_x:   Number($('numCompassOffsetX').value),
          offset_y:   Number($('numCompassOffsetY').value),
        },
        scale_bar: {
          enabled:            $('chkScaleEnabled').checked,
          position:           $('selScalePosition').value,
          bar_miles:          Number($('numScaleMiles').value),
          segments:           Number($('numScaleSegments').value),
          offset_from_border: Number($('numScaleOffset').value),
        },
        credit: {
          enabled:            $('chkCreditEnabled').checked,
          divider:            $('chkCreditDivider').checked,
          offset_from_border: Number($('numCreditOffset').value),
        },
        ornaments: {
          enabled:      $('chkOrnamentsEnabled').checked,
          glyph:        $('fOrnamentGlyph').value,
          size:         Number($('rngOrnamentSize').value),
          color:        [
            Number($('rngOrnamentColor_r').value),
            Number($('rngOrnamentColor_g').value),
            Number($('rngOrnamentColor_b').value),
          ],
          inset_x:      Number($('numOrnamentInsetX').value),
          inset_top:    Number($('numOrnamentInsetTop').value),
          inset_bottom: Number($('numOrnamentInsetBottom').value),
        },
        cartouche_enabled: $('chkCartoucheEnabled').checked,
      };
    }""")


def _read_decoration_yaml(page):
    """Get the YAML-shaped config the page would PUT to the server.

    Uses the same readDecorationFromControls() the commit path uses, so
    we exercise the actual round-trip code rather than a parallel impl.
    """
    return page.evaluate("() => readDecorationFromControls()")


def _run(run: TestRun) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        console_errors = setup_console_capture(page)

        # ---- Step 1: page load + control presence ----
        print("\n--- Step 1: decoration.html load ---")
        goto_page(page, project_url(SERVER, "/decoration", PROJECT),
                  wait_for_selector=".inspector")
        page.wait_for_function(
            "() => document.getElementById('chkCompassEnabled') "
            "  && document.getElementById('chkScaleEnabled') "
            "  && document.getElementById('chkCreditEnabled') "
            "  && document.getElementById('chkOrnamentsEnabled') "
            "  && document.getElementById('chkCartoucheEnabled')",
            timeout=8000)
        run.check("compass + scale + credit + ornaments + cartouche controls present",
                  True, "")

        # Wait briefly for hydrateFromConfig to populate the DOM
        page.wait_for_function(
            "() => document.getElementById('numScaleMiles').value !== ''",
            timeout=8000)

        # ---- Step 2: defaults match prior hard-coded behavior ----
        print("\n--- Step 2: defaults backward-compatible ---")
        s = _read_decoration_state(page)
        # Compass
        run.check("compass enabled by default",
                  s["compass"]["enabled"] is True,
                  f"got {s['compass']!r}")
        run.check("compass default position is bottom-right",
                  s["compass"]["position"] == "bottom-right",
                  f"got {s['compass']['position']!r}")
        run.check("compass default offset_x=20 (matches pipeline prior)",
                  s["compass"]["offset_x"] == 20,
                  f"got {s['compass']['offset_x']!r}")
        run.check("compass default offset_y=80 (matches pipeline prior)",
                  s["compass"]["offset_y"] == 80,
                  f"got {s['compass']['offset_y']!r}")
        # Scale bar
        run.check("scale_bar enabled by default",
                  s["scale_bar"]["enabled"] is True,
                  f"got {s['scale_bar']!r}")
        run.check("scale_bar default position is bottom-center",
                  s["scale_bar"]["position"] == "bottom-center",
                  f"got {s['scale_bar']['position']!r}")
        run.check("scale_bar default bar_miles=20 (matches pipeline prior)",
                  s["scale_bar"]["bar_miles"] == 20,
                  f"got {s['scale_bar']['bar_miles']!r}")
        run.check("scale_bar default segments=4",
                  s["scale_bar"]["segments"] == 4,
                  f"got {s['scale_bar']['segments']!r}")
        # Credit
        run.check("credit enabled by default",
                  s["credit"]["enabled"] is True,
                  f"got {s['credit']!r}")
        run.check("credit divider enabled by default",
                  s["credit"]["divider"] is True,
                  f"got {s['credit']!r}")
        run.check("credit default offset_from_border=28 (inside frame)",
                  s["credit"]["offset_from_border"] == 28,
                  f"got {s['credit']['offset_from_border']!r}")
        # Ornaments
        run.check("ornaments enabled by default",
                  s["ornaments"]["enabled"] is True, f"got {s['ornaments']!r}")
        run.check("ornaments default glyph is fleur-de-lis",
                  s["ornaments"]["glyph"] == "⚜",
                  f"got {s['ornaments']['glyph']!r}")
        run.check("ornaments default size=32 (matches fonts.fleur prior)",
                  s["ornaments"]["size"] == 32,
                  f"got {s['ornaments']['size']!r}")
        run.check("ornaments default color is dark brown [85,65,45]",
                  s["ornaments"]["color"] == [85, 65, 45],
                  f"got {s['ornaments']['color']!r}")
        run.check("ornaments default inset_x=35 (matches pipeline prior)",
                  s["ornaments"]["inset_x"] == 35,
                  f"got {s['ornaments']['inset_x']!r}")
        run.check("ornaments default inset_top=30 / inset_bottom=45 (matches prior)",
                  s["ornaments"]["inset_top"] == 30
                    and s["ornaments"]["inset_bottom"] == 45,
                  f"got {s['ornaments']!r}")

        # Cartouche
        run.check("cartouche enabled by default",
                  s["cartouche_enabled"] is True, "")

        # ---- Step 3: edits flow into readDecorationFromControls output ----
        print("\n--- Step 3: edit knobs, check YAML output ---")
        # Disable compass + change scale-bar position + bump credit offset +
        # change ornament glyph (via picker) + bump size + recolor
        page.evaluate("""() => {
          document.getElementById('chkCompassEnabled').checked = false;
          document.getElementById('chkCompassEnabled')
                  .dispatchEvent(new Event('change', {bubbles: true}));

          const sbSel = document.getElementById('selScalePosition');
          sbSel.value = 'bottom-left';
          sbSel.dispatchEvent(new Event('change', {bubbles: true}));

          const off = document.getElementById('numCreditOffset');
          off.value = '50';
          off.dispatchEvent(new Event('input', {bubbles: true}));

          // Click the ✦ chip in the glyph picker
          document.querySelector(
            "#ornamentGlyphPicker button[data-glyph='✦']").click();
          // Bump ornament size
          const sz = document.getElementById('rngOrnamentSize');
          sz.value = '48';
          sz.dispatchEvent(new Event('input', {bubbles: true}));
          // Recolor R channel
          const rR = document.getElementById('rngOrnamentColor_r');
          rR.value = '200';
          rR.dispatchEvent(new Event('input', {bubbles: true}));
          // Bump inset_top
          const it = document.getElementById('numOrnamentInsetTop');
          it.value = '60';
          it.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
        page.wait_for_timeout(120)
        yml = _read_decoration_yaml(page)
        run.check("readDecoration emits cfg.decoration block",
                  isinstance(yml.get("decoration"), dict),
                  f"got {type(yml.get('decoration'))}")
        deco = yml["decoration"]
        run.check("compass.enabled=False after toggle",
                  deco["compass"]["enabled"] is False,
                  f"got {deco['compass']}")
        run.check("scale_bar.position=bottom-left after edit",
                  deco["scale_bar"]["position"] == "bottom-left",
                  f"got {deco['scale_bar']}")
        run.check("credit.offset_from_border=50 after edit",
                  deco["credit"]["offset_from_border"] == 50,
                  f"got {deco['credit']}")
        # Ornaments — glyph + size + color + inset all changed
        run.check("ornaments.glyph=✦ after picker click",
                  deco["ornaments"]["glyph"] == "✦",
                  f"got {deco['ornaments']['glyph']!r}")
        run.check("ornaments.size=48 after slider change",
                  deco["ornaments"]["size"] == 48,
                  f"got {deco['ornaments']['size']!r}")
        run.check("ornaments.color[0]=200 after R slider",
                  deco["ornaments"]["color"][0] == 200,
                  f"got {deco['ornaments']['color']}")
        run.check("ornaments.inset_top=60 after edit",
                  deco["ornaments"]["inset_top"] == 60,
                  f"got {deco['ornaments']['inset_top']!r}")
        run.check("ornaments.enabled=True (toggle untouched)",
                  deco["ornaments"]["enabled"] is True,
                  f"got {deco['ornaments']['enabled']!r}")
        # Cartouche unchanged
        run.check("cartouche.enabled=True (untouched)",
                  deco["cartouche"]["enabled"] is True,
                  f"got {deco['cartouche']}")

        # ---- Step 4: radius_pct=0 (auto) is omitted from YAML ----
        print("\n--- Step 4: radius_pct=0 omitted from YAML ---")
        run.check("compass.radius_pct omitted when 0 (auto)",
                  "radius_pct" not in deco["compass"],
                  f"got {deco['compass']}")
        # Bump radius_pct, verify it appears
        page.evaluate("""() => {
          const r = document.getElementById('rngCompassRadius');
          r.value = '7';
          r.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
        page.wait_for_timeout(80)
        yml2 = _read_decoration_yaml(page)
        run.check("compass.radius_pct=7 emitted when nonzero",
                  yml2["decoration"]["compass"].get("radius_pct") == 7,
                  f"got {yml2['decoration']['compass']}")

        # ---- Step 5: PUT round-trip via /api/config ----
        print("\n--- Step 5: PUT round-trip preserves decoration block ---")
        # Commit current state to server, then GET it back and compare.
        result = page.evaluate(f"""async () => {{
          const cfg = readDecorationFromControls();
          const merged = {{...PROJECT_CONFIG, ...cfg}};
          const put = await fetch('/api/config/{PROJECT}', {{
            method: 'PUT',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(merged),
          }});
          if (!put.ok) return {{ok: false, status: put.status}};
          const got = await fetch('/api/config/{PROJECT}').then(r => r.json());
          return {{ok: true, deco: got.decoration}};
        }}""")
        run.check("PUT decoration round-trip succeeded (HTTP 200)",
                  result.get("ok") is True,
                  f"got {result!r}")
        deco_after = result.get("deco") or {}
        run.check("server-stored compass.enabled=False",
                  deco_after.get("compass", {}).get("enabled") is False,
                  f"got {deco_after.get('compass')}")
        run.check("server-stored scale_bar.position=bottom-left",
                  deco_after.get("scale_bar", {}).get("position") == "bottom-left",
                  f"got {deco_after.get('scale_bar')}")
        run.check("server-stored credit.offset_from_border=50",
                  deco_after.get("credit", {}).get("offset_from_border") == 50,
                  f"got {deco_after.get('credit')}")
        run.check("server-stored ornaments.glyph=✦",
                  deco_after.get("ornaments", {}).get("glyph") == "✦",
                  f"got {deco_after.get('ornaments')}")
        run.check("server-stored ornaments.size=48",
                  deco_after.get("ornaments", {}).get("size") == 48,
                  f"got {deco_after.get('ornaments')}")
        run.check("server-stored ornaments.color=[200,...]",
                  (deco_after.get("ornaments", {}).get("color") or [0])[0] == 200,
                  f"got {deco_after.get('ornaments', {}).get('color')}")
        run.check("server-stored ornaments.inset_top=60",
                  deco_after.get("ornaments", {}).get("inset_top") == 60,
                  f"got {deco_after.get('ornaments')}")
        run.check("server-stored compass.radius_pct=7",
                  deco_after.get("compass", {}).get("radius_pct") == 7,
                  f"got {deco_after.get('compass')}")

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
