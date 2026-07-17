import pytest

from provider_simulator.domain.registry import Registry, build_registry


def test_build_registry_from_default_topology():
    reg = build_registry()
    # 9 pools, 35 providers total (matches the TOPOLOGY table)
    assert set(reg.pools) == {
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
    assert len(reg.all_providers()) == 35


def test_provider_lookup_by_pool_and_pid():
    reg = build_registry()
    p = reg.provider("eth-sim", "1")
    assert p.key == "eth-sim:1"


def test_provider_lookup_miss_lists_valid_keys():
    reg = build_registry()
    with pytest.raises(KeyError) as exc:
        reg.provider("eth-sim", "99")
    assert "eth-sim" in str(exc.value)


def test_by_port_resolves_provider_and_endpoint():
    reg = build_registry()
    provider, endpoint = reg.by_port(18557)  # eth-sim:1 ws
    assert provider.key == "eth-sim:1"
    assert endpoint.transport == "ws"
    assert endpoint.interface == "jsonrpc"


def test_eth_and_btc_provider_1_are_distinct_objects():
    reg = build_registry()
    assert reg.provider("eth-sim", "1") is not reg.provider("btc-sim", "1")


def test_build_registry_rejects_duplicate_port():
    rows = [
        ("eth-sim", "eth", "1", [("jsonrpc", "http", 18545)]),
        ("btc-sim", "btc", "1", [("jsonrpc", "http", 18545)]),  # dup port
    ]
    with pytest.raises(ValueError) as exc:
        build_registry(rows)
    assert "18545" in str(exc.value)


def test_build_registry_rejects_duplicate_pool_pid():
    rows = [
        ("eth-sim", "eth", "1", [("jsonrpc", "http", 18545)]),
        ("eth-sim", "eth", "1", [("jsonrpc", "http", 18546)]),  # dup pool:pid
    ]
    with pytest.raises(ValueError) as exc:
        build_registry(rows)
    assert "eth-sim:1" in str(exc.value)


def test_build_registry_rejects_unknown_chain():
    rows = [("mystery-sim", "dogecoin", "1", [("jsonrpc", "http", 19999)])]
    with pytest.raises(ValueError) as exc:
        build_registry(rows)
    assert "dogecoin" in str(exc.value)


def test_registry_type_exposed():
    assert isinstance(build_registry(), Registry)
