"""Bitcoin chain — success-path response building.

Reimplements the BTC success path (today in ``handlers_btc.handle``) against the
new domain shapes. BTC has no chain-specific quirks (it uses the empty Quirks
base); ``blocks_behind`` and ``http_status`` come from the ScenarioConfig
snapshot. The head is an ``AdvancingHead`` based at ``BTC_LATEST_BLOCK``, shifted by
``blocks_behind``. Static until a test moves it, so a reply at rest is
byte-identical to the old fixed constant.

Height-tracking methods shift with ``blocks_behind``; ``getblockhash`` /
``getblock`` / ``getblockheader`` echo the requested height / hash so the
router's pruning verification sees what it asked for.
"""

from copy import deepcopy

from constants import BTC_LATEST_BLOCK
from provider_simulator.chains.base import AdvancingHead, Chain
from stubs_btc import BTC_ERROR_STUBS, BTC_METHOD_DEFAULTS, btc_block_hash

_HEIGHT_METHODS = {"getblockcount"}
_HEAD_HASH_METHODS = {"getbestblockhash", "getblockhash"}

#: The head hash the stubs are written against, beside the height.
_BASE_HEAD_HASH = btc_block_hash(BTC_LATEST_BLOCK)


def _restamp_head(value: object, effective_head: int) -> object:
    """Rewrite every base height and base head-hash in a stub to the head.

    Every stub is written against ``BTC_LATEST_BLOCK``, so a number equal to
    it IS this chain's height and a string equal to its hash IS this chain's
    head hash, wherever in the reply they sit.

    Walking the whole reply rather than listing the methods that carry one is
    the point. A hand-written list was tried first and missed five:
    ``getchaintips`` (a LIST of blocks), ``getblockstats``, ``getindexinfo``
    (nested two deep), ``gettxoutsetinfo`` and ``gettxout`` (the hash alone,
    with no height beside it to notice). Each would have answered the base
    height while ``getblockcount`` answered the moved one, on the same chain,
    in the same breath. A method added later is covered without an edit here.

    ``bool`` is rejected before ``int`` because it subclasses it, and a stub
    flag would otherwise be rewritten into a block height.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value == BTC_LATEST_BLOCK:
        return effective_head
    if isinstance(value, str) and value == _BASE_HEAD_HASH:
        return btc_block_hash(effective_head)
    if isinstance(value, dict):
        return {k: _restamp_head(v, effective_head) for k, v in value.items()}
    if isinstance(value, list):
        return [_restamp_head(v, effective_head) for v in value]
    return value


class BtcChain(Chain):
    name = "btc"

    def __init__(self) -> None:
        self.head = AdvancingHead(BTC_LATEST_BLOCK)

    def error_stub(self, name: str) -> dict:
        return BTC_ERROR_STUBS[name]

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
        elif method in BTC_METHOD_DEFAULTS:
            result = deepcopy(BTC_METHOD_DEFAULTS[method])
        else:
            result = None

        blocks_behind = scenario.get("blocks_behind", 0)
        effective_head = self.head.current() - blocks_behind

        # Recomputed whatever ``blocks_behind`` is. It used to be skipped at
        # zero, which was free while the head was a constant -- the stub
        # already held that number. A head that MOVES makes the skip wrong:
        # the reply would keep answering the base height and the router would
        # never see the head move. At rest this recomputes the same values the
        # stub carries, so a reply with a static head is unchanged.
        if method not in responses:
            # ``getblockhash`` WITH a height is left to the block below, which
            # validates it -- the walk would restamp the base hash and the
            # block below overwrites it anyway, so skipping is only clearer.
            if not (method == "getblockhash" and params):
                result = _restamp_head(result, effective_head)

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
        if method in ("getblock", "getblockheader") and params and isinstance(result, dict) and method not in responses:
            block_hash = params[0]
            if isinstance(block_hash, str):
                result = dict(result)
                result["hash"] = block_hash

        return scenario.get("http_status", 200), {"jsonrpc": "2.0", "id": req_id, "result": result}
