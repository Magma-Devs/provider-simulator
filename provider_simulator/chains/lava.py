"""Lava chain — success-path content for REST, gRPC, and Tendermint-RPC.

Unlike the single-interface chains, one Lava router pool speaks exactly one of
three application protocols — rest, grpc, or tendermintrpc — so ``build_success``
branches on the serving endpoint's interface. It returns response CONTENT only.
Turning that content into the wire form is the matching listener's job:

- REST:          the returned dict IS the bare HTTP JSON body (no envelope).
- Tendermint:    the returned dict IS the JSON-RPC envelope (``jsonrpc``/``id``/
                 ``result`` or ``error``) — the same shape EthChain returns.
- gRPC:          the returned dict is plain success-DATA (height, chain id, node
                 info). The gRPC listener serializes it into a protobuf message
                 and maps gRPC-only faults (errors, corruption) to status codes,
                 because those are wire concerns, not content.

This mirrors ``handlers_rest`` / ``handlers_tendermintrpc`` / ``handlers_grpc``,
reimplemented against the redesigned domain shapes (a ScenarioConfig snapshot and
a Quirks snapshot instead of a live ``ProviderState``). Lava has no chain-specific
quirks, so the quirks snapshot is unused.
"""

from copy import deepcopy
from typing import Any

from constants import ETH_LATEST_BLOCK, TM_LATEST_HEIGHT
from provider_simulator.chains.base import Chain
from stubs_rest import REST_ERROR_STUBS, REST_METHOD_DEFAULTS
from stubs_tendermintrpc import (
    TENDERMINT_ERROR_STUBS,
    TENDERMINT_METHOD_DEFAULTS,
    _abci_query_response,
    _block_response,
    _validators_response,
)

# gRPC head + chain identity. Kept here as plain data (the gRPC listener owns the
# protobuf); defined locally so importing this chain never pulls in grpcio.
GRPC_LATEST_BLOCK = 25_000_000
LAVA_SIM_CHAIN_ID = "lava-sim"

# Cosmos REST head — the ETH simulator's "latest block" reused so the same
# blocks_behind primitive shifts consistently. Cosmos heights are decimal.
_REST_LATEST_HEIGHT = int(ETH_LATEST_BLOCK, 16)


def _int_height(value: Any) -> int:
    """Parse a Cosmos-shape height (decimal string OR int) into an int."""
    if isinstance(value, int):
        return value
    return int(str(value))


def _to_int(value: Any, default: int) -> int:
    """Coerce a normalized param value to int, falling back to ``default``."""
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _pick_status(cfg: dict, primary: str, secondary: str, default: int) -> int:
    """Resolve an HTTP status from a per-method override, honoring precedence.

    ``primary`` wins over ``secondary`` — REST prefers ``http_status``, while
    Tendermint prefers ``status`` (the reverse). A missing or non-int value
    falls back to ``default``.
    """
    val = cfg.get(primary, cfg.get(secondary, default))
    return val if isinstance(val, int) and not isinstance(val, bool) else default


class LavaChain(Chain):
    name = "lava"

    def build_success(
        self, request: dict, scenario: dict, quirks: dict, interface: str = ""
    ) -> tuple[int, dict]:
        if interface == "rest":
            return self._build_rest(request, scenario)
        if interface == "tendermintrpc":
            return self._build_tm(request, scenario)
        if interface == "grpc":
            return self._build_grpc(request, scenario)
        raise ValueError(
            "LavaChain requires a rest / tendermintrpc / grpc endpoint, "
            f"got interface {interface!r}"
        )

    # ── REST ────────────────────────────────────────────────────────────────
    # Ports handlers_rest.handle. request = {verb, template, path_params, query,
    # body}. Returns (http_status, bare_rest_json_body). REST status precedence:
    # http_status wins over the deprecated `status` fallback.
    def _build_rest(self, request: dict, scenario: dict) -> tuple[int, dict]:
        verb = request.get("verb", "GET")
        template = request.get("template")
        path_params = request.get("path_params") or {}
        key = (verb, template)
        responses = scenario.get("responses") or {}
        method_cfg = responses.get(key) or responses.get("default", {})

        if isinstance(method_cfg, dict):
            err = None
            if "error_stub" in method_cfg:
                err = REST_ERROR_STUBS[method_cfg["error_stub"]]
            elif "error" in method_cfg:
                err = method_cfg["error"]
            if err is not None:
                return _pick_status(method_cfg, "http_status", "status", 500), {"error": err}
            if "body" in method_cfg:
                return (
                    _pick_status(method_cfg, "http_status", "status", 200),
                    method_cfg["body"],
                )

        if key not in REST_METHOD_DEFAULTS:
            return 404, {"code": "not_found", "method": verb, "path": template}
        result = deepcopy(REST_METHOD_DEFAULTS[key])

        blocks_behind = scenario.get("blocks_behind", 0)

        if template == "/cosmos/base/tendermint/v1beta1/blocks/latest":
            if blocks_behind != 0 and isinstance(result, dict):
                shifted = str(
                    _int_height(REST_METHOD_DEFAULTS[key]["block"]["header"]["height"])
                    - blocks_behind
                )
                result["block"]["header"]["height"] = shifted
                try:
                    result["block"]["last_commit"]["height"] = str(max(int(shifted) - 1, 0))
                except (KeyError, TypeError, ValueError):
                    pass
        elif template == "/cosmos/base/tendermint/v1beta1/blocks/{height}":
            requested = path_params.get("height")
            if requested is not None and isinstance(result, dict):
                result["block"]["header"]["height"] = str(requested)
                try:
                    result["block"]["last_commit"]["height"] = str(max(int(requested) - 1, 0))
                except (TypeError, ValueError):
                    pass
        elif template == "/cosmos/bank/v1beta1/balances/{address}":
            requested = path_params.get("address")
            if requested is not None and isinstance(result, dict):
                result["address"] = requested

        return scenario.get("http_status", 200), result

    # ── Tendermint-RPC ──────────────────────────────────────────────────────
    # Ports handlers_tendermintrpc.handle and wraps the result in the JSON-RPC
    # envelope (EthChain returns the full envelope too). request = {method,
    # params, id}. TM status precedence: `status` wins over `http_status`
    # (the reverse of REST — preserved deliberately).
    def _build_tm(self, request: dict, scenario: dict) -> tuple[int, dict]:
        req_id = request.get("id", 1)
        method = request.get("method", "unknown")
        params = request.get("params") or {}
        responses = scenario.get("responses") or {}
        method_cfg = responses.get(method) or responses.get("default", {})

        if isinstance(method_cfg, dict):
            err = None
            if "error_stub" in method_cfg:
                err = TENDERMINT_ERROR_STUBS[method_cfg["error_stub"]]
            elif "error" in method_cfg:
                err = method_cfg["error"]
            if err is not None:
                return (
                    _pick_status(method_cfg, "status", "http_status", 200),
                    {"jsonrpc": "2.0", "id": req_id, "error": err},
                )
            if "body" in method_cfg:
                return (
                    _pick_status(method_cfg, "status", "http_status", 200),
                    {"jsonrpc": "2.0", "id": req_id, "result": method_cfg["body"]},
                )

        http_status = scenario.get("http_status", 200)
        if method not in TENDERMINT_METHOD_DEFAULTS:
            return http_status, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            }

        blocks_behind = scenario.get("blocks_behind", 0)

        if method == "block":
            requested_height = params.get("height")
            if requested_height is not None:
                height_i = _to_int(requested_height, 0)
            else:
                height_i = max(TM_LATEST_HEIGHT - blocks_behind, 0)
            result = _block_response(height=height_i)
        elif method == "validators":
            height_raw = params.get("height")
            height_i = (
                _to_int(height_raw, TM_LATEST_HEIGHT)
                if height_raw is not None
                else max(TM_LATEST_HEIGHT - blocks_behind, 0)
            )
            page = max(_to_int(params.get("page"), 1), 1)
            per_page = max(_to_int(params.get("per_page"), 30), 1)
            result = _validators_response(height=height_i, page=page, per_page=per_page)
        elif method == "abci_query":
            height_raw = params.get("height")
            height_i = _to_int(height_raw, 0) if height_raw is not None else 0
            result = _abci_query_response(
                path=str(params.get("path") or ""),
                data=str(params.get("data") or ""),
                height=height_i,
            )
        else:
            result = deepcopy(TENDERMINT_METHOD_DEFAULTS[method])

        return http_status, {"jsonrpc": "2.0", "id": req_id, "result": result}

    # ── gRPC ────────────────────────────────────────────────────────────────
    # Returns plain success-DATA the gRPC listener serializes into a protobuf
    # message. request = {method}. Only the two unary methods the router uses
    # are covered (GetLatestBlock / GetNodeInfo). Per-method `responses` result
    # overrides win. gRPC faults (errors, corruption) are the listener's job.
    def _build_grpc(self, request: dict, scenario: dict) -> tuple[int, dict]:
        method = request.get("method", "unknown")
        responses = scenario.get("responses") or {}
        method_cfg = responses.get(method) or responses.get("default", {})
        if isinstance(method_cfg, dict) and "result" in method_cfg:
            return 200, {"grpc_method": method, "result": method_cfg["result"]}

        if method == "GetLatestBlock":
            head = GRPC_LATEST_BLOCK - scenario.get("blocks_behind", 0)
            return 200, {
                "grpc_method": method,
                "height": head,
                "chain_id": LAVA_SIM_CHAIN_ID,
            }
        if method == "GetNodeInfo":
            return 200, {
                "grpc_method": method,
                "network": LAVA_SIM_CHAIN_ID,
                "moniker": "lava-sim-grpc-provider",
                "version": "sim-1.0",
                "app_name": "lava-sim-app",
                "app_version": "sim-1.0",
            }
        return 200, {"grpc_method": method}
