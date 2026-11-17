"""mode="port_closed" through the control API routes, with no socket.

The same style as test_control_api.py: a real Registry, a ControlApi, no
listener. That is exactly the case these tests are about. With no listener
running there is nothing that could close a port, so the API has to refuse, or
answer an error, instead of storing a fault nothing performs.

What a running listener does with the mode is in
test_simulator_grpc_port_closed.py for a gRPC port, and in
integration/test_http_and_ws_port_closed.py for an http port and a ws port.
"""

import threading
import time

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


def test_the_error_for_a_port_that_did_not_move_names_the_listener_and_no_grpc_listener(monkeypatch):
    """An http port and a ws port have a listener too. So the text of the error
    says "The listener", and not "The gRPC listener"."""
    monkeypatch.setattr(control_api, "_PORT_SETTLE_S", 0.2)
    api, _port = _api_with_a_gate_nothing_serves()
    st, resp = api.apply_scenario({"providers": {KEY: {"mode": "port_closed"}}})
    assert st == 500
    assert "The listener did not reach the state the scenario asks for" in resp["error"], resp["error"]
    assert "gRPC listener" not in resp["error"], resp["error"]


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


def test_a_write_that_arrives_while_a_pass_runs_waits_for_the_gate(monkeypatch):
    """The last report is "open", and mode="down" needs the port open. But a
    pass of the loop runs, and it can still move the port. So the call asks the
    gate and waits for the next pass. This gate has no listener: the wait takes
    the whole settle time, and the call answers 500."""
    monkeypatch.setattr(control_api, "_PORT_SETTLE_S", 0.2)
    api, port = _api_with_a_gate_nothing_serves()
    gate = api.port_gates[port]
    gate.end_pass(gate.begin_pass(), is_open=True)
    gate.begin_pass()  # a pass begins, and it does not end
    st, resp = api.apply_scenario({"providers": {KEY: {"mode": "down"}}})
    assert st == 500, resp
    assert f"port {port} is not accepting connections" in resp["error"]


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
        ({"transports": "http2"}, "transports must be a list of transport names or None"),
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


# ── a call on one pool does not wait for the port of another pool ─────────────
#
# A write that has a port to wait for is held in that wait until the test lets
# it go. That keeps the pool of the write inside its lock for as long as the
# test needs, with no sleep and no listener. A call with no port to wait for
# passes at once, as it does in production, so a call that waits here waits for
# a lock and for nothing else.

POOLS = ("btc-sim", "eth-sim", "lava-sim-grpc")  # in name order: the sweep test relies on it
ORDERED_PAIRS = [(first, second) for first in POOLS for second in POOLS if first != second]


@pytest.fixture
def a_write_that_waits_for_its_port(monkeypatch):
    """The first call that has a port to wait for stays in that wait. Yields ``(inside, release)``."""
    inside, release = threading.Event(), threading.Event()

    def settle(self, wishes):
        if wishes and not inside.is_set():
            inside.set()
            release.wait(timeout=10)
        return ""

    monkeypatch.setattr(ControlApi, "_settle_ports", settle)
    yield inside, release
    release.set()


def _start_a_write_that_waits_for_its_port(api, pool, inside):
    """Close a port of ``pool`` with a write that cannot finish. Returns once the write is inside its wait."""
    provider = api.registry.provider(pool, "1")
    for endpoint in provider.endpoints:
        api.port_gates[endpoint.port] = PortGate(provider, endpoint)
    threading.Thread(
        target=api.apply_scenario, args=({"providers": {f"{pool}:1": {"mode": "port_closed"}}},), daemon=True
    ).start()
    assert inside.wait(timeout=5), f"the write to {pool} never reached its wait for the port"


def _run(call, done, answer):
    try:
        answer.append(call())
    finally:
        done.set()


def _run_each(calls):
    """Start each call on a thread of its own. Returns ``{name: (event set when it returned, [its answer])}``."""
    started = {}
    for name, call in calls.items():
        done, answer = threading.Event(), []
        threading.Thread(target=_run, args=(call, done, answer), daemon=True).start()
        started[name] = (done, answer)
    return started


@pytest.mark.parametrize("waiting_pool, other_pool", ORDERED_PAIRS)
def test_a_call_on_one_pool_does_not_wait_for_a_write_that_waits_for_the_port_of_another_pool(
    a_write_that_waits_for_its_port, waiting_pool, other_pool
):
    """A reset, a scenario write, and a write that names two pools, none of them the pool of the waiting write."""
    inside, _release = a_write_that_waits_for_its_port
    api = _api()
    _start_a_write_that_waits_for_its_port(api, waiting_pool, inside)
    third_pool = next(pool for pool in POOLS if pool not in (waiting_pool, other_pool))
    started = _run_each(
        {
            "reset": lambda: api.reset(pool=other_pool),
            "reset_all": lambda: api.reset_all(pool=other_pool),
            "a write to the pool": lambda: api.apply_scenario({"providers": {f"{other_pool}:1": {"mode": "down"}}}),
            "a write to the pool and a third pool": lambda: api.apply_scenario(
                {"providers": {f"{other_pool}:1": {"mode": "down"}, f"{third_pool}:1": {"mode": "down"}}}
            ),
        }
    )
    deadline = time.monotonic() + 3.0
    waited = [name for name, (done, _) in started.items() if not done.wait(max(deadline - time.monotonic(), 0.0))]
    assert (
        waited == []
    ), f"a write to {waiting_pool} was waiting for its port, and these calls on {other_pool} waited for it: {waited}"
    assert {name: answer[0][0] for name, (_, answer) in started.items()} == {name: 200 for name in started}


def test_a_call_on_a_pool_waits_for_a_write_that_waits_for_the_port_of_that_pool(a_write_that_waits_for_its_port):
    """The lock still keeps two writers of one pool apart, and a reset of every pool waits for any pool."""
    inside, release = a_write_that_waits_for_its_port
    api = _api()
    _start_a_write_that_waits_for_its_port(api, "eth-sim", inside)
    started = _run_each(
        {
            "reset": lambda: api.reset(pool="eth-sim"),
            "reset_all": lambda: api.reset_all(pool="eth-sim"),
            "a write to the pool": lambda: api.apply_scenario({"providers": {"eth-sim:1": {"mode": "down"}}}),
            "a write to the pool and another pool": lambda: api.apply_scenario(
                {"providers": {"eth-sim:1": {"mode": "down"}, "btc-sim:1": {"mode": "down"}}}
            ),
            "a reset of every pool": lambda: api.reset(),
        }
    )
    time.sleep(0.5)  # a call that took no lock would have returned by now
    returned_early = [name for name, (done, _) in started.items() if done.is_set()]
    release.set()
    never_returned = [name for name, (done, _) in started.items() if not done.wait(3.0)]
    assert (
        returned_early == []
    ), f"these calls returned while a write to eth-sim still waited for its port: {returned_early}"
    assert never_returned == [], f"these calls did not return after the waiting write was let go: {never_returned}"


def test_the_sweep_reverts_a_stale_scenario_of_one_pool_while_a_write_to_another_pool_waits(
    a_write_that_waits_for_its_port,
):
    """The pools go in name order, so btc-sim is done before the sweep reaches the waiting lava-sim-grpc."""
    import server as server_module

    inside, release = a_write_that_waits_for_its_port
    api = _api()
    assert api.apply_scenario({"providers": {"btc-sim:1": {"mode": "down"}}})[0] == 200
    _start_a_write_that_waits_for_its_port(api, "lava-sim-grpc", inside)
    provider = api.registry.provider("btc-sim", "1")
    # At this time every scenario written just now is older than the 900 s limit.
    sweeper = threading.Thread(
        target=server_module._revert_stale_scenarios, args=(api, 900, time.time() + 901), daemon=True
    )
    sweeper.start()
    deadline = time.monotonic() + 3.0
    while provider.scenario.snapshot()["mode"] != "success" and time.monotonic() < deadline:
        time.sleep(0.01)
    reverted, still_sweeping = provider.scenario.snapshot()["mode"] == "success", sweeper.is_alive()
    release.set()
    sweeper.join(timeout=10)
    assert (reverted, still_sweeping) == (True, True), (
        "the sweep must revert btc-sim and then wait at lava-sim-grpc, "
        f"got reverted={reverted} still_sweeping={still_sweeping}"
    )
    assert not sweeper.is_alive(), "the sweep did not finish after the waiting write was let go"
