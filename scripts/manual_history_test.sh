#!/usr/bin/env bash
# manual_history_test.sh
#
# Runs all manual history / reset curl tests against the deployed simulator.
# Each step prints a header, runs the curl command, then waits before continuing.
#
# Usage:
#   bash scripts/manual_history_test.sh
#   DELAY=3 bash scripts/manual_history_test.sh   # override pause between steps
#
# Note: history will never be truly empty while the router is running —
# background scoring/pruning calls fill it at ~8 entries/sec.
# Isolation is done by filtering on a rare method (eth_gasPrice) instead.

set -euo pipefail

CONTROL="https://sim-control.victoria.magmadevs.com"
ROUTER="https://eth-sim-jsonrpc.victoria.magmadevs.com"
DELAY=${DELAY:-2}   # seconds to pause between steps

# ── helpers ───────────────────────────────────────────────────────────────────

header() {
    echo ""
    echo "════════════════════════════════════════════════════════"
    echo "  $1"
    echo "════════════════════════════════════════════════════════"
}

pause() {
    echo ""
    echo "  ⏳  waiting ${DELAY}s..."
    sleep "$DELAY"
}

# Send eth_blockNumber through the router
rpc() {
    curl -s -X POST "$ROUTER" \
        -H "Content-Type: application/json" \
        -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' \
        | python3 -m json.tool
}

# Send a rare method the router never sends in background — used to isolate a single call
rpc_rare() {
    curl -s -X POST "$ROUTER" \
        -H "Content-Type: application/json" \
        -d '{"jsonrpc":"2.0","method":"eth_gasPrice","params":[],"id":1}' \
        | python3 -m json.tool
}

# Print count + last 5 entries only (avoids flooding terminal)
history_summary() {
    local url="$1"
    curl -s "$url" | python3 -c "
import json, sys
data = json.load(sys.stdin)
count = data.get('count', 0)
entries = data.get('history', [])
preview = entries[-5:]
print(json.dumps({'count': count, 'showing_last': len(preview), 'history': preview}, indent=2))
"
}

# ═════════════════════════════════════════════════════════════════════════════
# 0. Health check
# ═════════════════════════════════════════════════════════════════════════════

header "0. Health check"
curl -s "$CONTROL/health" | python3 -m json.tool
pause

# ═════════════════════════════════════════════════════════════════════════════
# 1. /reset  — resets scenario only, history survives
# ═════════════════════════════════════════════════════════════════════════════

header "1a. Dirty up — set provider 1 to error mode and make a call"
curl -s -X POST "$CONTROL/scenario" \
    -H "Content-Type: application/json" \
    -d '{"providers": {"1": {"mode": "error"}}}' | python3 -m json.tool
pause

rpc
pause

header "1b. POST /reset  — resets scenario, does NOT clear history"
curl -si -X POST "$CONTROL/reset"
pause

header "1c. Verify scenario is back to defaults  →  all providers mode: success"
curl -s "$CONTROL/scenario" | python3 -m json.tool
pause

header "1d. Verify history survived reset  →  count should be > 0"
history_summary "$CONTROL/history"
pause

# ═════════════════════════════════════════════════════════════════════════════
# 2. /history/clear  — wipes history only, scenario survives
# ═════════════════════════════════════════════════════════════════════════════

header "2a. Dirty up — set provider 2 to rate_limit"
curl -s -X POST "$CONTROL/scenario" \
    -H "Content-Type: application/json" \
    -d '{"providers": {"2": {"mode": "rate_limit"}}}' | python3 -m json.tool
pause

header "2b. POST /history/clear  — wipes history, does NOT touch scenario"
curl -si -X POST "$CONTROL/history/clear"
pause

header "2c. Verify history cleared  →  all entries should be AFTER the clear timestamp above"
history_summary "$CONTROL/history?last=5"
pause

header "2d. Verify scenario still has provider 2 in rate_limit  →  NOT reset"
curl -s "$CONTROL/scenario" | python3 -m json.tool
pause

# ═════════════════════════════════════════════════════════════════════════════
# 3. /reset/all  — resets both
# ═════════════════════════════════════════════════════════════════════════════

header "3a. Dirty up — set provider 3 to down and make two calls"
curl -s -X POST "$CONTROL/scenario" \
    -H "Content-Type: application/json" \
    -d '{"providers": {"3": {"mode": "down"}}}' | python3 -m json.tool
pause

rpc
rpc
pause

header "3b. POST /reset/all  — resets scenario AND clears history"
curl -si -X POST "$CONTROL/reset/all"
pause

header "3c. Verify scenario is clean  →  all providers mode: success"
curl -s "$CONTROL/scenario" | python3 -m json.tool
pause

header "3d. Verify history cleared  →  all entries should be AFTER the reset/all timestamp above"
history_summary "$CONTROL/history?last=5"
pause

# ═════════════════════════════════════════════════════════════════════════════
# 4. /history filters
# ═════════════════════════════════════════════════════════════════════════════

header "4a. Setup — set provider 2 to error, make a few calls to populate history"
curl -s -X POST "$CONTROL/scenario" \
    -H "Content-Type: application/json" \
    -d '{"providers": {"2": {"mode": "error"}}}' > /dev/null
rpc
rpc
rpc
pause

header "4b. GET /history?last=10  — last 10 seconds (compact)"
history_summary "$CONTROL/history?last=10"
pause

header "4c. GET /history?provider=1  — provider 1 only"
history_summary "$CONTROL/history?provider=1"
pause

header "4d. GET /history?provider=2  — provider 2 only (should show errors)"
history_summary "$CONTROL/history?provider=2"
pause

header "4e. GET /history?method=eth_blockNumber"
history_summary "$CONTROL/history?method=eth_blockNumber"
pause

header "4f. GET /history?status=success"
history_summary "$CONTROL/history?status=success"
pause

header "4g. GET /history?status=error"
history_summary "$CONTROL/history?status=error"
pause

header "4h. GET /history?status=rate_limit"
history_summary "$CONTROL/history?status=rate_limit"
pause

header "4i. GET /history?last=30&provider=2&status=error  — combined filters"
history_summary "$CONTROL/history?last=30&provider=2&status=error"
pause

# ═════════════════════════════════════════════════════════════════════════════
# 5. Isolate one specific request using a rare method
#
# Note: history is never truly empty (router background traffic fills it
# at ~8 entries/sec). We use eth_gasPrice — a method the router never sends
# in background — so the filter shows only our call.
# ═════════════════════════════════════════════════════════════════════════════

header "5a. Reset to clean scenario"
curl -si -X POST "$CONTROL/reset/all"
pause

header "5b. Send ONE request using eth_gasPrice  (router never sends this in background)"
rpc_rare
pause

header "5c. Filter by method=eth_gasPrice  →  count MUST be exactly 1"
curl -s "$CONTROL/history?method=eth_gasPrice" | python3 -m json.tool
pause

# ═════════════════════════════════════════════════════════════════════════════
# 6. Stats
# ═════════════════════════════════════════════════════════════════════════════

header "6. GET /stats  — all-time call counters per provider"
curl -s "$CONTROL/stats" | python3 -m json.tool
pause

# ═════════════════════════════════════════════════════════════════════════════
# Done — clean up
# ═════════════════════════════════════════════════════════════════════════════

header "✅  Done — running final reset/all to leave clean state"
curl -si -X POST "$CONTROL/reset/all"
echo ""
echo "All steps complete."