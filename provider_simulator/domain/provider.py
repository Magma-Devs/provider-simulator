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

    @property
    def key(self) -> str:
        return f"{self.pool.name}:{self.pid}"
