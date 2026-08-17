"""Lightning Network chain — success-path response building.

Reimplements the LN success path (today in ``handlers_lnd.handle``) against the
new domain shapes. LN has no chain-specific quirks (empty Quirks base);
``blocks_behind`` and ``http_status`` come from the ScenarioConfig snapshot.
``getinfo`` tracks the underlying BTC head (shifted by ``blocks_behind``);
``decodepayreq`` / ``openchannel`` / ``payinvoice`` echo their inputs.
"""

from copy import deepcopy

from constants import LN_BLOCK_HEIGHT
from provider_simulator.chains.base import Chain
from stubs_lnd import LND_ERROR_STUBS, LND_METHOD_DEFAULTS

_HEIGHT_METHODS = {"getinfo"}


class LnChain(Chain):
    name = "ln"

    def error_stub(self, name: str) -> dict:
        return LND_ERROR_STUBS[name]

    def build_success(self, request: dict, scenario: dict, quirks: dict, interface: str = "") -> tuple[int, dict]:
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
        elif method in LND_METHOD_DEFAULTS:
            result = deepcopy(LND_METHOD_DEFAULTS[method])
        else:
            result = None

        blocks_behind = scenario.get("blocks_behind", 0)

        if method not in responses and blocks_behind != 0:
            if method in _HEIGHT_METHODS and isinstance(result, dict):
                result["block_height"] = LN_BLOCK_HEIGHT - blocks_behind
                result["synced_to_chain"] = False

        if method == "decodepayreq" and params and method not in responses and isinstance(result, dict):
            invoice = params[0] if params else ""
            if isinstance(invoice, str) and invoice:
                result = dict(result)
                result["payment_request"] = invoice

        if method == "openchannel" and params and method not in responses and isinstance(result, dict):
            node_pubkey = params[0] if params else ""
            if isinstance(node_pubkey, str) and node_pubkey:
                result = dict(result)
                result["node_pubkey"] = node_pubkey

        if method == "payinvoice" and params and method not in responses and isinstance(result, dict):
            invoice = params[0] if params else ""
            if isinstance(invoice, str) and invoice:
                result = dict(result)
                result["payment_request"] = invoice

        return scenario.get("http_status", 200), {"jsonrpc": "2.0", "id": req_id, "result": result}
