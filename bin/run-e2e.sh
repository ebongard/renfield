#!/usr/bin/env bash
# run-e2e.sh — execute the browser-based E2E suite and emit an HTML report.
#
# Usage:
#   ./bin/run-e2e.sh                       # run every area, full report
#   ./bin/run-e2e.sh -k chat               # pytest -k filter pass-through
#   ./bin/run-e2e.sh tests/e2e/areas/test_chat.py
#
# Runs locally against https://renfield.local — needs:
#   * Playwright installed (`pip install playwright pytest-html`
#     + `python -m playwright install chromium`)
#   * PAPERLESS_API_URL + PAPERLESS_API_TOKEN env vars if you want the
#     Paperless-assertion tests to run (otherwise they skip cleanly)
#
# Report: tests/e2e/reports/e2e-report-<timestamp>.html (self-contained)
# Screenshots: tests/e2e/screenshots/<test-name>.png (one per test)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

REPORTS_DIR="tests/e2e/reports"
mkdir -p "$REPORTS_DIR"
mkdir -p "tests/e2e/screenshots"

TS="$(date +%Y%m%d-%H%M%S)"
REPORT_PATH="$REPORTS_DIR/e2e-report-${TS}.html"
JUNIT_PATH="$REPORTS_DIR/e2e-junit-${TS}.xml"

# Default: whole areas/ directory, but callers can override
TARGETS=()
PYTEST_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -*) PYTEST_ARGS+=("$1"); shift ;;
        *)  TARGETS+=("$1"); shift ;;
    esac
done
if [[ ${#TARGETS[@]} -eq 0 ]]; then
    TARGETS=("tests/e2e/areas/")
fi

export PYTHONPATH="${PYTHONPATH:-}:$REPO_ROOT:$REPO_ROOT/src/backend"

echo "=== Renfield E2E Suite ==="
echo "  Target(s):    ${TARGETS[*]}"
echo "  HTML report:  $REPORT_PATH"
echo "  JUnit XML:    $JUNIT_PATH"
echo "  Screenshots:  tests/e2e/screenshots/"
echo ""

python3 -m pytest \
    "${TARGETS[@]}" \
    -v \
    --tb=short \
    --html="$REPORT_PATH" \
    --self-contained-html \
    --junitxml="$JUNIT_PATH" \
    ${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"} || EXIT=$?

echo ""
echo "Report: file://$REPO_ROOT/$REPORT_PATH"
exit "${EXIT:-0}"
