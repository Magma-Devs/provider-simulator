"""Turn the topology table into live provider objects, and validate it.

build_registry is called once at startup. It builds one Pool per pool name and
one Provider per row (via Pool.add_provider, which owns the duplicate-pid
check). It refuses a table that would collide or misbehave at runtime — a port
used twice, a duplicate pool:pid, an unknown chain, two chains under one pool
name, an empty endpoint list, a non-positive port, an unknown interface or
transport, or a ':' inside a pool name or pid (it would make the "pool:pid"
address ambiguous) — each with a clear error, instead of failing mysteriously
on the first request.
"""

from provider_simulator import topology
from provider_simulator.domain.endpoint import INTERFACES, TRANSPORTS, Endpoint
from provider_simulator.domain.provider import Pool, Provider
from provider_simulator.domain.quirks import quirks_for


class Registry:
    def __init__(self) -> None:
        self.pools: dict[str, Pool] = {}
        self._by_port: dict[int, tuple[Provider, Endpoint]] = {}

    def provider(self, pool: str, pid: str) -> Provider:
        if pool not in self.pools:
            raise KeyError(f"no pool {pool!r}; pools are {sorted(self.pools)}")
        if pid not in self.pools[pool].providers:
            raise KeyError(
                f"no provider {pool}:{pid}; providers in {pool!r} are " f"{sorted(self.pools[pool].providers)}"
            )
        return self.pools[pool].providers[pid]

    def by_port(self, port: int) -> tuple[Provider, Endpoint]:
        if port not in self._by_port:
            raise KeyError(f"no endpoint on port {port}")
        return self._by_port[port]

    def all_providers(self) -> list[Provider]:
        return [p for pool in self.pools.values() for p in pool.providers.values()]

    def ports(self) -> list[int]:
        """Every bound listener port, sorted — the set a readiness probe checks."""
        return sorted(self._by_port)


def _validate_row(pool_name: str, chain: str, pid: str, endpoint_specs) -> None:
    if not pool_name or ":" in pool_name:
        raise ValueError(f"bad pool name {pool_name!r}: must be non-empty and contain no ':'")
    if not pid or ":" in pid:
        raise ValueError(f"bad pid {pid!r} in pool {pool_name!r}: non-empty, no ':' allowed")
    quirks_for(chain)  # raises with the known-chain list on an unknown chain
    if not endpoint_specs:
        raise ValueError(f"provider {pool_name}:{pid} has no endpoints — it would be unreachable")
    for interface, transport, port in endpoint_specs:
        if interface not in INTERFACES:
            raise ValueError(
                f"provider {pool_name}:{pid}: unknown interface {interface!r}; " f"valid: {list(INTERFACES)}"
            )
        if transport not in TRANSPORTS:
            raise ValueError(
                f"provider {pool_name}:{pid}: unknown transport {transport!r}; " f"valid: {list(TRANSPORTS)}"
            )
        if not isinstance(port, int) or port <= 0:
            raise ValueError(f"provider {pool_name}:{pid}: bad port {port!r}")


def build_registry(rows=None) -> Registry:
    """Build a Registry from topology rows. ``rows=None`` reads
    ``topology.TOPOLOGY`` at call time (so a test that patches the module
    attribute gets its patched table, not an import-time binding)."""
    if rows is None:
        rows = topology.TOPOLOGY
    reg = Registry()
    for pool_name, chain, pid, _name, _is_backup, endpoint_specs in rows:
        _validate_row(pool_name, chain, pid, endpoint_specs)
        pool = reg.pools.get(pool_name)
        if pool is None:
            pool = Pool(name=pool_name, chain=chain)
            reg.pools[pool_name] = pool
        elif pool.chain != chain:
            raise ValueError(
                f"pool {pool_name!r} declares two chains: {pool.chain!r} and {chain!r} — "
                "one pool serves exactly one chain"
            )
        endpoints = [Endpoint(i, t, port) for (i, t, port) in endpoint_specs]
        for ep in endpoints:
            if ep.port in reg._by_port:
                raise ValueError(f"duplicate port {ep.port} in topology")
        provider = pool.add_provider(pid, endpoints)  # raises on duplicate pool:pid
        for ep in endpoints:
            reg._by_port[ep.port] = (provider, ep)
    return reg
