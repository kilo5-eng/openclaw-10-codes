#!/usr/bin/env bash
# test-10codes.sh -- canonical PASS/FAIL smoke-test for 10-77, 10-323, 10-88
# Usage: bash /home/kcinc/.openclaw/workspace/test-10codes.sh [TICKER]
# Default ticker: BMNR

WORKSPACE=/home/kcinc/.openclaw/workspace
TICKER=${1:-BMNR}
PASS=1
TMPDIR_TEST=$(mktemp -d /tmp/test-10codes-XXXXXX)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

log()  { printf '[TEST] %s\n' "$*"; }
ok()   { printf '  OK  %s\n' "$*"; }
fail() { printf '  FAIL %s\n' "$*"; PASS=0; }

# ---------------------------------------------------------------
# TEST 1: 10-77 options engine
# ---------------------------------------------------------------
log "10-77 options engine -- $TICKER"
if python3 "$WORKSPACE/10-77-options-engine.py" "$TICKER" \
       > "$TMPDIR_TEST/10-77-out.txt" 2>&1; then
    ok "exit 0"
else
    fail "non-zero exit (log: $TMPDIR_TEST/10-77-out.txt)"
fi

# ---------------------------------------------------------------
# TEST 2: 10-77 ticker script
# ---------------------------------------------------------------
log "10-77 ticker script -- $TICKER"
if bash "$WORKSPACE/trading/scripts/data-fetch/mboum/10-77-ticker-v1.sh" "$TICKER" \
       > "$TMPDIR_TEST/10-77-ticker.txt" 2>&1; then
    ok "exit 0"
else
    fail "non-zero exit (log: $TMPDIR_TEST/10-77-ticker.txt)"
fi

# ---------------------------------------------------------------
# TEST 3: 10-323 options projection
# ---------------------------------------------------------------
log "10-323 options projection -- $TICKER"
JSON323="$TMPDIR_TEST/10-323.json"
if python3 "$WORKSPACE/trading/scripts/analysis/10-323.py" "$TICKER" \
       > "$JSON323" 2>"$TMPDIR_TEST/10-323-err.txt"; then
    if python3 -m json.tool "$JSON323" >/dev/null 2>&1; then
        ok "valid JSON output"
        if grep -q "$TICKER" "$JSON323"; then
            ok "ticker present in output"
        else
            fail "ticker '$TICKER' not found in 10-323 JSON"
        fi
    else
        fail "invalid JSON output"
    fi
else
    fail "non-zero exit ($(head -c 200 "$TMPDIR_TEST/10-323-err.txt"))"
fi

# ---------------------------------------------------------------
# TEST 4: 10-88 ticker dashboard
# Note: 10-88 emits a mixed text+JSON dashboard; we check exit 0
# and that the ticker symbol appears in stdout.
# ---------------------------------------------------------------
log "10-88 dashboard -- $TICKER"
OUT88="$TMPDIR_TEST/10-88.out"
if python3 "$WORKSPACE/trading/scripts/dashboards/10-88-ticker-dashboard.py" \
       --tickers "$TICKER" > "$OUT88" 2>"$TMPDIR_TEST/10-88-err.txt"; then
    ok "exit 0"
    if grep -q "$TICKER" "$OUT88"; then
        ok "ticker present in output"
    else
        fail "ticker '$TICKER' not found in 10-88 output"
    fi
else
    fail "non-zero exit ($(head -c 200 "$TMPDIR_TEST/10-88-err.txt"))"
fi

# ---------------------------------------------------------------
# Result
# ---------------------------------------------------------------
echo ""
if [ "$PASS" -eq 1 ]; then
    echo "PASS"
    exit 0
else
    echo "FAIL"
    exit 1
fi
