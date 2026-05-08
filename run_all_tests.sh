#!/usr/bin/env bash
# Run the full test suite — unit (pytest) + browser (Playwright).
# Each browser test self-manages its server via with_server.py. Test
# results print as they go; a non-zero overall exit signals at least
# one failure.
set -e

WITH_SERVER=~/.claude/plugins/marketplaces/anthropic-agent-skills/skills/webapp-testing/scripts/with_server.py

echo "================================================================"
echo "  Pipeline unit tests (pytest)"
echo "================================================================"
python3 -m pytest tests/ -q

echo ""
echo "================================================================"
echo "  Browser tests (Playwright)"
echo "================================================================"

BROWSER_TESTS=(
    "test_narrative_pickmode.py"
    "tests/playwright/test_place.py"
    "tests/playwright/test_paint.py"
    "tests/playwright/test_setup.py"
    "tests/playwright/test_routes_decoration_render.py"
    "tests/playwright/test_offset_deviation.py"
)

for test in "${BROWSER_TESTS[@]}"; do
    echo ""
    echo "---- $test ----"
    python3 "$WITH_SERVER" \
        --server "python3 -m src.server --port 5080" --port 5080 \
        --timeout 60 \
        -- python3 "$test"
done

echo ""
echo "================================================================"
echo "  All tests passed."
echo "================================================================"
