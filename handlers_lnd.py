"""
handlers_lnd.py — Lightning Network (LND) success-branch dispatch.

Sibling of ``handlers_eth`` and ``handlers_btc``. Same public surface:

    handle(state, request, snap, lava_headers) -> tuple[int, dict]

Routes the JSON-RPC request to ``LND_METHOD_DEFAULTS`` from ``stubs_lnd``,
applying:

- ``blocks_behind`` shift on ``getinfo.block_height`` so a stale-on-chain LN
  node still reports a consistent (BTC-chain-tracking) head — mirrors the BTC
  handler's ``blocks_behind`` semantics.
- ``decodepayreq`` echoes the requested invoice into the response so tests can
  assert round-trip behaviour without lifting real bolt11 bytes.
- ``openchannel`` echoes the remote pubkey from params when provided.
- Per-method ``state.responses`` overrides (named error stub, raw error
  envelope, or custom result) — same precedence as handlers_btc.

Why a separate module
---------------------
LN method namespace (``getinfo``, ``listchannels``, ``openchannel``,
``decodepayreq``, ``payinvoice``, ``listpeers``) overlaps with BTC L1's
``getinfo`` if the dispatcher merged dicts — keeping LN behind ``chain_family
== "ln"`` and routing through this module ensures BTC test scenarios don't
accidentally see an LN-shaped ``getinfo`` response. The module also acts as
the seam where future LN-impl divergence (lnd vs c-lightning vs eclair) can
be added without bloating handlers_btc.

Out of scope (MAG-1726)
-----------------------
- Real payment-channel state — every response is a canned stub.
- Gossip layer (``channel_announcement`` / ``node_announcement``).
- Onion routing — canned route data only.
"""

from copy import deepcopy
from typing import Any, Dict, Tuple

from stubs_lnd import LND_ERROR_STUBS, LND_METHOD_DEFAULTS


# Methods whose default carries a block_height field tracking the underlying
# BTC chain. blocks_behind shifts these the same way it shifts BTC's getblockcount.
_HEIGHT_METHODS = {"getinfo"}


def handle(state, request: dict, snap: dict, lava_headers: dict) -> Tuple[int, Dict[str, Any]]:
    """Resolve the LN success-path response for one JSON-RPC request.

    Args:
        state:         The live ``ProviderState`` — read for ``state.responses``
                       under ``state.lock``. Same contract as handlers_btc.handle.
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

    # Per-method error override path — mirrors handlers_btc.handle.
    err = None
    if "error_stub" in method_cfg:
        err = LND_ERROR_STUBS[method_cfg["error_stub"]]
    elif "error" in method_cfg:
        err = method_cfg["error"]
    if err is not None:
        http_st = method_cfg.get("http_status", 200)
        return http_st, {"jsonrpc": "2.0", "id": req_id, "error": err}

    # Success result — explicit override > LND_METHOD_DEFAULTS > null sentinel.
    # Mirrors handlers_btc: unknown methods get a parse-friendly null rather
    # than an error, so the router sees a well-formed but empty response.
    if "result" in method_cfg:
        result = method_cfg["result"]
    elif method in LND_METHOD_DEFAULTS:
        # deepcopy so per-request mutations (block_height shift, invoice echo,
        # pubkey echo) don't leak into the shared defaults dict.
        result = deepcopy(LND_METHOD_DEFAULTS[method])
    else:
        result = None

    blocks_behind = snap.get("blocks_behind", 0)

    # blocks_behind shift — only apply if the test hasn't overridden the
    # response explicitly via /scenario.
    if method not in state.responses and blocks_behind != 0:
        if method in _HEIGHT_METHODS and isinstance(result, dict):
            # getinfo carries block_height — the BTC chain head as the LN node
            # sees it. Shift it the same way BTC's getblockcount is shifted.
            from constants import LN_BLOCK_HEIGHT
            result["block_height"] = LN_BLOCK_HEIGHT - blocks_behind
            # synced_to_chain flips to False when the node is meaningfully
            # behind — real LND reports this when its underlying btcd/bitcoind
            # peer lags. Threshold of 1 block matches LND's own >= 6-block
            # lag-warning policy collapsed to "any positive lag = unsynced".
            if blocks_behind != 0:
                result["synced_to_chain"] = False

    # decodepayreq: echo the requested invoice into the response so tests can
    # confirm the simulator received what they sent. Real LND parses the
    # bolt11 string and emits derived fields; we keep it round-trip-friendly
    # by stashing the input in `payment_request` (LND's canonical name for the
    # echoed invoice string).
    if method == "decodepayreq" and params and method not in state.responses \
            and isinstance(result, dict):
        invoice = params[0] if params else ""
        if isinstance(invoice, str) and invoice:
            result = dict(result)
            result["payment_request"] = invoice

    # openchannel: when the caller passes a remote pubkey in params, echo it
    # back so the channel-point response is associated with the right peer.
    # LND's wire shape is positional (params[0]=node_pubkey, params[1]=amount),
    # so we read params[0] when present.
    if method == "openchannel" and params and method not in state.responses \
            and isinstance(result, dict):
        node_pubkey = params[0] if params else ""
        if isinstance(node_pubkey, str) and node_pubkey:
            result = dict(result)
            result["node_pubkey"] = node_pubkey

    # payinvoice: echo the requested invoice string so tests can correlate the
    # request with the response in /history without parsing the body. Stored
    # under `payment_request` (same convention as decodepayreq).
    if method == "payinvoice" and params and method not in state.responses \
            and isinstance(result, dict):
        invoice = params[0] if params else ""
        if isinstance(invoice, str) and invoice:
            result = dict(result)
            result["payment_request"] = invoice

    return snap.get("http_status", 200), {"jsonrpc": "2.0", "id": req_id, "result": result}
