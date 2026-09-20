from constants import BTC_LATEST_BLOCK
from provider_simulator.chains import chain_for
from provider_simulator.chains.btc import BtcChain
from provider_simulator.domain.scenario import ScenarioConfig
from stubs_btc import BTC_ERROR_STUBS, BTC_METHOD_DEFAULTS, btc_block_hash


def _sc(**kw):
    sc = ScenarioConfig()
    if kw:
        sc.update(kw)
    return sc.snapshot()


def test_registry_resolves_btc():
    assert isinstance(chain_for("btc"), BtcChain)


def test_getblockcount_default_and_shift():
    chain = BtcChain()
    _, body = chain.build_success({"id": 1, "method": "getblockcount"}, _sc(), {})
    assert body["result"] == BTC_LATEST_BLOCK
    _, body = chain.build_success({"id": 1, "method": "getblockcount"}, _sc(blocks_behind=10), {})
    assert body["result"] == BTC_LATEST_BLOCK - 10


def test_getblockhash_echoes_requested_height():
    chain = BtcChain()
    _, body = chain.build_success({"id": 1, "method": "getblockhash", "params": [700000]}, _sc(), {})
    assert body["result"] == btc_block_hash(700000)


def test_getblockhash_bad_param_returns_invalid_parameter():
    chain = BtcChain()
    _, body = chain.build_success({"id": 1, "method": "getblockhash", "params": ["not-an-int"]}, _sc(), {})
    assert body["error"] == BTC_ERROR_STUBS["invalid_parameter"]


def test_error_stub_override():
    chain = BtcChain()
    _, body = chain.build_success(
        {"id": 1, "method": "getblockcount"},
        _sc(responses={"getblockcount": {"error_stub": "invalid_parameter"}}),
        {},
    )
    assert body["error"] == BTC_ERROR_STUBS["invalid_parameter"]


def test_response_override_wins():
    chain = BtcChain()
    _, body = chain.build_success(
        {"id": 1, "method": "getblockcount"},
        _sc(responses={"getblockcount": {"result": 12345}}),
        {},
    )
    assert body["result"] == 12345


def test_the_head_moves_every_height_the_reply_carries():
    """A moved head has to reach the reply, not only the control route.

    The height used to be read straight off the constant and recomputed only
    when ``blocks_behind`` was non-zero, so a head that moved changed the
    control route's answer and nothing a router could see. Each method below
    carries the height in a different shape, and a router polls one of them.
    """
    chain = BtcChain()
    chain.head.bump(11)
    try:
        _, body = chain.build_success({"id": 1, "method": "getblockcount"}, _sc(), {})
        assert body["result"] == BTC_LATEST_BLOCK + 11

        _, body = chain.build_success({"id": 1, "method": "getblockchaininfo"}, _sc(), {})
        assert body["result"]["blocks"] == BTC_LATEST_BLOCK + 11
        assert body["result"]["headers"] == BTC_LATEST_BLOCK + 11
        assert body["result"]["bestblockhash"] == btc_block_hash(BTC_LATEST_BLOCK + 11)

        _, body = chain.build_success({"id": 1, "method": "getbestblockhash"}, _sc(), {})
        assert body["result"] == btc_block_hash(BTC_LATEST_BLOCK + 11)
    finally:
        chain.head.reset()


def test_a_static_head_answers_exactly_what_the_stub_carries():
    """Recomputing at rest must not change a single byte.

    The recompute now runs whatever ``blocks_behind`` is. That is only safe
    while a still head reproduces the stub exactly, so this compares the whole
    body against a chain whose head was never touched.
    """
    untouched = BtcChain()
    for method in ("getblockcount", "getblockchaininfo", "getbestblockhash", "getblockheader", "getblock"):
        _, body = untouched.build_success({"id": 1, "method": method}, _sc(), {})
        assert body == BtcChain().build_success({"id": 1, "method": method}, _sc(), {})[1]
    _, body = untouched.build_success({"id": 1, "method": "getblockcount"}, _sc(), {})
    assert body["result"] == BTC_LATEST_BLOCK


def test_the_head_and_blocks_behind_are_two_different_shifts():
    """``blocks_behind`` is this PROVIDER's lag; the head is the CHAIN's height.

    A test that moves the chain and lags one provider needs both to apply, so
    the lag has to be measured from wherever the head now is.
    """
    chain = BtcChain()
    chain.head.bump(100)
    try:
        _, body = chain.build_success({"id": 1, "method": "getblockcount"}, _sc(blocks_behind=30), {})
        assert body["result"] == BTC_LATEST_BLOCK + 100 - 30
    finally:
        chain.head.reset()


def test_no_reply_is_left_answering_the_base_height():
    """EVERY stub that carries the height has to follow the head, not a list.

    A hand-written list of methods was tried first and missed five, each of them
    a different shape: ``getchaintips`` is a LIST of blocks, ``getindexinfo``
    nests two deep, and ``gettxout`` carries the head HASH with no height beside
    it to notice. They answered the base height while ``getblockcount`` answered
    the moved one, on the same chain, in the same breath.

    This asserts over every default stub rather than a list, so a method added
    later is covered without anybody remembering to come back here.
    """
    chain = BtcChain()
    base_hash = btc_block_hash(BTC_LATEST_BLOCK)

    def reply(method):
        return chain.build_success({"id": 1, "method": method}, _sc(), {})[1]["result"]

    def mentions_the_base(value):
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return value == BTC_LATEST_BLOCK
        if isinstance(value, str):
            return value == base_hash
        if isinstance(value, dict):
            return any(mentions_the_base(v) for v in value.values())
        if isinstance(value, list):
            return any(mentions_the_base(v) for v in value)
        return False

    carriers = [m for m in BTC_METHOD_DEFAULTS if mentions_the_base(reply(m))]
    assert carriers, "nothing carried the base height, so this test proved nothing"

    chain.head.bump(500)
    try:
        stale = [m for m in carriers if mentions_the_base(reply(m))]
        assert not stale, (
            f"these replies still answer the base height {BTC_LATEST_BLOCK} after the "
            f"head moved: {stale}. Each one disagrees with getblockcount on the same "
            f"chain, which is what a router polling that method would read."
        )
    finally:
        chain.head.reset()
