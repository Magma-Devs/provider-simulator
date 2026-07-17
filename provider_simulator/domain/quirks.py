"""Per-chain configuration knobs, kept out of the universal ScenarioConfig.

Only a few chains need extra settings. Ethereum can model a provider whose head
is fresh but whose log index lags. Solana models the slot vs
lastValidBlockHeight gap and per-provider slot offsets. Every other chain uses
the empty base. Keeping these typed and separate means a Solana-only knob sent
to a Bitcoin provider is rejected, not silently dropped.

QUIRKS_BY_CHAIN is the temporary source of "which quirks class does this chain
use". When chains become classes (a later story) each chain owns its quirks
type and this mapping goes away.
"""

from dataclasses import dataclass

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


QUIRKS_BY_CHAIN: dict[str, type[Quirks]] = {
    "eth": EthQuirks,
    "btc": Quirks,
    "ln": Quirks,
    "solana": SolanaQuirks,
    "lava": Quirks,
}
