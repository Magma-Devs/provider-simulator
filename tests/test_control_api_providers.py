"""GET /providers — every fact about a provider, served instead of typed.

A test could ask the simulator which ports a provider listens on and nothing
else. Everything else it needed — which pool a router uses, what a provider is
called, which cross-validation group it belongs to — was written by hand in
the test code.

A typed table can be wrong and stay quiet. One said the eth-duo-sim router used
the eth-sim pool, so faults meant for one router landed on another's providers,
and on providers other tests were using at that moment. Nothing raised, because
eth-sim is a real pool name.

These tests pin the endpoint that removes the need to type any of it.
"""

from provider_simulator.control_api import ControlApi
from provider_simulator.domain.registry import build_registry
from provider_simulator.listeners.ws import WsSubscriptions


def _api():
    return ControlApi(build_registry(), WsSubscriptions())


def _providers(query=None):
    status, payload = _api().get_providers(query or {})
    assert status == 200, payload
    return payload["providers"]


# ── The whole set ─────────────────────────────────────────────────────────────
def test_every_provider_in_the_topology_is_served():
    """One entry per provider, keyed the way stats and scenario already key
    theirs: the pool name, a colon, then the pool slot."""
    providers = _providers()
    assert len(providers) == 51, sorted(providers)
    assert "eth-sim:1" in providers
    assert "lava-sim-tm:6" in providers


def test_an_entry_carries_every_fact_a_test_would_otherwise_type():
    """The example from the ticket, checked field by field. Literals on
    purpose: deriving them from the topology would compare the table with
    itself."""
    entry = _providers()["eth-sim:4"]
    assert entry["pool"] == "eth-sim"
    assert entry["pid"] == "4"
    assert entry["chain"] == "eth"
    assert entry["name"] == "EthBackupProvider4"
    assert entry["is_backup"] is True
    assert entry["group_label"] == ""
    ports = sorted(ep["port"] for ep in entry["endpoints"])
    assert ports == [18560, 18572]


def test_every_entry_has_the_same_keys():
    """A caller reads one entry and expects the next to have the same shape.
    A field that appears only sometimes is a field nobody can rely on."""
    expected = {"pool", "pid", "chain", "name", "is_backup", "group_label", "endpoints"}
    for key, entry in _providers().items():
        assert set(entry) == expected, f"{key} has {sorted(set(entry) ^ expected)}"


def test_no_two_providers_share_a_name_once_lowercased():
    """The chart lowercases every name before the router sees it, so two names
    differing only in case arrive identical and the router refuses to start."""
    seen: dict[str, str] = {}
    for key, entry in _providers().items():
        low = entry["name"].lower()
        assert low not in seen, f"{key} and {seen[low]} both lowercase to {low!r}"
        seen[low] = key


# ── The cross-validation group label ──────────────────────────────────────────
def test_the_three_labelled_providers_carry_their_group():
    """Cross-validation is a vote, and this label says which bloc a provider
    votes in. Agreement inside one bloc is not evidence, so the router only
    counts agreement that spans different blocs. A test checking that has to
    know which provider sits where. Read out of the deployed values file: two
    of eth-sim's providers share a bloc and the third is on its own."""
    providers = _providers()
    assert providers["eth-sim:1"]["group_label"] == "voting-group-1"
    assert providers["eth-sim:2"]["group_label"] == "voting-group-1"
    assert providers["eth-sim:3"]["group_label"] == "voting-group-2"


def test_a_provider_with_no_label_reports_an_empty_string():
    """Not null and not a missing key — a caller grouping by label can put
    every unlabelled provider in one bucket without a special case."""
    providers = _providers()
    unlabelled = [k for k, v in providers.items() if v["group_label"] == ""]
    assert len(unlabelled) == 42, f"expected 42 unlabelled, got {len(unlabelled)}"


# ── Filters ───────────────────────────────────────────────────────────────────
def test_filter_by_pool_returns_only_that_router_s_providers():
    providers = _providers({"pool": "btc-sim"})
    assert len(providers) == 3
    assert all(e["pool"] == "btc-sim" for e in providers.values())


def test_filter_by_pid_returns_that_slot_across_pools():
    providers = _providers({"pid": "1"})
    assert providers, "slot 1 exists in every pool"
    assert all(e["pid"] == "1" for e in providers.values())


def test_filter_by_is_backup_returns_the_backup_tier():
    providers = _providers({"is_backup": "true"})
    assert providers
    assert all(e["is_backup"] is True for e in providers.values())
    # eth-sim, lava-sim-rest, lava-sim-grpc and lava-sim-tm each have 3.
    assert len(providers) == 12, sorted(providers)


def test_filter_by_name_is_the_reverse_lookup():
    """The one a test actually needs: it reads a name out of a response header
    and has to learn which pool slot that was, so it can send that provider a
    fault. There was no way to do that before this endpoint."""
    providers = _providers({"name": "EthBackupProvider4"})
    assert list(providers) == ["eth-sim:4"]


def test_the_name_filter_ignores_case():
    """The chart lowercases the name before the router sees it, so what a test
    reads out of the header is lowercase and will never match the table's
    spelling otherwise."""
    assert list(_providers({"name": "ethbackupprovider4"})) == ["eth-sim:4"]


def test_filters_combine():
    providers = _providers({"pool": "eth-sim", "is_backup": "true"})
    assert sorted(providers) == ["eth-sim:4", "eth-sim:5", "eth-sim:6"]


def test_a_filter_narrows_the_set_but_never_changes_its_shape():
    """Same keys filtered as unfiltered. A reply that reshapes under a filter
    forces every caller to handle two shapes."""
    unfiltered = _providers()["eth-sim:4"]
    filtered = _providers({"pool": "eth-sim"})["eth-sim:4"]
    assert filtered == unfiltered


def test_a_filter_that_matches_nothing_returns_an_empty_set_not_an_error():
    """An empty result is a real answer to a real question. Only a question
    that cannot be answered is an error."""
    status, payload = _api().get_providers({"name": "NoSuchProvider99"})
    assert status == 200
    assert payload["providers"] == {}


def test_an_unknown_pool_is_an_error_that_names_the_pools_that_exist():
    """A pool name that does not exist is a typo, not an empty result. Answering
    with an empty set would let a test believe it had filtered correctly and
    found nothing, which is exactly the silence this endpoint exists to end."""
    status, payload = _api().get_providers({"pool": "eth-sim-typo"})
    assert status == 400
    assert "eth-sim-typo" in payload["error"]
    assert "eth-sim" in payload["error"]
