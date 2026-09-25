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


# --- a provider answers null for a block it has not reached -------------------
# A node that is behind does not fail when asked for a block above its own head.
# It answers HTTP 200 with a null result and no error field, so nothing retries
# and the caller records a gap in the chain that is not there.


def _behind(blocks: int):
    """A chain plus the scenario snapshot of ONE provider that is `blocks` behind."""
    sc = ScenarioConfig()
    sc.update({"blocks_behind": blocks})
    return EthChain(), sc.snapshot(), EthQuirks().snapshot()


def _upper(n: int) -> str:
    return "0x" + format(n, "X")


def test_block_one_above_effective_head_is_null_when_provider_is_behind():
    chain, sc, q = _behind(100)  # effective head == BASE - 100
    req = {"id": 1, "method": "eth_getBlockByNumber", "params": [hex(BASE - 99), False]}
    status, body = chain.build_success(req, sc, q)
    assert status == 200
    assert body["result"] is None
    assert "error" not in body


def test_block_at_effective_head_still_returns_a_block_when_provider_is_behind():
    chain, sc, q = _behind(100)
    at_head = hex(BASE - 100)
    status, body = chain.build_success({"id": 1, "method": "eth_getBlockByNumber", "params": [at_head, False]}, sc, q)
    assert status == 200
    assert isinstance(body["result"], dict), "a block at the head must be a block, not null"
    assert body["result"]["number"] == at_head


def test_block_far_below_effective_head_still_returns_a_block_when_provider_is_behind():
    chain, sc, q = _behind(1_000_000)
    _, body = chain.build_success({"id": 1, "method": "eth_getBlockByNumber", "params": ["0x100", False]}, sc, q)
    assert body["result"]["number"] == "0x100"


def test_block_above_chain_head_still_echoes_when_provider_is_not_behind():
    """blocks_behind of zero is untouched: the echo is what it was before."""
    chain, sc, q = _fresh()
    above = hex(BASE + 5_000)
    _, body = chain.build_success({"id": 1, "method": "eth_getBlockByNumber", "params": [above, False]}, sc, q)
    assert isinstance(body["result"], dict), "blocks_behind of zero must not take the new path"
    assert body["result"]["number"] == above


def test_one_provider_being_behind_does_not_change_what_its_peers_answer():
    """The chain object is shared by the pool; the scenario is per provider."""
    chain = EthChain()
    behind = ScenarioConfig()
    behind.update({"blocks_behind": 100})
    peer = ScenarioConfig()
    q = EthQuirks().snapshot()
    asked = hex(BASE - 50)  # above the behind provider's head, below the peer's
    req = {"id": 1, "method": "eth_getBlockByNumber", "params": [asked, False]}
    _, behind_body = chain.build_success(req, behind.snapshot(), q)
    _, peer_body = chain.build_success(req, peer.snapshot(), q)
    assert behind_body["result"] is None
    assert isinstance(peer_body["result"], dict), "the peer is at the head and must answer a block"
    assert peer_body["result"]["number"] == asked


def test_named_tags_still_return_a_block_when_provider_is_behind():
    """`pending` resolves ABOVE the effective head and must still be a block."""
    chain, sc, q = _behind(100)
    for tag, expected in (
        ("latest", _upper(BASE - 100)),
        ("pending", _upper(BASE - 99)),
        ("safe", _upper(BASE - 100)),
        ("finalized", _upper(BASE - 101)),
        ("earliest", "0x0"),
    ):
        _, body = chain.build_success({"id": 1, "method": "eth_getBlockByNumber", "params": [tag, False]}, sc, q)
        assert isinstance(body["result"], dict), f"{tag} must resolve to a block, not null"
        assert body["result"]["number"] == expected, tag


def test_unparseable_block_parameter_still_echoes_when_provider_is_behind():
    chain, sc, q = _behind(100)
    _, body = chain.build_success({"id": 1, "method": "eth_getBlockByNumber", "params": ["not-a-block", False]}, sc, q)
    assert body["result"]["number"] == "not-a-block"
