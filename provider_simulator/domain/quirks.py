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
from stubs_solana import (
    SOLANA_DEFAULT_CONFIRMED_LAG_SLOTS,
    SOLANA_DEFAULT_FINALIZED_LAG_SLOTS,
    SOLANA_DEFAULT_PROCESSED_LAG_SLOTS,
    SOLANA_DEFAULT_SLOT_BLOCK_GAP,
)
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
    """Settings of a simulated Solana provider.

    ``slot_offset`` is this provider's distance from the chain head, and
    ``slot_block_gap`` is the distance between ``context.slot`` and
    ``lastValidBlockHeight``. ``processed_lag_slots``, ``confirmed_lag_slots`` and
    ``finalized_lag_slots`` are how many slots below the head of the provider a
    reply at each commitment sits. They start at 0, so that every commitment
    answers the head; a test that wants the shape of a real network sets the lags
    that were measured on real networks (see ``stubs_solana``). Each one is a
    setting of its own, so an update that names one leaves the other two as they
    are. They never fall from processed to finalized, as on a real node.
    """

    slot_block_gap: int = SOLANA_DEFAULT_SLOT_BLOCK_GAP
    slot_offset: int = 0
    unknown_method_mode: str = "null"  # null | error
    processed_lag_slots: int = SOLANA_DEFAULT_PROCESSED_LAG_SLOTS
    confirmed_lag_slots: int = SOLANA_DEFAULT_CONFIRMED_LAG_SLOTS
    finalized_lag_slots: int = SOLANA_DEFAULT_FINALIZED_LAG_SLOTS

    def _validate(self, cfg: dict) -> None:
        """Refuse a lag that is not a count of slots below the head, and lags that no node has.

        ``True`` is refused although it is an int, and a text is refused although it
        can name a number: the chain subtracts the lag from the head, so any other
        type would fail on a request and not on the update.

        A node's processed slot is never below its confirmed slot, and its confirmed
        slot is never below its finalized slot. The order is read on the lags merged
        over the stored ones, because an update can name one lag.
        """
        names = ("processed_lag_slots", "confirmed_lag_slots", "finalized_lag_slots")
        for name in names:
            if name not in cfg:
                continue
            lag = cfg[name]
            if isinstance(lag, bool) or not isinstance(lag, int) or lag < 0:
                raise ValueError(f"{name} must be a non-negative integer (slots below the head), got {lag!r}")
        processed, confirmed, finalized = (cfg.get(name, getattr(self, name)) for name in names)
        if not processed <= confirmed <= finalized:
            raise ValueError(
                "processed_lag_slots <= confirmed_lag_slots <= finalized_lag_slots must hold: a node's processed "
                "slot is never below its confirmed slot, and its confirmed slot never below its finalized slot. "
                f"This update would make them {processed}, {confirmed} and {finalized}"
            )


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
