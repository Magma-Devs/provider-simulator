import pytest

from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool, Provider
from provider_simulator.domain.quirks import EthQuirks, Quirks, SolanaQuirks
from provider_simulator.domain.scenario import ScenarioConfig


def test_add_provider_wires_config_quirks_log_for_eth():
    pool = Pool(name="eth-sim", chain="eth")
    p = pool.add_provider("1", [Endpoint("jsonrpc", "http", 18545), Endpoint("jsonrpc", "ws", 18557)])
    assert isinstance(p, Provider)
    assert isinstance(p.scenario, ScenarioConfig)
    assert isinstance(p.quirks, EthQuirks)  # eth pool -> EthQuirks
    assert p.log.stats()["total_calls"] == 0
    assert [e.port for e in p.endpoints] == [18545, 18557]


def test_add_provider_registers_in_the_pool():
    pool = Pool(name="eth-sim", chain="eth")
    p = pool.add_provider("1", [Endpoint("jsonrpc", "http", 18545)])
    assert pool.providers["1"] is p
    assert p.pool is pool


def test_add_provider_rejects_duplicate_pid():
    pool = Pool(name="eth-sim", chain="eth")
    pool.add_provider("1", [Endpoint("jsonrpc", "http", 18545)])
    with pytest.raises(ValueError, match="eth-sim:1"):
        pool.add_provider("1", [Endpoint("jsonrpc", "http", 18546)])


def test_add_provider_picks_solana_quirks_for_solana_pool():
    pool = Pool(name="solana-sim", chain="solana")
    p = pool.add_provider("1", [Endpoint("jsonrpc", "http", 18582)])
    assert isinstance(p.quirks, SolanaQuirks)


def test_add_provider_uses_base_quirks_for_btc():
    pool = Pool(name="btc-sim", chain="btc")
    p = pool.add_provider("1", [Endpoint("jsonrpc", "http", 18575)])
    assert type(p.quirks) is Quirks


def test_provider_key_is_pool_colon_pid():
    pool = Pool(name="lava-sim-grpc", chain="lava")
    p = pool.add_provider("4", [Endpoint("grpc", "http2", 18563)])
    assert p.key == "lava-sim-grpc:4"


def test_two_providers_have_independent_state():
    pool = Pool(name="eth-sim", chain="eth")
    a = pool.add_provider("1", [Endpoint("jsonrpc", "http", 18545)])
    b = pool.add_provider("2", [Endpoint("jsonrpc", "http", 18546)])
    a.scenario.update({"mode": "down"})
    assert b.scenario.snapshot()["mode"] == "success"
    assert a.quirks is not b.quirks
    assert a.log is not b.log


def test_providers_are_identity_objects():
    # eq=False on purpose: dataclass equality would recurse through the
    # Pool<->Provider cycle (RecursionError) and make both types unhashable.
    pool_a = Pool(name="eth-sim", chain="eth")
    pool_b = Pool(name="eth-sim", chain="eth")
    a = pool_a.add_provider("1", [Endpoint("jsonrpc", "http", 18545)])
    b = pool_b.add_provider("1", [Endpoint("jsonrpc", "http", 18545)])
    assert a != b  # same shape, different objects — identity semantics
    assert a == a
    assert len({a, b}) == 2  # hashable, by identity


# ── the memory of executed transactions ───────────────────────────────────────


def _sui_provider() -> Provider:
    return Pool(name="sui-x", chain="sui").add_provider("1", [Endpoint("graphql", "http", 18999)])


def test_remember_executed_then_the_snapshot_holds_the_digest_and_its_time():
    p = _sui_provider()
    assert p.executed_snapshot() == {}
    p.remember_executed("d1", 12.5)
    assert p.executed_snapshot() == {"d1": 12.5}


def test_a_digest_that_ran_twice_keeps_the_time_of_its_first_run():
    p = _sui_provider()
    p.remember_executed("d1", 12.5)
    p.remember_executed("d1", 99.0)
    assert p.executed_snapshot() == {"d1": 12.5}


def test_the_memory_keeps_10000_digests_and_drops_the_oldest_first():
    p = _sui_provider()
    for i in range(10_001):
        p.remember_executed(f"d{i}", float(i))
    snapshot = p.executed_snapshot()
    assert len(snapshot) == 10_000
    assert "d0" not in snapshot
    assert snapshot["d1"] == 1.0 and snapshot["d10000"] == 10000.0


def test_the_snapshot_of_executed_digests_is_a_copy():
    p = _sui_provider()
    p.remember_executed("d1", 12.5)
    snapshot = p.executed_snapshot()
    snapshot["d2"] = 1.0
    del snapshot["d1"]
    assert p.executed_snapshot() == {"d1": 12.5}


def test_forget_executed_empties_the_memory_and_a_digest_can_run_again():
    p = _sui_provider()
    p.remember_executed("d1", 12.5)
    p.forget_executed()
    assert p.executed_snapshot() == {}
    p.remember_executed("d1", 30.0)
    assert p.executed_snapshot() == {"d1": 30.0}


def test_two_providers_do_not_share_their_executed_digests():
    pool = Pool(name="sui-x", chain="sui")
    a = pool.add_provider("1", [Endpoint("graphql", "http", 18999)])
    b = pool.add_provider("2", [Endpoint("graphql", "http", 19000)])
    a.remember_executed("d1", 1.0)
    assert b.executed_snapshot() == {}
