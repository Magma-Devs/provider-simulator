"""Chain classes: one per blockchain the simulator fakes.

Each chain owns how it builds a success response, its chain head / slot, and
which quirks it accepts. The listener (a later story) calls ``build_success`` on
the provider's chain. Until then these are additive — the flat ``handlers_*``
modules still drive the running server, and this package reimplements the same
behaviour against the redesigned domain shapes (ScenarioConfig + Quirks).

``chain_for(name)`` is the registry lookup. The lava chain (rest / grpc /
tendermintrpc) is not here yet — it serves three interfaces, so its
``build_success`` needs an interface dimension the single-interface chains
don't; that design is pinned in a later story.
"""

from provider_simulator.chains.base import AdvancingHead, Chain
from provider_simulator.chains.btc import BtcChain
from provider_simulator.chains.eth import EthChain
from provider_simulator.chains.lava import LavaChain
from provider_simulator.chains.ln import LnChain
from provider_simulator.chains.solana import SolanaChain

CHAINS: dict[str, Chain] = {
    "eth": EthChain(),
    "btc": BtcChain(),
    "ln": LnChain(),
    "solana": SolanaChain(),
    "lava": LavaChain(),
}


def chain_for(name: str) -> Chain:
    """Return the chain instance for a chain name; unknown names fail loudly."""
    try:
        return CHAINS[name]
    except KeyError:
        raise ValueError(f"unknown chain {name!r}; known chains are {sorted(CHAINS)}") from None


__all__ = [
    "AdvancingHead",
    "Chain",
    "EthChain",
    "BtcChain",
    "LnChain",
    "SolanaChain",
    "LavaChain",
    "CHAINS",
    "chain_for",
]
