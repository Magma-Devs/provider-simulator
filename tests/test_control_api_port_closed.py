"""mode="port_closed" through the control API routes, with no socket.

The same style as test_control_api.py: a real Registry, a ControlApi, no
listener. That is exactly the case these tests are about. With no gRPC listener
running there is nothing that could close a port, so the API has to refuse, or
answer an error, instead of storing a fault nothing performs.

What a running listener does with the mode is in
test_simulator_grpc_port_closed.py.
"""

import pytest

from provider_simulator import control_api
from provider_simulator.control_api import ControlApi
from provider_simulator.domain.registry import build_registry
from provider_simulator.listeners.ws import WsSubscriptions
from provider_simulator.port_gate import PortGate

KEY = "lava-sim-grpc:1"


def _api():
    return ControlApi(build_registry(), WsSubscriptions())


def _api_with_a_gate_nothing_serves():
    """A gate with no serve loop behind it: the listener that never acts."""
    api = _api()
    provider = api.registry.provider("lava-sim-grpc", "1")
    endpoint = provider.endpoints[0]
    api.port_gates[endpoint.port] = PortGate(provider, endpoint)
    return api, endpoint.port


def test_port_closed_is_one_of_the_modes():
    st, resp = _api().apply_scenario({"providers": {KEY: {"mode": "banana"}}})
    assert st == 400
    assert "'port_closed'" in resp["error"], "the list of allowed modes must name it"


def test_with_no_grpc_listener_it_is_refused_and_not_stored():
    api = _api()
    st, resp = api.apply_scenario({"providers": {KEY: {"mode": "port_closed"}}})
    assert st == 409
    assert "runs no gRPC listener there" in resp["error"]
    assert "18548" in resp["error"], "the refusal must name the port"
    assert api.registry.provider("lava-sim-grpc", "1").scenario.snapshot()["mode"] == "success"


def test_an_endpoint_that_is_not_grpc_is_a_400_whether_or_not_a_listener_runs():
    """The reason that can never change comes first. A caller told "no listener"
    for a JSON-RPC provider would go looking for a listener that cannot exist."""
    api = _api()
    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"mode": "port_closed"}}})
    assert st == 400
    assert "only a gRPC endpoint can close its port" in resp["error"]
    assert "jsonrpc/http :18545" in resp["error"] and "jsonrpc/ws :18557" in resp["error"]
    assert api.registry.provider("eth-sim", "1").scenario.snapshot()["mode"] == "success"


def test_one_refused_provider_refuses_the_whole_request():
    """Staged all-or-nothing, like every other refusal: the healthy block beside
    the refused one must not be applied."""
    api = _api()
    st, _ = api.apply_scenario({"providers": {"btc-sim:1": {"mode": "down"}, "eth-sim:1": {"mode": "port_closed"}}})
    assert st == 400
    assert api.registry.provider("btc-sim", "1").scenario.snapshot()["mode"] == "success"


def test_a_listener_that_never_acts_is_an_error_naming_pool_provider_and_port(monkeypatch):
    monkeypatch.setattr(control_api, "_PORT_SETTLE_S", 0.2)
    api, port = _api_with_a_gate_nothing_serves()
    st, resp = api.apply_scenario({"providers": {KEY: {"mode": "port_closed"}}})
    assert st == 500, f"a port that never closed must not be answered {st}"
    for named in ("pool 'lava-sim-grpc'", "provider '1'", f"port {port}", "is not closed"):
        assert named in resp["error"], f"the error must name {named}: {resp['error']!r}"


@pytest.mark.parametrize("route", ["reset", "reset_all"])
def test_a_reset_that_cannot_reopen_the_port_is_an_error_too(monkeypatch, route):
    """The gate has no listener, so its port never accepts. A reset that answered
    200 would promise a port the caller cannot connect to."""
    monkeypatch.setattr(control_api, "_PORT_SETTLE_S", 0.2)
    api, port = _api_with_a_gate_nothing_serves()
    st, resp = getattr(api, route)()
    assert st == 500
    assert f"port {port} is not accepting connections" in resp["error"]


def test_clearing_history_waits_for_no_port(monkeypatch):
    """Clearing history changes no scenario, so no port has to move. With the
    wait in place this would take the whole settle time and answer 500."""
    monkeypatch.setattr(control_api, "_PORT_SETTLE_S", 0.2)
    api, _port = _api_with_a_gate_nothing_serves()
    st, _ = api.clear_history()
    assert st == 200


def test_the_ports_closed_by_a_scenario_are_read_from_the_scenario():
    api, port = _api_with_a_gate_nothing_serves()
    assert api.ports_closed_by_scenario() == []
    api.registry.provider("lava-sim-grpc", "1").scenario.update({"mode": "port_closed"})
    assert api.ports_closed_by_scenario() == [port]
    api.registry.provider("lava-sim-grpc", "1").scenario.update({"mode": "down"})
    assert api.ports_closed_by_scenario() == []


@pytest.mark.parametrize(
    "bad_filter, reason",
    [
        ({"ports": 18548}, "ports must be a list of integers or None"),
        ({"ports": []}, "ports must not be empty"),
        ({"transports": "http2"}, "unknown transport(s)"),
    ],
)
def test_a_malformed_filter_is_still_refused_by_the_scenario_itself(bad_filter, reason):
    """The port_closed rule reads the filters before the scenario has checked
    their shape. It must stand aside for a malformed one, not crash on it and
    not answer with a less useful reason."""
    api, _port = _api_with_a_gate_nothing_serves()
    st, resp = api.apply_scenario({"providers": {KEY: {"mode": "port_closed", **bad_filter}}})
    assert st == 400
    assert reason in resp["error"]
