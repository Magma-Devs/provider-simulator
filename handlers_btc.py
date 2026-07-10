"""
handlers_btc.py — Bitcoin success-branch dispatch for the provider simulator.

Sibling of ``handlers_eth``. Same public surface:

    handle(state, request, snap, lava_headers) -> tuple[int, dict]

Routes the JSON-RPC request to ``BTC_METHOD_DEFAULTS`` from ``stubs_btc``,
applying:

- ``blocks_behind`` shifts for height-tracking methods (``getblockcount``,
  ``getblockhash``, ``getbestblockhash``, ``getblockchaininfo.blocks``).
- Block-hash echo for ``getblock`` and ``getblockheader`` so a request for
  block N gets a response with ``height = N`` and ``hash = btc_block_hash(N)``
  (mirrors the ETH ``eth_getBlockByNumber`` echo behaviour).
- Per-method ``state.responses`` overrides (named error stub, raw error
  envelope, or custom result) — same precedence as the ETH handler.

Why a separate module
---------------------
ETH method-name namespace (``eth_*`` / ``net_*`` / ``web3_*`` / ``trace_*`` /
``debug_*``) doesn't overlap with the BTC namespace (no prefix; raw verb names
like ``getblockcount``), so a runtime lookup against a merged dict would work
in practice — but a dedicated module keeps the chain-specific quirks
(int-vs-hex, no ``"0x"`` prefix on hashes, height-as-decimal) cleanly contained
and makes the seam easy to grep when REST / gRPC sims (MAG-1777, MAG-1780)
ship with their own handler modules.
"""

from copy import deepcopy
from typing import Any, Dict, Tuple

from stubs_btc import BTC_ERROR_STUBS, BTC_METHOD_DEFAULTS, btc_block_hash

# Methods whose default value is a height that should be shifted by
# state.blocks_behind. Listed once so we can adjust the rule centrally.
_HEIGHT_METHODS = {"getblockcount"}

# Methods whose default value is a block hash that should be re-computed from
# the (effective) chain head when blocks_behind is non-zero.
_HEAD_HASH_METHODS = {"getbestblockhash", "getblockhash"}


def handle(state, request: dict, snap: dict, lava_headers: dict) -> Tuple[int, Dict[str, Any]]:
    """Resolve the Bitcoin success-path response for one JSON-RPC request.

    Args:
        state:         The live ``ProviderState`` — read for ``state.responses``
                       under ``state.lock``. Same contract as ``handlers_eth.handle``.
        request:       Parsed JSON-RPC body (``method``, ``params`` optional).
        snap:          ``ProviderState.snapshot()`` dict — read for
                       ``blocks_behind`` and ``http_status``.
        lava_headers:  Captured ``lava-*`` headers, threaded through for symmetry.

    Returns:
        ``(http_status, response_body)``. Either the success envelope with the
        method's stub (possibly shifted by ``blocks_behind`` or echoed from
        ``params``) or the error envelope when the test override emits one via
        ``responses[method] = {"error_stub": ...}`` / ``{"error": ...}``.
    """
    req_id = request.get("id", 1)
    method = request.get("method", "unknown")
    params = request.get("params", [])

    # Look up method-specific override (named or default)
    with state.lock:
        method_cfg = state.responses.get(method) or state.responses.get("default", {})

    # Per-method error override path — mirrors handlers_eth.handle.
    err = None
    if "error_stub" in method_cfg:
        err = BTC_ERROR_STUBS[method_cfg["error_stub"]]
    elif "error" in method_cfg:
        err = method_cfg["error"]
    if err is not None:
        http_st = method_cfg.get("http_status", 200)
        return http_st, {"jsonrpc": "2.0", "id": req_id, "error": err}

    # Success result — explicit override > BTC_METHOD_DEFAULTS > "0x1" sentinel.
    # NOTE: BTC fallback sentinel is the integer 0, not "0x1" — BTC methods
    # return decimal numbers, hex strings, or dicts depending on the call.
    # Unknown methods get a JSON-RPC parse-friendly null result.
    if "result" in method_cfg:
        result = method_cfg["result"]
    elif method in BTC_METHOD_DEFAULTS:
        # deepcopy so per-request mutations (block-hash echo) don't leak into
        # the shared defaults dict.
        result = deepcopy(BTC_METHOD_DEFAULTS[method])
    else:
        result = None

    blocks_behind = snap.get("blocks_behind", 0)
    effective_head = _btc_head(blocks_behind)

    # blocks_behind shifts — only apply if the test hasn't overridden the
    # response explicitly via /scenario.
    if method not in state.responses and blocks_behind != 0:
        if method in _HEIGHT_METHODS:
            result = effective_head
        elif method in _HEAD_HASH_METHODS:
            # getblockhash takes a height param; honour it if present, else
            # fall back to the effective head.
            if method == "getblockhash" and params:
                result = btc_block_hash(int(params[0]))
            else:
                result = btc_block_hash(effective_head)
        elif method == "getblockchaininfo" and isinstance(result, dict):
            result["blocks"] = effective_head
            result["headers"] = effective_head
            result["bestblockhash"] = btc_block_hash(effective_head)
        elif method == "getblockheader" and isinstance(result, dict):
            result["height"] = effective_head
            result["hash"] = btc_block_hash(effective_head)
        elif method == "getblock" and isinstance(result, dict):
            result["height"] = effective_head
            result["hash"] = btc_block_hash(effective_head)

    # getblockhash: echo the requested height back in the hash so the router's
    # pruning verification can confirm it received the asked-for block.
    if method == "getblockhash" and params and method not in state.responses:
        try:
            height = int(params[0])
        except (TypeError, ValueError):
            return 200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": BTC_ERROR_STUBS["invalid_parameter"],
            }
        result = btc_block_hash(height)

    # getblock / getblockheader: echo the requested block hash + height.
    # Bitcoin Core's getblock takes a HASH string (not a height). Tests that
    # pass a hash from a previous getblockhash call will see it reflected.
    if (
        method in ("getblock", "getblockheader")
        and params
        and isinstance(result, dict)
        and method not in state.responses
    ):
        block_hash = params[0]
        if isinstance(block_hash, str):
            result = dict(result)
            result["hash"] = block_hash

    return snap.get("http_status", 200), {"jsonrpc": "2.0", "id": req_id, "result": result}


def _btc_head(blocks_behind: int) -> int:
    """Return the effective Bitcoin chain head after applying blocks_behind.

    Mirrors handlers_eth's hex-arithmetic but in decimal: ETH expresses the
    head as a hex string, BTC expresses it as a JSON number.
    """
    from constants import BTC_LATEST_BLOCK

    return BTC_LATEST_BLOCK - blocks_behind
