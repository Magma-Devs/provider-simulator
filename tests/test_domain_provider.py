from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool, Provider, build_provider
from provider_simulator.domain.quirks import EthQuirks, Quirks, SolanaQuirks
from provider_simulator.domain.scenario import ScenarioConfig


def test_build_provider_wires_config_quirks_log_for_eth():
    pool = Pool(name="eth-sim", chain="eth", providers={})
    p = build_provider(
        pool, "1", [Endpoint("jsonrpc", "http", 18545), Endpoint("jsonrpc", "ws", 18557)]
    )
    assert isinstance(p, Provider)
    assert isinstance(p.scenario, ScenarioConfig)
    assert isinstance(p.quirks, EthQuirks)  # eth pool -> EthQuirks
    assert p.log.stats()["total_calls"] == 0
    assert [e.port for e in p.endpoints] == [18545, 18557]


def test_build_provider_picks_solana_quirks_for_solana_pool():
    pool = Pool(name="solana-sim", chain="solana", providers={})
    p = build_provider(pool, "1", [Endpoint("jsonrpc", "http", 18582)])
    assert isinstance(p.quirks, SolanaQuirks)


def test_build_provider_uses_base_quirks_for_btc():
    pool = Pool(name="btc-sim", chain="btc", providers={})
    p = build_provider(pool, "1", [Endpoint("jsonrpc", "http", 18575)])
    assert type(p.quirks) is Quirks


def test_provider_key_is_pool_colon_pid():
    pool = Pool(name="lava-sim-grpc", chain="lava", providers={})
    p = build_provider(pool, "4", [Endpoint("grpc", "http2", 18563)])
    assert p.key == "lava-sim-grpc:4"


def test_two_providers_have_independent_state():
    pool = Pool(name="eth-sim", chain="eth", providers={})
    a = build_provider(pool, "1", [Endpoint("jsonrpc", "http", 18545)])
    b = build_provider(pool, "2", [Endpoint("jsonrpc", "http", 18546)])
    a.scenario.update({"mode": "down"})
    assert b.scenario.snapshot()["mode"] == "success"
    assert a.quirks is not b.quirks
    assert a.log is not b.log
