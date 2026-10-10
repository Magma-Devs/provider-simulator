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


def test_with_no_listener_it_is_refused_and_not_stored():
    api = _api()
    st, resp = api.apply_scenario({"providers": {KEY: {"mode": "port_closed"}}})
    assert st == 409
    assert "runs no listener there" in resp["error"]
    assert "18548" in resp["error"], "the refusal must name the port"
    assert api.registry.provider("lava-sim-grpc", "1").scenario.snapshot()["mode"] == "success"


def test_an_http_and_ws_provider_with_no_listener_is_refused_and_not_stored():
    """An http port and a ws port can close, as a gRPC port can. So this API,
    which has no listener, refuses the mode for the same reason as for a gRPC
    port: no listener runs on the two ports, so no port would close."""
    api = _api()
    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"mode": "port_closed"}}})
    assert st == 409
    assert "runs no listener there" in resp["error"]
    assert "18545" in resp["error"] and "18557" in resp["error"], "the refusal must name both ports"
    assert api.registry.provider("eth-sim", "1").scenario.snapshot()["mode"] == "success"


def test_one_refused_provider_refuses_the_whole_request():
    """Staged all-or-nothing, like every other refusal: the healthy block beside
    the refused one must not be applied."""
    api = _api()
    st, _ = api.apply_scenario({"providers": {"btc-sim:1": {"mode": "down"}, "eth-sim:1": {"mode": "port_closed"}}})
    assert st == 409
    assert api.registry.provider("btc-sim", "1").scenario.snapshot()["mode"] == "success"


@pytest.mark.parametrize(
    "refused_block",
    [
        {"mode": "down", "transports": ["grpc"]},
        {"mode": "down", "ports": []},
        {"mode": "down", "pause_at": "mid_body"},
    ],
)
def test_a_block_the_scenario_itself_refuses_refuses_the_whole_request_too(refused_block):
    """These three are refused by ScenarioConfig, the rules the write applies,
    and not by a rule of the control API. Every block is checked against them
    before any block is written, so the healthy block before the refused one
    is not applied either."""
    api = _api()
    st, resp = api.apply_scenario({"providers": {"btc-sim:1": {"mode": "down"}, "eth-sim:1": refused_block}})
    assert st == 400
    assert resp["error"].startswith("eth-sim:1: "), "the refusal must name the provider it refused"
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


def test_a_write_that_moves_no_port_waits_for_no_gate(monkeypatch):
    """The gate reported "open", and mode="down" needs the port open. No port
    has to move, so the call waits for no gate. This gate has no listener: a
    wait would take the whole settle time and answer 500."""
    monkeypatch.setattr(control_api, "_PORT_SETTLE_S", 0.2)
    api, port = _api_with_a_gate_nothing_serves()
    gate = api.port_gates[port]
    gate.end_pass(gate.begin_pass(), is_open=True)
    st, resp = api.apply_scenario({"providers": {KEY: {"mode": "down"}}})
    assert st == 200, resp


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
