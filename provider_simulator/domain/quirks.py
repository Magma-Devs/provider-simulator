"""Per-chain configuration knobs, kept out of the universal ScenarioConfig.

Only a few chains need extra settings. Ethereum can model a provider whose head
is fresh but whose log index lags. Solana models the slot vs
lastValidBlockHeight gap and per-provider slot offsets. Sui models the chain id,
the delay of its GraphQL index, and a provider that lies about a digest. Every
other chain uses the empty base. Keeping these typed and separate means a Solana-only knob sent
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
from stubs_sui_graphql import SUI_CHAIN_ID


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


@dataclass
class SuiQuirks(Quirks):
    """Settings of a simulated Sui GraphQL provider.

    ``chain_id`` is what ``chainIdentifier`` answers. ``transaction_index_lag_ms``
    is the delay that a real GraphQL index has between running a transaction and
    showing it: 0 turns the delay off, and a lookup then finds any digest. Above
    0, a lookup finds only a digest that this provider executed, and only after
    the delay has passed. ``digest_salt`` makes this provider answer other
    checkpoint digests than its peers, as a provider that lies about the chain.
    """

    chain_id: str = SUI_CHAIN_ID
    transaction_index_lag_ms: int = 0
    digest_salt: str = ""

    def _validate(self, cfg: dict) -> None:
        for name in ("chain_id", "digest_salt"):
            if name in cfg and not isinstance(cfg[name], str):
                raise ValueError(f"{name} must be a string, got {cfg[name]!r}")
        if "chain_id" in cfg and not cfg["chain_id"]:
            raise ValueError("chain_id must not be empty: it is the answer to chainIdentifier")
        if "transaction_index_lag_ms" in cfg:
            lag = cfg["transaction_index_lag_ms"]
            if isinstance(lag, bool) or not isinstance(lag, int) or lag < 0:
                raise ValueError(f"transaction_index_lag_ms must be a non-negative integer, got {lag!r}")


_QUIRKS_BY_CHAIN: dict[str, type[Quirks]] = {
    "eth": EthQuirks,
    "btc": Quirks,
    "ln": Quirks,
    "solana": SolanaQuirks,
    "lava": Quirks,
    "sui": SuiQuirks,
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
