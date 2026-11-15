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
        # {follower: leader}: the pools that read another pool's head. See topology.FOLLOWS.
        self.follows: dict[str, str] = {}

    def state_group(self, pool: str) -> list[str]:
        """Every pool that shares chain state with ``pool``, ``pool`` included, sorted.

        Two pools share state when they hold the same chain object, or when one follows
        the other. The group is closed: a pool that shares state with a member is a member.
        A pool of its own, which is nearly every pool, is a group of one.
        """
        if pool not in self.pools:
            raise KeyError(f"no pool {pool!r}; pools are {sorted(self.pools)}")
        group = {pool}
        grew = True
        while grew:
            grew = False
            held = {id(self.pools[name].chain_object) for name in group}
            for name, other in self.pools.items():
                if name not in group and id(other.chain_object) in held:
                    group.add(name)
                    grew = True
            for follower, leader in self.follows.items():
                if (follower in group) != (leader in group):
                    group.update((follower, leader))
                    grew = True
        return sorted(group)

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


def build_registry(rows=None, follows=None) -> Registry:
    """Build a Registry from topology rows. ``rows=None`` reads
    ``topology.TOPOLOGY`` at call time (so a test that patches the module
    attribute gets its patched table, not an import-time binding).

    Every pool gets a chain object of its own, with a head of its own, except a pool that
    ``follows`` names: ``{follower: leader}``, read from ``topology.FOLLOWS`` when it is None.
    A follower of a pool of another chain reads that pool's head. A follower of a pool of its
    own chain holds the leader's chain object. An entry whose follower is not in the rows is
    left out, so a table of a few rows needs no entry for pools that it does not have.
    """
    # Imported here and not at the top: chains.base imports domain.quirks, which loads this package,
    # so an import at the top is circular and fails when the chains package is imported first.
    from provider_simulator.chains import new_chain

    if rows is None:
        rows = topology.TOPOLOGY
    if follows is None:
        follows = topology.FOLLOWS
    reg = Registry()
    chain_of_pool: dict[str, str] = {}
    for pool_name, chain, pid, name, is_backup, group_label, endpoint_specs in rows:
        _validate_row(pool_name, chain, pid, endpoint_specs)
        if chain_of_pool.setdefault(pool_name, chain) != chain:
            raise ValueError(
                f"pool {pool_name!r} declares two chains: {chain_of_pool[pool_name]!r} and {chain!r} — "
                "one pool serves exactly one chain"
            )
    reg.follows = _checked_follows(follows, chain_of_pool)

    # A pool is built before the pools that follow it, and then put in the order of its first row.
    built: dict[str, Pool] = {}
    for pool_name, chain in chain_of_pool.items():
        if pool_name not in reg.follows:
            built[pool_name] = Pool(name=pool_name, chain=chain)
    for follower, leader_name in reg.follows.items():
        leader = built[leader_name]
        chain = chain_of_pool[follower]
        chain_object = leader.chain_object if chain == leader.chain else new_chain(chain, follows=leader.chain_object)
        built[follower] = Pool(name=follower, chain=chain, chain_object=chain_object)
    reg.pools.update({pool_name: built[pool_name] for pool_name in chain_of_pool})

    for pool_name, chain, pid, name, is_backup, group_label, endpoint_specs in rows:
        pool = reg.pools[pool_name]
        endpoints = [Endpoint(i, t, port) for (i, t, port) in endpoint_specs]
        for ep in endpoints:
            if ep.port in reg._by_port:
                raise ValueError(f"duplicate port {ep.port} in topology")
        provider = pool.add_provider(
            pid, endpoints, name=name, is_backup=is_backup, group_label=group_label
        )  # raises on duplicate pool:pid
        for ep in endpoints:
            reg._by_port[ep.port] = (provider, ep)
    return reg


def _checked_follows(follows, chain_of_pool: dict[str, str]) -> dict[str, str]:
    """The ``{follower: leader}`` entries whose follower is in the table, each one checked."""
    kept = {follower: leader for follower, leader in follows.items() if follower in chain_of_pool}
    for follower, leader in kept.items():
        if leader not in chain_of_pool:
            raise ValueError(f"pool {follower!r} follows pool {leader!r}, which the topology does not have")
        if leader in kept:
            raise ValueError(
                f"pool {follower!r} follows pool {leader!r}, which itself follows a pool: "
                "a pool that follows another cannot be followed"
            )
    return kept
