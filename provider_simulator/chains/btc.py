"""Bitcoin chain — success-path response building.

Reimplements the BTC success path (today in ``handlers_btc.handle``) against the
new domain shapes. BTC has no chain-specific quirks (it uses the empty Quirks
base); ``blocks_behind`` and ``http_status`` come from the ScenarioConfig
snapshot. The head is the static ``BTC_LATEST_BLOCK`` constant shifted by
``blocks_behind`` (BTC has no advancing-head control today).

Height-tracking methods shift with ``blocks_behind``; ``getblockhash`` /
``getblock`` / ``getblockheader`` echo the requested height / hash so the
router's pruning verification sees what it asked for.
"""

from copy import deepcopy

from constants import BTC_LATEST_BLOCK
from provider_simulator.chains.base import Chain
from stubs_btc import BTC_ERROR_STUBS, BTC_METHOD_DEFAULTS, btc_block_hash

_HEIGHT_METHODS = {"getblockcount"}
_HEAD_HASH_METHODS = {"getbestblockhash", "getblockhash"}


class BtcChain(Chain):
    name = "btc"

    def error_stub(self, name: str) -> dict:
        return BTC_ERROR_STUBS[name]

    def build_success(
        self, request: dict, scenario: dict, quirks: dict, interface: str = ""
    ) -> tuple[int, dict]:
        req_id = request.get("id", 1)
        method = request.get("method", "unknown")
        params = request.get("params", [])
        responses = scenario.get("responses") or {}
        method_cfg = responses.get(method) or responses.get("default", {})

        err = None
        if "error_stub" in method_cfg:
            err = self.error_stub(method_cfg["error_stub"])
        elif "error" in method_cfg:
            err = method_cfg["error"]
        if err is not None:
            return method_cfg.get("http_status", 200), {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": err,
            }

        if "result" in method_cfg:
            result = method_cfg["result"]
        elif method in BTC_METHOD_DEFAULTS:
            result = deepcopy(BTC_METHOD_DEFAULTS[method])
        else:
            result = None

        blocks_behind = scenario.get("blocks_behind", 0)
        effective_head = BTC_LATEST_BLOCK - blocks_behind

        if method not in responses and blocks_behind != 0:
            if method in _HEIGHT_METHODS:
                result = effective_head
            elif method in _HEAD_HASH_METHODS:
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

        # getblockhash echoes the requested height (independent of blocks_behind).
        if method == "getblockhash" and params and method not in responses:
            try:
                height = int(params[0])
            except (TypeError, ValueError):
                return 200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": BTC_ERROR_STUBS["invalid_parameter"],
                }
            result = btc_block_hash(height)

        # getblock / getblockheader take a HASH and echo it back.
        if (
            method in ("getblock", "getblockheader")
            and params
            and isinstance(result, dict)
            and method not in responses
        ):
            block_hash = params[0]
            if isinstance(block_hash, str):
                result = dict(result)
                result["hash"] = block_hash

        return scenario.get("http_status", 200), {"jsonrpc": "2.0", "id": req_id, "result": result}
