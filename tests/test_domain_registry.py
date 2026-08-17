import pytest

import provider_simulator.topology as topology_module
from provider_simulator.domain.registry import build_registry
from provider_simulator.topology import TOPOLOGY


def test_build_registry_maps_topology_rows_one_to_one():
    reg = build_registry()
    # Derived from the table, not hardcoded: the property this test owns is
    # "every row becomes exactly one provider, no silent drop or merge".
    assert set(reg.pools) == {row[0] for row in TOPOLOGY}
    assert len(reg.all_providers()) == len({(pool, pid) for pool, _c, pid, _n, _b, _group, _e in TOPOLOGY})


def test_provider_lookup_by_pool_and_pid():
    reg = build_registry()
    p = reg.provider("eth-sim", "1")
    assert p.key == "eth-sim:1"


def test_provider_lookup_miss_names_the_valid_options():
    reg = build_registry()
    with pytest.raises(KeyError) as exc:
        reg.provider("eth-sim", "99")
    assert "providers in 'eth-sim'" in str(exc.value)
    with pytest.raises(KeyError) as exc:
        reg.provider("no-such-pool", "1")
    assert "pools are" in str(exc.value)


def test_by_port_resolves_provider_and_endpoint():
    reg = build_registry()
    provider, endpoint = reg.by_port(18557)  # eth-sim:1 ws
    assert provider.key == "eth-sim:1"
    assert endpoint.transport == "ws"
    assert endpoint.interface == "jsonrpc"


def test_eth_and_btc_provider_1_are_distinct_objects():
    reg = build_registry()
    assert reg.provider("eth-sim", "1") is not reg.provider("btc-sim", "1")


def test_eth_duo_sim_providers_resolve_and_are_distinct_from_eth_sim():
    reg = build_registry()
    high = reg.provider("eth-duo-sim", "1")
    low = reg.provider("eth-duo-sim", "2")
    assert high.key == "eth-duo-sim:1"
    assert low.key == "eth-duo-sim:2"
    # Distinct objects from eth-sim's pid 1/2 — a /scenario flip on one pool
    # can no longer leak into the other's provider state (the isolation gap
    # dedicated ports close).
    assert high is not reg.provider("eth-sim", "1")
    assert low is not reg.provider("eth-sim", "2")


def test_build_registry_reads_patched_topology(monkeypatch):
    small = (("eth-sim", "eth", "1", "EthProvider1", False, "", (("jsonrpc", "http", 18545),)),)
    monkeypatch.setattr(topology_module, "TOPOLOGY", small)
    reg = build_registry()  # default path must see the patched table
    assert len(reg.all_providers()) == 1


def test_build_registry_rejects_duplicate_port():
    rows = [
        ("eth-sim", "eth", "1", "EthProvider1", False, "", [("jsonrpc", "http", 18545)]),
        ("btc-sim", "btc", "1", "BtcProvider1", False, "", [("jsonrpc", "http", 18545)]),  # dup port
    ]
    with pytest.raises(ValueError, match="18545"):
        build_registry(rows)


def test_build_registry_rejects_duplicate_pool_pid():
    rows = [
        ("eth-sim", "eth", "1", "EthProvider1", False, "", [("jsonrpc", "http", 18545)]),
        ("eth-sim", "eth", "1", "EthProvider1", False, "", [("jsonrpc", "http", 18546)]),  # dup pool:pid
    ]
    with pytest.raises(ValueError, match="eth-sim:1"):
        build_registry(rows)


def test_build_registry_rejects_unknown_chain():
    rows = [("mystery-sim", "dogecoin", "1", "MysteryProvider1", False, "", [("jsonrpc", "http", 19999)])]
    with pytest.raises(ValueError, match="dogecoin"):
        build_registry(rows)


def test_build_registry_rejects_two_chains_under_one_pool():
    rows = [
        ("x-sim", "solana", "1", "XProvider1", False, "", [("jsonrpc", "http", 19991)]),
        ("x-sim", "eth", "2", "XProvider2", False, "", [("jsonrpc", "http", 19992)]),  # chain conflict
    ]
    with pytest.raises(ValueError, match="two chains"):
        build_registry(rows)


def test_build_registry_rejects_empty_endpoint_list():
    rows = [("btc-sim", "btc", "4", "BtcProvider4", False, "", [])]
    with pytest.raises(ValueError, match="no endpoints"):
        build_registry(rows)


def test_build_registry_rejects_bad_port_and_bad_names():
    with pytest.raises(ValueError, match="bad port"):
        build_registry([("btc-sim", "btc", "1", "BtcProvider1", False, "", [("jsonrpc", "http", 0)])])
    with pytest.raises(ValueError, match="bad pid"):
        build_registry([("btc-sim", "btc", "", "BtcProvider0", False, "", [("jsonrpc", "http", 19993)])])
    with pytest.raises(ValueError, match="no ':'"):
        build_registry([("btc-sim", "btc", "grpc:1", "BtcProvidergrpc:1", False, "", [("jsonrpc", "http", 19994)])])
    with pytest.raises(ValueError, match="bad pool name"):
        build_registry([("lava:grpc", "lava", "1", "LavaGrpcProvider1", False, "", [("grpc", "http2", 19995)])])


def test_build_registry_rejects_unknown_interface_and_transport():
    with pytest.raises(ValueError, match="unknown interface"):
        build_registry([("btc-sim", "btc", "1", "BtcProvider1", False, "", [("bitcoinrpc", "http", 19996)])])
    with pytest.raises(ValueError, match="unknown transport"):
        build_registry([("btc-sim", "btc", "1", "BtcProvider1", False, "", [("jsonrpc", "grpc", 19997)])])
