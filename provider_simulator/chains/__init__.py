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
from provider_simulator.chains.sui import SuiChain, SuiTestnetChain

# The class that builds a chain object, for each chain name.
_CHAIN_CLASSES: dict[str, type[Chain]] = {
    "eth": EthChain,
    "btc": BtcChain,
    "ln": LnChain,
    "solana": SolanaChain,
    "lava": LavaChain,
    "sui": SuiChain,
    # Sui testnet answers with the code of Sui mainnet. It is a chain of its own
    # for its default chain identifier and for a head of its own.
    "suit": SuiTestnetChain,
}


def new_chain(name: str, *, follows: Chain | None = None) -> Chain:
    """Build a chain object that no other pool holds, with a head of its own.

    Each pool answers from a chain object of its own, so a head moved or a reset
    made through one pool never reaches another. ``follows`` names the one case
    where a pool reads another pool's head: LN reports the height of the BTC chain
    beneath it, so an LN chain object is handed the head of a BTC chain object and
    owns no head itself. ``POST /advance`` on LN is refused: you advance BTC.
    """
    try:
        chain_class = _CHAIN_CLASSES[name]
    except KeyError:
        raise ValueError(f"unknown chain {name!r}; known chains are {sorted(_CHAIN_CLASSES)}") from None
    if follows is None:
        return chain_class()
    if chain_class is not LnChain:
        raise ValueError(f"chain {name!r} reports a height of its own, so it follows no other chain")
    if not isinstance(follows, BtcChain):
        raise ValueError(f"chain 'ln' follows the head of chain 'btc', not that of chain {follows.name!r}")
    return LnChain(btc_head=follows.head)


# The chain object that a chain name stands for when no pool holds one: a request
# that names a chain that no pool serves moves the registered object. A pool does not
# take its chain object from here, it builds one with ``new_chain``.
_BTC = new_chain("btc")

CHAINS: dict[str, Chain] = {
    "eth": new_chain("eth"),
    "btc": _BTC,
    "ln": new_chain("ln", follows=_BTC),
    "solana": new_chain("solana"),
    "lava": new_chain("lava"),
    "sui": new_chain("sui"),
    "suit": new_chain("suit"),
}


def chain_for(name: str) -> Chain:
    """Return the REGISTERED chain object of a chain name; unknown names fail loudly.

    A pool does not answer from this object. It holds a chain object of its own, built with
    ``new_chain``. The registered object answers for a chain that no pool serves, and it is what
    the tests of a chain class ask for when they need a chain and no pool.
    """
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
    "SuiChain",
    "SuiTestnetChain",
    "LavaChain",
    "CHAINS",
    "chain_for",
    "new_chain",
]
