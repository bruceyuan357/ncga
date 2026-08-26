#!/usr/bin/env bash
# Real-LLM smoke probe — hits every public endpoint against a running server.
# Exit code 0 = all pass. Run after every deploy, and as the body of a daily cron
# (the kind of test mocked unit tests cannot catch — see Cycle 17 V4 max_tokens
# regression where the test suite stayed green while /api/rate was broken).
#
# Usage:
#   tools/smoke.sh                       # against http://127.0.0.1:8000
#   BASE=https://ncga.example.com tools/smoke.sh
#
# Requirements: curl, python3, jq optional.
# .env at repo root must contain NCGA_AUTH_TOKEN and a working DEEPSEEK_API_KEY.

set -u
BASE="${BASE:-http://127.0.0.1:8000}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOKEN="$(grep -E '^NCGA_AUTH_TOKEN=' "$ROOT/.env" | cut -d= -f2-)"
PASS=0
FAIL=0

run() {
    local label="$1"; shift
    local expect="$1"; shift
    local out http
    out=$(curl -sS -w '\n___HTTP___%{http_code}' "$@" 2>&1)
    http="${out##*___HTTP___}"
    body="${out%___HTTP___*}"
    if [[ "$http" == "$expect" ]]; then
        printf '  \033[32m✓\033[0m %-45s HTTP %s\n' "$label" "$http"
        PASS=$((PASS+1))
    else
        printf '  \033[31m✗\033[0m %-45s HTTP %s (want %s)\n' "$label" "$http" "$expect"
        printf '      body: %s\n' "$(printf '%s' "$body" | head -c 200)"
        FAIL=$((FAIL+1))
    fi
}

echo "smoke: $BASE"

# --- GET endpoints (no auth) ---
run "GET /api/healthz"      200 "$BASE/api/healthz"
run "GET /api/presets"      200 "$BASE/api/presets"
run "GET /api/scenarios"    200 "$BASE/api/scenarios"
run "GET /api/quality-stats" 200 "$BASE/api/quality-stats"
run "GET /api/transform-modes" 200 "$BASE/api/transform-modes"
run "GET / (SPA)"           200 "$BASE/"
run "GET /static/app.js"    200 "$BASE/static/app.js"
# --path-as-is stops curl from squashing /static/../app.py to /app.py client-side,
# so the server actually receives the dot-dot path.
run "GET /static/../app.py (path traversal denied)" 404 --path-as-is "$BASE/static/../app.py"
run "GET /static/%2e%2e%2fapp.py (encoded traversal denied)" 404 --path-as-is "$BASE/static/%2e%2e%2fapp.py"

# --- auth gating ---
run "POST /api/rewrite (no auth → 401)" 401 -X POST -H 'Content-Type: application/json' \
    -d '{"text":"hi","target_variety":"beijing_mandarin"}' "$BASE/api/rewrite"
run "POST /api/transform (no auth → 401)" 401 -X POST -H 'Content-Type: application/json' \
    -d '{"text":"hi","mode":"polish"}' "$BASE/api/transform"

# --- POST endpoints (real LLM — costs a few cents per smoke run) ---
H=(-H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json')
run "POST /api/rewrite (real LLM)"    200 -X POST "${H[@]}" \
    -d '{"text":"今天我不想去","target_variety":"beijing_mandarin","scenario":"friends_casual"}' "$BASE/api/rewrite"
run "POST /api/rate (real LLM, fixed Cycle 17)" 200 -X POST "${H[@]}" \
    -d '{"original":"今天我不想去","rewritten":"今儿个我还真不怎么想去嘿","target_variety":"beijing_mandarin","scenario":"friends_casual"}' "$BASE/api/rate"
run "POST /api/transform polish (real LLM, Cycle 23)" 200 -X POST "${H[@]}" \
    -d '{"text":"这个句子写得有一点点不太通顺吧","mode":"polish"}' "$BASE/api/transform"
run "POST /api/explain (real LLM)"    200 -X POST "${H[@]}" \
    -d '{"original":"今天我不想去","rewritten":"今儿个我还真不怎么想去嘿","target_variety":"beijing_mandarin"}' "$BASE/api/explain"

# --- regression: 2026-08 reasoning-eats-max_tokens empty-content bug ---
# deepseek-v4-flash thinking used to consume the whole token budget on hard
# dialects (hokkien was the worst) → zero content → silent heuristic degrade.
# HTTP stays 200 in that case, so assert the body says degraded=false.
BODY=$(curl -sS -X POST "${H[@]}" \
    -d '{"text":"当局者迷，旁观者清。","target_variety":"hokkien_written"}' "$BASE/api/rewrite")
if printf '%s' "$BODY" | grep -q '"degraded": false'; then
    printf '  \033[32m✓\033[0m %-45s HTTP 200\n' "POST /api/rewrite hokkien (degraded=false)"
    PASS=$((PASS+1))
else
    printf '  \033[31m✗\033[0m %-45s\n' "POST /api/rewrite hokkien (degraded=false)"
    printf '      body: %s\n' "$(printf '%s' "$BODY" | head -c 200)"
    FAIL=$((FAIL+1))
fi

# --- validation diagnostics (Cycle 16) ---
run "POST /api/rewrite missing field → 400" 400 -X POST "${H[@]}" \
    -d '{"target_variety":"beijing_mandarin"}' "$BASE/api/rewrite"
run "POST /api/rewrite bad variety → 400" 400 -X POST "${H[@]}" \
    -d '{"text":"hi","target_variety":"klingon_mandarin"}' "$BASE/api/rewrite"
run "POST /api/rewrite malformed JSON → 400" 400 -X POST "${H[@]}" \
    --data-binary '{"text":"hi",,broken' "$BASE/api/rewrite"

echo
echo "passed: $PASS, failed: $FAIL"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
