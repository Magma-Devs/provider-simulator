"""The four things a test can do to the router's store, through the control API.

Read, cut off, restore, flush. There is deliberately no write.

The distinction this file guards hardest: **an unreachable store answers 503,
never 200 with an empty list.** A caller has to be able to tell "the router
stored nothing" from "my own setup is broken", and those two produce the same
bytes unless something insists otherwise.
"""

from __future__ import annotations

import pytest

from provider_simulator.resp_control import RespControlApi
from provider_simulator.resp_proxy import ERROR, FORWARDING, TIMEOUT
from tests.resp_fake_store import FakeRespStore


@pytest.fixture
def store():
    with FakeRespStore() as fake:
        yield fake


@pytest.fixture
def control(store):
    api = RespControlApi()
    api.register("primary", "127.0.0.1", store.port)
    return api


# ── read ──────────────────────────────────────────────────────────────────────


def test_an_empty_store_answers_200_with_no_entries(control):
    status, payload = control.get_entries("primary", {})
    assert status == 200
    assert payload["count"] == 0
    assert payload["entries"] == []


def test_a_store_holding_one_key_answers_200_with_one_entry(store, control):
    """The positive control for the test above. Without it, a route hard-wired
    to answer nothing would pass the empty case and every test built on it."""
    store.put("sr:rel:f:ETH1:abc:1", '{"result":"0x1"}', ttl=3600)
    status, payload = control.get_entries("primary", {})
    assert status == 200
    assert payload["count"] == 1
    assert payload["entries"][0]["key"] == "sr:rel:f:ETH1:abc:1"
    assert payload["entries"][0]["value"] == '{"result":"0x1"}'


def test_an_unreachable_store_answers_503_not_an_empty_200(store, control):
    """The failure this file exists to prevent.

    A 200 with an empty list here would tell a test the router stored nothing,
    when what happened is that the reader never got an answer. The test would
    then report the feature broken, and the real fault would be its own setup.
    """
    store.stop()
    status, payload = control.get_entries("primary", {})
    assert status == 503
    assert payload["read"] == "failed"
    assert "not an empty store" in payload["note"]


def test_a_pattern_narrows_what_is_returned(store, control):
    store.put("sr:chaintip:ETH1", "20000000")
    store.put("elsewhere:key", "value")
    status, payload = control.get_entries("primary", {"pattern": "sr:*"})
    assert status == 200
    assert [e["key"] for e in payload["entries"]] == ["sr:chaintip:ETH1"]


def test_reading_works_while_the_router_is_cut_off(store, control):
    """The reason the reader goes straight to the store rather than through the
    proxy. It is what lets a test prove the entry was never lost, rather than
    assume it."""
    store.put("sr:chaintip:ETH1", "20000000")
    control.cut_off("primary", {"kind": ERROR})
    status, payload = control.get_entries("primary", {})
    assert status == 200
    assert payload["count"] == 1


def test_an_unknown_store_answers_404_and_lists_what_exists(control):
    status, payload = control.get_entries("nope", {})
    assert status == 404
    assert payload["stores"] == ["primary"]


# ── state ─────────────────────────────────────────────────────────────────────


def test_state_reports_the_gate_and_whether_the_store_answers(store, control):
    status, payload = control.get_state("primary")
    assert status == 200
    assert payload["state"] == FORWARDING
    assert payload["store_reachable"] is True


def test_state_tells_a_cut_off_router_from_a_dead_store(store, control):
    """From the router's side those look the same. This is the one place that
    can separate them, which is what makes it worth measuring here and now
    rather than reporting a remembered value."""
    control.cut_off("primary", {"kind": TIMEOUT})
    _, payload = control.get_state("primary")
    assert payload["state"] == TIMEOUT
    assert payload["store_reachable"] is True

    store.stop()
    _, payload = control.get_state("primary")
    assert payload["state"] == TIMEOUT
    assert payload["store_reachable"] is False


# ── cut off and restore ───────────────────────────────────────────────────────


def test_cutting_off_needs_a_kind(control):
    """Not defaulted, because the router counts the two separately. A caller that
    did not choose has not said which half of that behaviour it is testing."""
    status, payload = control.cut_off("primary", {})
    assert status == 400
    assert payload["expected"] == [TIMEOUT, ERROR]


def test_an_unknown_kind_answers_400_rather_than_picking_one(control):
    status, payload = control.cut_off("primary", {"kind": "frozen"})
    assert status == 400
    assert "frozen" in payload["error"]
    _, state = control.get_state("primary")
    assert state["state"] == FORWARDING


def test_both_kinds_are_accepted(control):
    for kind in (TIMEOUT, ERROR):
        status, payload = control.cut_off("primary", {"kind": kind})
        assert status == 200
        assert payload["proxy"]["state"] == kind


def test_restoring_puts_the_gate_back(control):
    control.cut_off("primary", {"kind": ERROR})
    status, payload = control.restore("primary")
    assert status == 200
    assert payload["proxy"]["state"] == FORWARDING


# ── latency ───────────────────────────────────────────────────────────────────


def test_setting_a_latency_answers_with_the_value_it_set(control):
    status, payload = control.set_latency("primary", {"ms": 150})
    assert status == 200
    assert payload["proxy"]["latency_ms"] == 150


def test_the_state_route_reports_the_current_latency(control):
    control.set_latency("primary", {"ms": 150})
    status, payload = control.get_state("primary")
    assert status == 200
    assert payload["latency_ms"] == 150


def test_a_latency_for_a_store_that_does_not_exist_is_refused(control):
    status, payload = control.set_latency("no-such-store", {"ms": 150})
    assert status == 404
    assert "no-such-store" in payload["error"]


def test_a_latency_with_no_value_says_what_it_needed(control):
    status, payload = control.set_latency("primary", {})
    assert status == 400
    assert "ms" in payload["error"]


def test_a_non_numeric_latency_is_refused(control):
    status, payload = control.set_latency("primary", {"ms": "abc"})
    assert status == 400
    assert "abc" in payload["error"]


def test_a_negative_latency_is_refused(control):
    status, payload = control.set_latency("primary", {"ms": -1})
    assert status == 400


# ── flush ─────────────────────────────────────────────────────────────────────


def test_flush_empties_the_store(store, control):
    store.put("sr:one", "1")
    assert control.get_entries("primary", {})[1]["count"] == 1
    status, _ = control.flush("primary")
    assert status == 200
    assert control.get_entries("primary", {})[1]["count"] == 0


def test_flush_works_while_the_router_is_cut_off(store, control):
    """A test setting up its next case should not have to put the router back
    first."""
    store.put("sr:one", "1")
    control.cut_off("primary", {"kind": ERROR})
    assert control.flush("primary")[0] == 200
    assert control.get_entries("primary", {})[1]["count"] == 0


def test_flush_on_an_unreachable_store_answers_503(store, control):
    store.stop()
    status, _ = control.flush("primary")
    assert status == 503


# ── there is no write ─────────────────────────────────────────────────────────


def test_the_control_api_offers_no_way_to_put_an_entry_in():
    """A test that can plant an entry will plant one instead of making the
    router store it, and then it measures the store rather than the router.

    Checked as a whole surface rather than by trying a name: the claim is that
    no such route exists, whatever it might have been called.
    """
    surface = {name for name in dir(RespControlApi) if not name.startswith("_")}
    assert surface == {
        "cut_off",
        "flush",
        "get_entries",
        "get_state",
        "get_stores",
        "health",
        "names",
        "register",
        "reset_counters",
        "restore",
        "set_latency",
    }


# ── health ────────────────────────────────────────────────────────────────────


def test_health_answers_without_touching_the_store(store, control):
    """Says the listener is up, and nothing about the store. A health route that
    went to the store would report the whole simulator unhealthy whenever a test
    had deliberately cut the store off."""
    store.stop()
    status, payload = control.health()
    assert status == 200
    assert payload["stores"] == ["primary"]


# ── a reset puts the gate back ────────────────────────────────────────────────


def test_resetting_the_simulator_restores_a_cut_off_gate(store):
    """A cut-off is per-process state, and it outlives a test that dies early.

    Missing this is worse than missing a staged cache entry. A staged entry that
    survives makes the next test pass for the wrong reason; a cut-off that
    survives leaves the router unable to reach its store for the rest of the run,
    and every later test STILL passes — a router with no cache answers every
    request correctly from the chain nodes. Nothing in any reply says the cache
    was never consulted.
    """
    from provider_simulator.control_api import ControlApi
    from provider_simulator.domain.registry import build_registry
    from provider_simulator.listeners.ws import WsSubscriptions

    resp = RespControlApi()
    resp.register("primary", "127.0.0.1", store.port)
    registry = build_registry()
    api = ControlApi(registry, WsSubscriptions(), None, {}, resp.proxies)

    resp.cut_off("primary", {"kind": ERROR})
    assert resp.get_state("primary")[1]["state"] == ERROR

    status, payload = api.reset_all(None)
    assert status == 200
    assert payload["resp_proxies"] == ["primary"]
    assert resp.get_state("primary")[1]["state"] == FORWARDING


def test_a_history_only_clear_leaves_the_gate_alone(store):
    """Scenario-scoped, like a provider's fault settings. Clearing history is not
    a request to undo what a test deliberately set up."""
    from provider_simulator.control_api import ControlApi
    from provider_simulator.domain.registry import build_registry
    from provider_simulator.listeners.ws import WsSubscriptions

    resp = RespControlApi()
    resp.register("primary", "127.0.0.1", store.port)
    api = ControlApi(build_registry(), WsSubscriptions(), None, {}, resp.proxies)

    resp.cut_off("primary", {"kind": TIMEOUT})
    api.clear_history(None)
    assert resp.get_state("primary")[1]["state"] == TIMEOUT
