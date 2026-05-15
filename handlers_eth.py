"""
handlers_eth.py — Ethereum success-branch dispatch for the provider simulator.

This module owns the chain-specific success path that runs AFTER all fault
branches in ``JSONRPCHandler.do_POST`` (down / hang / drop / rate-limit /
forced-error / probabilistic-error) have been evaluated and skipped.

Public surface — a single function:

    handle(state, request, snap, lava_headers) -> tuple[int, dict]

The handler returns ``(http_status, response_body)``; the caller (``do_POST``)
is responsible for the actual HTTP write, history accounting, and corruption
emission. Keeping I/O outside this module makes it trivial to add ``handlers_btc``
and any future chain handlers (REST / gRPC sims tracked under MAG-1777 / MAG-1780)
with the same signature.

Why the seam is here (not earlier)
----------------------------------
Fault branches (mode==down / hang / drop_connection / rate_limit / mode==error /
probabilistic error) are **chain-agnostic**: they apply identically to ETH and
BTC requests. Keeping them in ``do_POST`` avoids duplicating that logic per
chain. Only the success path (look up a method-specific result, apply
``blocks_behind`` shifts, echo back the requested block number) is chain-specific
and lives here.
"""

from typing import Any, Dict, Tuple

from stubs import ERROR_STUBS, METHOD_DEFAULTS


def _hex_upper(n: int) -> str:
    """Format an int as an upper-case hex string ("0x" + uppercase digits).

    Matches the legacy hardcoded ETH values (e.g. "0x1312D00") so existing
    tests that assert exact-string equality keep passing.
    """
    return "0x" + format(n, "X")


def handle(state, request: dict, snap: dict, lava_headers: dict) -> Tuple[int, Dict[str, Any]]:
    """Resolve the Ethereum success-path response for one JSON-RPC request.

    Args:
        state:         The live ``ProviderState`` (for ``state.responses`` and
                       ``state.lock``). Mutations are *not* expected here; we
                       only read the per-method overrides under the lock.
        request:       Parsed JSON-RPC request body (must contain ``method``;
                       ``params`` is optional).
        snap:          Snapshot dict from ``ProviderState.snapshot()`` —
                       used for ``blocks_behind`` and ``http_status``.
        lava_headers:  Captured ``lava-*`` request headers (currently unused
                       in the ETH success path but threaded through for
                       symmetry with the handler signature).

    Returns:
        ``(http_status, response_body)``. The body is the JSON-RPC envelope
        either ``{"jsonrpc": "2.0", "id": ..., "result": ...}`` for the success
        path, or ``{"jsonrpc": "2.0", "id": ..., "error": ...}`` for the
        per-method error-override path (responses[method] = {"error_stub": ...}
        or responses[method] = {"error": ...}).
    """
    req_id = request.get("id", 1)
    method = request.get("method", "unknown")
    params = request.get("params", [])

    # Look up method-specific result
    with state.lock:
        method_cfg = state.responses.get(method) or state.responses.get("default", {})

    # Per-method error override (Phase 1.4 chain-domain errors).
    #
    # Two ways for a test to inject an error on one method while leaving
    # other methods on their success path:
    #
    #   1. Named catalogue (primary, mirrors how METHOD_DEFAULTS works):
    #          responses[method] = {"error_stub": "revert"}
    #      The simulator resolves the name against its local ERROR_STUBS
    #      dict — single source of truth for envelope content.
    #
    #   2. Raw envelope (escape-hatch for ad-hoc shapes that don't earn
    #      a permanent catalogue entry):
    #          responses[method] = {"error": {"code": -32099, "message": "..."}}
    #
    # Unknown stub name raises KeyError — the test gets a loud failure
    # rather than a silent fallback (typo visibility).
    err = None
    if "error_stub" in method_cfg:
        err = ERROR_STUBS[method_cfg["error_stub"]]
    elif "error" in method_cfg:
        err = method_cfg["error"]
    if err is not None:
        http_st = method_cfg.get("http_status", 200)
        return http_st, {"jsonrpc": "2.0", "id": req_id, "error": err}

    result = method_cfg.get("result", METHOD_DEFAULTS.get(method, "0x1"))

    blocks_behind = snap.get("blocks_behind", 0)

    # eth_blockNumber: shift head by blocks_behind unless overridden via responses
    if method == "eth_blockNumber" and method not in state.responses and blocks_behind != 0:
        head = int(METHOD_DEFAULTS["eth_blockNumber"], 16)
        result = _hex_upper(head - blocks_behind)

    # eth_getBlockByNumber: echo the requested block number so the router's
    # pruning verification sees the correct block number in the response.
    # Named tags ("latest"/"safe"/"pending"/"finalized") shift by blocks_behind.
    if method == "eth_getBlockByNumber" and isinstance(result, dict):
        if params:
            head = int("0x1312D00", 16)
            effective_latest = _hex_upper(head - blocks_behind)
            named = {
                "latest":    effective_latest,
                "earliest":  "0x0",
                "pending":   _hex_upper(head - blocks_behind + 1),
                "safe":      effective_latest,
                "finalized": _hex_upper(head - blocks_behind - 1),
            }
            result = dict(result)
            result["number"] = named.get(params[0], params[0])

    return snap.get("http_status", 200), {"jsonrpc": "2.0", "id": req_id, "result": result}
