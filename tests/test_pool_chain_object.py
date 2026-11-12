"""A pool holds its chain object.

``Pool.chain`` is the name of the chain. ``Pool.chain_object`` is the chain that the
providers of the pool answer from. Every reader that starts from a pool takes the chain
there: the listeners, the scope of a reset, and the check of ``frozen_head``.

Today every pool of one chain holds the same object, so a head moved on the chain is the
head that every pool of it answers. The tests below pin that sharing, and they pin that a
reader uses the object that its pool holds. A change to one object for each pool is then a
change of what the registry hands to a pool, and the readers need no change.
"""

import json

import pytest

from provider_simulator import topology
from provider_simulator.chains import CHAINS
from provider_simulator.chains.eth import EthChain
from provider_simulator.control_api import ControlApi
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.domain.registry import build_registry
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
