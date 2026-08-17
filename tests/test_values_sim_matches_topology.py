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

Router entries that dial real chain nodes (base, eth, solana, btc, …) have no
pool and are skipped. The skip is decided by "no pool of this name in
TOPOLOGY", never by a name list, so a new sim router is covered the moment its
pool row lands.

Two pools have no router entry here at all: ``eth-duo-sim`` lives in
smart_router_automation's k3d-only routers.yml, and ``ln-sim`` has no router
wired yet. This file reads the values entries, so those pools are simply never
visited — it does not claim to cover them.

Nothing in this file asserts anything about the FORMAT of a provider name.
The naming scheme is still an open decision.
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

POOLS = {pool for pool, _chain, _pid, _name, _backup, _eps in TOPOLOGY}

# port -> the provider key that owns it, so a mismatch can name the pool and
# the pool slot the port actually belongs to.
POOL_SLOT_OF_PORT = {
    port: f"{pool}:{pid}" for pool, _chain, pid, _name, _backup, eps in TOPOLOGY for (_i, _t, port) in eps
}

# pool -> how many providers the topology gives it. A plain dict, not a
# Counter: an unknown pool must raise, not answer zero and read as an entry
# that legitimately lists no providers.
PROVIDERS_PER_POOL = {pool: sum(1 for row_pool, _c, _p, _n, _b, _e in TOPOLOGY if row_pool == pool) for pool in POOLS}

# The router entries this file checks: every values entry whose id is a pool.
# Derived, never listed — a new sim router is covered as soon as its pool row
# exists, and no entry can be quietly excluded by editing a name list here.
SIM_ROUTERS = tuple(r for r in VALUES_ROUTERS if r.router_id in POOLS)
REAL_NODE_ROUTERS = tuple(r for r in VALUES_ROUTERS if r.router_id not in POOLS)


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
    written — which is how the drift this file guards against started."""
    for router in REAL_NODE_ROUTERS:
        assert router.router_id not in POOLS, (
            f"router {router.router_id!r} was skipped, but pool {router.router_id!r} " f"exists in the topology"
        )
    for router in SIM_ROUTERS:
        assert router.router_id in POOLS
