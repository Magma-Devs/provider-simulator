"""Chain classes: one per blockchain the simulator fakes.

Each chain owns how it builds a success response, its chain head, and which
quirks it accepts. The listener (a later story) calls ``build_success`` on the
provider's chain. Until then these are additive — the flat ``handlers_*``
modules still drive the running server, and this package reimplements the same
behaviour against the redesigned domain shapes (ScenarioConfig + Quirks).

``chain_for(name)`` is the registry lookup; more chains are added as their
classes land.
"""

from provider_simulator.chains.base import AdvancingHead, Chain
from provider_simulator.chains.eth import EthChain

CHAINS: dict[str, Chain] = {
    "eth": EthChain(),
}


def chain_for(name: str) -> Chain:
    """Return the chain instance for a chain name; unknown names fail loudly."""
    try:
        return CHAINS[name]
    except KeyError:
        raise ValueError(f"unknown chain {name!r}; known chains are {sorted(CHAINS)}") from None


__all__ = ["AdvancingHead", "Chain", "EthChain", "CHAINS", "chain_for"]
