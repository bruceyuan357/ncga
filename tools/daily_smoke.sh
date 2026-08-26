#!/usr/bin/env bash
# Daily real-LLM smoke wrapper for cron.
#
# tools/smoke.sh needs a running server; a dev machine doesn't run NCGA 24/7.
# This wrapper starts app.py when the port is free (and stops only the server
# it started), runs the 18+1 probes, and appends a timestamped line to the log.
#
#   tools/daily_smoke.sh           # run once, human-readable
#
# Cron example (crontab -e):
#   17 9 * * * "/Users/bruce/bruce development/01 NCGA/NCGA/tools/daily_smoke.sh" >> /dev/null 2>&1

set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${BASE:-http://127.0.0.1:8000}"
LOG_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/ncga"
LOG="$LOG_DIR/smoke.log"
mkdir -p "$LOG_DIR"

STARTED=0
if ! curl -sf -o /dev/null --max-time 2 "$BASE/api/healthz"; then
    cd "$ROOT"
    "$ROOT/.venv/bin/python" app.py >> "$LOG_DIR/smoke-server.log" 2>&1 &
    STARTED=1
    for _ in $(seq 1 20); do
        curl -sf -o /dev/null --max-time 2 "$BASE/api/healthz" && break
        sleep 0.5
    done
fi

OUT="$(cd "$ROOT" && tools/smoke.sh 2>&1)"
CODE=$?
SUMMARY="$(printf '%s' "$OUT" | tail -1)"
TS="$(date '+%Y-%m-%d %H:%M:%S')"
if [ "$CODE" -eq 0 ]; then
    printf '%s PASS %s\n' "$TS" "$SUMMARY" >> "$LOG"
else
    printf '%s FAIL %s\n' "$TS" "$SUMMARY" >> "$LOG"
    printf '%s\n' "$OUT" >> "$LOG"
fi
printf '%s\n' "$OUT"

if [ "$STARTED" -eq 1 ] && [ -f "$ROOT/.ncga.pid" ]; then
    kill "$(cat "$ROOT/.ncga.pid")" 2>/dev/null || true
fi
exit "$CODE"
