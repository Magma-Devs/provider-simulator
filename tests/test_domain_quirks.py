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
    assert q.snapshot() == {"logs_indexed_up_to": None, "logs_lag_mode": "empty"}
    q.update({"logs_indexed_up_to": 100, "logs_lag_mode": "partial"})
    assert q.snapshot() == {"logs_indexed_up_to": 100, "logs_lag_mode": "partial"}


def test_solana_quirks_defaults_share_the_one_gap_constant():
    q = SolanaQuirks()
    snap = q.snapshot()
    assert snap["slot_block_gap"] == SOLANA_DEFAULT_SLOT_BLOCK_GAP
    assert snap["slot_offset"] == 0
    assert snap["unknown_method_mode"] == "null"


def test_solana_quirks_lags_start_at_zero_so_every_commitment_answers_the_head():
    """No lag by default. A real network's shape (measured on 2026-11-10: processed is the head,
    confirmed and finalized one slot below it) is what a test sets, and no default."""
    snap = SolanaQuirks().snapshot()
    assert (snap["processed_lag_slots"], snap["confirmed_lag_slots"], snap["finalized_lag_slots"]) == (0, 0, 0)


def test_solana_quirks_merge_a_lag_by_field_and_a_reset_restores_the_defaults():
    q = SolanaQuirks()
    q.update({"finalized_lag_slots": 32})
    snap = q.snapshot()
    assert (snap["processed_lag_slots"], snap["confirmed_lag_slots"], snap["finalized_lag_slots"]) == (0, 0, 32)
    q.update({"confirmed_lag_slots": 2})
    snap = q.snapshot()
    assert (snap["processed_lag_slots"], snap["confirmed_lag_slots"], snap["finalized_lag_slots"]) == (0, 2, 32)
    q.reset()
    snap = q.snapshot()
    assert (snap["processed_lag_slots"], snap["confirmed_lag_slots"], snap["finalized_lag_slots"]) == (0, 0, 0)


@pytest.mark.parametrize("name", ["processed_lag_slots", "confirmed_lag_slots", "finalized_lag_slots"])
@pytest.mark.parametrize("bad", [-1, "1", True, 1.5, None, [1]])
def test_solana_quirks_refuse_a_lag_that_is_not_a_count_of_slots_and_write_nothing(name, bad):
    q = SolanaQuirks()
    before = q.snapshot()
    with pytest.raises(ValueError, match=f"{name} must be a non-negative integer"):
        q.update({name: bad})
    assert q.snapshot() == before


def test_solana_quirks_accept_a_lag_of_zero_for_every_commitment():
    q = SolanaQuirks()
    q.update({"processed_lag_slots": 0, "confirmed_lag_slots": 0, "finalized_lag_slots": 0})
    assert q.snapshot()["finalized_lag_slots"] == 0


@pytest.mark.parametrize(
    "stored, write",
    [
        ({}, {"processed_lag_slots": 1}),
        ({}, {"confirmed_lag_slots": 1}),
        ({"confirmed_lag_slots": 1, "finalized_lag_slots": 1}, {"finalized_lag_slots": 0}),
        ({"confirmed_lag_slots": 2, "finalized_lag_slots": 2}, {"processed_lag_slots": 3}),
    ],
)
def test_solana_quirks_refuse_lags_that_no_node_has_and_write_nothing(stored, write):
    """A node's processed slot is never below its confirmed slot, and its confirmed slot is never
    below its finalized slot. The rule reads the lags merged over the stored ones, because a write
    can name one lag."""
    q = SolanaQuirks()
    q.update(stored)
    before = q.snapshot()
    with pytest.raises(ValueError, match="processed_lag_slots <= confirmed_lag_slots <= finalized_lag_slots"):
        q.update(write)
    assert q.snapshot() == before


def test_solana_quirks_accept_lags_raised_one_commitment_at_a_time_in_order():
    """The positive control of the rule above: each write keeps the order."""
    q = SolanaQuirks()
    for name in ("finalized_lag_slots", "confirmed_lag_slots", "processed_lag_slots"):
        q.update({name: 5})
    snap = q.snapshot()
    assert (snap["processed_lag_slots"], snap["confirmed_lag_slots"], snap["finalized_lag_slots"]) == (5, 5, 5)


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
