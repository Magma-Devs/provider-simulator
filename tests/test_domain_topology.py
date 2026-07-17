from provider_simulator.domain.quirks import QUIRKS_BY_CHAIN
from provider_simulator.topology import TOPOLOGY, iter_rows


def test_every_port_is_unique():
    ports = [port for _pool, _chain, _pid, eps in TOPOLOGY for (_i, _t, port) in eps]
    assert len(ports) == len(set(ports)), "duplicate port in TOPOLOGY"


def test_every_pool_pid_is_unique():
    keys = [(pool, pid) for pool, _chain, pid, _eps in TOPOLOGY]
    assert len(keys) == len(set(keys)), "duplicate (pool, pid) in TOPOLOGY"


def test_every_chain_is_known():
    for _pool, chain, _pid, _eps in TOPOLOGY:
        assert chain in QUIRKS_BY_CHAIN, f"unknown chain {chain!r}"


def test_expected_pools_present():
    pools = {row[0] for row in TOPOLOGY}
    assert pools == {
        "eth-sim",
        "eth-solo",
        "btc-sim",
        "ln-sim",
        "solana-sim",
        "solana-solo",
        "lava-sim-grpc",
        "lava-sim-rest",
        "lava-sim-tm",
    }


def test_eth_sim_provider_1_has_http_and_ws():
    rows = [r for r in TOPOLOGY if r[0] == "eth-sim" and r[2] == "1"]
    assert len(rows) == 1
    eps = rows[0][3]
    assert (("jsonrpc", "http", 18545) in eps) and (("jsonrpc", "ws", 18557) in eps)


def test_lava_rest_and_grpc_provider_1_are_distinct_pools():
    rest1 = [r for r in TOPOLOGY if r[0] == "lava-sim-rest" and r[2] == "1"]
    grpc1 = [r for r in TOPOLOGY if r[0] == "lava-sim-grpc" and r[2] == "1"]
    assert rest1 and grpc1
    assert rest1[0][3] == [("rest", "http", 18551)]
    assert grpc1[0][3] == [("grpc", "http2", 18548)]


def test_iter_rows_matches_topology():
    assert list(iter_rows()) == TOPOLOGY
