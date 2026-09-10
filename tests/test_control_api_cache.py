"""Offline tests for the cache-sim control routes.

These drive ``ControlApi`` methods directly, the way the other control-API tests
do, so the routing decisions are under test without a socket.

The contract worth stating up front, because it is the one that bites: ``data``
is the response body as **plain text**. The cache-sim base64-encodes it once on
the way out, because that is how Go writes a byte field. A caller that
base64-encodes it first gets it encoded twice, and the router then serves
gibberish while every status line still says success.
"""

from __future__ import annotations

import base64

from provider_simulator.cache_sim import CacheSimRegistry
from provider_simulator.control_api import ControlApi
from provider_simulator.domain.registry import build_registry
from provider_simulator.listeners.ws import WsSubscriptions


def api(*names: str) -> ControlApi:
    """A ControlApi carrying the named cache-sims and nothing else."""
    caches = CacheSimRegistry()
    for name in names:
        caches.get_or_create(name)
    return ControlApi(build_registry(), WsSubscriptions(), caches)


class TestAnUnknownCacheIsRefusedAndNamesWhatExists:
    """Never a silent success. Staging into a cache nothing serves would leave
    the test running against an unstaged one -- passing, measuring nothing."""

    def test_staging_an_unknown_name_is_a_404_listing_the_real_ones(self) -> None:
        status, payload = api("secondary").cache_stage("typo", {"mode": "miss"})
        assert status == 404
        assert payload["caches"] == ["secondary"]
        assert "typo" in payload["error"]

    def test_reading_an_unknown_name_is_a_404(self) -> None:
        status, _ = api("secondary").get_cache_calls("typo")
        assert status == 404

    def test_resetting_an_unknown_name_is_a_404(self) -> None:
        status, _ = api("secondary").cache_reset("typo")
        assert status == 404

    def test_a_simulator_with_no_cache_sims_refuses_every_cache_route(self) -> None:
        status, payload = api().get_cache("secondary")
        assert status == 404
        assert payload["caches"] == []


class TestStaging:
    def test_data_is_plain_text_and_is_encoded_once(self) -> None:
        """The trap this test exists for: encoding the body yourself gets it
        encoded twice, and the router serves gibberish with a success status."""
        control = api("secondary")
        control.cache_stage("secondary", {"mode": "hit", "entry": {"data": '{"result":"0x1"}'}})

        sim = control.caches.get("secondary")
        assert sim is not None
        body = sim.plan(b"{}").body
        assert body is not None
        assert base64.b64decode(body["reply"]["data"]) == b'{"result":"0x1"}'

    def test_a_staged_entry_carries_every_field_the_trust_rules_need(self) -> None:
        control = api("secondary")
        status, _ = control.cache_stage(
            "secondary",
            {
                "mode": "hit",
                "entry": {
                    "data": "body",
                    "sig": "c2ln",
                    "latest_block": 99_000_000,
                    "metadata": [{"name": "X-Internal-Node", "value": "zone-a-7"}],
                    "is_node_error": True,
                    "status_code": 429,
                },
            },
        )
        assert status == 200

        sim = control.caches.get("secondary")
        assert sim is not None
        body = sim.plan(b"{}").body
        assert body is not None
        assert body["reply"]["latest_block"] == 99_000_000
        assert body["reply"]["metadata"] == [{"name": "X-Internal-Node", "value": "zone-a-7"}]
        assert body["is_node_error"] is True
        assert body["status_code"] == 429

    def test_an_unknown_mode_is_a_400_naming_the_real_modes(self) -> None:
        status, payload = api("secondary").cache_stage("secondary", {"mode": "hitt"})
        assert status == 400
        assert "hit" in payload["error"] and "miss" in payload["error"]

    def test_a_hit_with_no_entry_is_a_400(self) -> None:
        status, payload = api("secondary").cache_stage("secondary", {"mode": "hit"})
        assert status == 400
        assert "entry" in payload["error"]

    def test_a_non_object_entry_is_a_400(self) -> None:
        status, payload = api("secondary").cache_stage("secondary", {"mode": "hit", "entry": "nope"})
        assert status == 400
        assert "entry" in payload["error"]

    def test_a_non_object_body_is_a_400(self) -> None:
        status, _ = api("secondary").cache_stage("secondary", ["not", "an", "object"])
        assert status == 400

    def test_mode_defaults_to_hit_when_an_entry_is_given(self) -> None:
        control = api("secondary")
        status, payload = control.cache_stage("secondary", {"entry": {"data": "x"}})
        assert status == 200
        assert payload["cache"]["mode"] == "hit"

    def test_a_refused_stage_leaves_the_previous_answer_in_place(self) -> None:
        control = api("secondary")
        control.cache_stage("secondary", {"mode": "hit", "entry": {"data": "first"}})
        control.cache_stage("secondary", {"mode": "hitt"})

        sim = control.caches.get("secondary")
        assert sim is not None
        body = sim.plan(b"{}").body
        assert body is not None
        assert base64.b64decode(body["reply"]["data"]) == b"first"


class TestReadingTheCallLog:
    def test_the_call_route_reports_the_count_and_the_calls(self) -> None:
        control = api("secondary")
        sim = control.caches.get("secondary")
        assert sim is not None
        sim.plan(b'{"chain_id":"ETH1","requested_block":17}')

        status, payload = control.get_cache_calls("secondary")
        assert status == 200
        assert payload["count"] == 1
        assert payload["calls"][0]["requested_block"] == 17

    def test_clearing_the_calls_keeps_the_staged_answer(self) -> None:
        """The same split the provider reset/history-clear pair keeps: a test
        that warms an entry then wants a clean count needs one without the
        other."""
        control = api("secondary")
        control.cache_stage("secondary", {"mode": "hit", "entry": {"data": "x"}})
        sim = control.caches.get("secondary")
        assert sim is not None
        sim.plan(b"{}")

        status, payload = control.cache_clear_calls("secondary")
        assert status == 200
        assert payload["cache"]["calls"] == 0
        assert payload["cache"]["has_entry"] is True

    def test_reset_drops_the_answer_as_well_as_the_calls(self) -> None:
        control = api("secondary")
        control.cache_stage("secondary", {"mode": "hit", "entry": {"data": "x"}})
        status, payload = control.cache_reset("secondary")
        assert status == 200
        assert payload["cache"]["has_entry"] is False
        assert payload["cache"]["mode"] == "miss"


class TestListingTheCaches:
    def test_every_cache_sim_is_listed_with_its_state(self) -> None:
        status, payload = api("internal", "other-zone").get_caches()
        assert status == 200
        assert payload["count"] == 2
        assert sorted(c["name"] for c in payload["caches"]) == ["internal", "other-zone"]

    def test_a_simulator_with_none_lists_none(self) -> None:
        status, payload = api().get_caches()
        assert status == 200
        assert payload == {"caches": [], "count": 0}

    def test_two_caches_stage_independently(self) -> None:
        control = api("internal", "other-zone")
        control.cache_stage("internal", {"mode": "hit", "entry": {"data": "x"}})

        internal = control.caches.get("internal")
        other = control.caches.get("other-zone")
        assert internal is not None and other is not None
        assert internal.state()["has_entry"] is True
        assert other.state()["has_entry"] is False


class TestTheDefaultConstructorStillWorks:
    """Existing callers pass two arguments. They must keep working, and must
    not gain a cache-sim they never asked for."""

    def test_control_api_without_caches_serves_the_provider_routes(self) -> None:
        control = ControlApi(build_registry(), WsSubscriptions())
        status, _ = control.get_stats()
        assert status == 200

    def test_control_api_without_caches_has_no_cache_sims(self) -> None:
        control = ControlApi(build_registry(), WsSubscriptions())
        assert control.get_caches() == (200, {"caches": [], "count": 0})


class TestTheSelfTestRoute:
    """``POST /cache/<name>/selftest-write`` proves the call record can see a
    write, so a test asserting "no writes arrived" is reading a real absence.

    The dialling half needs a listener and lives in the wire tests. These two
    pin the branches that answer without dialling anything.
    """

    def test_an_unknown_cache_is_a_404_naming_the_ones_that_exist(self) -> None:
        status, payload = api("secondary").cache_selftest_write("nosuch")
        assert status == 404
        assert payload["caches"] == ["secondary"]

    def test_a_cache_with_no_listener_port_refuses_rather_than_dialling(self) -> None:
        """A cache-sim built with no gRPC listener has nothing to dial.

        Answering 200 here would report "the record can see a write" without
        any call having been made, which is the exact false reassurance this
        route exists to remove.
        """
        status, payload = api("secondary").cache_selftest_write("secondary")
        assert status == 409
        assert "no listener port" in payload["error"]


class TestThePathDispatch:
    """``/cache/<name>/<action>`` parsing, which the ControlApi tests cannot
    see because they are called with the name already split out.

    A path that parses to the wrong action would answer 404 rather than do the
    wrong thing, but a path that parses to the wrong *name* would stage into
    another cache -- so both halves are pinned here.
    """

    def test_the_name_and_action_split_on_the_first_slash(self) -> None:
        from server import _cache_route

        assert _cache_route("/cache/secondary/entry") == ("secondary", "entry")
        assert _cache_route("/cache/secondary/calls/clear") == ("secondary", "calls/clear")
        assert _cache_route("/cache/secondary") == ("secondary", "")
        assert _cache_route("/cache/secondary/") == ("secondary", "")

    def test_a_nameless_path_is_refused_rather_than_defaulted(self) -> None:
        from server import _dispatch_cache_get, _dispatch_cache_post

        assert _dispatch_cache_get(api("secondary"), "/cache/")[0] == 404
        assert _dispatch_cache_post(api("secondary"), "/cache/", {})[0] == 404

    def test_each_post_action_reaches_its_route(self) -> None:
        from server import _dispatch_cache_post

        control = api("secondary")
        assert _dispatch_cache_post(control, "/cache/secondary/entry", {"entry": {"data": "x"}})[0] == 200
        assert _dispatch_cache_post(control, "/cache/secondary/calls/clear", {})[0] == 200
        assert _dispatch_cache_post(control, "/cache/secondary/reset", {})[0] == 200
        # 409, not 404: the route is wired, and this ControlApi has no ports.
        # A 404 here would mean the action never reached the ControlApi at all.
        assert _dispatch_cache_post(control, "/cache/secondary/selftest-write", {})[0] == 409

    def test_each_get_action_reaches_its_route(self) -> None:
        from server import _dispatch_cache_get

        control = api("secondary")
        assert _dispatch_cache_get(control, "/cache/secondary/calls")[0] == 200
        assert _dispatch_cache_get(control, "/cache/secondary")[0] == 200

    def test_an_unknown_action_is_a_404_listing_the_real_ones(self) -> None:
        from server import _dispatch_cache_get, _dispatch_cache_post

        status, payload = _dispatch_cache_post(api("secondary"), "/cache/secondary/wipe", {})
        assert status == 404
        assert "entry" in payload["actions"]

        status, payload = _dispatch_cache_get(api("secondary"), "/cache/secondary/history")
        assert status == 404

    def test_a_post_action_is_not_reachable_by_get(self) -> None:
        """`reset` changes state; a GET must not perform it."""
        from server import _dispatch_cache_get

        assert _dispatch_cache_get(api("secondary"), "/cache/secondary/reset")[0] == 404


class TestTheServerWiring:
    """The bootstrap: which cache-sims exist, and on which ports."""

    def test_the_default_server_carries_the_configured_cache_sims(self) -> None:
        from constants import CACHE_SIM_PORTS
        from server import SimulatorServer

        srv = SimulatorServer(host="127.0.0.1", scenario_ttl_s=0)
        assert srv.cache_ports == CACHE_SIM_PORTS
        assert srv.caches.names() == sorted(CACHE_SIM_PORTS)

    def test_the_ports_are_overridable_like_the_control_port(self) -> None:
        """A second server in one process needs its own ports."""
        from server import SimulatorServer

        srv = SimulatorServer(host="127.0.0.1", scenario_ttl_s=0, cache_ports={"other": 19199})
        assert srv.cache_ports == {"other": 19199}
        assert srv.caches.names() == ["other"]

    def test_no_cache_ports_means_no_cache_sims_and_no_crash(self) -> None:
        from server import SimulatorServer

        srv = SimulatorServer(host="127.0.0.1", scenario_ttl_s=0, cache_ports={})
        assert srv.caches.names() == []
        assert srv.control.get_caches() == (200, {"caches": [], "count": 0})

    def test_a_cache_sim_port_never_collides_with_a_provider_port(self) -> None:
        """The provider block and the cache block must stay disjoint, or adding
        a pool silently steals a cache's port."""
        from constants import CACHE_SIM_PORTS, CONTROL_PORT
        from provider_simulator.domain.registry import build_registry

        provider_ports = set(build_registry().ports())
        cache_ports = set(CACHE_SIM_PORTS.values())
        assert not (provider_ports & cache_ports)
        assert CONTROL_PORT not in cache_ports


class TestTheSharedResetClearsCacheSims:
    """A staged entry that survived into the next test would be served to a
    test expecting a cold cache: green, for the wrong reason.

    Nothing used to clear them. POST /reset/all runs ControlApi.reset_all, which
    iterates providers and chain heads and never touched the cache-sims, so the
    only thing that cleared one was a route no shared fixture calls.
    """

    def _staged(self) -> ControlApi:
        control = api("secondary")
        control.cache_stage("secondary", {"mode": "hit", "entry": {"data": "x"}})
        sim = control.caches.get("secondary")
        assert sim is not None
        sim.plan(b"{}")
        return control

    def test_reset_all_clears_the_staged_entry_and_the_call_log(self) -> None:
        control = self._staged()
        status, payload = control.reset_all()
        assert status == 200
        assert payload["caches"] == ["secondary"]

        sim = control.caches.get("secondary")
        assert sim is not None
        assert sim.call_count() == 0
        assert sim.plan(b"{}").body == {"reply": None}, "a staged hit survived reset_all"

    def test_reset_clears_the_answer_but_keeps_the_call_log(self) -> None:
        control = self._staged()
        control.reset()
        sim = control.caches.get("secondary")
        assert sim is not None
        assert sim.plan(b"{}").body == {"reply": None}

    def test_clear_history_clears_the_call_log_but_keeps_the_answer(self) -> None:
        control = self._staged()
        control.clear_history()
        sim = control.caches.get("secondary")
        assert sim is not None
        assert sim.call_count() == 0
        assert sim.plan(b"{}").body != {"reply": None}, "clear_history dropped the staged entry"

    def test_a_pool_scoped_reset_leaves_cache_sims_alone(self) -> None:
        """A cache has no pool, so a caller narrowing to one is not asking
        about it. Clearing it anyway would wipe state the caller meant to keep."""
        control = self._staged()
        status, payload = control.reset_all(pool="eth-sim")
        assert status == 200
        assert payload["caches"] == []

        sim = control.caches.get("secondary")
        assert sim is not None
        assert sim.plan(b"{}").body != {"reply": None}
