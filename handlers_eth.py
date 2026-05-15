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

Per-method failure shapes (Q7 discipline from MAG-1716)
-------------------------------------------------------
The two stateful per-method behaviours this handler injects on the success
path — read this when reasoning about a test that's not behaving as expected:

  eth_blockNumber
      blocks_behind=N → returns hex(head - N). Default N=0 = current head.
      No interaction with logs_indexed_up_to — the head is always reported
      truthfully relative to blocks_behind even when logs are lagged.

  eth_getBlockByNumber
      blocks_behind=N → "latest"/"safe"/"finalized"/"pending" tags resolve to
      shifted heights; explicit hex block numbers are echoed back verbatim
      in result["number"] so the router's pruning-verification sees the
      requested height.

  eth_getLogs (MAG-1791)
      logs_indexed_up_to=None (default) → unchanged: returns whatever the
      configured response or METHOD_DEFAULTS["eth_getLogs"] yields (today
      that's an empty list; tests overriding ``responses`` see their payload).

      logs_indexed_up_to=K AND toBlock > K:
        - logs_lag_mode="empty"   → result = [] (the most common production
                                     failure shape: provider claims "no logs
                                     in that range" while head is fresh).
        - logs_lag_mode="partial" → filter the configured response to entries
                                     with int(entry["blockNumber"], 16) <= K.

      "head-fresh + logs-lagged" is the divergence this primitive exposes —
      a real production hazard for Kraken-CCIP-style consumers that poll
      ``eth_getLogs`` to detect cross-chain events. Cross-validation catches
      this when enabled (response divergence); without CV, the bug surfaces
      as silently-missed events. See MAG-1791 for the test bundle that
      exercises both paths.
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

    # eth_getLogs (MAG-1791): apply logs_indexed_up_to / logs_lag_mode when set.
    # Models the head-fresh + logs-lagged divergence — provider answers
    # eth_blockNumber correctly but lags on event indexing.
    #
    # Resolution rules:
    #   - logs_indexed_up_to=None → no change (today's behaviour). Bypass.
    #   - Resolve toBlock from params[0]:
    #       params shape: [{"fromBlock": <hex|tag>, "toBlock": <hex|tag>, ...}]
    #       "latest" / "safe" / "finalized" / "pending" → effective current head
    #         shifted by blocks_behind (matches eth_blockNumber's reported head).
    #       hex string → int(hex, 16).
    #       Missing toBlock → treat as "latest".
    #   - If resolved toBlock <= logs_indexed_up_to → no lag in range, no change.
    #   - If resolved toBlock > logs_indexed_up_to:
    #       mode="empty"   → return result=[].
    #       mode="partial" → filter list-shaped result, keep only entries whose
    #                        blockNumber (parsed as hex) is <= logs_indexed_up_to.
    #                        If result isn't a list, fall back to empty (safe
    #                        fallback so tests with unusual response overrides
    #                        don't blow up — though the canonical path uses lists).
    if method == "eth_getLogs":
        logs_indexed = snap.get("logs_indexed_up_to")
        if logs_indexed is not None:
            head_int = int(METHOD_DEFAULTS["eth_blockNumber"], 16) - blocks_behind
            # Resolve toBlock — the upper bound of the query range
            to_block = _resolve_block_tag(params, "toBlock", head_int)
            if to_block is not None and to_block > logs_indexed:
                mode = snap.get("logs_lag_mode", "empty")
                if mode == "partial" and isinstance(result, list):
                    result = [
                        entry for entry in result
                        if _entry_blocknum_le(entry, logs_indexed)
                    ]
                else:
                    # "empty" (default) — or any non-list result on partial mode
                    result = []

    return snap.get("http_status", 200), {"jsonrpc": "2.0", "id": req_id, "result": result}


def _resolve_block_tag(params: list, key: str, head_int: int):
    """Resolve a fromBlock/toBlock value from eth_getLogs params to an int.

    Returns None when the key cannot be resolved (no params, key absent,
    unparseable value) — caller treats None as "don't apply lag filtering".

    Tags ("latest", "safe", "finalized", "pending") resolve to ``head_int``
    so a query like ``{"toBlock": "latest"}`` is treated as touching the
    current head — which is exactly where logs-indexing-lag exposes itself.
    """
    if not params or not isinstance(params[0], dict):
        return None
    raw = params[0].get(key, "latest")
    if isinstance(raw, str):
        if raw in ("latest", "safe", "finalized", "pending"):
            return head_int
        try:
            return int(raw, 16) if raw.startswith("0x") else int(raw)
        except (ValueError, TypeError):
            return None
    if isinstance(raw, int):
        return raw
    return None


def _entry_blocknum_le(entry: dict, threshold: int) -> bool:
    """Return True if entry["blockNumber"] parses to an int <= threshold.

    Defensive: tolerates missing/malformed blockNumber by treating it as
    failing the threshold check (entry is excluded from the partial-mode
    response). Real log entries always carry a hex blockNumber; this guard
    is here only to keep the simulator robust when tests override
    ``responses["eth_getLogs"]["result"]`` with hand-crafted dicts.
    """
    if not isinstance(entry, dict):
        return False
    raw = entry.get("blockNumber")
    if isinstance(raw, int):
        return raw <= threshold
    if isinstance(raw, str):
        try:
            return int(raw, 16) <= threshold
        except (ValueError, TypeError):
            return False
    return False
