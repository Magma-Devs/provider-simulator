"""Per-chain configuration knobs, kept out of the universal ScenarioConfig.

Only a few chains need extra settings. Ethereum can model a provider whose head
is fresh but whose log index lags. Solana models the slot vs
lastValidBlockHeight gap and per-provider slot offsets. Sui models the chain id,
the delay of its GraphQL index, and a provider that lies about a digest. Sui
testnet has the same settings, with the chain id of the testnet as the default.
Every other chain uses the empty base. Keeping these typed and separate means a Solana-only knob sent
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
from stubs_sui_graphql import SUI_CHAIN_ID, SUI_TESTNET_CHAIN_ID


@dataclass
class Quirks(IntrospectiveConfig):
    """No chain-specific knobs. Used by btc / ln / lava."""

    def for_port(self, snapshot: dict, port: int) -> dict:
        """The snapshot as the endpoint at ``port`` serves it. Every quirk applies on
        every port of the provider, unless the quirks of its chain say otherwise."""
        return snapshot


@dataclass
class EthQuirks(Quirks):
    """Settings of a simulated Ethereum provider.

    ``logs_indexed_up_to`` and ``logs_lag_mode`` model a head that is fresh while
    the log index lags.

    ``earliest_block`` models a pruned node, one that keeps only recent history.
    ``earliest`` resolves to that height, a block below it answers ``null``, and a
    state read at a height below it answers ``missing trie node``. None, the
    default, is an archive node: ``earliest`` is block 0 and every height is
    served. A router's archive check asks for ``earliest`` and expects block 0, so
    any value above 0 fails it.

    ``earliest_block_ports`` names the ports where ``earliest_block`` applies. None,
    the default, applies it on every port of the provider. A provider that serves one
    interface and transport at two addresses can so be a pruned node at one address
    and an archive node at the other, which is how a test shows that a router checks
    each node url at its own address (MAG-4505 F5).
    """

    logs_indexed_up_to: int | None = None
    logs_lag_mode: str = "empty"  # empty | partial
    earliest_block: int | None = None
    earliest_block_ports: list[int] | None = None

    def _validate(self, cfg: dict) -> None:
        if "earliest_block" in cfg:
            value = cfg["earliest_block"]
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"earliest_block must be a non-negative integer or null, got {value!r}")
        if "earliest_block_ports" in cfg:
            ports = cfg["earliest_block_ports"]
            if ports is not None and (
                not isinstance(ports, list)
                or not ports
                or any(isinstance(p, bool) or not isinstance(p, int) or p <= 0 for p in ports)
            ):
                raise ValueError(f"earliest_block_ports must be a non-empty list of ports or null, got {ports!r}")

    def for_port(self, snapshot: dict, port: int) -> dict:
        """The snapshot as the endpoint at ``port`` serves it: ``earliest_block``
        applies only at the ports ``earliest_block_ports`` names, when it names any."""
        ports = snapshot.get("earliest_block_ports")
        if ports is not None and port not in ports:
            return {**snapshot, "earliest_block": None}
        return snapshot


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


@dataclass
class SuiTestnetQuirks(SuiQuirks):
    """Settings of a simulated Sui testnet GraphQL provider.

    The settings of ``SuiQuirks``. Only the default of ``chain_id`` differs: a provider
    of a testnet pool answers the chain identifier of Sui testnet. A reset of the pool
    writes the defaults back, so the provider keeps answering the testnet identifier,
    and a router configured for Sui testnet passes its chain-id check at every start.
    """

    chain_id: str = SUI_TESTNET_CHAIN_ID


_QUIRKS_BY_CHAIN: dict[str, type[Quirks]] = {
    "eth": EthQuirks,
    "btc": Quirks,
    "ln": Quirks,
    "solana": SolanaQuirks,
    "lava": Quirks,
    "sui": SuiQuirks,
    "suit": SuiTestnetQuirks,
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
