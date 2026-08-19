"""The deployed router values file must agree with the topology.

Why this file exists
--------------------
The same provider data is written by hand in three separate repos. The
topology in ``provider_simulator/topology.py`` says which pool owns which
port. ``config/values_sim.yml`` is the smart-router Helm values file, and it
is what actually gets deployed: each router entry lists the urls its router
dials. Nothing compared the two, so they drifted for three months and
produced a real wiring defect — the ``btc-sim`` router entry pointed at
Ethereum's ports (18545-18547), and every BTC request landed on an ETH
listener. The topology was right; the hand-typed values file was wrong.

What it checks
--------------
For every router entry in the values file whose ``id`` equals a pool name:

1. Every url's port is a port the topology declares.
2. Every url's port belongs to *that* pool — the assertion the BTC defect
   would have failed.
3. The entry lists exactly as many providers as the topology gives the pool —
   an equality, so an extra hand-typed provider fails as loudly as a missing
   one.
4. All of one provider's urls resolve to the SAME pool slot — a provider whose
   http and ws urls point at two different slots passes 1 to 3 unnoticed.

Router entries that dial real chain nodes (base, eth, solana, btc, …) have no
pool and are skipped. The skip is decided by "no pool of this name in
TOPOLOGY", never by a name list, so a new sim router is covered the moment its
pool row lands.

That rule cannot check itself. An entry whose id is misspelt is not a pool, so
it is skipped as a real-node router and never looked at again — which is how a
typo would get past every check here. So a second, independent classification
is used for the two checks that ask which entries are sim routers at all: an
entry that dials the provider simulator's own host IS one, whatever its id
says. The two classifications are compared against each other, and they are
typed in different files by different hands.

Two pools have no router entry here at all: ``eth-duo-sim`` lives in
smart_router_automation's k3d-only routers.yml, and ``ln-sim`` has no router
wired yet. This file reads the values entries, so those pools are simply never
visited — it does not claim to cover them.

It also checks the NAMES. That scheme is decided: pool, then role, then the
word Provider, then the pool slot.
"""

import re
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

import pytest

from provider_simulator.topology import TOPOLOGY

VALUES_SIM = Path(__file__).resolve().parents[1] / "config" / "values_sim.yml"

# The three lines this file reads out of the values file, pinned to the
# indentation the file is written at: a router entry under ``routers:``, a
# provider under that router's ``nodes:``, and a url under that provider's
# ``endpoints:``. The repo has no YAML dependency (requirements.txt carries
# only the gRPC runtime), so rather than add one for three fields, the reader
# below matches these shapes and ``test_the_values_file_is_read_whole`` proves
# no line was dropped.
_ROUTER_LINE = re.compile(r'^  - id:\s*"?(?P<value>[^"\s]+)"?\s*$')
_PROVIDER_LINE = re.compile(r'^      - name:\s*"?(?P<value>[^"\s]+)"?\s*$')
_URL_LINE = re.compile(r'^          - url:\s*"?(?P<value>[^"\s]+)"?\s*$')

# The same three fields found at ANY indentation. Counting these and comparing
# against what the reader attributed is what stops a reformatted values file
# from turning this suite green by parsing nothing.
_ANY_ROUTER_LINE = re.compile(r"^\s*- id:", re.M)
_ANY_PROVIDER_LINE = re.compile(r"^\s*- name:", re.M)
_ANY_URL_LINE = re.compile(r"^\s*- url:", re.M)


class ValuesProvider(NamedTuple):
    """One ``nodes:`` entry — the router's own name for a provider, and the
    urls it dials it on. The simulator never sees this name; it is the string
    the router reports in ``Lava-Provider-Address``."""

    name: str
    urls: tuple[str, ...]


class ValuesRouter(NamedTuple):
    """One ``routers:`` entry. ``router_id`` is compared against pool names —
    a sim router's id equals its pool."""

    router_id: str
    providers: tuple[ValuesProvider, ...]


def _read_values_routers() -> tuple[ValuesRouter, ...]:
    """Read the router entries out of the deployed values file.

    Each url is attributed to the provider above it and the router above that,
    so a url can never be counted against the wrong pool.

    A provider line with no router above it, or a url line with no provider, is
    passed over rather than raised on. Raising here would take the whole module
    down at import with a shape error; passing over leaves the line uncounted,
    and ``test_the_values_file_is_read_whole`` then fails naming what was
    dropped.
    """
    parsed: list[tuple[str, list[tuple[str, list[str]]]]] = []

    for line in VALUES_SIM.read_text().splitlines():
        router_match = _ROUTER_LINE.match(line)
        if router_match:
            parsed.append((router_match.group("value"), []))
            continue
        provider_match = _PROVIDER_LINE.match(line)
        if provider_match:
            if parsed:
                parsed[-1][1].append((provider_match.group("value"), []))
            continue
        url_match = _URL_LINE.match(line)
        if url_match:
            if parsed and parsed[-1][1]:
                parsed[-1][1][-1][1].append(url_match.group("value"))
            continue

    return tuple(
        ValuesRouter(router_id, tuple(ValuesProvider(name, tuple(urls)) for name, urls in providers))
        for router_id, providers in parsed
    )


def _port_of_url(url: str) -> int | None:
    """The TCP port a url dials, or None when it names none. The values file
    carries http, ws and grpc schemes; urlparse reads the port out of all
    three. Placeholder urls in the real-node entries name no port, and those
    entries are skipped before this is ever called on them."""
    try:
        return urlparse(url).port
    except ValueError:
        return None


VALUES_ROUTERS = _read_values_routers()

POOLS = {pool for pool, _chain, _pid, _name, _backup, _group, _eps in TOPOLOGY}

# port -> the provider key that owns it, so a mismatch can name the pool and
# the pool slot the port actually belongs to.
POOL_SLOT_OF_PORT = {
    port: f"{pool}:{pid}" for pool, _chain, pid, _name, _backup, _group, eps in TOPOLOGY for (_i, _t, port) in eps
}

# pool -> how many providers the topology gives it. A plain dict, not a
# Counter: an unknown pool must raise, not answer zero and read as an entry
# that legitimately lists no providers.
PROVIDERS_PER_POOL = {
    pool: sum(1 for row_pool, _c, _p, _n, _b, _group, _e in TOPOLOGY if row_pool == pool) for pool in POOLS
}

# The router entries this file checks: every values entry whose id is a pool.
# Derived, never listed — a new sim router is covered as soon as its pool row
# exists, and no entry can be quietly excluded by editing a name list here.
SIM_ROUTERS = tuple(r for r in VALUES_ROUTERS if r.router_id in POOLS)
REAL_NODE_ROUTERS = tuple(r for r in VALUES_ROUTERS if r.router_id not in POOLS)

# The host every simulated provider is dialled on. One Kubernetes service
# fronts all of them, so a url pointing here is a url into the simulator, and a
# url pointing anywhere else is a real chain node.
SIM_PROVIDER_HOST = "provider-simulator.lava-infra.svc.cluster.local"


def _dials_the_simulator(router: ValuesRouter) -> bool:
    """Whether a router entry is a sim router, decided WITHOUT asking whether
    its id is a pool.

    A second way of telling the two kinds apart is needed, and it has to be
    independent of the first. "Is its id in POOLS?" cannot answer a question
    about ids that are not pools: filtering on pool membership and then
    asserting pool membership restates the filter. A misspelt ``eth-sym``, or a
    new ``ghost-sim``, would simply be classified as a real-node router and
    skipped.

    The host is that independent signal. Every simulated provider in this file
    is dialled at one Kubernetes service name and no real-node entry uses it,
    so "dials that host" identifies a sim router from the urls alone, without
    consulting the topology.

    Its one blind spot is an entry that gets the host wrong as well as the id.
    That entry resolves to nothing the moment it deploys and fails on its own —
    unlike a wrong id, which deploys cleanly and quietly dials nothing.
    """
    return any(urlparse(url).hostname == SIM_PROVIDER_HOST for provider in router.providers for url in provider.urls)


# Chosen by the host each entry dials, so an id that is not a pool still lands
# in this set and can be compared against POOLS.
SIM_LIKE_ROUTERS = tuple(r for r in VALUES_ROUTERS if _dials_the_simulator(r))


# ── the reader is honest before anything is asserted on what it read ─────────


def test_the_values_file_is_read_whole():
    """Every id, name and url line in the file was attributed to something.

    Without this, a values file written at a different indentation would parse
    to nothing and every check below would pass on an empty set — the exact
    silent-green failure this suite exists to prevent."""
    text = VALUES_SIM.read_text()
    read_providers = [p for r in VALUES_ROUTERS for p in r.providers]
    read_urls = [u for p in read_providers for u in p.urls]

    assert len(VALUES_ROUTERS) == len(_ANY_ROUTER_LINE.findall(text)), "a router entry was dropped"
    assert len(read_providers) == len(_ANY_PROVIDER_LINE.findall(text)), "a provider was dropped"
    assert len(read_urls) == len(_ANY_URL_LINE.findall(text)), "a url was dropped"
    assert all(p.urls for p in read_providers), "a provider was read with no url at all"


def test_both_kinds_of_router_entry_are_present():
    """The split must be a real split. If every entry fell into the skipped
    side the three checks below would run on nothing and still report green;
    if none did, the skip rule stopped working."""
    assert SIM_ROUTERS, f"no values entry matched a pool; pools are {sorted(POOLS)}"
    assert REAL_NODE_ROUTERS, "no values entry was skipped; the real-node routers went missing"
    assert len(SIM_ROUTERS) + len(REAL_NODE_ROUTERS) == len(VALUES_ROUTERS)


# ── the deployed urls against the table ──────────────────────────────────────


@pytest.mark.parametrize("router", SIM_ROUTERS, ids=lambda r: r.router_id)
def test_every_sim_router_url_dials_a_port_the_topology_declares(router):
    """A port no pool listens on is a request into a closed door. The router
    would deploy, verify nothing, and every relay through that provider would
    fail as an upstream outage rather than as the typo it is."""
    for provider in router.providers:
        for url in provider.urls:
            port = _port_of_url(url)
            assert port is not None, f"router {router.router_id!r} url {url!r} names no port"
            assert port in POOL_SLOT_OF_PORT, (
                f"router {router.router_id!r} dials port {port} ({url!r}), " f"which no pool in the topology listens on"
            )


@pytest.mark.parametrize("router", SIM_ROUTERS, ids=lambda r: r.router_id)
def test_every_sim_router_url_dials_a_port_inside_its_own_pool(router):
    """The BTC defect, in one assertion. btc-sim's entry pointed at
    18545-18547 — real, bound, answering ports, owned by eth-sim. Every check
    that only asked "does this port exist?" passed, and BTC requests were
    served by the Ethereum handler for three months. A pool's router must dial
    that pool and no other."""
    for provider in router.providers:
        for url in provider.urls:
            port = _port_of_url(url)
            assert port is not None, f"router {router.router_id!r} url {url!r} names no port"
            owner = POOL_SLOT_OF_PORT.get(port)
            assert owner is not None, (
                f"router {router.router_id!r} dials port {port} ({url!r}), " f"which belongs to no pool in the topology"
            )
            assert owner.split(":")[0] == router.router_id, (
                f"router {router.router_id!r} dials port {port} ({url!r}), "
                f"which belongs to {owner}, not to pool {router.router_id!r}"
            )


@pytest.mark.parametrize("router", SIM_ROUTERS, ids=lambda r: r.router_id)
def test_every_sim_router_lists_exactly_its_pools_providers(router):
    """An equality, both directions. One provider short and the router is
    quietly running a smaller pool than every test assumes; one provider extra
    and it dials a listener the simulator never binds. Neither shows up as a
    deploy error."""
    assert len(router.providers) == PROVIDERS_PER_POOL[router.router_id], (
        f"router {router.router_id!r} lists {len(router.providers)} providers, "
        f"but pool {router.router_id!r} has {PROVIDERS_PER_POOL[router.router_id]} "
        f"in the topology"
    )


def test_a_router_entry_is_skipped_only_when_no_pool_carries_its_name():
    """The skip must stay a lookup. A hardcoded list of real-node router ids
    would silently stop covering any sim router added after the list was
    written — which is how the drift this file guards against started.

    Checked against the host each entry dials, because checking it against
    SIM_ROUTERS and REAL_NODE_ROUTERS proves nothing: those two are BUILT by
    the pool lookup, so asserting the lookup on them only restates how they
    were split. The host classification is typed in the values file and the
    pool classification in the topology, by different hands, so comparing them
    is a real comparison.

    The direction where an entry dials the simulator under an id that is not a
    pool has its own test below. This is the other one: an entry named after a
    pool that does not dial the simulator at all — an ``eth-sim`` entry
    repointed at real chain nodes, which would keep every check in this file
    happy while the sim suite ran against production.
    """
    named_after_a_pool = {r.router_id for r in SIM_ROUTERS}
    dials_the_simulator = {r.router_id for r in SIM_LIKE_ROUTERS}
    stragglers = sorted(named_after_a_pool - dials_the_simulator)
    assert not stragglers, (
        f"these entries are named after a pool but dial no provider-simulator url: "
        f"{stragglers}. Either the urls were repointed away from the simulator, or the "
        f"id borrows a pool name it should not."
    )


# ── the deployed NAMES against the table ─────────────────────────────────────
#
# The naming scheme was undecided when this file was written, so it checked
# ports and said nothing about names. It is decided now: pool, then role, then
# the word Provider, then the pool slot. The same 37 names are typed in three
# values files across three repositories, and nothing compared any of them to
# the topology.
#
# A wrong name does not raise. The router reports whatever is typed here, and
# every test that correlates on that name then matches nothing — it compares
# two empty sets and passes.

# pool:pid -> the name the topology holds for it.
NAME_OF_POOL_SLOT = {f"{pool}:{pid}": name for pool, _chain, pid, name, _backup, _group, _eps in TOPOLOGY}

# Pools with no entry in THIS values file, each with the reason. Written down
# rather than derived, so the coverage check below stays an equality with two
# named holes instead of quietly becoming "whatever both happen to contain".
POOLS_WITH_NO_ROUTER_HERE = {
    # Declared in smart_router_automation's k3d-only tools/local-cluster/routers.yml.
    "eth-duo-sim",
    # No router dials it yet; the ports are allocated so the pattern stays symmetric.
    "ln-sim",
    # Three selection-policy pools, each meant for a router that boots with a
    # different upstream-selection setting. The listeners are declared here so
    # the ports are reserved and cannot be handed to something else. No router
    # entry exists in this values file for them, and none was found checked in
    # anywhere else at the time of writing, although three matching routers do
    # run on the local k3d cluster. Wire them properly, or drop these rows.
    "eth-best-sim",
    "eth-priority-sim",
    "eth-precedence-sim",
}


def _slots_of_provider(provider: ValuesProvider) -> set[str]:
    """EVERY pool slot this values provider dials, read from its ports.

    A provider's urls are meant to be one node reached on several transports —
    the six eth-sim providers each carry an http url and a ws url — so this set
    should never hold more than one slot.
    """
    slots = set()
    for url in provider.urls:
        port = _port_of_url(url)
        if port in POOL_SLOT_OF_PORT:
            slots.add(POOL_SLOT_OF_PORT[port])
    return slots


def _slot_of_provider(provider: ValuesProvider) -> str | None:
    """The one pool slot a values provider is, or None when it dials no port
    the topology declares.

    Matched by port rather than by position in the list. Position would agree
    with the topology right up until someone reorders one file, and then it
    would compare the wrong two names and still pass most of the time.

    Every url is resolved, not just the first one that lands. Taking the first
    would leave a provider whose second url was retyped onto another slot's
    port completely unchecked: both ports still belong to the pool, so the
    pool-level checks pass, the provider count is still right, and the name
    would be compared against whichever slot happened to be read first. This
    refuses instead, naming the provider and the slots it straddles.
    """
    slots = _slots_of_provider(provider)
    assert len(slots) <= 1, (
        f"provider {provider.name!r} dials more than one pool slot — {sorted(slots)}. "
        f"Its urls are meant to be one node on several transports, so its name cannot "
        f"be checked against any single slot."
    )
    return next(iter(slots)) if slots else None


@pytest.mark.parametrize("router", SIM_ROUTERS, ids=lambda r: r.router_id)
def test_every_provider_name_matches_the_one_the_topology_holds(router):
    """The check that stops a typo deploying.

    The values file is typed by a person and the router reports exactly what it
    says. A name that is wrong by one letter still deploys, still serves
    traffic, and every test correlating on that name silently matches nothing.
    """
    checked = 0
    for provider in router.providers:
        slot = _slot_of_provider(provider)
        assert slot is not None, (
            f"router {router.router_id!r} provider {provider.name!r} dials no port "
            f"the topology declares, so its name cannot be checked against anything"
        )
        expected = NAME_OF_POOL_SLOT[slot]
        assert provider.name == expected, (
            f"{slot} is named {provider.name!r} in the values file and "
            f"{expected!r} in the topology. The router reports the values-file "
            f"name, so every test correlating on {expected!r} would match nothing."
        )
        checked += 1
    assert checked == len(router.providers), "a provider was skipped without failing"


def test_every_provider_in_this_file_was_actually_name_checked():
    """A guard on the check above, not a second check.

    Its assertions live inside a loop over one router's providers. If the port
    join stopped resolving, every loop body would be skipped and the whole
    parametrised set would pass having compared nothing.
    """
    resolved = [p for r in SIM_ROUTERS for p in r.providers if _slot_of_provider(p) is not None]
    total = [p for r in SIM_ROUTERS for p in r.providers]
    assert resolved, "no provider resolved to a pool slot; the name check compared nothing"
    assert len(resolved) == len(
        total
    ), f"{len(total) - len(resolved)} of {len(total)} providers did not resolve to a slot"


@pytest.mark.parametrize("router", SIM_ROUTERS, ids=lambda r: r.router_id)
def test_every_provider_dials_exactly_one_pool_slot(router):
    """One values provider is one node.

    Its urls are that node reached on different transports — an http url and a
    ws url for each of the six eth-sim providers — so every url must resolve to
    the same pool slot.

    A provider straddling two slots of the SAME pool satisfies every other
    check in this file. Both ports exist, both belong to the pool, and the
    provider count is unchanged, so the port checks and the count check all
    pass. Only this comparison sees it, and what it costs is real: the two
    transports of one provider answer as two different simulated nodes, so a
    fault set on that provider lands on one of them and the test watching the
    other sees nothing wrong.
    """
    for provider in router.providers:
        slots = _slots_of_provider(provider)
        assert len(slots) <= 1, (
            f"router {router.router_id!r} provider {provider.name!r} dials {len(slots)} "
            f"different pool slots — {sorted(slots)} — across its urls {list(provider.urls)}. "
            f"All of a provider's urls must be the same node."
        )


# ── every pool accounted for, in both directions ─────────────────────────────


def test_every_pool_the_values_file_deploys_exists_in_the_topology():
    """A values entry naming a pool the simulator does not have would deploy a
    router pointing at nothing.

    Read off SIM_LIKE_ROUTERS, not SIM_ROUTERS. SIM_ROUTERS is built by keeping
    the entries whose id is a pool, so subtracting POOLS from it is empty every
    time and this could never fail — a new ``ghost-sim`` entry would be sorted
    into the real-node routers and skipped, and the file would stay green.
    SIM_LIKE_ROUTERS is chosen by the host the entry dials, so ``ghost-sim``
    still lands in the set and its id is actually compared.
    """
    deployed = {r.router_id for r in SIM_LIKE_ROUTERS}
    missing = sorted(deployed - POOLS)
    assert not missing, (
        f"these router entries dial the provider simulator but their ids are not pools "
        f"in the topology: {missing}. Either the id is a typo, or the pool row was never "
        f"added to provider_simulator/topology.py."
    )


def test_every_pool_in_the_topology_is_deployed_or_named_as_an_exception():
    """The other direction, which is the one that rots.

    A pool added to the topology and forgotten in the values file is a pool no
    router dials — it listens and nothing arrives. Two pools legitimately have
    no entry here and are listed with their reasons; anything else is an
    oversight, and this fails naming it.
    """
    deployed = {r.router_id for r in SIM_ROUTERS}
    unaccounted = sorted(POOLS - deployed - POOLS_WITH_NO_ROUTER_HERE)
    assert not unaccounted, (
        f"these pools exist in the topology but no router here dials them: {unaccounted}. "
        f"Either add the router entry, or add the pool to POOLS_WITH_NO_ROUTER_HERE "
        f"with the reason no router needs it."
    )


def test_the_named_exceptions_are_real_pools_and_really_absent():
    """An exception list that names a pool which does not exist, or one that IS
    deployed, is a hole someone can widen without noticing."""
    deployed = {r.router_id for r in SIM_ROUTERS}
    for pool in POOLS_WITH_NO_ROUTER_HERE:
        assert pool in POOLS, f"{pool!r} is excused but is not a pool at all"
        assert pool not in deployed, f"{pool!r} is excused but this values file does deploy it"


# ── one name, one provider, across the whole deployment ──────────────────────


def test_no_two_providers_share_a_name_once_lowercased():
    """Across every pool, not only inside one.

    The chart lowercases a name before the router sees it, so two names
    differing only in case arrive identical. The router refuses to start when
    the clash is inside one chain and one api-interface; a clash across two
    pools starts cleanly and confuses every person who reads a response header
    afterwards, and every test that looks a name up.
    """
    seen: dict[str, str] = {}
    for slot, name in NAME_OF_POOL_SLOT.items():
        low = name.lower()
        clash = seen.get(low)
        assert clash is None, f"{slot} and {clash} are both named {low!r} once lowercased"
        seen[low] = slot
    assert len(seen) == len(NAME_OF_POOL_SLOT)
