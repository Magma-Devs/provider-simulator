"""Lightning Network chain — success-path response building.

Reimplements the LN success path (today in ``handlers_lnd.handle``) against the
new domain shapes. LN has no chain-specific quirks (empty Quirks base);
``blocks_behind`` and ``http_status`` come from the ScenarioConfig snapshot.
``getinfo`` tracks the underlying BTC head (shifted by ``blocks_behind``) --
really tracks it: the chain is handed btc's own head at construction, so a
test that moves btc moves what an LN node reports it has synced to. It said
this before while reading a separate constant, which was invisible only
because btc's height could not move either;
``decodepayreq`` / ``openchannel`` / ``payinvoice`` echo their inputs.
"""

from copy import deepcopy

from constants import LN_BLOCK_HEIGHT
from provider_simulator.chains.base import Chain
from stubs_lnd import LND_ERROR_STUBS, LND_METHOD_DEFAULTS

_HEIGHT_METHODS = {"getinfo"}


class LnChain(Chain):
    def __init__(self, btc_head=None) -> None:
        """``btc_head`` is btc's own AdvancingHead, or None to stand alone.

        Passed in rather than looked up so this chain never imports the
        registry that builds it. LN owns no head of its own: there is nothing
        to advance HERE, you advance btc and this follows.
        """
        self._btc_head = btc_head

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

        if method not in responses and method in _HEIGHT_METHODS and isinstance(result, dict):
            # The height follows the chain whatever ``blocks_behind`` is --
            # it used to be recomputed only when the node was BEHIND, which
            # was invisible while the height was a constant and wrong the
            # moment btc's head could move.
            base = self._btc_head.current() if self._btc_head is not None else LN_BLOCK_HEIGHT
            result["block_height"] = base - blocks_behind
            # Only a node that is actually behind is out of sync. Keeping
            # this inside the lag check is the half that was always right.
            if blocks_behind != 0:
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
