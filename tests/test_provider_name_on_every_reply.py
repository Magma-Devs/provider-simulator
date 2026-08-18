"""Every reply that describes a provider says what it is called.

Three read endpoints describe providers and none of them said which provider,
in the words the router uses. All three identify a provider as pool plus slot
— eth-sim:4 — and a person reading any of those replies could not tell that
eth-sim:4 is the one the router calls EthBackupProvider4. Only the router's
response header said that, and only while the cluster was running.

The history entries had the same gap, and worse: a history record is saved
into a test report and read days later with nothing running. A record that
cannot be read without the system it came from is a poor record.
"""

from provider_simulator.control_api import ControlApi
from provider_simulator.domain.registry import build_registry
from provider_simulator.listeners.ws import WsSubscriptions
from provider_simulator.topology import TOPOLOGY

NAME_OF = {f"{pool}:{pid}": name for pool, _c, pid, name, _b, _g, _e in TOPOLOGY}


def _api():
    return ControlApi(build_registry(), WsSubscriptions())


def test_topology_reports_a_name_for_every_provider():
    """Beside the providers key, never inside it. Two readers in the
    automation project read the per-slot value as a list of endpoints and one
    raises on purpose when it is not a list — nesting the name would break
    both."""
    _, body = _api().get_topology()
    for pool, details in body["topology"].items():
        assert "names" in details, f"pool {pool} reports no names"
        for pid, name in details["names"].items():
            assert name == NAME_OF[f"{pool}:{pid}"]


def test_the_topology_provider_entries_are_still_a_plain_list_of_endpoints():
    """The proof the shape did not move. This is what the two readers in the
    automation project rely on, and one of them raises rather than guessing if
    it ever stops being a list."""
    _, body = _api().get_topology()
    for pool, details in body["topology"].items():
        for pid, endpoints in details["providers"].items():
            assert isinstance(endpoints, list), f"{pool}:{pid} is no longer a list"
            for endpoint in endpoints:
                assert set(endpoint) == {"interface", "transport", "port"}


def test_stats_reports_a_name_inside_each_entry():
    _, body = _api().get_stats()
    assert body["providers"], "no providers in stats"
    for key, entry in body["providers"].items():
        assert entry["name"] == NAME_OF[key], key


def test_scenario_reports_a_name_inside_each_entry():
    _, body = _api().get_scenario()
    assert body["providers"], "no providers in scenario"
    for key, entry in body["providers"].items():
        assert entry["name"] == NAME_OF[key], key


def test_a_provider_absent_from_the_topology_appears_in_no_reply():
    """The three replies are built from the registry, which is built from the
    topology. Anything else appearing in one of them came from somewhere that
    is not the single source."""
    api = _api()
    known = set(NAME_OF)
    for label, (_, body) in {
        "stats": api.get_stats(),
        "scenario": api.get_scenario(),
        "providers": api.get_providers({}),
    }.items():
        extra = sorted(set(body["providers"]) - known)
        assert not extra, f"{label} reports providers the topology does not have: {extra}"
    _, topo = api.get_topology()
    from_topology = {f"{pool}:{pid}" for pool, d in topo["topology"].items() for pid in d["providers"]}
    assert not sorted(from_topology - known)


def test_every_history_entry_carries_the_provider_name():
    """A history record is read out of a saved test report, with nothing
    running. Without the name the reader has to ask a live simulator what
    eth-sim:4 was called."""
    registry = build_registry()
    provider = registry.provider("eth-sim", "4")
    entry = provider.log.record_arrival(interface="jsonrpc", transport="http", port=18560)
    provider.log.finalize(entry, method="eth_blockNumber", status="success", latency_ms=1, request_id=1)
    entry = provider.log.get_history()[-1]
    assert entry["name"] == "EthBackupProvider4"
    assert entry["pool"] == "eth-sim"
    assert entry["pid"] == "4"
