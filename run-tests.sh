#!/usr/bin/env bash
#
# Run the test suite for doc_router.
#
# Modes:
#   ./run-tests.sh            → unit + integration (pytest)
#   ./run-tests.sh unit       → unit tests only (tests/test_unit.py)
#   ./run-tests.sh routing    → routing/integration tests only (tests/test_routing.py)
#   ./run-tests.sh pymupdf    → pymupdf4llm-api tests only (tests/test_pymupdf_api.py)
#   ./run-tests.sh e2e        → E2E smoke tests against running containers
#   ./run-tests.sh all        → pytest suite + E2E (requires containers up)
#
# Setup (first run):
#   python3 -m venv .venv
#   source .venv/bin/activate
#   pip install -r doc-router/requirements.txt \
#               -r pymupdf4llm-api/requirements.txt \
#               -r tests/requirements.txt
#
# Then:
#   ./run-tests.sh

set -euo pipefail

cd "$(dirname "$0")"

MODE="${1:-pytest}"

# Auto-activate .venv if present and not already activated.
if [ -d ".venv" ] && [ -z "${VIRTUAL_ENV:-}" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

run_pytest() {
    local target="${1:-tests/}"
    echo "→ pytest $target"
    PYTHONPATH="doc-router:pymupdf4llm-api" pytest -q "$target"
}

case "$MODE" in
    pytest|"")
        run_pytest tests/
        ;;
    unit)
        run_pytest tests/test_unit.py
        ;;
    routing)
        run_pytest tests/test_routing.py
        ;;
    pymupdf)
        run_pytest tests/test_pymupdf_api.py
        ;;
    e2e)
        bash tests/e2e.sh
        ;;
    all)
        run_pytest tests/
        echo
        echo "→ E2E (requires containers up)"
        bash tests/e2e.sh
        ;;
    *)
        echo "Unknown mode: $MODE" >&2
        echo "Usage: $0 [pytest|unit|routing|pymupdf|e2e|all]" >&2
        exit 1
        ;;
esac
