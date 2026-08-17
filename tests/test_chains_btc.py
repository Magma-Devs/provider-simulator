from constants import BTC_LATEST_BLOCK
from provider_simulator.chains import chain_for
from provider_simulator.chains.btc import BtcChain
from provider_simulator.domain.scenario import ScenarioConfig
from stubs_btc import BTC_ERROR_STUBS, btc_block_hash


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
