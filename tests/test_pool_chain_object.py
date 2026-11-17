"""A pool holds its chain object, and it is a chain object of its own.

``Pool.chain`` is the name of the chain. ``Pool.chain_object`` is the chain that the
providers of the pool answer from. Every reader that starts from a pool takes the chain
there: the listeners, the scope of a reset, and the check of ``frozen_head``. ``POST /advance``
takes the object of the pool that a request names, and it needs the name once a chain has
several chain objects.

Every pool holds a chain object of its own, so a head moved or a reset made through one pool
never reaches another. The one exception is a pool that ``topology.FOLLOWS`` lists: the LN pool
reads the head of the BTC pool that it follows. The tests below pin that, and they pin that a
reader uses the object that its pool holds. The registered objects of ``CHAINS`` belong to no
pool: a request for a chain that no pool serves moves one.
"""

import json

import pytest

from provider_simulator import topology
from provider_simulator.chains import CHAINS, Chain
from provider_simulator.chains.base import AdvancingHead
from provider_simulator.chains.eth import EthChain
from provider_simulator.chains.lava import LavaChain
from provider_simulator.control_api import ControlApi
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.domain.registry import Registry, build_registry
from provider_simulator.listeners import JsonRpcListener, RawRequest
from provider_simulator.listeners.ws import WsSubscriptions

HTTP = Endpoint("jsonrpc", "http", 18545)
ETH_BASE = 20_000_000  # the eth head of a simulator that nobody advanced: 0x1312D00


@pytest.fixture(autouse=True)
def _the_registered_eth_head_starts_and_ends_at_its_base():
    """The heads live on the registered chain objects, and every test of the session shares them."""
    CHAINS["eth"].head.reset()
    yield
    CHAINS["eth"].head.reset()


def _api(registry=None):
    return ControlApi(registry or build_registry(), WsSubscriptions())


def _block_number(provider) -> int:
    """What ``eth_blockNumber`` answers on the first endpoint of this provider."""
    listener = JsonRpcListener(provider, provider.endpoints[0])
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber"}).encode()
    body = listener.serve(RawRequest(body=request, headers={})).body
    assert isinstance(body, dict), body
    return int(body["result"], 16)


# ── which object a pool holds ─────────────────────────────────────────────────
@pytest.mark.parametrize("chain", sorted(CHAINS))
def test_a_pool_built_without_a_chain_object_builds_one_of_its_own(chain):
    first = Pool(name=f"{chain}-first-sim", chain=chain)
    second = Pool(name=f"{chain}-second-sim", chain=chain)
    assert first.chain_object is not CHAINS[chain]
    assert type(first.chain_object) is type(CHAINS[chain])
    assert first.chain_object is not second.chain_object


def test_every_pool_of_the_topology_holds_a_chain_object_that_no_other_pool_holds():
    registry = build_registry()
    pools = list(registry.pools.values())
    # The expected count comes from the rows of the table, not from the registry under test.
    assert len(pools) == len({row[0] for row in topology.TOPOLOGY})
    assert len({id(pool.chain_object) for pool in pools}) == len(pools)
    for pool in pools:
        assert pool.chain_object is not CHAINS[pool.chain], pool.name
    # The check is not vacuous: some chain is served by more than one pool, and each one has its own.
    pools_of = {}
    for pool in pools:
        pools_of.setdefault(pool.chain, []).append(pool)
    assert max(len(of_chain) for of_chain in pools_of.values()) > 1


def test_the_registry_follows_what_the_topology_declares_and_no_other_pool_shares_state():
    registry = build_registry()
    assert registry.follows == {"ln-sim": "btc-sim"}
    assert registry.state_group("btc-sim") == ["btc-sim", "ln-sim"]
    assert registry.state_group("ln-sim") == ["btc-sim", "ln-sim"]
    alone = [name for name in registry.pools if name not in ("btc-sim", "ln-sim")]
    assert alone, "the other pools exist, so the check must examine some"
    for name in alone:
        assert registry.state_group(name) == [name], name


def _ln_height(provider) -> int:
    """What ``getinfo`` answers as the block height on the first endpoint of this LN provider."""
    listener = JsonRpcListener(provider, provider.endpoints[0])
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getinfo"}).encode()
    body = listener.serve(RawRequest(body=request, headers={})).body
    assert isinstance(body, dict), body
    return int(body["result"]["block_height"])


def test_the_pool_that_follows_another_reports_the_head_of_that_pool_and_moves_none_of_its_own():
    registry = build_registry()
    api = _api(registry)
    ln = registry.provider("ln-sim", "1")
    before = _ln_height(ln)
    status, reply = api.advance({"pool": "btc-sim", "blocks": 5})
    assert status == 200, reply
    assert reply["pools"] == ["btc-sim", "ln-sim"], "a move of btc-sim moves what ln-sim answers"
    assert _ln_height(ln) == before + 5
    status, reply = api.advance({"chain": "ln", "pool": "ln-sim", "blocks": 1})
    assert status == 400 and "no advanceable head" in reply["error"], reply
    assert _ln_height(ln) == before + 5
    status, _ = api.reset("btc-sim")
    assert status == 200
    assert _ln_height(ln) == before


# ── what one pool's head does to another pool ─────────────────────────────────
def test_a_head_moved_through_one_pool_is_not_the_head_that_the_providers_of_another_pool_answer():
    registry = build_registry()
    api = _api(registry)
    first = registry.provider("eth-sim", "1")
    second = registry.provider("eth-solo-sim", "1")
    assert (_block_number(first), _block_number(second)) == (ETH_BASE, ETH_BASE)
    status, _ = api.advance({"pool": "eth-sim", "blocks": 3})
    assert status == 200
    assert (_block_number(first), _block_number(second)) == (ETH_BASE + 3, ETH_BASE)


def test_a_reset_of_one_pool_leaves_the_head_that_the_providers_of_another_pool_answer():
    registry = build_registry()
    api = _api(registry)
    reset_pool = registry.provider("eth-sim", "1")
    other = registry.provider("eth-solo-sim", "1")
    api.advance({"pool": "eth-solo-sim", "blocks": 5})
    api.advance({"pool": "eth-sim", "blocks": 2})
    assert (_block_number(reset_pool), _block_number(other)) == (ETH_BASE + 2, ETH_BASE + 5)
    status, reply = api.reset("eth-sim")
    assert status == 200
    assert reply["pools"] == ["eth-sim"]
    assert (_block_number(reset_pool), _block_number(other)) == (ETH_BASE, ETH_BASE + 5)


# ── a reader uses the object that its pool holds ──────────────────────────────
def test_a_listener_answers_from_the_chain_object_of_its_pool():
    own = EthChain()
    provider = Pool(name="own-chain-sim", chain="eth", chain_object=own).add_provider("1", [HTTP])
    own.head.bump(7)
    assert _block_number(provider) == ETH_BASE + 7
    assert CHAINS["eth"].head.current() == ETH_BASE  # the registered chain did not move


def test_a_reset_of_a_pool_rewinds_the_chain_object_of_that_pool_and_not_the_registered_one():
    registry = build_registry()
    api = _api(registry)
    own = EthChain()
    registry.pools["eth-solo-sim"].chain_object = own
    own.head.bump(4)
    CHAINS["eth"].head.bump(9)
    status, reply = api.reset("eth-solo-sim")
    assert status == 200
    assert reply["chains"] == ["eth"]  # the reply still names the chain, not the object
    assert own.head.current() == ETH_BASE
    assert CHAINS["eth"].head.current() == ETH_BASE + 9


class _EthChainThatDoesNotReadFrozenHead(EthChain):
    reads_frozen_head = False


def test_frozen_head_is_refused_when_the_chain_object_of_the_pool_does_not_read_it():
    registry = build_registry()
    api = _api(registry)
    status, reply = api.apply_scenario({"providers": {"eth-sim:1": {"frozen_head": 19_999_000}}})
    assert status == 200, reply  # the chain object of eth-sim reads it
    registry.pools["eth-solo-sim"].chain_object = _EthChainThatDoesNotReadFrozenHead()
    status, reply = api.apply_scenario({"providers": {"eth-solo-sim:1": {"frozen_head": 19_999_000}}})
    assert status == 400
    assert "the eth chain does not read it" in reply["error"], reply


# ── POST /advance: which chain object a request moves, and when it needs a pool ────────────
# A chain whose pools all hold one chain object needs no pool in the request. That is a chain that
# one pool serves. A chain with several chain objects has no single object to pick, so a request
# without a pool is refused and not guessed. Every pool holds an object of its own, so that is every
# chain that several pools serve. The groups follow object identity: two objects of one class are
# two objects.
def _pools_of(chain):
    """The pools that the topology table gives to a chain, sorted. Read from the rows of the table
    and not from a registry, so the expected value does not depend on the code under test."""
    return sorted({row[0] for row in topology.TOPOLOGY if row[1] == chain})


def _heads_of(chain_object):
    return {name: head.current() for name, head in chain_object.iter_heads()}


def _api_where_pools_hold(chain_objects: dict[str, Chain]) -> ControlApi:
    """The control API over the real registry, after each named pool was given a chain object of its own."""
    registry = build_registry()
    for pool_name, chain_object in chain_objects.items():
        registry.pools[pool_name].chain_object = chain_object
    return _api(registry)


def _the_error_for_eth_with_one_chain_object_for_each_pool():
    pools = _pools_of("eth")
    return (
        f"chain 'eth' has {len(pools)} chain objects, so name the pool with \"pool\". "
        f"Pools that share one object: {[[pool] for pool in pools]!r}"
    )


# ── a chain whose pools hold one chain object ─────────────────────────────────
@pytest.mark.parametrize("chain", ["suit"])
def test_a_read_of_a_chain_that_one_pool_serves_needs_no_pool_and_names_that_pool(chain):
    assert len(_pools_of(chain)) == 1, "this holds for a chain that one pool serves"
    status, reply = _api().advance({"chain": chain})
    assert status == 200, reply
    assert reply["pool"] is None
    assert reply["pools"] == _pools_of(chain)


@pytest.mark.parametrize("chain", ["eth", "btc", "solana", "lava", "sui"])
def test_a_request_that_names_only_a_chain_is_refused_when_several_pools_serve_it(chain):
    pools = _pools_of(chain)
    assert len(pools) > 1, "this holds for a chain that several pools serve"
    status, reply = _api().advance({"chain": chain})
    assert status == 400, reply
    assert reply == {
        "error": f'chain {chain!r} has {len(pools)} chain objects, so name the pool with "pool". '
        f"Pools that share one object: {[[pool] for pool in pools]!r}"
    }


def test_the_reply_to_a_read_through_a_named_pool_holds_exactly_these_fields():
    status, reply = _api().advance({"chain": "eth", "pool": "eth-sim"})
    assert status == 200, reply
    assert reply == {
        "status": "ok",
        "chain": "eth",
        "pool": "eth-sim",
        "pools": ["eth-sim"],
        "head_name": "default",
        "heads": {"default": ETH_BASE},
        "head": ETH_BASE,
    }


def test_a_move_through_a_named_pool_names_the_pools_that_share_its_state():
    status, reply = _api().advance({"chain": "eth", "pool": "eth-sim", "blocks": 3})
    assert status == 200, reply
    assert reply["head"] == ETH_BASE + 3
    assert reply["pools"] == ["eth-sim"]


# ── naming a pool ─────────────────────────────────────────────────────────────
def test_a_read_that_names_only_a_pool_answers_for_the_chain_of_the_pool():
    api = _api()
    status, reply = api.advance({"pool": "btc-sim"})
    assert status == 200, reply
    assert (reply["chain"], reply["pool"]) == ("btc", "btc-sim")
    assert reply["heads"] == {"default": api.registry.pools["btc-sim"].chain_object.head.current()}
    assert reply["head"] != ETH_BASE  # eth, the chain that a request without a chain gets, did not answer
    # ln-sim follows btc-sim, so it answers the head that was read.
    assert reply["pools"] == ["btc-sim", "ln-sim"]


def test_a_move_that_names_only_a_pool_moves_the_chain_object_of_that_pool():
    api = _api()
    own = api.registry.pools["btc-sim"].chain_object
    own_before, registered_before = own.head.current(), CHAINS["btc"].head.current()
    other_before = api.registry.pools["btc-failover-sim"].chain_object.head.current()
    status, reply = api.advance({"pool": "btc-sim", "blocks": 4})
    assert status == 200, reply
    assert (reply["chain"], reply["pool"], reply["pools"]) == ("btc", "btc-sim", ["btc-sim", "ln-sim"])
    assert own.head.current() == own_before + 4
    assert CHAINS["btc"].head.current() == registered_before
    assert api.registry.pools["btc-failover-sim"].chain_object.head.current() == other_before


def test_a_pool_that_serves_another_chain_than_the_one_named_is_refused():
    status, reply = _api().advance({"chain": "eth", "pool": "btc-sim"})
    assert status == 400
    assert reply == {"error": "pool 'btc-sim' serves chain 'btc', not chain 'eth'"}


def test_a_move_through_a_pool_of_another_chain_is_refused_and_moves_no_head():
    api = _api()
    btc_before = CHAINS["btc"].head.current()
    status, reply = api.advance({"chain": "eth", "pool": "btc-sim", "blocks": 5})
    assert status == 400, reply
    assert CHAINS["eth"].head.current() == ETH_BASE
    assert CHAINS["btc"].head.current() == btc_before


def test_an_unknown_chain_is_refused_as_an_unknown_chain_when_the_pool_is_valid():
    status, reply = _api().advance({"chain": "no-such-chain", "pool": "eth-sim"})
    assert status == 400
    assert "there is no chain 'no-such-chain'" in reply["error"]


@pytest.mark.parametrize(
    "pool",
    [
        pytest.param("no-such-sim", id="a-name-that-no-pool-has"),
        pytest.param("", id="an-empty-name"),
    ],
)
def test_an_unknown_pool_is_refused_with_the_error_that_a_reset_gives_for_it(pool):
    api = _api()
    status, reply = api.advance({"chain": "eth", "pool": pool})
    reset_status, reset_reply = api.reset(pool)
    assert reset_status == 400
    assert (status, reply) == (400, reset_reply)
    assert f"no pool {pool!r}" in reply["error"]


@pytest.mark.parametrize(
    "pool",
    [
        pytest.param(5, id="a-number"),
        pytest.param(True, id="a-boolean"),
        pytest.param(["eth-sim"], id="a-list"),
        pytest.param({"name": "eth-sim"}, id="an-object"),
        # Only null means no pool. A value that Python reads as false is a wrong name, not a missing one.
        pytest.param(0, id="zero"),
        pytest.param(False, id="false"),
        pytest.param([], id="an-empty-list"),
        pytest.param({}, id="an-empty-object"),
    ],
)
def test_a_pool_that_is_not_a_string_is_refused(pool):
    status, reply = _api().advance({"chain": "eth", "pool": pool})
    assert status == 400
    assert reply == {"error": f"pool must be a string, got {type(pool).__name__}"}


def test_a_pool_set_to_null_is_the_same_as_no_pool():
    api = _api()
    status, reply = api.advance({"chain": "suit", "pool": None})
    assert status == 200, reply
    assert reply["pool"] is None
    assert (status, reply) == api.advance({"chain": "suit"})


# ── a chain with several chain objects ────────────────────────────────────────
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"chain": "eth"}, id="a-read-that-names-the-chain"),
        pytest.param({}, id="a-read-that-names-nothing-so-the-chain-is-eth"),
        pytest.param({"chain": "eth", "blocks": 0}, id="a-move-of-zero-blocks-which-is-a-read"),
        pytest.param({"chain": "eth", "blocks": 5}, id="a-move-of-blocks"),
        pytest.param({"chain": "eth", "per_second": 0}, id="a-change-of-the-rate"),
    ],
)
def test_a_request_without_a_pool_is_refused_when_the_chain_has_several_chain_objects(body):
    status, reply = _api().advance(body)
    assert status == 400, reply
    assert reply == {"error": _the_error_for_eth_with_one_chain_object_for_each_pool()}


def test_the_refusal_lists_every_group_when_a_chain_has_three_chain_objects():
    registry = Registry()
    first, second = EthChain(), EthChain()
    for name, held in [("a-sim", first), ("b-sim", first), ("c-sim", second), ("d-sim", EthChain())]:
        registry.pools[name] = Pool(name=name, chain="eth", chain_object=held)
    status, reply = ControlApi(registry, WsSubscriptions()).advance({"chain": "eth"})
    assert status == 400, reply
    assert reply == {
        "error": "chain 'eth' has 3 chain objects, so name the pool with \"pool\". "
        "Pools that share one object: [['a-sim', 'b-sim'], ['c-sim'], ['d-sim']]"
    }


def test_the_groups_and_the_pool_names_in_a_group_are_sorted_whatever_the_order_of_the_registry():
    """The registry holds its pools in the order c, b, d, a. The text must not follow that order."""
    registry = Registry()
    shared = EthChain()
    for name, held in [("c-sim", shared), ("b-sim", shared), ("d-sim", EthChain()), ("a-sim", EthChain())]:
        registry.pools[name] = Pool(name=name, chain="eth", chain_object=held)
    status, reply = ControlApi(registry, WsSubscriptions()).advance({"chain": "eth"})
    assert status == 400, reply
    assert reply == {
        "error": "chain 'eth' has 3 chain objects, so name the pool with \"pool\". "
        "Pools that share one object: [['a-sim'], ['b-sim', 'c-sim'], ['d-sim']]"
    }


def test_a_refused_move_moves_no_chain_object():
    api = _api()
    status, _ = api.advance({"chain": "eth", "blocks": 5})
    assert status == 400
    eth_pools = [pool for pool in api.registry.pools.values() if pool.chain == "eth"]
    assert len(eth_pools) > 1, "the chain has several pools, so the check must examine more than one"
    assert {pool.chain_object.head.current() for pool in eth_pools} == {ETH_BASE}
    assert CHAINS["eth"].head.current() == ETH_BASE


@pytest.mark.parametrize(
    "moved, untouched",
    [("eth-solo-sim", "eth-sim"), ("eth-sim", "eth-solo-sim")],
    ids=["a-move-through-eth-solo-sim", "a-move-through-eth-sim"],
)
def test_a_move_through_a_named_pool_moves_only_the_chain_object_that_pool_holds(moved, untouched):
    api = _api()
    status, reply = api.advance({"chain": "eth", "pool": moved, "blocks": 4})
    assert status == 200, reply
    pools = api.registry.pools
    assert pools[moved].chain_object.head.current() == ETH_BASE + 4
    assert pools[untouched].chain_object.head.current() == ETH_BASE
    assert CHAINS["eth"].head.current() == ETH_BASE
    assert (reply["pool"], reply["head"]) == (moved, ETH_BASE + 4)


def test_a_read_through_a_named_pool_returns_the_head_of_the_chain_object_that_pool_holds():
    api = _api()
    pools = api.registry.pools
    pools["eth-solo-sim"].chain_object.head.bump(7)
    pools["eth-sim"].chain_object.head.bump(2)
    assert api.advance({"chain": "eth", "pool": "eth-solo-sim"})[1]["head"] == ETH_BASE + 7
    assert api.advance({"chain": "eth", "pool": "eth-sim"})[1]["head"] == ETH_BASE + 2


def test_the_reply_names_the_pools_that_share_the_chain_object_it_used():
    shared = EthChain()
    api = _api_where_pools_hold({"eth-solo-sim": shared, "eth-duo-sim": shared})
    status, reply = api.advance({"chain": "eth", "pool": "eth-duo-sim"})
    assert (status, reply["pools"]) == (200, ["eth-duo-sim", "eth-solo-sim"]), reply
    status, reply = api.advance({"chain": "eth", "pool": "eth-sim"})
    assert (status, reply["pools"]) == (200, ["eth-sim"]), reply


def test_the_pools_of_another_chain_do_not_count_toward_the_chain_objects_of_a_chain():
    # eth has a chain object for each of its pools, and a chain that one pool serves needs no pool.
    status, reply = _api().advance({"chain": "suit"})
    assert status == 200, reply
    assert reply["pools"] == _pools_of("suit")


# ── which object serves a request that names no pool ──────────────────────────
def test_a_request_without_a_pool_uses_the_chain_object_that_every_pool_of_the_chain_holds():
    shared = EthChain()
    api = _api_where_pools_hold({pool: shared for pool in _pools_of("eth")})
    status, reply = api.advance({"chain": "eth", "blocks": 2})
    assert status == 200, reply
    assert shared.head.current() == ETH_BASE + 2
    assert CHAINS["eth"].head.current() == ETH_BASE  # the registered object serves no pool, so it stays
    assert reply["pools"] == _pools_of("eth")


def test_a_chain_that_no_pool_serves_is_moved_through_the_registered_chain_object():
    api = ControlApi(Registry(), WsSubscriptions())
    status, reply = api.advance({"chain": "eth", "blocks": 3})
    assert status == 200, reply
    assert reply["pools"] == []
    assert CHAINS["eth"].head.current() == ETH_BASE + 3


def test_a_reset_of_every_pool_rewinds_the_registered_object_that_a_chain_with_no_pool_moved():
    api = ControlApi(Registry(), WsSubscriptions())
    api.advance({"chain": "eth", "blocks": 3})
    assert CHAINS["eth"].head.current() == ETH_BASE + 3

    status, reply = api.reset()

    assert status == 200, reply
    assert CHAINS["eth"].head.current() == ETH_BASE


# ── the rule for head names does not change when a pool is named ──────────────
def test_a_move_on_a_chain_with_several_heads_still_needs_a_head_name_when_the_pool_is_named():
    """A pool name does not stand in for a head name: a move on a chain with several heads still names one."""
    api = _api_where_pools_hold({"lava-sim-grpc": LavaChain()})
    status, reply = api.advance({"chain": "lava", "pool": "lava-sim-grpc", "blocks": 3})
    assert status == 400
    assert "serves several heads, so name the one to move with 'head'" in reply["error"]


def test_a_chain_whose_several_heads_are_named_after_no_interface_answers_each_of_them(
    monkeypatch,
):
    """Only the heads of a lava chain are named after interfaces, and only they can be refused.

    A chain whose several heads are named after something else answers each of them, whatever
    interfaces the pool serves. This pool has no provider, so it serves no interface at all.
    """

    class TwoHeadedChain(EthChain):
        def __init__(self) -> None:
            super().__init__()
            self.heads = {"fast": AdvancingHead(100), "slow": AdvancingHead(50)}

    held = TwoHeadedChain()
    monkeypatch.setitem(CHAINS, "two-headed", held)
    registry = Registry()
    registry.pools["x-sim"] = Pool(name="x-sim", chain="two-headed", chain_object=held)

    status, reply = ControlApi(registry, WsSubscriptions()).advance(
        {"chain": "two-headed", "pool": "x-sim", "head": "fast", "blocks": 2}
    )

    assert status == 200, reply
    assert held.heads["fast"].current() == 102
    assert held.heads["slow"].current() == 50


def test_a_read_through_a_named_pool_of_a_chain_with_several_heads_needs_no_head_name():
    own = LavaChain()
    own.heads["rest"].bump(9)
    api = _api_where_pools_hold({"lava-sim-grpc": own})
    status, reply = api.advance({"chain": "lava", "pool": "lava-sim-grpc"})
    assert status == 200, reply
    assert reply["head_name"] is None
    assert reply["heads"] == _heads_of(own)
    assert reply["heads"]["rest"] != CHAINS["lava"].heads["rest"].current()


def test_a_named_head_through_a_named_pool_moves_only_that_head_of_the_chain_object_that_pool_holds():
    api = _api()
    own = api.registry.pools["lava-sim-rest"].chain_object
    other = api.registry.pools["lava-sim-grpc"].chain_object
    own_before, other_before, registered_before = _heads_of(own), _heads_of(other), _heads_of(CHAINS["lava"])
    status, reply = api.advance({"chain": "lava", "pool": "lava-sim-rest", "head": "rest", "blocks": 3})
    assert status == 200, reply
    assert _heads_of(own) == {**own_before, "rest": own_before["rest"] + 3}
    assert _heads_of(other) == other_before
    assert _heads_of(CHAINS["lava"]) == registered_before


# ── a head that the pool does not serve ───────────────────────────────────────
# A chain that serves one head for each interface is read over one interface by a pool that serves
# it. Only the head of an interface that the pool serves is a head that its providers answer.
@pytest.mark.parametrize(
    "pool, head",
    [("lava-sim-rest", "rest"), ("lava-sim-grpc", "grpc"), ("lava-sim-tm", "tendermintrpc")],
    ids=["rest", "grpc", "tendermintrpc"],
)
def test_a_head_of_an_interface_that_the_pool_serves_is_moved_and_read(pool, head):
    api = _api()
    before = api.advance({"chain": "lava", "pool": pool, "head": head})[1]["heads"][head]
    status, reply = api.advance({"chain": "lava", "pool": pool, "head": head, "blocks": 4})
    assert status == 200, reply
    assert reply["heads"][head] == before + 4


@pytest.mark.parametrize("blocks", [0, 3], ids=["a-read", "a-move"])
def test_a_head_of_an_interface_that_the_pool_does_not_serve_is_refused_and_nothing_moves(blocks):
    api = _api()
    own = api.registry.pools["lava-sim-grpc"].chain_object
    before = _heads_of(own)
    status, reply = api.advance({"chain": "lava", "pool": "lava-sim-grpc", "head": "rest", "blocks": blocks})
    assert status == 400, reply
    assert reply == {
        "error": "pool 'lava-sim-grpc' serves no 'rest' interface, so no provider of it answers the head "
        "'rest'. Heads it answers: ['grpc']"
    }
    assert _heads_of(own) == before


def test_a_change_of_the_rate_of_a_head_that_the_pool_does_not_serve_is_refused():
    api = _api()
    status, reply = api.advance({"chain": "lava", "pool": "lava-sim-rest", "head": "grpc", "per_second": 5})
    assert status == 400, reply
    assert "serves no 'grpc' interface" in reply["error"], reply


def test_a_read_through_a_pool_that_names_no_head_still_answers_every_head_of_the_chain_object():
    api = _api()
    status, reply = api.advance({"chain": "lava", "pool": "lava-sim-grpc"})
    assert status == 200, reply
    assert reply["head_name"] is None
    assert sorted(reply["heads"]) == ["grpc", "rest", "tendermintrpc"]


def test_a_pool_whose_providers_serve_different_interfaces_answers_the_head_of_each_one_of_them():
    """A pool serves an interface when any of its providers does, and not only the first provider."""
    rows = (
        ("l-sim", "lava", "1", "l-provider-1", False, "", (("rest", "http", 30_100),)),
        ("l-sim", "lava", "2", "l-provider-2", False, "", (("grpc", "http2", 30_101),)),
    )
    api = _api(build_registry(rows, follows={}))

    for head in ("rest", "grpc"):
        status, reply = api.advance({"chain": "lava", "pool": "l-sim", "head": head, "blocks": 1})
        assert status == 200, reply
    status, reply = api.advance({"chain": "lava", "pool": "l-sim", "head": "tendermintrpc", "blocks": 1})
    assert status == 400, reply
    assert "serves no 'tendermintrpc' interface" in reply["error"], reply


def test_a_head_that_is_not_the_name_of_an_interface_is_not_checked_against_the_interfaces_of_the_pool():
    status, reply = _api().advance({"chain": "eth", "pool": "eth-sim", "head": "default", "blocks": 1})
    assert status == 200, reply
