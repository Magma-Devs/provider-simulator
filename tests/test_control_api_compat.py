import pytest

from provider_simulator.control_api_compat import translate_block


def test_eth_primary_content_fault_scopes_to_http():
    pool, pid, sc, q = translate_block("1", {"chain_family": "eth", "mode": "error"})
    assert (pool, pid) == ("eth-sim", "1")
    assert sc["mode"] == "error"
    assert sc["transports"] == ["http"]
    assert q == {}


def test_eth_ws_scopes_to_ws():
    pool, pid, sc, _ = translate_block("2", {"chain_family": "ws", "mode": "error"})
    assert (pool, pid) == ("eth-sim", "2")
    assert sc["transports"] == ["ws"]


def test_down_is_provider_wide_no_filter():
    pool, pid, sc, _ = translate_block("1", {"chain_family": "btc", "mode": "down"})
    assert (pool, pid) == ("btc-sim", "1")
    assert "transports" not in sc  # provider-wide


def test_rest_and_grpc_and_tm_pools():
    assert translate_block("3", {"chain_family": "rest", "mode": "error"})[0] == "lava-sim-rest"
    assert translate_block("3", {"chain_family": "grpc", "mode": "error"})[0] == "lava-sim-grpc"
    assert (
        translate_block("3", {"chain_family": "tendermintrpc", "mode": "error"})[0] == "lava-sim-tm"
    )


def test_grpc_owning_transport_is_http2():
    _, _, sc, _ = translate_block("1", {"chain_family": "grpc", "mode": "error"})
    assert sc["transports"] == ["http2"]


def test_backup_pid_remapping():
    assert translate_block("4", {"chain_family": "eth", "mode": "error"})[:2] == ("eth-sim", "4")
    assert translate_block("7", {"chain_family": "grpc", "mode": "error"})[:2] == (
        "lava-sim-grpc",
        "4",
    )
    assert translate_block("10", {"chain_family": "rest", "mode": "error"})[:2] == (
        "lava-sim-rest",
        "4",
    )
    assert translate_block("13", {"chain_family": "tendermintrpc", "mode": "error"})[:2] == (
        "lava-sim-tm",
        "4",
    )


def test_ws_backup_pids_owning_transport_is_ws():
    pool, pid, sc, _ = translate_block("16", {"chain_family": "eth", "mode": "error"})
    assert (pool, pid) == ("eth-sim", "4")
    assert sc["transports"] == ["ws"]


def test_solo_pids():
    assert translate_block("19", {"chain_family": "eth", "mode": "error"})[:2] == (
        "eth-solo-sim",
        "1",
    )
    assert translate_block("20", {"chain_family": "solana", "mode": "error"})[:2] == (
        "solana-solo-sim",
        "1",
    )


def test_solana_quirks_are_renamed():
    _, _, _, q = translate_block(
        "1",
        {"chain_family": "solana", "solana_slot_offset": 5, "solana_slot_block_gap": 10},
    )
    assert q == {"slot_offset": 5, "slot_block_gap": 10}


def test_eth_quirks_kept():
    _, _, _, q = translate_block(
        "1", {"chain_family": "eth", "logs_indexed_up_to": 100, "logs_lag_mode": "partial"}
    )
    assert q == {"logs_indexed_up_to": 100, "logs_lag_mode": "partial"}


def test_missing_chain_family_raises():
    with pytest.raises(ValueError, match="missing chain_family"):
        translate_block("1", {"mode": "down"})


def test_unknown_chain_family_raises():
    with pytest.raises(ValueError, match="unknown chain_family"):
        translate_block("1", {"chain_family": "dogecoin", "mode": "error"})


def test_unknown_pid_raises():
    with pytest.raises(ValueError, match="unknown legacy pid"):
        translate_block("99", {"chain_family": "eth", "mode": "error"})
