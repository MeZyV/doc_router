#!/usr/bin/env bash
#
# E2E smoke tests against running containers.
#
# Usage:
#   docker compose up -d --build
#   bash tests/e2e.sh
#
# Override endpoints with env vars if running elsewhere:
#   ROUTER_URL=http://host:8000 bash tests/e2e.sh
#
set -euo pipefail

ROUTER_URL="${ROUTER_URL:-http://localhost:8000}"
PYMUPDF_URL="${PYMUPDF_URL:-http://localhost:8001}"
TIKA_URL="${TIKA_URL:-http://localhost:9998}"
MINERU_URL="${MINERU_URL:-http://localhost:8002}"
EXTERNAL_API_KEY="${EXTERNAL_API_KEY:-}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }

PASS=0
FAIL=0

ok()   { green "  ok  $1"; PASS=$((PASS+1)); }
ko()   { red   "  ko  $1"; FAIL=$((FAIL+1)); }

# Run a command; print PASS/FAIL based on exit code.
check() {
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then ok "$name"; else ko "$name"; fi
}

# Auth header (only when EXTERNAL_API_KEY is set)
auth_header=()
if [ -n "$EXTERNAL_API_KEY" ]; then
    auth_header=(-H "Authorization: Bearer $EXTERNAL_API_KEY")
fi

yellow "[1/4] Healthchecks"
check "doc-router    /health/live"  curl -fsS "$ROUTER_URL/health/live"
# /health/ready requires the API key when set
check "doc-router    /health/ready" curl -fsS "${auth_header[@]}" "$ROUTER_URL/health/ready"
check "tika          /tika"         curl -fsS "$TIKA_URL/tika"
check "mineru-router /health"       curl -fsS "$MINERU_URL/health"
# pymupdf4llm-api is only running under the `remote-pymupdf` profile.
if curl -fsS --max-time 2 "$PYMUPDF_URL/health/live" >/dev/null 2>&1; then
    ok "pymupdf4llm   /health/live (remote profile active)"
else
    yellow "  skip pymupdf4llm-api (local mode — service not started)"
fi

yellow "[2/4] Generate sample PDF"
PDF="$TMP_DIR/sample.pdf"
python3 - "$PDF" <<'PY'
import sys, fitz
doc = fitz.open()
page = doc.new_page()
page.insert_text((72, 72), "End-to-end test document.\n" * 5
                            + "Lorem ipsum dolor sit amet, consectetur adipiscing elit.")
doc.save(sys.argv[1])
doc.close()
PY
ok "sample.pdf generated ($(wc -c < "$PDF") bytes)"

yellow "[3/4] PUT /process — text PDF"
RESP="$TMP_DIR/resp.json"
HTTP_CODE=$(curl -sS -o "$RESP" -w "%{http_code}" \
    -X PUT "$ROUTER_URL/process" \
    -H "Content-Type: application/pdf" \
    -H "X-Filename: sample.pdf" \
    "${auth_header[@]}" \
    --data-binary "@$PDF")

if [ "$HTTP_CODE" = "200" ]; then
    ok "PUT /process returned 200"
else
    ko "PUT /process returned $HTTP_CODE"
    cat "$RESP"
fi

python3 - "$RESP" <<'PY' && ok "response shape valid" || ko "response shape invalid"
import sys, json
data = json.load(open(sys.argv[1]))
assert "page_content" in data, "missing page_content"
assert "metadata" in data, "missing metadata"
assert data["page_content"].strip(), "empty page_content"
assert data["metadata"].get("parser") in {"pymupdf4llm", "mineru"}, f"unexpected parser: {data['metadata']}"
assert data["metadata"].get("router") == "doc-router", "missing router metadata"
print(f"parser={data['metadata']['parser']} chars={len(data['page_content'])}")
PY

yellow "[4/4] Negative paths"

HTTP_CODE=$(curl -sS -o /dev/null -w "%{http_code}" \
    -X PUT "$ROUTER_URL/process" \
    -H "Content-Type: application/pdf" \
    -H "X-Filename: empty.pdf" \
    "${auth_header[@]}")
[ "$HTTP_CODE" = "400" ] && ok "empty body → 400" || ko "empty body returned $HTTP_CODE (expected 400)"

if [ -n "$EXTERNAL_API_KEY" ]; then
    HTTP_CODE=$(curl -sS -o /dev/null -w "%{http_code}" \
        -X PUT "$ROUTER_URL/process" \
        -H "Content-Type: application/pdf" \
        -H "X-Filename: x.pdf" \
        -H "Authorization: Bearer wrong-key" \
        --data-binary "@$PDF")
    [ "$HTTP_CODE" = "401" ] && ok "wrong API key → 401" || ko "wrong API key returned $HTTP_CODE (expected 401)"
fi

echo
if [ "$FAIL" -eq 0 ]; then
    green "All checks passed ($PASS)."
    exit 0
else
    red "$FAIL check(s) failed — $PASS passed."
    exit 1
fi
