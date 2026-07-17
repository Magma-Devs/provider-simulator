"""Provider identity: pools, providers, and the factory that wires one.

A provider is one named node entry in one router's config — the same unit the
router scores and blocks. It owns its fault config, its chain-specific quirks,
its telemetry log, and the endpoints it listens on. State is never shared
between providers, so a fault on one can never leak to another. Two chains that
happen to number their providers the same (eth-sim:1 and btc-sim:1) are
different objects in different pools.
"""

from dataclasses import dataclass, field

from provider_simulator.domain.call_log import CallLog
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.quirks import QUIRKS_BY_CHAIN, Quirks
from provider_simulator.domain.scenario import ScenarioConfig


@dataclass
class Pool:
    name: str
    chain: str  # chain name; becomes a Chain object in a later story
    providers: dict[str, "Provider"] = field(default_factory=dict)


@dataclass
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


def build_provider(pool: Pool, pid: str, endpoints: list[Endpoint]) -> Provider:
    quirks_cls = QUIRKS_BY_CHAIN[pool.chain]
    return Provider(
        pool=pool,
        pid=pid,
        scenario=ScenarioConfig(),
        quirks=quirks_cls(),
        log=CallLog(),
        endpoints=list(endpoints),
    )
