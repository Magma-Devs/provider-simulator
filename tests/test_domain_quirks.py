from provider_simulator.domain.quirks import (
    QUIRKS_BY_CHAIN,
    EthQuirks,
    Quirks,
    SolanaQuirks,
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
    try:
        q.update({"logs_lag_mode": "empty"})
    except ValueError as exc:
        assert "logs_lag_mode" in str(exc)
    else:
        raise AssertionError("an ETH quirk must not apply to a Solana provider")


def test_quirks_by_chain_maps_every_chain():
    assert QUIRKS_BY_CHAIN["eth"] is EthQuirks
    assert QUIRKS_BY_CHAIN["solana"] is SolanaQuirks
    assert QUIRKS_BY_CHAIN["btc"] is Quirks
    assert QUIRKS_BY_CHAIN["ln"] is Quirks
    assert QUIRKS_BY_CHAIN["lava"] is Quirks
