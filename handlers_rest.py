"""
handlers_rest.py — REST success-branch dispatch for the provider simulator (MAG-1777).

Sibling of ``handlers_eth`` and ``handlers_btc``. Same public surface, REST
shape:

    handle(state, verb, template, path_params, query, body, snap, lava_headers)
        -> tuple[int, dict]

The caller (``RestHandler`` in ``server.py``) is responsible for the HTTP
write itself plus history accounting; this module decides the response body
and HTTP status given a parsed REST request.

Path matching happens in the caller — by the time ``handle`` runs we already
know which ``(verb, template_str)`` matched, and any ``{var}`` placeholders
have been peeled off into ``path_params``. The handler uses ``path_params``
for echo-style endpoints (``/balances/{address}``) and ``query`` (parsed
``?key=value`` dict) for pagination-style endpoints.

What the handler does, in order
-------------------------------
1. Look up ``state.responses[(verb, template)]`` for a test-supplied override.
2. If the override carries a named ``error_stub`` (resolved against
   ``REST_ERROR_STUBS``) or a raw ``error`` envelope, return it with the
   configured HTTP status.
3. If the override carries a ``body`` key, return that body with the
   configured ``http_status`` (default 200; ``status`` is accepted as a
   deprecated fallback — ``http_status`` wins when both are present).
4. Otherwise resolve the stub from ``REST_METHOD_DEFAULTS`` (deep-copied so
   per-request mutations don't leak), apply path-specific echo / shift /
   blocks_behind logic, and return it.

Why a separate module
---------------------
Same rationale as ``handlers_btc``: REST has its own naming convention
(verb + path templates) and response shapes (no JSON-RPC envelope, no
``id`` field to echo). Cramming REST dispatch into ``handlers_eth`` would
have forced if-branches on every line. A dedicated module makes the seam
easy to grep when gRPC sims (MAG-1780) ship their own handler.
"""

from copy import deepcopy
from typing import Any, Dict, Tuple

from stubs_rest import REST_ERROR_STUBS, REST_METHOD_DEFAULTS


def handle(
    state,
    verb: str,
    template: str,
    path_params: Dict[str, str],
    query: Dict[str, list],
    body: Any,
    snap: Dict[str, Any],
    lava_headers: Dict[str, str],
) -> Tuple[int, Dict[str, Any]]:
    """Resolve the REST success-path response for one matched request.

    Args:
        state:        Live ``ProviderState`` — read for ``state.responses``
                      under ``state.lock`` for per-(verb, template) overrides.
        verb:         HTTP verb in upper-case (``"GET"`` / ``"POST"`` / …).
        template:     The path template that matched (e.g.
                      ``"/cosmos/bank/v1beta1/balances/{address}"``). Used as
                      the lookup key into ``REST_METHOD_DEFAULTS`` and
                      ``state.responses``.
        path_params:  ``{"address": "cosmos1abc...", ...}`` — parsed ``{var}``
                      placeholders from the live URL.
        query:        Parsed query string from ``urllib.parse.parse_qs`` —
                      ``{"key": ["value", ...]}``.
        body:         Decoded JSON body (dict / list / None). Empty for GET.
        snap:         ``ProviderState.snapshot()`` — read for ``blocks_behind``
                      and ``http_status``.
        lava_headers: Captured ``lava-*`` headers, threaded through for
                      symmetry with the other handlers.

    Returns:
        ``(http_status, response_body)``. The body is the bare REST JSON
        object (no JSON-RPC envelope) — the caller serialises it to bytes
        and emits.
    """
    key = (verb, template)

    # 1. Per-(verb, template) override path.
    #
    # ``state.responses`` is keyed by string in the JSON-RPC handlers (single
    # method name). REST keys are 2-element lists on the wire (JSON has no
    # tuple type); ``state.update`` already re-tuples them, so the dict here
    # uses tuple keys.
    with state.lock:
        method_cfg = state.responses.get(key) or state.responses.get("default", {})

    if isinstance(method_cfg, dict):
        # Both override branches below resolve their HTTP status the same
        # way: "http_status" is the primary key (the name every other
        # handler and the provider-wide snap use); "status" is the
        # deprecated REST-only fallback — migrate callers, then remove.
        # When both are present, http_status wins.

        # Per-(verb, template) error override — mirrors handlers_eth's
        # per-method error path, two flavours:
        #
        #   1. Named catalogue (primary):
        #          responses[(verb, template)] = {"error_stub": "not_found"}
        #      Resolved against REST_ERROR_STUBS — single source of truth
        #      for envelope content. Unknown stub name raises KeyError so
        #      a typo fails loudly rather than falling back silently.
        #
        #   2. Raw envelope (escape-hatch for ad-hoc shapes):
        #          responses[(verb, template)] =
        #              {"http_status": 503, "error": {"code": "internal", "message": "..."}}
        err = None
        if "error_stub" in method_cfg:
            err = REST_ERROR_STUBS[method_cfg["error_stub"]]
        elif "error" in method_cfg:
            err = method_cfg["error"]
        if err is not None:
            http_st = method_cfg.get("http_status", method_cfg.get("status", 500))
            return http_st, {"error": err}

        # Custom body override — replaces the stub entirely.
        # Shape: {"http_status": 200, "body": {"balances": [...], "pagination": {...}}}
        if "body" in method_cfg:
            http_st = method_cfg.get("http_status", method_cfg.get("status", 200))
            return http_st, method_cfg["body"]

    # 2. Stub lookup with deep-copy guard.
    if key in REST_METHOD_DEFAULTS:
        result = deepcopy(REST_METHOD_DEFAULTS[key])
    else:
        # Unknown (verb, template) — caller should have 404'd before reaching
        # us, but return a defensive empty-object response so callers don't
        # crash on KeyError.
        return 404, {"code": "not_found", "method": verb, "path": template}

    blocks_behind = snap.get("blocks_behind", 0)

    # 3. Path-specific echo / shift logic.
    #
    # The 5 v1 paths all live under /cosmos/base/tendermint/v1beta1/blocks/* +
    # node_info + balances + validators. Only blocks/* and balances/* need
    # request-time adjustment; node_info and validators are static.

    if template == "/cosmos/base/tendermint/v1beta1/blocks/latest":
        # Apply blocks_behind shift to the head height.
        if blocks_behind != 0 and isinstance(result, dict):
            shifted = str(
                _int_height(REST_METHOD_DEFAULTS[key]["block"]["header"]["height"]) - blocks_behind
            )
            result["block"]["header"]["height"] = shifted
            # last_commit.height tracks one-below-head; keep the relationship.
            try:
                result["block"]["last_commit"]["height"] = str(max(int(shifted) - 1, 0))
            except (KeyError, TypeError, ValueError):
                pass

    elif template == "/cosmos/base/tendermint/v1beta1/blocks/{height}":
        # Echo the requested height back so the router's pruning verification
        # sees the matching height in the response (mirrors the ETH
        # eth_getBlockByNumber echo).
        requested = path_params.get("height")
        if requested is not None and isinstance(result, dict):
            result["block"]["header"]["height"] = str(requested)
            try:
                result["block"]["last_commit"]["height"] = str(max(int(requested) - 1, 0))
            except (TypeError, ValueError):
                # Non-numeric heights (named tags? Cosmos doesn't have them
                # today, but be defensive) — leave the stub's default value.
                pass

    elif template == "/cosmos/bank/v1beta1/balances/{address}":
        # Echo the requested address — not strictly part of the Cosmos REST
        # body shape, but useful for tests asserting the path param made it
        # through unchanged. Stored in a sibling ``address`` field rather than
        # mutating ``balances[*]`` so the body remains schema-compatible.
        requested = path_params.get("address")
        if requested is not None and isinstance(result, dict):
            result["address"] = requested

    return snap.get("http_status", 200), result


def _int_height(value: Any) -> int:
    """Parse a Cosmos-shape height (decimal string OR int) into a Python int.

    Cosmos JSON returns heights as strings; the helper accepts both shapes so
    test overrides that set an int height don't blow up.
    """
    if isinstance(value, int):
        return value
    return int(str(value))
