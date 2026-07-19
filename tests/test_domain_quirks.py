import pytest

from provider_simulator.domain.quirks import (
    EthQuirks,
    Quirks,
    SolanaQuirks,
    known_chains,
    quirks_for,
)
from stubs_solana import SOLANA_DEFAULT_SLOT_BLOCK_GAP


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


def test_quirks_for_unknown_chain_lists_the_valid_ones():
    with pytest.raises(ValueError, match="dogecoin"):
        quirks_for("dogecoin")
    assert known_chains() == ["btc", "eth", "lava", "ln", "solana"]
