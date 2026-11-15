"""Pools share no chain state, except where the topology says so.

A move of a head, or a reset, through pool A does not change what pool B answers. That holds for
every ordered pair of pools of the topology, except a pair that ``topology.FOLLOWS`` joins: the LN
pool reads the head of the BTC pool that it follows.

The check below runs over the real topology. It reports how many pools and how many pairs it
examined, so a check that examined nothing cannot pass. It is run again over a registry that is
built the way the simulator once was, with one chain object for every pool of a chain, and it must
fail there. A check that cannot fail proves nothing.
"""

import json

import pytest

from provider_simulator import topology
from provider_simulator.chains import CHAINS, EthChain, LnChain, new_chain
from provider_simulator.chains.btc import BtcChain
from provider_simulator.control_api import ControlApi
from provider_simulator.domain.endpoint import INTERFACES
from provider_simulator.domain.provider import Pool
from provider_simulator.domain.registry import Registry, build_registry
from provider_simulator.listeners import JsonRpcListener, RawRequest
from provider_simulator.listeners.ws import WsSubscriptions

MOVED_BEFORE = 7
MOVED_BY_A = 3


def _api(registry: Registry) -> ControlApi:
    return ControlApi(registry, WsSubscriptions())


def _heads_of(pool: Pool) -> dict[str, int]:
    """Every head that the chain object of a pool owns."""
    return {name: head.current() for name, head in pool.chain_object.iter_heads()}


def _heads_that_a_pool_serves(pool: Pool) -> list[str]:
    """The heads of the chain object of a pool that a provider of the pool answers.

    A head that is named for an interface is answered by a pool that serves the interface. Any
    other head, such as the single head of eth, is answered by every pool of the chain.
    """
    served = {endpoint.interface for provider in pool.providers.values() for endpoint in provider.endpoints}
    return [name for name in _heads_of(pool) if name not in INTERFACES or name in served]


def _move_every_head(api: ControlApi, pool: Pool, blocks: int) -> None:
    """Move each head that a pool serves, through the control API, naming the pool."""
    for head_name in _heads_that_a_pool_serves(pool):
        status, reply = api.advance({"chain": pool.chain, "pool": pool.name, "head": head_name, "blocks": blocks})
        assert status == 200, reply


def check_that_pools_share_no_chain_state(registry: Registry) -> tuple[int, int]:
    """Move and reset every pool in turn, and prove that no other pool saw it.

    For each ordered pair (A, B) of pools: B is moved first, so that its heads are not the base that
    a reset would leave. Then A is moved, and A is reset. After each step the heads of B must be what
    they were. A pair that the registry declares joined (a follower and its leader) is not a pair of
    independent pools, and it is left out. Two pools that merely hold one chain object are NOT left
    out: that is the sharing that this check exists to find.

    Returns the number of pools and the number of ordered pairs that it examined.
    """
    api = _api(registry)
    names = sorted(registry.pools)
    declared = {frozenset(pair) for pair in registry.follows.items()}
    pairs = 0
    for b_name in names:
        b = registry.pools[b_name]
        for a_name in names:
            if a_name == b_name or frozenset((a_name, b_name)) in declared:
                continue
            pairs += 1
            _move_every_head(api, b, MOVED_BEFORE)
            before = _heads_of(b)
            _move_every_head(api, registry.pools[a_name], MOVED_BY_A)
            assert _heads_of(b) == before, f"a move of {a_name} changed the heads of {b_name}"
            status, reply = api.reset(a_name)
            assert status == 200, reply
            assert _heads_of(b) == before, f"a reset of {a_name} changed the heads of {b_name}"
            api.reset(b_name)
    return len(names), pairs


def _a_registry_that_shares_one_chain_object_for_each_chain() -> Registry:
    """The state of the simulator before each pool held a chain object of its own."""
    registry = build_registry()
    shared = {chain: new_chain(chain) for chain in {pool.chain for pool in registry.pools.values()}}
    shared["ln"] = new_chain("ln", follows=shared["btc"])
    for pool in registry.pools.values():
        pool.chain_object = shared[pool.chain]
    return registry


def test_a_move_or_a_reset_of_one_pool_changes_no_other_pool_of_the_topology():
    pools_in_the_table = {row[0] for row in topology.TOPOLOGY}

    examined_pools, examined_pairs = check_that_pools_share_no_chain_state(build_registry())

    # The expected counts come from the rows of the table, not from the registry under test.
    assert examined_pools == len(pools_in_the_table)
    declared_pairs = 2 * len(topology.FOLLOWS)
    assert examined_pairs == len(pools_in_the_table) * (len(pools_in_the_table) - 1) - declared_pairs
    print(f"examined {examined_pools} pools and {examined_pairs} ordered pairs of pools")


def test_the_check_fails_on_a_registry_where_the_pools_of_a_chain_share_one_chain_object():
    with pytest.raises(AssertionError, match=r"a move of \S+ changed the heads of \S+"):
        check_that_pools_share_no_chain_state(_a_registry_that_shares_one_chain_object_for_each_chain())


def test_the_check_fails_when_only_a_reset_reaches_another_pool(monkeypatch):
    """A registry that isolates the moves and shares what a reset rewinds. The move assertion
    passes there, so the reset assertion is the one that has to fail."""
    api_reset = ControlApi.reset

    def a_reset_that_rewinds_every_pool_of_the_chain(self, pool=None):
        status, reply = api_reset(self, pool)
        if pool is not None:
            for other in self.registry.pools.values():
                if other.chain == self.registry.pools[pool].chain:
                    for _, head in other.chain_object.iter_heads():
                        head.reset()
        return status, reply

    monkeypatch.setattr(ControlApi, "reset", a_reset_that_rewinds_every_pool_of_the_chain)

    with pytest.raises(AssertionError, match=r"a reset of \S+ changed the heads of \S+"):
        check_that_pools_share_no_chain_state(build_registry())


def test_only_the_pools_that_the_topology_declares_share_chain_state():
    registry = build_registry()

    joined = {name for name in registry.pools if registry.state_group(name) != [name]}

    assert joined == set(topology.FOLLOWS) | set(topology.FOLLOWS.values())
    assert topology.FOLLOWS == {"ln-sim": "btc-sim"}


# ── The reset of every pool ───────────────────────────────────────────────


def test_a_reset_of_every_pool_rewinds_the_head_of_a_chain_object_that_only_one_pool_holds():
    registry = build_registry()
    api = _api(registry)
    base = {name: _heads_of(pool) for name, pool in registry.pools.items()}
    for name in ("eth-sim", "btc-sim", "solana-sim", "lava-sim-grpc", "sui-graphql-sim"):
        _move_every_head(api, registry.pools[name], 5)
    assert {name: _heads_of(pool) for name, pool in registry.pools.items()} != base, "the moves must show"

    status, reply = api.reset()

    assert status == 200, reply
    assert {name: _heads_of(pool) for name, pool in registry.pools.items()} == base
    assert reply["chains"] == sorted({row[1] for row in topology.TOPOLOGY})


def test_a_reset_of_every_pool_also_rewinds_the_registered_chain_object_that_no_pool_holds():
    api = _api(Registry())
    registered = CHAINS["eth"].head
    before = registered.current()
    registered.bump(3)
    try:
        status, reply = api.reset()
        assert status == 200, reply
        assert registered.current() == before
    finally:
        registered.reset()


def test_a_reset_of_every_pool_rewinds_a_chain_object_that_two_pools_hold():
    registry = Registry()
    shared = EthChain()
    for name in ("a-sim", "b-sim"):
        registry.pools[name] = Pool(name=name, chain="eth", chain_object=shared)
    base = shared.head.current()
    shared.head.bump(6)

    status, reply = _api(registry).reset()

    assert status == 200, reply
    assert shared.head.current() == base
    assert reply["chains"] == sorted(CHAINS), "the registered chain objects are rewound too, so every chain is named"
    assert reply["pools"] == ["a-sim", "b-sim"]


# ── The chain factory ──────────────────────────────────────────────────────


@pytest.mark.parametrize("chain", sorted(CHAINS))
def test_new_chain_builds_an_object_that_is_not_registered_and_not_shared(chain):
    first, second = new_chain(chain), new_chain(chain)

    assert first is not second
    assert first is not CHAINS[chain]
    assert type(first) is type(CHAINS[chain])


def test_new_chain_refuses_a_chain_that_it_does_not_know_and_lists_the_chains_it_knows():
    known = r"\['btc', 'eth', 'lava', 'ln', 'solana', 'sui', 'suit'\]"
    with pytest.raises(ValueError, match=f"unknown chain 'nope'; known chains are {known}"):
        new_chain("nope")


def test_an_ln_chain_that_follows_a_btc_chain_reads_the_head_of_that_chain_and_owns_no_head():
    btc = new_chain("btc")
    ln = new_chain("ln", follows=btc)

    assert isinstance(ln, LnChain) and ln.iter_heads() == []
    btc.head.bump(9)
    assert ln._btc_head is btc.head


def test_a_chain_that_reports_a_height_of_its_own_follows_no_other_chain():
    with pytest.raises(ValueError, match="chain 'eth' reports a height of its own, so it follows no other chain"):
        new_chain("eth", follows=new_chain("btc"))


def test_ln_follows_the_head_of_a_btc_chain_and_of_no_other_chain():
    with pytest.raises(ValueError, match="chain 'ln' follows the head of chain 'btc', not that of chain 'eth'"):
        new_chain("ln", follows=EthChain())


# ── The registry and a follower ────────────────────────────────────────────

HTTP_ROW = (("jsonrpc", "http", 0),)


def _rows(*pools_and_chains):
    """One provider row for each (pool, chain), each on a port of its own."""
    return tuple(
        (pool, chain, "1", f"{pool}-provider", False, "", (("jsonrpc", "http", 30_000 + index),))
        for index, (pool, chain) in enumerate(pools_and_chains)
    )


def test_a_follower_of_a_pool_of_another_chain_reads_the_head_of_that_pool():
    registry = build_registry(_rows(("b-sim", "btc"), ("l-sim", "ln")), follows={"l-sim": "b-sim"})

    leader, follower = registry.pools["b-sim"], registry.pools["l-sim"]
    assert follower.chain_object is not leader.chain_object
    leader.chain_object.head.bump(4)
    assert follower.chain_object._btc_head is leader.chain_object.head
    assert registry.state_group("l-sim") == ["b-sim", "l-sim"]


def test_a_follower_of_a_pool_of_its_own_chain_holds_the_object_of_that_pool():
    registry = build_registry(_rows(("a-sim", "eth"), ("b-sim", "eth"), ("c-sim", "eth")), follows={"b-sim": "a-sim"})

    assert registry.pools["b-sim"].chain_object is registry.pools["a-sim"].chain_object
    assert registry.pools["c-sim"].chain_object is not registry.pools["a-sim"].chain_object
    assert registry.state_group("a-sim") == ["a-sim", "b-sim"]
    assert registry.state_group("c-sim") == ["c-sim"]


def test_a_follower_is_built_after_its_leader_whatever_the_order_of_the_rows():
    registry = build_registry(_rows(("l-sim", "ln"), ("b-sim", "btc")), follows={"l-sim": "b-sim"})

    assert list(registry.pools) == ["l-sim", "b-sim"], "the pools keep the order of their rows"
    assert registry.pools["l-sim"].chain_object._btc_head is registry.pools["b-sim"].chain_object.head


def test_an_entry_whose_follower_is_not_in_the_rows_is_left_out():
    registry = build_registry(_rows(("a-sim", "eth")), follows={"l-sim": "b-sim"})

    assert registry.follows == {}


def test_a_follower_whose_leader_is_not_in_the_rows_is_refused():
    with pytest.raises(ValueError, match="pool 'l-sim' follows pool 'b-sim', which the topology does not have"):
        build_registry(_rows(("l-sim", "ln")), follows={"l-sim": "b-sim"})


def test_a_leader_that_follows_another_pool_is_refused():
    rows = _rows(("a-sim", "btc"), ("b-sim", "btc"), ("l-sim", "ln"))
    with pytest.raises(ValueError, match="a pool that follows another cannot be followed"):
        build_registry(rows, follows={"l-sim": "b-sim", "b-sim": "a-sim"})


def test_a_follower_that_cannot_follow_its_leader_is_refused_by_the_chain():
    with pytest.raises(ValueError, match="chain 'ln' follows the head of chain 'btc'"):
        build_registry(_rows(("e-sim", "eth"), ("l-sim", "ln")), follows={"l-sim": "e-sim"})


def test_state_group_refuses_a_pool_that_the_registry_does_not_have():
    with pytest.raises(KeyError, match="no pool 'nope-sim'"):
        build_registry().state_group("nope-sim")


def test_a_registry_that_holds_one_object_for_two_pools_puts_them_in_one_group():
    registry = Registry()
    shared = BtcChain()
    for name, held in [("a-sim", shared), ("b-sim", shared), ("c-sim", BtcChain())]:
        registry.pools[name] = Pool(name=name, chain="btc", chain_object=held)

    assert registry.state_group("a-sim") == registry.state_group("b-sim") == ["a-sim", "b-sim"]
    assert registry.state_group("c-sim") == ["c-sim"]


# ── The replies name the pools that share a state ──────────────────────────


def test_a_reset_of_a_pool_names_the_pool_and_the_pools_that_share_its_state():
    api = _api(build_registry())

    assert api.reset("eth-sim")[1]["pools"] == ["eth-sim"]
    assert api.reset("btc-sim")[1]["pools"] == ["btc-sim", "ln-sim"]
    assert api.reset("ln-sim")[1]["pools"] == ["btc-sim", "ln-sim"]


def test_a_reset_of_every_pool_names_every_pool():
    registry = build_registry()

    reply = _api(registry).reset()[1]

    assert reply["pools"] == sorted(registry.pools)
    assert reply["pool"] is None


def test_a_clear_of_the_history_alone_rewinds_no_chain_state_and_names_no_pool():
    reply = _api(build_registry()).clear_history("btc-sim")[1]

    assert reply["pools"] == []


def test_the_topology_names_for_each_pool_the_other_pools_that_share_its_state():
    registry = build_registry()

    topology_reply = _api(registry).get_topology()[1]["topology"]

    assert topology_reply["btc-sim"]["shares_state_with"] == ["ln-sim"]
    assert topology_reply["ln-sim"]["shares_state_with"] == ["btc-sim"]
    assert topology_reply["eth-sim"]["shares_state_with"] == []
    assert set(topology_reply) == set(registry.pools)
    assert all("shares_state_with" in entry for entry in topology_reply.values())


def test_a_read_of_the_head_of_btc_sim_names_ln_sim_which_answers_that_head():
    registry = build_registry()
    api = _api(registry)

    status, reply = api.advance({"chain": "btc", "pool": "btc-sim", "blocks": 2})

    assert status == 200, reply
    ln = registry.provider("ln-sim", "1")
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getinfo"}).encode()
    answer = JsonRpcListener(ln, ln.endpoints[0]).serve(RawRequest(body=request, headers={})).body
    assert answer["result"]["block_height"] == reply["head"], "ln-sim answers the head that was moved"
    assert reply["pools"] == ["btc-sim", "ln-sim"]
