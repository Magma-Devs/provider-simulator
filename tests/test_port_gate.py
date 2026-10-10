"""The port gate, and the one decision it reads. No simulator, no gRPC.

``test_simulator_grpc_port_closed.py`` shows the whole path on real gRPC
servers, and ``integration/test_http_and_ws_port_closed.py`` shows it on real
http and ws servers. This file pins the three rules that path depends on and
cannot isolate:

- a report from a serve-loop pass that began BEFORE the caller asked is not an
  answer to the caller,
- a port the serve loop reports closed is only closed when a connection to it is
  really refused, and
- a gate gives no last report while an ask has no answer, so a caller waits for
  that port again.

All three are about a caller being told the port changed when it had not.
"""

import socket

import pytest

from provider_simulator import fault_policy
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.port_gate import PortGate


def _free_port() -> int:
    """A port nothing listens on: bound once to learn the number, then released."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _gate(port: int) -> PortGate:
    endpoint = Endpoint("grpc", "http2", port)
    provider = Pool(name="lava-sim-grpc", chain="lava").add_provider("1", [endpoint])
    return PortGate(provider, endpoint)


# ── the decision ─────────────────────────────────────────────────────────────

_GRPC = Endpoint("grpc", "http2", 18548)


@pytest.mark.parametrize(
    "scenario, closed",
    [
        ({"mode": "port_closed"}, True),
        ({"mode": "port_closed", "transports": ["http2"]}, True),
        ({"mode": "port_closed", "ports": [18548]}, True),
        ({"mode": "port_closed", "transports": ["http2"], "ports": [18548]}, True),
        ({"mode": "port_closed", "transports": ["ws"]}, False),
        ({"mode": "port_closed", "ports": [18549]}, False),
        ({"mode": "port_closed", "transports": ["http2"], "ports": [18549]}, False),
        ({"mode": "down"}, False),
        ({"mode": "success"}, False),
        ({}, False),
    ],
)
def test_a_port_is_closed_by_the_mode_and_the_filters_together(scenario, closed):
    assert fault_policy.port_closed(scenario, _GRPC) is closed


def test_the_ladder_has_no_verdict_for_port_closed():
    """A request that still reaches the endpoint arrived at an open port, so it
    is answered as an open port answers. See fault_policy.port_closed."""
    assert fault_policy.ladder("port_closed", {"mode": "port_closed"}) is fault_policy.NONE_VERDICT


def test_the_gate_reads_the_providers_own_scenario():
    gate = _gate(_free_port())
    assert gate.wants_closed() is False
    gate.provider.scenario.update({"mode": "port_closed"})
    assert gate.wants_closed() is True
    gate.provider.scenario.reset()
    assert gate.wants_closed() is False


def test_a_pause_cannot_be_set_together_with_port_closed():
    """The scenario's own rule for a mode that never writes a reply."""
    gate = _gate(_free_port())
    with pytest.raises(ValueError, match="cannot apply with mode='port_closed'"):
        gate.provider.scenario.update({"mode": "port_closed", "pause_at": "mid_body"})


# ── waiting ──────────────────────────────────────────────────────────────────


def test_a_report_from_a_pass_that_began_before_the_ask_is_not_accepted():
    """The stale report. The pass read the scenario before the caller wrote it,
    so what it reports is the port as it was."""
    gate = _gate(_free_port())  # nothing listens: the port really is closed
    seen_by_the_old_pass = gate.begin_pass()
    ticket = gate.ask()
    gate.end_pass(seen_by_the_old_pass, is_open=False)
    assert gate.wait(ticket, want_open=False, timeout_s=0.2) is False

    gate.end_pass(gate.begin_pass(), is_open=False)  # a pass that began after the ask
    assert gate.wait(ticket, want_open=False, timeout_s=0.2) is True


def test_no_pass_at_all_is_a_timeout_not_a_success():
    gate = _gate(_free_port())
    assert gate.wait(gate.ask(), want_open=False, timeout_s=0.1) is False


def test_a_port_reported_closed_that_still_accepts_is_not_closed():
    """The report and the socket disagree, as they do when something else still
    listens on the port. The socket is what a router meets, so it wins."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)  # room for every probe below; none is ever accepted
    try:
        gate = _gate(listener.getsockname()[1])
        ticket = gate.ask()
        gate.end_pass(gate.begin_pass(), is_open=False)
        assert gate.accepts() is True
        assert gate.wait(ticket, want_open=False, timeout_s=0.2) is False
        # The control: the same report for an OPEN port is accepted, so the
        # refusal above is the socket's doing and not a wait that never passes.
        ticket = gate.ask()
        gate.end_pass(gate.begin_pass(), is_open=True)
        assert gate.wait(ticket, want_open=True, timeout_s=0.2) is True
    finally:
        listener.close()


def test_a_port_reported_open_that_refuses_is_not_open():
    gate = _gate(_free_port())
    ticket = gate.ask()
    gate.end_pass(gate.begin_pass(), is_open=True)
    assert gate.accepts() is False
    assert gate.wait(ticket, want_open=True, timeout_s=0.2) is False


def test_asking_wakes_the_serve_loop_and_survives_one_that_is_gone():
    gate = _gate(_free_port())
    woken = []
    gate.attach(lambda: woken.append(True))
    assert gate.ask() == 1
    assert woken == [True]

    def gone() -> None:
        raise RuntimeError("Event loop is closed")

    gate.attach(gone)
    assert gate.ask() == 2, "a dead serve loop must not raise into the control API"


# ── the last report ──────────────────────────────────────────────────────────


def test_a_gate_with_no_finished_pass_has_no_report():
    """A new gate gives None, and not False. False says that a pass left the
    port closed, and no pass ran."""
    assert _gate(_free_port()).last_report() is None


@pytest.mark.parametrize("is_open", [True, False], ids=["open", "closed"])
def test_the_last_report_is_what_the_newest_pass_left(is_open):
    gate = _gate(_free_port())
    gate.end_pass(gate.begin_pass(), is_open=is_open)
    assert gate.last_report() is is_open


def test_a_gate_with_an_ask_that_no_pass_answered_has_no_report():
    """An ask asks for a new pass. The report of an older pass shows the port
    before the ask, so the gate gives no report until a newer pass finishes."""
    gate = _gate(_free_port())
    gate.end_pass(gate.begin_pass(), is_open=True)
    assert gate.last_report() is True

    seen_by_the_old_pass = gate.begin_pass()
    gate.ask()
    assert gate.last_report() is None
    gate.end_pass(seen_by_the_old_pass, is_open=True)  # this pass began before the ask
    assert gate.last_report() is None

    gate.end_pass(gate.begin_pass(), is_open=False)  # a pass that began after the ask
    assert gate.last_report() is False
