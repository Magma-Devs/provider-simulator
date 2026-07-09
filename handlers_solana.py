"""
handlers_solana.py — Solana success-branch dispatch for the provider simulator.

Sibling of ``handlers_eth`` / ``handlers_btc`` / ``handlers_lnd``. Same public
surface:

    handle(state, request, snap, lava_headers) -> tuple[int, dict]

Why Solana needs its own handler
--------------------------------
The smart-router's Solana consistency filter compares two numbers that come
from two different places in a single relay reply:

  * the per-user ``seenBlock`` — parsed from ``result.context.slot``.
  * the endpoint chain-tracker value — parsed from
    ``result.value.lastValidBlockHeight``.

On real Solana mainnet these two differ by roughly 22 million: the slot is the
monotonic ledger position (~419M and climbing) while ``lastValidBlockHeight``
is the smaller block-height counter (~397M). The router treats the gap as a
sync problem: when it exceeds the 50-block consistency threshold every endpoint
is filtered out and the client gets "No pairings available".

To reproduce that deterministically the ``getLatestBlockhash`` stub here emits
BOTH numbers from one response with a configurable gap. ``solana_slot_block_gap``
(read off the provider snapshot, default 21_900_000) sets the distance:

    slot                 = S
    lastValidBlockHeight = S - gap

so a test can dial the gap above or below the router's threshold and watch the
filter fire or pass.

Method scope (MAG-2231)
-----------------------
Four methods — just enough for the router's Solana spec verification plus the
``getLatestBlockhash`` divergence that drives the bug:

  * ``getLatestBlockhash`` — the divergence carrier (context.slot vs
    value.lastValidBlockHeight).
  * ``getSlot``            — current slot scalar.
  * ``getHealth``          — "ok".
  * ``getVersion``         — small version object.

Real Solana account/transaction/program state simulation is out of scope —
every response is a canned stub. Tests that need richer state override
per-method via ``responses[method] = {"result": ...}``.

Fault handling
--------------
``mode="down"`` (provider-wide unreachable) is honored upstream in
``server.py`` BEFORE this handler is reached — the JSON-RPC listener emits the
transport failure itself, identical to the BTC / LN / ETH handlers, so this
module never sees a down request. The success path below is the only thing
this module owns; per-method error overrides (``responses[method] =
{"error": ...}``) are honored the same way the sibling handlers honor them.
"""

from typing import Any, Dict, Tuple

import stubs_solana

# The Solana chain constants live in stubs_solana — the Solana member of the
# stubs_* family, next to stubs_btc / stubs_lnd. Re-exported here so existing
# ``from handlers_solana import SOLANA_...`` importers keep working; new code
# should import them from stubs_solana directly.
SOLANA_BASE_SLOT = stubs_solana.SOLANA_BASE_SLOT
SOLANA_DEFAULT_SLOT_BLOCK_GAP = stubs_solana.SOLANA_DEFAULT_SLOT_BLOCK_GAP
SOLANA_BLOCKHASH = stubs_solana.SOLANA_BLOCKHASH
SOLANA_CORE_VERSION = stubs_solana.SOLANA_CORE_VERSION
SOLANA_FEATURE_SET = stubs_solana.SOLANA_FEATURE_SET

# Named Solana JSON-RPC error catalogue — the inner ``error`` objects a test can
# inject by name via ``responses[method] = {"error_stub": "<name>"}`` (mirrors
# stubs.ERROR_STUBS for ETH). Single source of truth for the error envelope the
# router's Solana classifier is tested against. Codes follow Solana's documented
# JSON-RPC errors: -32005 / -32007 / -32009 are node/ledger transients the router
# should RETRY; -32016 / -32002 are client/tx errors it should fast-fail;
# -32601 / -32602 are the JSON-RPC standard method / params errors.
SOLANA_ERROR_STUBS: Dict[str, Dict[str, Any]] = {
    "method_not_found": {"code": -32601, "message": "Method not found"},
    "invalid_params": {"code": -32602, "message": "Invalid params"},
    "node_behind": {
        "code": -32005,
        "message": "Node is behind by 100 slots",
        "data": {"numSlotsBehind": 100},
    },
    "slot_skipped": {
        "code": -32007,
        "message": "Slot 123456789 was skipped, or missing due to ledger jump to recent snapshot",
    },
    "long_term_storage_slot_skipped": {
        "code": -32009,
        "message": "Slot 123456789 was skipped, or missing in long-term storage",
    },
    "min_context_slot_not_reached": {
        "code": -32016,
        "message": "Minimum context slot has not been reached",
    },
    "transaction_simulation_failed": {
        "code": -32002,
        "message": "Transaction simulation failed",
    },
    "blockhash_not_found": {
        "code": -32002,
        "message": "Transaction simulation failed: Blockhash not found",
        "data": {"err": "BlockhashNotFound"},
    },
}


def handle(state, request: dict, snap: dict, lava_headers: dict) -> Tuple[int, Dict[str, Any]]:
    """Resolve the Solana success-path response for one JSON-RPC request.

    Args:
        state:         The live ``ProviderState`` — read for ``state.responses``
                       under ``state.lock``. Same contract as
                       ``handlers_btc.handle``.
        request:       Parsed JSON-RPC body (``method``, ``params`` optional).
        snap:          ``ProviderState.snapshot()`` dict — read for
                       ``solana_slot_block_gap`` and ``http_status``.
        lava_headers:  Captured ``lava-*`` headers, threaded through for symmetry.

    Returns:
        ``(http_status, response_body)``. Either the success envelope with the
        method's stub or the error envelope when the test override emits one via
        ``responses[method] = {"error": ...}``.
    """
    req_id = request.get("id", 1)
    method = request.get("method", "unknown")

    # Look up method-specific override (named or default) — mirrors the BTC / LN
    # handlers. Read under the lock because state.responses is mutated by
    # /scenario from another thread.
    with state.lock:
        method_cfg = state.responses.get(method) or state.responses.get("default", {})

    # Per-method error override — mirrors handlers_eth.handle. Two ways to inject
    # an error on one method: a named catalogue entry (``error_stub``) resolved
    # against SOLANA_ERROR_STUBS, or a raw ``error`` envelope. An unknown stub
    # name raises KeyError so a typo fails loudly instead of falling through.
    err = None
    if "error_stub" in method_cfg:
        err = SOLANA_ERROR_STUBS[method_cfg["error_stub"]]
    elif "error" in method_cfg:
        err = method_cfg["error"]
    if err is not None:
        http_st = method_cfg.get("http_status", 200)
        return http_st, {"jsonrpc": "2.0", "id": req_id, "error": err}

    # Explicit per-method result override wins over the computed stub.
    if "result" in method_cfg:
        return snap.get("http_status", 200), {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": method_cfg["result"],
        }

    slot = _solana_slot(snap)

    if method == "getLatestBlockhash":
        # The divergence carrier. context.slot and value.lastValidBlockHeight are
        # separated by the configured gap so the router's consistency filter sees
        # a per-user seenBlock (slot) far ahead of the chain-tracker value
        # (lastValidBlockHeight). gap defaults to SOLANA_DEFAULT_SLOT_BLOCK_GAP.
        gap = snap.get("solana_slot_block_gap", SOLANA_DEFAULT_SLOT_BLOCK_GAP)
        result = {
            "context": {"slot": slot},
            "value": {
                "blockhash": SOLANA_BLOCKHASH,
                "lastValidBlockHeight": slot - gap,
            },
        }
    elif method == "getSlot":
        result = slot
    elif method == "getHealth":
        result = "ok"
    elif method == "getVersion":
        result = {
            "solana-core": SOLANA_CORE_VERSION,
            "feature-set": SOLANA_FEATURE_SET,
        }
    else:
        # Unknown method. Default: parse-friendly null result (backward-compat,
        # mirroring the BTC / LN fallback). Opt-in via
        # solana_unknown_method_mode="error": return a real -32601 method-not-
        # found so the router's Solana error classifier can be exercised.
        if snap.get("solana_unknown_method_mode") == "error":
            return snap.get("http_status", 200), {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": SOLANA_ERROR_STUBS["method_not_found"],
            }
        result = None

    return snap.get("http_status", 200), {"jsonrpc": "2.0", "id": req_id, "result": result}


def _solana_slot(snap: dict) -> int:
    """Return the effective Solana slot for this provider's request.

    ``SOLANA_BASE_SLOT + solana_slot_offset`` (offset read off the provider
    snapshot, default 0). The base is a fixed realistic mainnet slot; the
    per-provider offset shifts THIS provider's reported slot off that base so a
    test can stand up multiple providers at different slots (one current, others
    stale-behind) and watch the router's Solana consistency filter keep the
    current provider and drop the stale ones. Offset 0 ⇒ the base verbatim, so
    every provider sits at the same slot (no divergence). Negative = behind the
    base, positive = ahead.

    The slot stays fixed per request (the simulator does not advance it off the
    wall clock) so tests can pin exact equality; the offset and the
    slot ↔ lastValidBlockHeight gap are the only moving parts under test.
    """
    return SOLANA_BASE_SLOT + snap.get("solana_slot_offset", 0)
