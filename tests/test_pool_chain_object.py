"""A pool holds its chain object.

``Pool.chain`` is the name of the chain. ``Pool.chain_object`` is the chain that the
providers of the pool answer from. Every reader that starts from a pool takes the chain
there: the listeners, the scope of a reset, and the check of ``frozen_head``. ``POST /advance``
takes the object of the pool that a request names, and it needs the name once a chain has
several chain objects.

Today every pool of one chain holds the same object, so a head moved on the chain is the
head that every pool of it answers. The tests below pin that sharing, and they pin that a
reader uses the object that its pool holds. A change to one object for each pool is then a
change of what the registry hands to a pool, and the readers need no change.
"""

import json

import pytest

from provider_simulator import topology
from provider_simulator.chains import CHAINS, Chain
from provider_simulator.chains.btc import BtcChain
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
def test_a_pool_built_without_a_chain_object_holds_the_registered_chain_of_its_name(chain):
    pool = Pool(name=f"{chain}-test-sim", chain=chain)
    assert pool.chain_object is CHAINS[chain]


def test_every_pool_of_the_topology_holds_the_registered_chain_of_its_chain():
    registry = build_registry()
    pools = list(registry.pools.values())
    # The expected count comes from the rows of the table, not from the registry under test.
    assert len(pools) == len({row[0] for row in topology.TOPOLOGY})
    for pool in pools:
        assert pool.chain_object is CHAINS[pool.chain], pool.name
    # The check is not vacuous: some chain is served by more than one pool, and those pools share the object.
    pools_of = {}
    for pool in pools:
        pools_of.setdefault(pool.chain, []).append(pool)
    assert max(len(of_chain) for of_chain in pools_of.values()) > 1
    for chain, of_chain in pools_of.items():
        assert len({id(pool.chain_object) for pool in of_chain}) == 1, chain


# ── the sharing that exists today ─────────────────────────────────────────────
def test_a_head_moved_on_the_chain_is_the_head_that_the_providers_of_every_pool_of_it_answer():
    registry = build_registry()
    api = _api(registry)
    first = registry.provider("eth-sim", "1")
    second = registry.provider("eth-solo-sim", "1")
    assert (_block_number(first), _block_number(second)) == (ETH_BASE, ETH_BASE)
    status, _ = api.advance({"chain": "eth", "blocks": 3})
    assert status == 200
    assert (_block_number(first), _block_number(second)) == (ETH_BASE + 3, ETH_BASE + 3)


def test_a_reset_of_one_pool_rewinds_the_head_that_the_providers_of_every_pool_of_the_chain_answer():
    """The sharing that the move to one chain object for each pool removes. That change turns this
    test into its opposite: the other pool keeps its head."""
    registry = build_registry()
    api = _api(registry)
    other = registry.provider("eth-solo-sim", "1")
    api.advance({"chain": "eth", "blocks": 5})
    assert _block_number(other) == ETH_BASE + 5
    status, _ = api.reset("eth-sim")
    assert status == 200
    assert _block_number(other) == ETH_BASE


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
    assert status == 200, reply  # the registered eth chain reads it
    registry.pools["eth-solo-sim"].chain_object = _EthChainThatDoesNotReadFrozenHead()
    status, reply = api.apply_scenario({"providers": {"eth-solo-sim:1": {"frozen_head": 19_999_000}}})
    assert status == 400
    assert "the eth chain does not read it" in reply["error"], reply


# ── POST /advance: which chain object a request moves, and when it needs a pool ────────────
# A chain whose pools all hold one chain object needs no pool in the request. That is every chain
# today. A chain with several chain objects has no single object to pick, so a request without a
# pool is refused and not guessed. The groups follow object identity: two objects of one class are
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


def _api_where_eth_solo_sim_has_its_own_chain_object():
    """Two objects of one class serve eth: the registered one, held by every other eth pool, and a new
    one held by eth-solo-sim alone. Returns the API and the new object."""
    own = EthChain()
    return _api_where_pools_hold({"eth-solo-sim": own}), own


def _the_error_for_eth_with_two_chain_objects():
    others = [pool for pool in _pools_of("eth") if pool != "eth-solo-sim"]
    return (
        "chain 'eth' has 2 chain objects, so name the pool with \"pool\". "
        f"Pools that share one object: {[others, ['eth-solo-sim']]!r}"
    )


# ── a chain whose pools hold one chain object ─────────────────────────────────
@pytest.mark.parametrize("chain", ["eth", "btc", "solana", "lava", "sui", "suit"])
def test_a_read_of_a_chain_with_one_chain_object_names_every_pool_of_the_chain(chain):
    status, reply = _api().advance({"chain": chain})
    assert status == 200, reply
    assert reply["pool"] is None
    assert reply["pools"] == _pools_of(chain)


def test_the_reply_to_a_read_of_one_chain_holds_exactly_these_fields():
    status, reply = _api().advance({"chain": "eth"})
    assert status == 200, reply
    assert reply == {
        "status": "ok",
        "chain": "eth",
        "pool": None,
        "pools": _pools_of("eth"),
        "head_name": "default",
        "heads": {"default": ETH_BASE},
        "head": ETH_BASE,
    }


def test_a_move_of_a_chain_with_one_chain_object_names_every_pool_of_the_chain():
    status, reply = _api().advance({"chain": "eth", "blocks": 3})
    assert status == 200, reply
    assert reply["head"] == ETH_BASE + 3
    assert reply["pools"] == _pools_of("eth")


# ── naming a pool ─────────────────────────────────────────────────────────────
def test_a_read_that_names_only_a_pool_answers_for_the_chain_of_the_pool():
    status, reply = _api().advance({"pool": "btc-sim"})
    assert status == 200, reply
    assert (reply["chain"], reply["pool"]) == ("btc", "btc-sim")
    assert reply["heads"] == {"default": CHAINS["btc"].head.current()}
    assert reply["head"] != ETH_BASE  # eth, the chain that a request without a chain gets, did not answer
    assert reply["pools"] == _pools_of("btc")


def test_a_move_that_names_only_a_pool_moves_the_chain_object_of_that_pool():
    own = BtcChain()
    api = _api_where_pools_hold({"btc-sim": own})
    own_before, registered_before = own.head.current(), CHAINS["btc"].head.current()
    status, reply = api.advance({"pool": "btc-sim", "blocks": 4})
    assert status == 200, reply
    assert (reply["chain"], reply["pool"], reply["pools"]) == ("btc", "btc-sim", ["btc-sim"])
    assert own.head.current() == own_before + 4
    assert CHAINS["btc"].head.current() == registered_before


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


def test_an_unknown_pool_is_refused_with_the_error_that_a_reset_gives_for_it():
    api = _api()
    status, reply = api.advance({"chain": "eth", "pool": "no-such-sim"})
    reset_status, reset_reply = api.reset("no-such-sim")
    assert reset_status == 400
    assert (status, reply) == (400, reset_reply)
    assert "no pool 'no-such-sim'" in reply["error"]


@pytest.mark.parametrize(
    "pool",
    [
        pytest.param(5, id="a-number"),
        pytest.param(True, id="a-boolean"),
        pytest.param(["eth-sim"], id="a-list"),
        pytest.param({"name": "eth-sim"}, id="an-object"),
    ],
)
def test_a_pool_that_is_not_a_string_is_refused(pool):
    status, reply = _api().advance({"chain": "eth", "pool": pool})
    assert status == 400
    assert reply == {"error": f"pool must be a string, got {type(pool).__name__}"}


def test_a_pool_set_to_null_is_the_same_as_no_pool():
    api = _api()
    status, reply = api.advance({"chain": "eth", "pool": None})
    assert status == 200, reply
    assert reply["pool"] is None
    assert (status, reply) == api.advance({"chain": "eth"})


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
    api, _ = _api_where_eth_solo_sim_has_its_own_chain_object()
    status, reply = api.advance(body)
    assert status == 400, reply
    assert reply == {"error": _the_error_for_eth_with_two_chain_objects()}


def test_the_refusal_lists_every_group_when_a_chain_has_three_chain_objects():
    api = _api_where_pools_hold({"eth-solo-sim": EthChain(), "eth-duo-sim": EthChain()})
    status, reply = api.advance({"chain": "eth"})
    assert status == 400, reply
    others = [pool for pool in _pools_of("eth") if pool not in ("eth-duo-sim", "eth-solo-sim")]
    groups = [others, ["eth-duo-sim"], ["eth-solo-sim"]]
    assert reply == {
        "error": "chain 'eth' has 3 chain objects, so name the pool with \"pool\". "
        f"Pools that share one object: {groups!r}"
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
    api, own = _api_where_eth_solo_sim_has_its_own_chain_object()
    status, _ = api.advance({"chain": "eth", "blocks": 5})
    assert status == 400
    assert CHAINS["eth"].head.current() == ETH_BASE
    assert own.head.current() == ETH_BASE


@pytest.mark.parametrize(
    "pool, moved_is_own",
    [("eth-solo-sim", True), ("eth-sim", False)],
    ids=["a-pool-with-a-chain-object-of-its-own", "a-pool-that-holds-the-registered-chain-object"],
)
def test_a_move_through_a_named_pool_moves_only_the_chain_object_that_pool_holds(pool, moved_is_own):
    api, own = _api_where_eth_solo_sim_has_its_own_chain_object()
    status, reply = api.advance({"chain": "eth", "pool": pool, "blocks": 4})
    assert status == 200, reply
    moved, untouched = (own, CHAINS["eth"]) if moved_is_own else (CHAINS["eth"], own)
    assert moved.head.current() == ETH_BASE + 4
    assert untouched.head.current() == ETH_BASE
    assert (reply["pool"], reply["head"]) == (pool, ETH_BASE + 4)


def test_a_read_through_a_named_pool_returns_the_head_of_the_chain_object_that_pool_holds():
    api, own = _api_where_eth_solo_sim_has_its_own_chain_object()
    own.head.bump(7)
    CHAINS["eth"].head.bump(2)
    assert api.advance({"chain": "eth", "pool": "eth-solo-sim"})[1]["head"] == ETH_BASE + 7
    assert api.advance({"chain": "eth", "pool": "eth-sim"})[1]["head"] == ETH_BASE + 2


def test_the_reply_names_the_pools_that_share_the_chain_object_it_used():
    shared = EthChain()
    api = _api_where_pools_hold({"eth-solo-sim": shared, "eth-duo-sim": shared})
    status, reply = api.advance({"chain": "eth", "pool": "eth-duo-sim"})
    assert (status, reply["pools"]) == (200, ["eth-duo-sim", "eth-solo-sim"]), reply
    others = [pool for pool in _pools_of("eth") if pool not in ("eth-duo-sim", "eth-solo-sim")]
    status, reply = api.advance({"chain": "eth", "pool": "eth-sim"})
    assert (status, reply["pools"]) == (200, others), reply


def test_the_pools_of_another_chain_do_not_count_toward_the_chain_objects_of_a_chain():
    api, _ = _api_where_eth_solo_sim_has_its_own_chain_object()
    status, reply = api.advance({"chain": "btc"})
    assert status == 200, reply
    assert reply["pools"] == _pools_of("btc")


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


# ── the rule for head names does not change when a pool is named ──────────────
def test_a_move_on_a_chain_with_several_heads_still_needs_a_head_name_when_the_pool_is_named():
    """A pool name does not stand in for a head name: a move on a chain with several heads still names one."""
    api = _api_where_pools_hold({"lava-sim-grpc": LavaChain()})
    status, reply = api.advance({"chain": "lava", "pool": "lava-sim-grpc", "blocks": 3})
    assert status == 400
    assert "serves several heads, so name the one to move with 'head'" in reply["error"]


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
    own = LavaChain()
    api = _api_where_pools_hold({"lava-sim-grpc": own})
    own_before, registered_before = _heads_of(own), _heads_of(CHAINS["lava"])
    status, reply = api.advance({"chain": "lava", "pool": "lava-sim-grpc", "head": "rest", "blocks": 3})
    assert status == 200, reply
    assert _heads_of(own) == {**own_before, "rest": own_before["rest"] + 3}
    assert _heads_of(CHAINS["lava"]) == registered_before
