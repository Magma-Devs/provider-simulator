"""The real bootstrap, and the faults that only appear there.

Every other RESP test builds the pieces directly — a ``RespProxy`` handed to a
throwaway server, a ``_RespControlServer`` constructed by hand. That proves the
pieces work and proves nothing about ``SimulatorServer.start()``, which is what
``run.py`` actually runs. A change that dropped a listener from the bootstrap,
attached the wrong api object, or never started its thread would leave the
deployed simulator without this feature while every other RESP test stayed
green.

So these go through the shared session simulator — the same object a pod runs,
on the same ports, started by ``tests/conftest.py``. An earlier version of this
file booted a SECOND ``SimulatorServer`` of its own and collided with it on
every topology port; the tests passed alone and errored in the suite.

**These tests have no store behind them, and that is deliberate.** The session
simulator points at the deployment's default store address, which is not running
during a test run. That is enough to prove wiring: a listener attached to the
wrong api, or to none, answers 404 "unknown path" rather than the RESP api's own
503. What the routes DO with a reachable store is proved in
``test_resp_control_routes.py``, against a store that is really there.

Readiness is here for the same reason. ``_ready()`` and ``wait_ready()`` both
read ``registry.ports()``, which is built from the topology table. Neither RESP
listener is a topology row, so both walked straight past them and the pod could
report ready while either was unbound.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import pytest

from constants import RESP_CONTROL_PORT, RESP_PROXY_PORTS
from server import SimulatorServer

_PROXY_PORT = RESP_PROXY_PORTS["primary"]

# Ports nothing binds, for the construct-only tests below. Far from both the
# service ports and the provider block so a simulator running on this machine
# cannot make one of them accidentally succeed.
_UNBOUND_PROXY_PORT = 29102
_UNBOUND_CONTROL_PORT = 29101


def _get(path: str) -> tuple[int, dict]:
    request = urllib.request.Request(f"http://127.0.0.1:{RESP_CONTROL_PORT}{path}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _accepts(port: int) -> bool:
    probe = socket.socket()
    probe.settimeout(0.5)
    try:
        return probe.connect_ex(("127.0.0.1", port)) == 0
    finally:
        probe.close()


# ── the bootstrap binds them ──────────────────────────────────────────────────


def test_starting_the_server_binds_both_resp_listeners(sim_server):
    assert _accepts(_PROXY_PORT), "the RESP proxy port is not bound by SimulatorServer.start()"
    assert _accepts(RESP_CONTROL_PORT), "the RESP control port is not bound by SimulatorServer.start()"


def test_the_control_listener_is_wired_to_the_resp_api(sim_server):
    """Bound is not the same as wired.

    A listener attached to the wrong api object, or to none, answers on the
    right port and does the wrong thing. So ask for an action that does not
    exist: the RESP api refuses with its OWN list of actions, while any other
    handler on that port answers the generic "unknown path".

    **Deliberately a route that touches no store.** The first version of this
    test called ``/resp/keys`` and required the 503 a missing store produces.
    That reads the deployment's default store address, which does not resolve
    during a test run, and name resolution is not bounded by the socket timeout
    — so it passed alone and timed out inside the full suite, where the machine
    is busier. A wiring test must not depend on how fast a name fails to
    resolve. What the routes do against a store that is really there is proved
    in test_resp_control_routes.py.
    """
    status, payload = _get("/resp/nonsense")
    assert status == 404
    assert payload["actions"] == ["keys", "state"], f"this is not the RESP api answering: {payload}"


def test_health_lists_the_store_the_bootstrap_registered(sim_server):
    status, payload = _get("/health")
    assert status == 200
    assert payload["stores"] == ["primary"]


# ── readiness counts them ─────────────────────────────────────────────────────


def test_readiness_counts_both_resp_ports(sim_server):
    assert _PROXY_PORT in sim_server.extra_ready_ports
    assert RESP_CONTROL_PORT in sim_server.extra_ready_ports


def test_wait_ready_would_notice_a_resp_listener_that_never_bound():
    """The negative. A readiness set that ignored these ports would return at
    once whether or not they were up, so this proves the set is really used.

    Constructed and never started, on ports nothing binds, so ``wait_ready``
    must time out naming one of them rather than pass.
    """
    srv = SimulatorServer(
        host="127.0.0.1",
        scenario_ttl_s=0,
        resp_control_port=_UNBOUND_CONTROL_PORT,
        resp_proxy_ports={"primary": _UNBOUND_PROXY_PORT},
    )
    with pytest.raises(TimeoutError) as caught:
        srv.wait_ready(timeout_s=0.5)
    named = str(caught.value)
    assert str(_UNBOUND_PROXY_PORT) in named or str(_UNBOUND_CONTROL_PORT) in named


# ── a second store name is refused rather than silently aliased ───────────────
#
# These construct a server and never start it, so they bind nothing and cannot
# collide with the session simulator.


def test_two_store_names_against_one_target_are_refused():
    """Two names would each get a proxy and a reader, ``?store=`` would appear
    to choose between them, and both would reach the same store. A test would
    then believe it had read one store and cut off the other, and nothing would
    say so — there is no error the wrong answer produces on its own.
    """
    with pytest.raises(ValueError) as caught:
        SimulatorServer(
            host="127.0.0.1",
            scenario_ttl_s=0,
            resp_proxy_ports={"primary": _UNBOUND_PROXY_PORT, "second": _UNBOUND_PROXY_PORT + 1},
        )
    assert "same store" in str(caught.value)


def test_one_store_name_is_accepted():
    """The positive control. A guard that refused every configuration would pass
    the test above and break the feature."""
    srv = SimulatorServer(host="127.0.0.1", scenario_ttl_s=0, resp_proxy_ports={"primary": _UNBOUND_PROXY_PORT})
    assert srv.resp_control.names() == ["primary"]


def test_no_resp_ports_means_no_resp_listeners():
    """An empty mapping runs the simulator with no RESP half at all, the way an
    empty cache-ports mapping runs it with no cache-sim."""
    srv = SimulatorServer(host="127.0.0.1", scenario_ttl_s=0, resp_proxy_ports={})
    assert srv.extra_ready_ports == frozenset()
    assert srv.resp_control.names() == []
