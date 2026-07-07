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

import threading
import time
from typing import Any, Dict, Tuple

from stubs import ETH_ERROR_STUBS, ETH_METHOD_DEFAULTS


# --- MAG-1897: optional advancing eth head ---------------------------------
# The simulated eth head is normally a STATIC constant
# (ETH_METHOD_DEFAULTS["eth_blockNumber"] = "0x1312D00"). A test that needs the
# router's per-endpoint sync optimizer to actually DEMOTE a stale provider must
# let the head MOVE: the optimizer's forward-only sync ratchet only releases a
# provider's lag as the cluster head advances past it, so on a static head a
# stale provider's internal SyncBlock stays pinned at the head and never earns a
# sync penalty (it scores normalized_sync == 1, same as healthy peers).
#
# This clock is OPT-IN (default static => byte-identical to the old behaviour)
# and driven via the control API:
#   POST /advance {"per_second": R}  -> enable (R>0) / freeze (R<=0) continuous advance
#   POST /advance {"blocks": N}      -> one-time bump of the head by N blocks
#   POST /reset and /reset/all       -> reset the head to its static base
# current_eth_head() is the single source the eth success-path reads for the head.
_HEAD_LOCK = threading.Lock()
_head_base = int(ETH_METHOD_DEFAULTS["eth_blockNumber"], 16)  # 20_000_000
_head_extra = 0      # manual bumps + folded continuous advance (blocks above base)
_head_rate = 0.0     # continuous advance, blocks/sec (0.0 = off => static head)
_head_anchor = 0.0   # time.monotonic() when the current rate took effect


def current_eth_head() -> int:
    """Current simulated eth head (int). Equals the static base unless advancing
    has been enabled via POST /advance (MAG-1897)."""
    with _HEAD_LOCK:
        extra = _head_extra
        if _head_rate > 0.0:
            extra += int((time.monotonic() - _head_anchor) * _head_rate)
        return _head_base + extra


def set_eth_advance(rate_per_sec: float) -> None:
    """Enable (rate>0) or freeze (rate<=0) continuous head advance. Folds elapsed
    advance into the static offset so toggling never moves the head backward."""
    global _head_extra, _head_rate, _head_anchor
    with _HEAD_LOCK:
        if _head_rate > 0.0:
            _head_extra += int((time.monotonic() - _head_anchor) * _head_rate)
        _head_rate = float(rate_per_sec) if rate_per_sec and rate_per_sec > 0.0 else 0.0
        _head_anchor = time.monotonic()


def bump_eth_head(blocks: int) -> None:
    """One-time advance of the head by ``blocks`` (independent of the continuous rate)."""
    global _head_extra
    with _HEAD_LOCK:
        _head_extra += int(blocks)


def reset_eth_head() -> None:
    """Reset the head to its static base and disable advancing (POST /reset[/all])."""
    global _head_extra, _head_rate
    with _HEAD_LOCK:
        _head_extra = 0
        _head_rate = 0.0


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
    #   1. Named catalogue (primary, mirrors how ETH_METHOD_DEFAULTS works):
    #          responses[method] = {"error_stub": "revert"}
    #      The simulator resolves the name against its local ETH_ERROR_STUBS
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
        err = ETH_ERROR_STUBS[method_cfg["error_stub"]]
    elif "error" in method_cfg:
        err = method_cfg["error"]
    if err is not None:
        http_st = method_cfg.get("http_status", 200)
        return http_st, {"jsonrpc": "2.0", "id": req_id, "error": err}

    result = method_cfg.get("result", ETH_METHOD_DEFAULTS.get(method, "0x1"))

    blocks_behind = snap.get("blocks_behind", 0)

    # eth_blockNumber: report the (optionally advancing — MAG-1897) head shifted by
    # blocks_behind. Skipped only when a response result is explicitly configured
    # (specific or via "default"), preserving the response-override path. With a
    # static head and blocks_behind=0 this yields the same "0x1312D00" as before.
    if method == "eth_blockNumber" and "result" not in method_cfg:
        result = _hex_upper(current_eth_head() - blocks_behind)

    # eth_getBlockByNumber: echo the requested block number so the router's
    # pruning verification sees the correct block number in the response.
    # Named tags ("latest"/"safe"/"pending"/"finalized") shift by blocks_behind.
    if method == "eth_getBlockByNumber" and isinstance(result, dict):
        if params:
            head = current_eth_head()
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
            head_int = current_eth_head() - blocks_behind
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
