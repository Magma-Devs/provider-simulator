"""Per-chain configuration knobs, kept out of the universal ScenarioConfig.

Only a few chains need extra settings. Ethereum can model a provider whose head
is fresh but whose log index lags. Solana models the slot vs
lastValidBlockHeight gap and per-provider slot offsets. Every other chain uses
the empty base. Keeping these typed and separate means a Solana-only knob sent
to a Bitcoin provider is rejected, not silently dropped.

quirks_for(chain) is the ONE authority for both "which chains exist" and
"which quirks class a chain uses" — the registry's validation and provider
construction both go through it, so the known-chain set can never drift
between call sites. When chains become classes (a later story) each chain
owns its quirks type and this accessor delegates to it — one module to edit.
"""

from dataclasses import dataclass
from types import MappingProxyType

from provider_simulator.domain.introspective_config import IntrospectiveConfig
from stubs_solana import SOLANA_DEFAULT_SLOT_BLOCK_GAP


@dataclass
class Quirks(IntrospectiveConfig):
    """No chain-specific knobs. Used by btc / ln / lava."""


@dataclass
class EthQuirks(Quirks):
    logs_indexed_up_to: int | None = None
    logs_lag_mode: str = "empty"  # empty | partial


@dataclass
class SolanaQuirks(Quirks):
    slot_block_gap: int = SOLANA_DEFAULT_SLOT_BLOCK_GAP
    slot_offset: int = 0
    unknown_method_mode: str = "null"  # null | error


_QUIRKS_BY_CHAIN: dict[str, type[Quirks]] = {
    "eth": EthQuirks,
    "btc": Quirks,
    "ln": Quirks,
    "solana": SolanaQuirks,
    "lava": Quirks,
}

# Read-only view: callers can't mutate the known-chain set (which drives
# registry validation) by reaching into this mapping.
QUIRKS_BY_CHAIN = MappingProxyType(_QUIRKS_BY_CHAIN)


def known_chains() -> list[str]:
    return sorted(_QUIRKS_BY_CHAIN)


def quirks_for(chain: str) -> type[Quirks]:
    """Return the quirks class for a chain; unknown chains fail with the full
    valid list so a topology typo is diagnosed at the error site."""
    try:
        return _QUIRKS_BY_CHAIN[chain]
    except KeyError:
        raise ValueError(f"unknown chain {chain!r}; known chains are {known_chains()}") from None
