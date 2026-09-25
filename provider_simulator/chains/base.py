"""Chain base class and the advancing-head primitive.

A Chain builds the success-path response for one blockchain and owns any state
that response depends on — most notably the block head. The head is INSTANCE
state on the chain, not a module global, so each chain has its own and nothing
is shared across the process by accident.

THE RULE FOR BLOCK HASHES
-------------------------
Derive a block hash from the block HEIGHT and from nothing else.

Never from the provider — not its name, not its pool slot, not a quirk it
carries, not how far behind it is. Two of our providers asked about the same
height must answer with the same hash, even when they sit at different heads.

The reason is not tidiness. The router watches for forks, and two providers
giving different hashes for one height is exactly how a real chain announces
that it has split. A hash that changed with the provider would make the router
believe our simulator had forked. It would then behave differently for the rest
of the test — and the test would go on passing, having measured our harness
instead of the router. Nothing turns red. Nobody looks.

A head-relative reply is a separate matter and is fine. ``getbestblockhash``
names no height, so a lagging provider naming an older block there is correct,
not a fork.

``tests/test_chains_block_hash_agreement.py`` enforces this. A new chain must
add an entry there, even an empty one; the suite fails on a missing key rather
than skipping a chain in silence.
"""

import threading
import time
from abc import ABC, abstractmethod

from provider_simulator.domain.quirks import Quirks


class AdvancingHead:
    """A block head that is static by default and can be advanced on demand.

    Static (rate 0) reads return the base value, byte-identical to a fixed
    constant. A test that needs the router's sync optimizer to demote a stale
    provider turns on a continuous rate (blocks/sec) or bumps the head once;
    toggling the rate never moves the head backward (elapsed advance is folded
    into a fixed offset first).
    """

    def __init__(self, base: int) -> None:
        self._lock = threading.Lock()
        self._base = base
        self._extra = 0  # one-time bumps + folded continuous advance
        self._rate = 0.0  # blocks/sec; 0 = static
        self._anchor = 0.0  # time.monotonic() when the rate took effect

    def current(self) -> int:
        with self._lock:
            extra = self._extra
            if self._rate > 0.0:
                extra += int((time.monotonic() - self._anchor) * self._rate)
            return self._base + extra

    def set_rate(self, rate_per_sec: float) -> None:
        with self._lock:
            if self._rate > 0.0:
                self._extra += int((time.monotonic() - self._anchor) * self._rate)
            self._rate = float(rate_per_sec) if rate_per_sec and rate_per_sec > 0.0 else 0.0
            self._anchor = time.monotonic()

    def bump(self, blocks: int) -> None:
        with self._lock:
            self._extra += int(blocks)

    def reset(self) -> None:
        with self._lock:
            self._extra = 0
            self._rate = 0.0


class Chain(ABC):
    """One blockchain's success-path builder.

    Subclasses set ``name`` and ``quirks_type`` and implement ``build_success``.
    ``build_success`` receives the parsed request plus two snapshot dicts — the
    provider's ScenarioConfig and its Quirks — and returns
    ``(http_status, response_body)``. It never mutates provider state.
    """

    name: str
    quirks_type: type[Quirks] = Quirks

    #: Heads this chain serves, keyed by the interface that serves each one.
    #:
    #: A chain that speaks one protocol keeps a single ``head`` attribute and
    #: leaves this empty — eth does. A chain that speaks several serves a
    #: DIFFERENT height on each, so one head cannot describe it: lava answers
    #: 20 million over REST, 25 million over gRPC and 5 million over
    #: Tendermint-RPC. The router tracks a tip per endpoint, so a head per
    #: interface is what it is actually observing.
    #
    # Declared as a type only. A mutable default here would be ONE dict shared
    # by every chain that does not set its own, so writing a head onto eth
    # would give btc and solana the same one. ``iter_heads`` reads it through
    # ``getattr`` for exactly that reason.
    heads: dict[str, "AdvancingHead"]

    def iter_heads(self) -> "list[tuple[str, AdvancingHead]]":
        """Every head this chain owns, as ``(name, head)``.

        Callers that move or reset a head go through this rather than reading
        ``head`` directly, so a chain with one head and a chain with several
        are handled by the same code. A chain with neither yields nothing.

        The single ``head`` is reported under the name ``"default"``, which is
        also the name ``POST /advance`` assumes when a caller names none.
        """
        found: "list[tuple[str, AdvancingHead]]" = []
        single = getattr(self, "head", None)
        if single is not None:
            found.append(("default", single))
        found.extend(sorted(getattr(self, "heads", {}).items()))
        return found

    @abstractmethod
    def build_success(self, request: dict, scenario: dict, quirks: dict, interface: str = "") -> tuple[int, dict]:
        """Return (http_status, response_body) for the success path.

        ``interface`` is the application protocol of the serving endpoint
        (jsonrpc / rest / grpc / tendermintrpc). Single-interface chains ignore
        it; multi-interface chains (lava) branch on it.
        """
