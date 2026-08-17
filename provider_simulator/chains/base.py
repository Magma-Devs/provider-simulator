"""Chain base class and the advancing-head primitive.

A Chain builds the success-path response for one blockchain and owns any state
that response depends on — most notably the block head. The head is INSTANCE
state on the chain, not a module global, so each chain has its own and nothing
is shared across the process by accident.
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

    @abstractmethod
    def build_success(self, request: dict, scenario: dict, quirks: dict, interface: str = "") -> tuple[int, dict]:
        """Return (http_status, response_body) for the success path.

        ``interface`` is the application protocol of the serving endpoint
        (jsonrpc / rest / grpc / tendermintrpc). Single-interface chains ignore
        it; multi-interface chains (lava) branch on it.
        """
