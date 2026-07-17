"""Turn the topology table into live provider objects, and validate it.

build_registry is called once at startup. It builds one Pool per pool name and
one Provider per row, wiring each provider's config/quirks/log/endpoints. It
refuses a table that would collide at runtime — a port used twice, a pool:pid
used twice, or a chain with no known quirks type — with a clear error, instead
of failing mysteriously on the first request.
"""

from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool, Provider, build_provider
from provider_simulator.domain.quirks import QUIRKS_BY_CHAIN
from provider_simulator.topology import TOPOLOGY


class Registry:
    def __init__(self) -> None:
        self.pools: dict[str, Pool] = {}
        self._by_port: dict[int, tuple[Provider, Endpoint]] = {}

    def provider(self, pool: str, pid: str) -> Provider:
        if pool not in self.pools or pid not in self.pools[pool].providers:
            valid = [p.key for p in self.all_providers()]
            raise KeyError(f"no provider {pool}:{pid}; valid providers are {valid}")
        return self.pools[pool].providers[pid]

    def by_port(self, port: int) -> tuple[Provider, Endpoint]:
        if port not in self._by_port:
            raise KeyError(f"no endpoint on port {port}")
        return self._by_port[port]

    def all_providers(self) -> list[Provider]:
        return [p for pool in self.pools.values() for p in pool.providers.values()]


def build_registry(rows=TOPOLOGY) -> Registry:
    reg = Registry()
    for pool_name, chain, pid, endpoint_specs in rows:
        if chain not in QUIRKS_BY_CHAIN:
            raise ValueError(
                f"pool {pool_name!r} names unknown chain {chain!r}; "
                f"known chains are {sorted(QUIRKS_BY_CHAIN)}"
            )
        pool = reg.pools.get(pool_name)
        if pool is None:
            pool = Pool(name=pool_name, chain=chain, providers={})
            reg.pools[pool_name] = pool
        if pid in pool.providers:
            raise ValueError(f"duplicate provider {pool_name}:{pid} in topology")
        endpoints = [Endpoint(i, t, port) for (i, t, port) in endpoint_specs]
        provider = build_provider(pool, pid, endpoints)
        pool.providers[pid] = provider
        for ep in endpoints:
            if ep.port in reg._by_port:
                raise ValueError(f"duplicate port {ep.port} in topology")
            reg._by_port[ep.port] = (provider, ep)
    return reg
