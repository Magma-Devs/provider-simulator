from provider_simulator.chains import CHAINS, chain_for
from provider_simulator.chains.eth import EthChain
from provider_simulator.domain.quirks import EthQuirks
from provider_simulator.domain.scenario import ScenarioConfig
from stubs import ETH_ERROR_STUBS, ETH_METHOD_DEFAULTS

BASE = int(ETH_METHOD_DEFAULTS["eth_blockNumber"], 16)


def _fresh():
    return EthChain(), ScenarioConfig().snapshot(), EthQuirks().snapshot()


def test_registry_resolves_eth():
    assert isinstance(chain_for("eth"), EthChain)
    assert CHAINS["eth"].name == "eth"
    assert CHAINS["eth"].quirks_type is EthQuirks


def test_chain_for_unknown_raises():
    try:
        chain_for("dogecoin")
    except ValueError as exc:
        assert "dogecoin" in str(exc)
    else:
        raise AssertionError("unknown chain must raise")


def test_block_number_default_is_base_head():
    chain, sc, q = _fresh()
    status, body = chain.build_success({"id": 1, "method": "eth_blockNumber"}, sc, q)
    assert status == 200
    assert body["result"] == "0x1312D00"  # == BASE, upper-hex, matches legacy


def test_block_number_shifts_by_blocks_behind():
    chain = EthChain()
    sc = ScenarioConfig()
    sc.update({"blocks_behind": 5})
    status, body = chain.build_success({"id": 1, "method": "eth_blockNumber"}, sc.snapshot(), EthQuirks().snapshot())
    assert body["result"] == "0x" + format(BASE - 5, "X")


def test_get_block_by_number_echoes_explicit_hex():
    chain, sc, q = _fresh()
    req = {"id": 1, "method": "eth_getBlockByNumber", "params": ["0x100", False]}
    _, body = chain.build_success(req, sc, q)
    assert body["result"]["number"] == "0x100"


def test_get_block_by_number_latest_resolves_to_head():
    chain, sc, q = _fresh()
    req = {"id": 1, "method": "eth_getBlockByNumber", "params": ["latest", False]}
    _, body = chain.build_success(req, sc, q)
    assert body["result"]["number"] == "0x1312D00"


def test_response_override_wins():
    chain = EthChain()
    sc = ScenarioConfig()
    sc.update({"responses": {"eth_call": {"result": "0xABC"}}})
    _, body = chain.build_success({"id": 7, "method": "eth_call"}, sc.snapshot(), EthQuirks().snapshot())
    assert body["result"] == "0xABC"
    assert body["id"] == 7


def test_error_stub_override():
    chain = EthChain()
    sc = ScenarioConfig()
    sc.update({"responses": {"eth_call": {"error_stub": "revert"}}})
    status, body = chain.build_success({"id": 1, "method": "eth_call"}, sc.snapshot(), EthQuirks().snapshot())
    assert status == 200
    assert body["error"] == ETH_ERROR_STUBS["revert"]


def test_raw_error_override():
    chain = EthChain()
    sc = ScenarioConfig()
    sc.update({"responses": {"eth_call": {"error": {"code": -32099, "message": "synthetic"}}}})
    _, body = chain.build_success({"id": 1, "method": "eth_call"}, sc.snapshot(), EthQuirks().snapshot())
    assert body["error"] == {"code": -32099, "message": "synthetic"}


def test_get_logs_empty_when_range_exceeds_indexed():
    chain = EthChain()
    q = EthQuirks()
    q.update({"logs_indexed_up_to": 100, "logs_lag_mode": "empty"})
    sc = ScenarioConfig()
    sc.update({"responses": {"eth_getLogs": {"result": [{"blockNumber": "0x200"}]}}})
    req = {"id": 1, "method": "eth_getLogs", "params": [{"toBlock": "latest"}]}
    _, body = chain.build_success(req, sc.snapshot(), q.snapshot())
    assert body["result"] == []


def test_get_logs_partial_keeps_only_indexed():
    chain = EthChain()
    q = EthQuirks()
    q.update({"logs_indexed_up_to": 0x150, "logs_lag_mode": "partial"})
    sc = ScenarioConfig()
    sc.update({"responses": {"eth_getLogs": {"result": [{"blockNumber": "0x100"}, {"blockNumber": "0x200"}]}}})
    req = {"id": 1, "method": "eth_getLogs", "params": [{"toBlock": "0x300"}]}
    _, body = chain.build_success(req, sc.snapshot(), q.snapshot())
    assert body["result"] == [{"blockNumber": "0x100"}]


def test_advancing_head_moves_block_number():
    chain, sc, q = _fresh()
    chain.head.bump(10)
    _, body = chain.build_success({"id": 1, "method": "eth_blockNumber"}, sc, q)
    assert body["result"] == "0x" + format(BASE + 10, "X")
    chain.head.reset()
    _, body = chain.build_success({"id": 1, "method": "eth_blockNumber"}, sc, q)
    assert body["result"] == "0x1312D00"


def test_http_status_from_scenario():
    chain = EthChain()
    sc = ScenarioConfig()
    sc.update({"http_status": 418})
    status, _ = chain.build_success({"id": 1, "method": "eth_blockNumber"}, sc.snapshot(), EthQuirks().snapshot())
    assert status == 418
