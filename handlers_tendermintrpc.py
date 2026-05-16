"""Tendermint-RPC success-branch dispatch for the provider simulator (MAG-1841).

Sibling of ``handlers_eth``, ``handlers_btc``, ``handlers_grpc``, and
``handlers_rest``. Same public surface, Tendermint-RPC shape:

    handle(state, method, params, snap, lava_headers) -> tuple[int, dict]

The caller (``TendermintHandler`` in ``server.py``) is responsible for
parsing the wire (GET URI vs POST body), normalizing params via
``_normalize_tm_params`` (top of this module), writing the HTTP response,
and recording history. This module decides the JSON-RPC ``result`` body
given a method name + normalized params dict.

Tendermint-RPC wire shapes
--------------------------

Tendermint exposes every method twice — GET URI form and POST JSON-RPC body
form. The two ship semantically-equivalent params via different wire
encodings, so the test client gets to pick whichever transport is more
convenient (POST is the default for our typed client).

* **GET URI form**: ``GET /abci_query?path=%22/store/auth/key%22&height=%224500000%22``.
  Values are URL-decoded then JSON-decoded — quoted strings (literal quotes
  around the value) are how CometBFT distinguishes string params from bare
  bools / ints.
* **POST JSON-RPC body**: ``POST /`` with ``{"jsonrpc":"2.0","id":N,"method":"abci_query","params":{"path":"/store/auth/key","height":"4500000"}}``.
  Values are JSON-typed already; no quote-stripping needed.

``_normalize_tm_params`` turns either shape into a flat ``Dict[str, Any]``
with Python-native values, so the per-method branches in ``handle`` only
ever read normalized dicts and don't have to care about the wire.

Method coverage (v1) — 7 methods
--------------------------------

* status, health, abci_info, net_info — no per-request transform
* block — echoes the requested ``height`` so test assertions on
  ``block.header.height == str(H)`` pass cleanly. Mirrors the ETH
  ``eth_getBlockByNumber`` echo (handlers_eth) and the REST
  ``/blocks/{height}`` echo (handlers_rest).
* validators — paginates the validator pool by ``page`` / ``per_page``.
* abci_query — echoes the requested ``height`` and returns the 9-key
  CometBFT response envelope. Other ABCI fields (``code``, ``log``,
  ``info``, ``value``, …) are stubbed minimally because tests assert on
  the envelope shape, not on chain-domain query results.

Adding a new method: extend ``TENDERMINT_METHOD_DEFAULTS`` in
``stubs_tendermintrpc.py`` and add the per-request branch here if any
echo / slicing / shift is needed.
"""

import json
from copy import deepcopy
from typing import Any, Dict, List, Tuple

from stubs_tendermintrpc import (
    TENDERMINT_METHOD_DEFAULTS,
    _abci_query_response,
    _block_response,
    _validators_response,
)


def _normalize_tm_params(verb: str, raw_params: Any) -> Dict[str, Any]:
    """Turn either wire shape (GET parse_qs OR POST body) into a flat dict.

    GET form delivers params via ``urllib.parse.parse_qs`` —
    ``Dict[str, List[str]]`` with each value JSON-encoded (literal quotes
    around strings, booleans bare, string-ints quoted). POST form delivers
    a JSON-typed dict already. The normaliser:

    1. Unwraps GET single-element lists to scalars.
    2. JSON-decodes string values when they look like JSON literals
       (``"42"``, ``"\\"hello\\""``, ``"true"``, …). Falls back to the
       raw string when the value is a plain unquoted string that
       happens to come through GET — e.g. CometBFT historically
       accepted both shapes.
    3. Leaves non-string scalars (bool, int, None) alone.

    Returns an empty dict when the input is ``None`` or non-dict (e.g.
    JSON-RPC positional params, which Tendermint doesn't use).

    Args:
        verb: ``"GET"`` or ``"POST"``. Stored for logging / future per-verb
            branching; the current normaliser is verb-agnostic.
        raw_params: Either a parse_qs output (GET) or a parsed JSON dict
            (POST).

    Returns:
        Flat ``Dict[str, Any]`` with Python-native scalar values.
    """
    del verb  # Verb-agnostic today; kept in the signature for future use.
    if raw_params is None:
        return {}
    if not isinstance(raw_params, dict):
        # Tendermint params are always named; positional (list) form is
        # an error. Returning empty dict makes the handler fall through
        # to the stub defaults rather than crashing.
        return {}

    out: Dict[str, Any] = {}
    for key, value in raw_params.items():
        # GET parse_qs gives Dict[str, List[str]] — pick the first
        # value for single-valued params. Multi-valued keys are not
        # part of the CometBFT contract; if a test sends them we
        # silently keep only the first.
        if isinstance(value, list):
            value = value[0] if value else ""

        # JSON-decode string values when they look like JSON literals.
        # ``json.loads('"hello"')`` → ``"hello"`` (strips quotes).
        # ``json.loads('42')``      → 42.
        # ``json.loads('true')``    → True.
        # ``json.loads('/foo')``    → ValueError (falls back to raw).
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                pass  # Keep the raw string.
        out[key] = value
    return out


def _to_int(value: Any, default: int) -> int:
    """Coerce a normalized param value to int, falling back to ``default``."""
    if isinstance(value, bool):  # bool is a subclass of int; reject explicitly
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def handle(
    state,
    method: str,
    params: Dict[str, Any],
    snap: Dict[str, Any],
    lava_headers: Dict[str, str],
) -> Tuple[int, Dict[str, Any]]:
    """Resolve the success-path JSON-RPC ``result`` body for one TM request.

    Args:
        state:        Live ``ProviderState`` — read for ``state.responses``
                      under ``state.lock`` for per-method overrides.
        method:       Tendermint method name (``"status"`` / ``"block"`` /
                      ``"abci_query"`` / ...). The dispatcher already
                      extracted this from the URI (GET) or body (POST).
        params:       Normalized params dict (post-``_normalize_tm_params``).
        snap:         ``ProviderState.snapshot()`` — read for
                      ``blocks_behind`` and ``http_status``.
        lava_headers: Captured ``lava-*`` headers, threaded through for
                      symmetry with the other handlers (no current use
                      inside this module).

    Returns:
        ``(http_status, result_body)``. The caller wraps ``result_body``
        in the JSON-RPC envelope (``{"jsonrpc":"2.0","id":...,"result":...}``).
    """
    del lava_headers  # Threaded through for parity with handlers_rest; unused here.

    # 1. Per-method override path.
    #
    # ``state.responses`` is keyed by string in the JSON-RPC handlers (single
    # method name). Same convention here.
    with state.lock:
        method_cfg = state.responses.get(method) or state.responses.get("default", {})

    if isinstance(method_cfg, dict):
        # Error envelope override — surfaces in the JSON-RPC envelope's
        # ``error`` field rather than ``result``. The caller wraps.
        # Shape: {"status": 200, "error": {"code": -32603, "message": "..."}}
        # The caller branch on the presence of "error" key in the returned dict.
        if "error" in method_cfg:
            http_st = method_cfg.get("status", method_cfg.get("http_status", 200))
            return http_st, {"error": method_cfg["error"]}

        # Custom body override — replaces the stub's result entirely.
        # Shape: {"status": 200, "body": {<arbitrary result>}}
        if "body" in method_cfg:
            http_st = method_cfg.get("status", method_cfg.get("http_status", 200))
            return http_st, method_cfg["body"]

    # 2. Stub lookup with deep-copy guard.
    if method not in TENDERMINT_METHOD_DEFAULTS:
        # Unknown method — Tendermint convention is JSON-RPC -32601
        # (method not found). The caller wraps the error into the envelope.
        return snap.get("http_status", 200), {
            "error": {"code": -32601, "message": f"Unknown method: {method}"}
        }

    blocks_behind = snap.get("blocks_behind", 0)

    # 3. Per-method request-time logic.

    if method == "block":
        # Echo the requested height (or apply blocks_behind shift to the head).
        requested_height = params.get("height")
        if requested_height is not None:
            height_i = _to_int(requested_height, 0)
        else:
            height_i = max(_block_height_default() - blocks_behind, 0)
        return snap.get("http_status", 200), _block_response(height=height_i)

    if method == "validators":
        height_raw = params.get("height")
        height_i = (
            _to_int(height_raw, _block_height_default())
            if height_raw is not None
            else max(_block_height_default() - blocks_behind, 0)
        )
        page = max(_to_int(params.get("page"), 1), 1)
        per_page = max(_to_int(params.get("per_page"), 30), 1)
        return snap.get("http_status", 200), _validators_response(
            height=height_i, page=page, per_page=per_page
        )

    if method == "abci_query":
        # Echo the requested height. The path / data / prove params are
        # accepted but not interpreted — the sim doesn't run an ABCI app.
        height_raw = params.get("height")
        height_i = _to_int(height_raw, 0) if height_raw is not None else 0
        return snap.get("http_status", 200), _abci_query_response(
            path=str(params.get("path") or ""),
            data=str(params.get("data") or ""),
            height=height_i,
        )

    # 4. Static stub path — no per-request transform needed
    # (status / health / abci_info / net_info).
    result = deepcopy(TENDERMINT_METHOD_DEFAULTS[method])
    return snap.get("http_status", 200), result


def _block_height_default() -> int:
    """Default block height used when no ``height`` param is supplied.

    Wraps the constant pull so unit tests can monkey-patch this helper
    without touching ``constants.TM_LATEST_HEIGHT`` directly.
    """
    from constants import TM_LATEST_HEIGHT

    return TM_LATEST_HEIGHT


def supported_methods() -> List[str]:
    """List of TM methods this handler accepts.

    Useful for the dispatcher's 404 / -32601 decision and for tests
    asserting on the v1 method set.
    """
    return sorted(TENDERMINT_METHOD_DEFAULTS.keys())
