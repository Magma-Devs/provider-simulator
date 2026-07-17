"""Provider identity: pools, providers, and how a pool builds its providers.

A provider is one named node entry in one router's config — the same unit the
router scores and blocks. It owns its fault config, its chain-specific quirks,
its telemetry log, and the endpoints it listens on. State is never shared
between providers, so a fault on one can never leak to another. Two chains that
happen to number their providers the same (eth-sim:1 and btc-sim:1) are
different objects in different pools.

Construction goes through Pool.add_provider, which builds, duplicate-checks,
and registers atomically — a provider cannot exist half-attached to a pool.

Pool and Provider are identity objects (eq=False): two providers are "equal"
only if they are the same object. Dataclass-generated equality would recurse
through the Pool<->Provider cycle and make both types unhashable.
"""

import threading
from dataclasses import dataclass, field

from provider_simulator.domain.call_log import CallLog
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.quirks import Quirks, quirks_for
from provider_simulator.domain.scenario import ScenarioConfig


@dataclass(eq=False)
class Pool:
    name: str
    chain: str  # chain name; becomes a Chain object in a later story
    providers: dict[str, "Provider"] = field(default_factory=dict, init=False)

    def add_provider(self, pid: str, endpoints: list[Endpoint]) -> "Provider":
        """Build a provider, refuse duplicates, and register it — one atomic
        step, so no caller can create a provider its pool doesn't know about."""
        if pid in self.providers:
            raise ValueError(f"duplicate provider {self.name}:{pid}")
        provider = Provider(
            pool=self,
            pid=pid,
            scenario=ScenarioConfig(),
            quirks=quirks_for(self.chain)(),
            log=CallLog(pool=self.name, pid=pid),
            endpoints=list(endpoints),
        )
        self.providers[pid] = provider
        return provider


@dataclass(eq=False)
class Provider:
    pool: Pool
    pid: str
    scenario: ScenarioConfig
    quirks: Quirks
    log: CallLog
    endpoints: list[Endpoint]
    # Runtime counter for the fail_first_n sequenced fault — not config, not
    # telemetry, so it lives here. Endpoints targeted by a scenario consume it;
    # the control API resets it when a new fail_first_n scenario is applied.
    _fail_count: int = field(default=0, init=False, repr=False)
    _fail_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def key(self) -> str:
        return f"{self.pool.name}:{self.pid}"

    def consume_fail(self) -> int:
        """Increment and return the fail_first_n counter (targeted endpoints)."""
        with self._fail_lock:
            self._fail_count += 1
            return self._fail_count

    def peek_fail(self) -> int:
        """Read the fail_first_n counter without advancing it (observers)."""
        with self._fail_lock:
            return self._fail_count

    def reset_fail(self) -> None:
        """Reset the fail_first_n counter (a fresh fail_first_n scenario / reset)."""
        with self._fail_lock:
            self._fail_count = 0
