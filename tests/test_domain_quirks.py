import pytest

from provider_simulator.domain.quirks import (
    EthQuirks,
    Quirks,
    SolanaQuirks,
    SuiQuirks,
    known_chains,
    quirks_for,
)
from stubs_solana import SOLANA_DEFAULT_SLOT_BLOCK_GAP
from stubs_sui_graphql import SUI_CHAIN_ID, SUI_TESTNET_CHAIN_ID


def test_base_quirks_has_no_fields():
    assert Quirks().snapshot() == {}


def test_eth_quirks_fields_and_defaults():
    q = EthQuirks()
    assert q.snapshot() == {
        "logs_indexed_up_to": None,
        "logs_lag_mode": "empty",
        "earliest_block": None,
        "earliest_block_ports": None,
    }
    q.update(
        {"logs_indexed_up_to": 100, "logs_lag_mode": "partial", "earliest_block": 5, "earliest_block_ports": [18638]}
    )
    assert q.snapshot() == {
        "logs_indexed_up_to": 100,
        "logs_lag_mode": "partial",
        "earliest_block": 5,
        "earliest_block_ports": [18638],
    }


@pytest.mark.parametrize("bad", [-1, True, "0x10", 1.5])
def test_eth_quirks_refuse_an_earliest_block_that_is_not_a_height(bad):
    q = EthQuirks()
    with pytest.raises(ValueError, match="earliest_block"):
        q.update({"earliest_block": bad})
    assert q.snapshot()["earliest_block"] is None, "a refused update writes nothing"


def test_eth_quirks_accept_null_to_turn_the_pruned_node_off():
    q = EthQuirks()
    q.update({"earliest_block": 10})
    q.update({"earliest_block": None})
    assert q.snapshot()["earliest_block"] is None


@pytest.mark.parametrize("bad", [[], "18638", 18638, [True], [-1], [0], ["18638"]])
def test_eth_quirks_refuse_earliest_block_ports_that_are_not_a_list_of_ports(bad):
    q = EthQuirks()
    with pytest.raises(ValueError, match="earliest_block_ports"):
        q.update({"earliest_block_ports": bad})
    assert q.snapshot()["earliest_block_ports"] is None, "a refused update writes nothing"


def test_the_pruned_node_applies_only_at_the_ports_it_names():
    q = EthQuirks()
    q.update({"earliest_block": 7})
    assert q.for_port(q.snapshot(), 1)["earliest_block"] == 7, "no ports named: every port"
    q.update({"earliest_block_ports": [2]})
    assert q.for_port(q.snapshot(), 2)["earliest_block"] == 7
    assert q.for_port(q.snapshot(), 1)["earliest_block"] is None, "another port is an archive node"
    assert q.snapshot()["earliest_block"] == 7, "the live config is not changed"


def test_quirks_of_other_chains_apply_at_every_port():
    q = SolanaQuirks()
    assert q.for_port(q.snapshot(), 1) == q.snapshot()


def test_solana_quirks_defaults_share_the_one_gap_constant():
    q = SolanaQuirks()
    snap = q.snapshot()
    assert snap["slot_block_gap"] == SOLANA_DEFAULT_SLOT_BLOCK_GAP
    assert snap["slot_offset"] == 0
    assert snap["unknown_method_mode"] == "null"


def test_solana_quirks_reject_an_eth_field():
    q = SolanaQuirks()
    with pytest.raises(ValueError, match="logs_lag_mode"):
        q.update({"logs_lag_mode": "empty"})


def test_quirks_for_maps_every_chain():
    assert quirks_for("eth") is EthQuirks
    assert quirks_for("solana") is SolanaQuirks
    assert quirks_for("btc") is Quirks
    assert quirks_for("ln") is Quirks
    assert quirks_for("lava") is Quirks
    assert quirks_for("sui") is SuiQuirks


def test_quirks_for_unknown_chain_lists_the_valid_ones():
    with pytest.raises(ValueError, match="dogecoin"):
        quirks_for("dogecoin")
    assert known_chains() == ["btc", "eth", "lava", "ln", "solana", "sui", "suit"]


def test_sui_quirks_defaults_are_the_mainnet_id_no_lag_and_no_salt():
    assert SuiQuirks().snapshot() == {"chain_id": SUI_CHAIN_ID, "transaction_index_lag_ms": 0, "digest_salt": ""}


def test_sui_quirks_accept_a_chain_id_a_lag_and_a_salt_and_reset_restores_the_defaults():
    q = SuiQuirks()
    q.update({"chain_id": SUI_TESTNET_CHAIN_ID, "transaction_index_lag_ms": 1300, "digest_salt": "x"})
    assert q.snapshot() == {"chain_id": SUI_TESTNET_CHAIN_ID, "transaction_index_lag_ms": 1300, "digest_salt": "x"}
    q.reset()
    assert q.snapshot() == {"chain_id": SUI_CHAIN_ID, "transaction_index_lag_ms": 0, "digest_salt": ""}


def test_sui_quirks_reject_an_unknown_key_and_a_value_that_cannot_apply():
    q = SuiQuirks()
    with pytest.raises(ValueError, match="slot_offset"):
        q.update({"slot_offset": 1})
    for bad in (
        {"transaction_index_lag_ms": -1},
        {"transaction_index_lag_ms": "200"},
        {"transaction_index_lag_ms": True},
    ):
        with pytest.raises(ValueError, match="transaction_index_lag_ms"):
            q.update(bad)
    for bad in ({"chain_id": 5}, {"chain_id": ""}, {"digest_salt": 5}):
        with pytest.raises(ValueError, match=next(iter(bad))):
            q.update(bad)
    assert q.snapshot()["transaction_index_lag_ms"] == 0
