"""``mode="port_closed"`` on an ``http`` port and on a ``ws`` port, over real sockets.

A closed port is not a reply. ``mode="down"`` accepts the connection and
answers HTTP 503. ``mode="port_closed"`` closes the port: the simulator refuses
a new connection, and each open connection ends. A closed port receives no
request, so the history of its provider gets no row.

``TestTheServerOfOneProviderPort`` has the tests of ``_ProviderHTTPServer``, the
server class that can close its port. Each test makes a server on a port that
the system gives, with a small handler class of this file. These tests use no
simulator.

The tests of each other class use the shared simulator of the session (see
conftest.py) and the provider ``eth-sim:1``, which has an ``http`` port and a
``ws`` port. The control API answers only when the port has changed. So each
test reads the port with a raw TCP connection on the line after the control
call, with no sleep.

The last test is in no class. It starts a simulator of its own, because it
stops that simulator.

What a gRPC port does with the mode is in tests/test_simulator_grpc_port_closed.py.
"""

from __future__ import annotations

import errno
import http.client
import json
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.request

import pytest

import server as server_module
from constants import CACHE_SIM_PORTS, CONTROL_PORT, RESP_CONTROL_PORT, RESP_PROXY_PORTS
from provider_simulator import topology
from provider_simulator.topology import port_of
from tests.ws_client import WsClient

# The address of a server on a port that the system gives.
_ANY_PORT = ("127.0.0.1", 0)

# The provider of the tests of the shared simulator, with its two ports.
KEY = "eth-sim:1"
HTTP_PORT = port_of("eth-sim", "1")
WS_PORT = port_of("eth-sim", "1", transport="ws")
# The http port of provider 2 of the same pool. No test closes it.
OTHER_HTTP_PORT = port_of("eth-sim", "2")

_CHAIN_ID_CALL = {"jsonrpc": "2.0", "id": 7, "method": "eth_chainId", "params": []}
_SUBSCRIBE = {"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["newHeads"]}
# The errors of a request whose connection ended with no reply.
_CONNECTION_ERRORS = (ConnectionError, http.client.HTTPException, urllib.error.URLError)


# ── helpers ──────────────────────────────────────────────────────────────────


def _post(url: str, body: dict, timeout: float = 15) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _set(control: str, key: str, **block) -> tuple[int, dict]:
    return _post(f"{control}/scenario", {"providers": {key: block}})


def _scenario(control: str, key: str) -> dict:
    _, body = _get(f"{control}/scenario")
    return body["providers"][key]


def _rows(control: str, pool: str, pid: str) -> list[tuple]:
    """The history rows of one provider, each one as (method, status, port)."""
    _, body = _get(f"{control}/history?pool={pool}&pid={pid}")
    return [(row["method"], row["status"], row["port"]) for row in body["history"]]


def _subscriptions(control: str) -> list[tuple]:
    """The entries of GET /ws/subscriptions, each one as (subscription id, pool, provider id)."""
    _, body = _get(f"{control}/ws/subscriptions")
    return [(entry["subscription_id"], entry["pool"], entry["pid"]) for entry in body["subscriptions"]]


def _connect(port: int) -> int:
    """Try one TCP connection. 0 when the port accepts it, else the errno."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(2.0)
    try:
        return probe.connect_ex(("127.0.0.1", port))
    finally:
        probe.close()


def _bytes_until_the_end(sock: socket.socket | None, timeout: float) -> bytes:
    """Read a connection until it ends, and return each byte that came.

    The end is the end of the stream, or a reset. If no end comes in
    ``timeout`` seconds, the read raises TimeoutError: the connection is still
    open."""
    assert sock is not None, "the client has no connection"
    sock.settimeout(timeout)
    received = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                return received
            received += chunk
    except ConnectionError:
        return received


@pytest.fixture(autouse=True)
def every_port_open(request):
    """Send ``POST /reset/all`` before and after each test of the shared
    simulator, and require HTTP 200. The reset opens each port that a test
    closed, and it clears the history.

    A test that does not ask for ``sim`` uses no shared simulator, so it gets
    no reset."""
    if "sim" not in request.fixturenames:
        yield
        return
    control = request.getfixturevalue("sim")["control"]
    status, body = _post(f"{control}/reset/all", {})
    assert status == 200, f"the reset before the test failed: {body}"
    yield
    status, body = _post(f"{control}/reset/all", {})
    assert status == 200, f"the reset that opens each port again failed: {body}"


# ── the server of one provider port ──────────────────────────────────────────


class _OneByteThenHold(socketserver.BaseRequestHandler):
    """Sends one byte, and then holds the connection until the connection ends."""

    def handle(self) -> None:
        self.request.sendall(b"x")
        self.request.recv(1)


def _one_byte_from(port: int) -> bytes:
    """Open one connection, read the byte that the handler sends, and close."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        return client.recv(1)


def _stop(server) -> None:
    """Stop the server of a test. A test that failed before ``close_port()``
    left a serve loop that runs, and this function stops that loop."""
    if not server.closed_by_scenario:
        server.shutdown()
    server.server_close()


def _handle_error_of(server, error: Exception) -> None:
    """Call ``handle_error`` as ``socketserver`` calls it: while it handles the error."""
    try:
        raise error
    except Exception:
        server.handle_error(None, ("127.0.0.1", 0))


class TestTheServerOfOneProviderPort:
    def test_close_port_refuses_a_new_connection_at_once(self):
        """Twenty rounds. In each round the serve loop sits in its poll when
        the port closes. On Linux a listening socket accepts connections for as
        long as that poll runs, if the server only closes the socket."""
        serve_threads = []
        try:
            for round_number in range(20):
                server = server_module._ProviderHTTPServer(_ANY_PORT, _OneByteThenHold)
                port = server.server_port
                serve_threads.append(server.serve())
                try:
                    assert _one_byte_from(port) == b"x", f"round {round_number}: the server did not serve"
                    server.close_port()
                    refused = _connect(port)
                    assert refused == errno.ECONNREFUSED, (
                        f"round {round_number}: port {port} must refuse a connection right after close_port(); "
                        f"connect_ex gave {refused} ({errno.errorcode.get(refused, 'accepted')})"
                    )
                    # A new server binds the port at once. A port that is still in use raises OSError here.
                    server_module._ProviderHTTPServer(("127.0.0.1", port), _OneByteThenHold).server_close()
                finally:
                    _stop(server)
        finally:
            # Each serve loop ends in its own time, so one wait at the end is sufficient for all.
            for serve_thread in serve_threads:
                serve_thread.join(timeout=2.0)

    def test_close_port_ends_an_open_connection(self):
        server = server_module._ProviderHTTPServer(_ANY_PORT, _OneByteThenHold)
        serve_thread = server.serve()
        client = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        try:
            assert client.recv(1) == b"x", "after this byte the handler holds the connection"
            server.close_port()
            client.settimeout(2.0)
            try:
                rest = client.recv(1)  # a timeout fails the test: the connection is still open
            except ConnectionError:
                rest = b""  # a reset ends the connection too
            assert rest == b"", f"the client read {rest!r} from a closed port"
        finally:
            client.close()
            _stop(server)
            serve_thread.join(timeout=2.0)

    def test_close_port_returns_while_the_serve_loop_still_runs(self, monkeypatch):
        """``BaseServer.shutdown`` waits for the serve loop. ``close_port`` must
        not wait for it, because a control call waits for ``close_port``. Here
        ``shutdown`` cannot finish before the test lets it."""
        server = server_module._ProviderHTTPServer(_ANY_PORT, _OneByteThenHold)
        port = server.server_port
        serve_thread = server.serve()
        called, let_it_run = threading.Event(), threading.Event()
        real_shutdown = server.shutdown

        def held_back() -> None:
            called.set()
            let_it_run.wait(timeout=3.0)
            real_shutdown()

        monkeypatch.setattr(server, "shutdown", held_back)
        try:
            assert _one_byte_from(port) == b"x", "the server did not serve"
            started = time.monotonic()
            server.close_port()
            took = time.monotonic() - started
            refused = _connect(port)
            assert took < 1.0, f"close_port() took {took:.2f}s: it waited for the serve loop"
            assert refused == errno.ECONNREFUSED
            assert called.wait(timeout=2.0), "close_port() did not tell the serve loop to stop"
        finally:
            let_it_run.set()
            _stop(server)
            serve_thread.join(timeout=2.0)
        assert not serve_thread.is_alive(), "the serve loop must end after shutdown ran"

    def test_a_connection_accepted_after_the_close_began_is_ended(self):
        """The serve loop can accept a connection in the moment of the close.
        ``close_port`` then does not have the socket in its copy of the client
        sockets, so ``get_request`` ends the connection.

        The test sets the flag of the close by hand. The listening socket is
        still open, so ``get_request`` accepts the connection that waits."""
        server = server_module._ProviderHTTPServer(_ANY_PORT, _OneByteThenHold)
        client = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        try:
            server.closed_by_scenario = True
            with pytest.raises(OSError):
                server.get_request()
            client.settimeout(2.0)
            assert client.recv(1) == b"", "the client must read the end of the stream"
        finally:
            client.close()
            server.server_close()

    def test_a_server_closed_before_its_first_poll_ends_its_serve_thread_with_no_error(self, monkeypatch):
        """The port can close before the serve thread registers the socket for
        its poll. The serve loop then stops with an error of the closed socket.
        The thread must end with no error: a thread of a provider port never
        raises."""
        thread_errors: list[str] = []
        monkeypatch.setattr(threading, "excepthook", lambda args: thread_errors.append(repr(args.exc_value)))
        server = server_module._ProviderHTTPServer(_ANY_PORT, _OneByteThenHold)
        server.close_port()
        serve_thread = server.serve()
        serve_thread.join(timeout=2.0)
        assert not serve_thread.is_alive(), "the serve thread of a closed server did not end"
        assert thread_errors == []

    def test_only_an_os_error_of_a_closed_server_is_dropped(self, capsys):
        """``close_port`` ends the socket of each worker thread, so the next
        read or write of the thread fails with an OSError. The server drops
        that error. It still prints each other error: an OSError before the
        close, and an error that is not an OSError."""
        server = server_module._ProviderHTTPServer(_ANY_PORT, _OneByteThenHold)
        try:
            _handle_error_of(server, OSError("an error before the close"))
            assert "OSError: an error before the close" in capsys.readouterr().err

            server.close_port()
            _handle_error_of(server, BrokenPipeError("the close ended this socket"))
            assert capsys.readouterr().err == ""

            _handle_error_of(server, TypeError("a defect of the handler"))
            assert "TypeError: a defect of the handler" in capsys.readouterr().err
        finally:
            server.server_close()


# ── what a scenario cannot close ─────────────────────────────────────────────


class TestWhatAScenarioCannotClose:
    def test_only_provider_ports_have_a_gate(self, sim):
        """A scenario can close a port only if the port has a gate. Each
        provider port has one. A port that is not of a provider has none: the
        control port, each cache simulator port, each RESP proxy port and the
        RESP control port."""
        gated = set(sim["server"].control.port_gates)
        provider_ports = set(sim["registry"].ports())
        assert gated == provider_ports, (
            f"{len(gated)} ports have a gate, and the registry has {len(provider_ports)} provider ports. "
            f"Provider ports with no gate: {sorted(provider_ports - gated)}. "
            f"Gates of a port that is not of a provider: {sorted(gated - provider_ports)}"
        )
        not_of_a_provider = {CONTROL_PORT, RESP_CONTROL_PORT, *CACHE_SIM_PORTS.values(), *RESP_PROXY_PORTS.values()}
        assert not gated & not_of_a_provider, f"a scenario can close {sorted(gated & not_of_a_provider)}"

    def test_a_field_that_acts_on_one_request_is_still_refused_with_the_mode(self, sim):
        """``latency_ms`` delays the reply to one request, and a closed port
        receives no request. So the control API refuses the two together, for
        an ``http`` provider as for a gRPC provider."""
        control = sim["control"]
        status, body = _set(control, KEY, mode="port_closed", latency_ms=300)
        ports = (_connect(HTTP_PORT), _connect(WS_PORT))
        assert status == 400, body
        assert "latency_ms=300 cannot apply with mode 'port_closed'" in body["error"], body
        assert KEY in body["error"], "the refusal must name the provider"
        stored = _scenario(control, KEY)
        assert (stored["mode"], stored["latency_ms"]) == ("success", 0), "a refused block must not be stored"
        assert ports == (0, 0), "a refused block must leave both ports open"


# ── an http port is closed ───────────────────────────────────────────────────


class TestAnHttpPortIsClosed:
    @pytest.mark.parametrize(
        "key, port",
        [
            pytest.param("eth-sim:1", port_of("eth-sim", "1"), id="jsonrpc"),
            pytest.param("lava-sim-rest:1", port_of("lava-sim-rest", "1", "rest", "http"), id="rest"),
            pytest.param("lava-sim-tm:1", port_of("lava-sim-tm", "1", "tendermintrpc", "http"), id="tendermintrpc"),
        ],
    )
    def test_a_new_connection_is_refused_on_the_line_after_the_call(self, sim, key, port):
        """One provider of each ``http`` handler class. The port accepts first,
        so "refused" is not also true for a port that never listened."""
        assert _connect(port) == 0, f"port {port} must accept with no fault set"
        status, body = _set(sim["control"], key, mode="port_closed")
        refused = _connect(port)
        assert status == 200, body
        assert body["applied"][key] == {"mode": "port_closed"}
        assert refused == errno.ECONNREFUSED, (
            f"port {port} must refuse a connection when the control call has returned; "
            f"connect_ex gave {refused} ({errno.errorcode.get(refused, 'accepted')})"
        )

    def test_the_closed_provider_stores_no_row(self, sim):
        """A closed port receives no request, so the history of its provider
        gets no row. Provider 2 is ``down`` and gets the same request, and its
        history gets one row. That control shows that the history sees a
        request that arrives."""
        control = sim["control"]
        status, body = _post(
            f"{control}/scenario", {"providers": {KEY: {"mode": "port_closed"}, "eth-sim:2": {"mode": "down"}}}
        )
        assert status == 200, body

        with pytest.raises(urllib.error.URLError) as refused:
            _post(f"http://127.0.0.1:{HTTP_PORT}", _CHAIN_ID_CALL)
        assert isinstance(refused.value.reason, ConnectionRefusedError), refused.value
        status, _ = _post(f"http://127.0.0.1:{OTHER_HTTP_PORT}", _CHAIN_ID_CALL)
        assert status == 503, "the down provider answers on an open connection"

        assert _rows(control, "eth-sim", "1") == []
        assert _rows(control, "eth-sim", "2") == [("*", "down", OTHER_HTTP_PORT)]

    @pytest.mark.parametrize(
        "block, closed, still_open",
        [
            pytest.param({"transports": ["http"]}, HTTP_PORT, WS_PORT, id="transports-http"),
            pytest.param({"ports": [HTTP_PORT]}, HTTP_PORT, WS_PORT, id="ports-http"),
            pytest.param({"transports": ["ws"]}, WS_PORT, HTTP_PORT, id="transports-ws"),
        ],
    )
    def test_a_filter_closes_only_the_endpoint_that_it_names(self, sim, block, closed, still_open):
        status, body = _set(sim["control"], KEY, mode="port_closed", **block)
        ports = (_connect(closed), _connect(still_open))
        assert status == 200, body
        assert ports == (errno.ECONNREFUSED, 0), f"{block}: port {closed} must refuse and port {still_open} must accept"
        status, reply = _post(f"http://127.0.0.1:{OTHER_HTTP_PORT}", _CHAIN_ID_CALL)
        assert status == 200 and "result" in reply, "provider 2 must answer"

    def test_with_no_filter_the_http_port_and_the_ws_port_close(self, sim):
        status, body = _set(sim["control"], KEY, mode="port_closed")
        ports = (_connect(HTTP_PORT), _connect(WS_PORT))
        assert status == 200, body
        assert ports == (errno.ECONNREFUSED, errno.ECONNREFUSED)

    def test_a_request_in_flight_ends_and_the_control_call_does_not_wait_for_it(self, sim):
        """The provider hangs first, so one request sits in the simulator for
        up to 30 seconds. The close ends the connection of that request at
        once, and the control call does not wait for the handler. The row of
        the request stays as the simulator wrote it."""
        control = sim["control"]
        status, body = _set(control, KEY, mode="hang")
        assert status == 200, body
        outcome: dict = {}

        def held_request() -> None:
            try:
                outcome["reply"] = _post(f"http://127.0.0.1:{HTTP_PORT}", _CHAIN_ID_CALL, timeout=20)
            except Exception as exc:  # the test reads how the request ended
                outcome["error"] = exc

        caller = threading.Thread(target=held_request, daemon=True)
        caller.start()
        the_held_row = [("eth_chainId", "hang", HTTP_PORT)]
        deadline = time.monotonic() + 5.0
        while _rows(control, "eth-sim", "1") != the_held_row and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _rows(control, "eth-sim", "1") == the_held_row, "the provider must hold the request before the close"

        started = time.monotonic()
        status, body = _set(control, KEY, mode="port_closed")
        took = time.monotonic() - started
        refused = _connect(HTTP_PORT)
        caller.join(timeout=5.0)

        assert status == 200, body
        assert took < 2.0, f"the control call took {took:.2f}s: it must not wait for the handler that hangs"
        assert refused == errno.ECONNREFUSED
        assert not caller.is_alive(), "the request must end when the port closes, and not at its own timeout"
        assert "reply" not in outcome, f"a closed port sent a reply: {outcome.get('reply')}"
        assert isinstance(outcome["error"], _CONNECTION_ERRORS), repr(outcome["error"])
        assert _rows(control, "eth-sim", "1") == the_held_row, "the row stays, and the close adds no row"


# ── a WebSocket connection of a closed port ──────────────────────────────────


class TestAWsConnectionOfAClosedPort:
    def test_a_connection_with_a_subscription_ends_and_its_entry_leaves_the_registry(self, sim):
        """The connection ends as when a node dies: the client reads the end of
        the stream, or a reset, and no close frame. The entry of the
        subscription leaves the registry when the thread of the connection
        ends. The control call does not wait for that, so the test polls."""
        control = sim["control"]
        with WsClient("127.0.0.1", WS_PORT, "/ws") as client:
            client.send_json(_SUBSCRIBE)
            sub_id = client.recv_json(timeout=2.0)["result"]
            assert _subscriptions(control) == [(sub_id, "eth-sim", "1")]

            status, body = _set(control, KEY, mode="port_closed", transports=["ws"])
            refused = _connect(WS_PORT)
            assert status == 200, body
            assert refused == errno.ECONNREFUSED
            received = _bytes_until_the_end(client.sock, timeout=2.0)
        assert received == b"", f"the connection must end with no close frame; the client read {received!r}"

        deadline = time.monotonic() + 2.0
        while _subscriptions(control) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _subscriptions(control) == [], "the subscription of a connection that ended is still in the registry"

    def test_a_connection_with_no_subscription_ends_too(self, sim):
        with WsClient("127.0.0.1", WS_PORT, "/ws") as client:
            status, body = _set(sim["control"], KEY, mode="port_closed", transports=["ws"])
            assert status == 200, body
            received = _bytes_until_the_end(client.sock, timeout=2.0)
        assert received == b"", f"the connection must end with no close frame; the client read {received!r}"


# ── the port opens again ─────────────────────────────────────────────────────


def _sweep_past_the_time_to_live(sim) -> None:
    """One pass of the scenario time-to-live sweep, one second after the time-to-live."""
    ttl_s = 900
    server_module._revert_stale_scenarios(sim["server"].control, ttl_s, time.time() + ttl_s + 1)


# Each way to open a port that ``mode="port_closed"`` closed. A control call
# gives (status, body). The sweep answers nobody, so it gives None.
_WAYS_TO_OPEN = {
    "mode=success": lambda sim: _set(sim["control"], KEY, mode="success"),
    "mode=down": lambda sim: _set(sim["control"], KEY, mode="down"),
    "POST /reset": lambda sim: _post(f"{sim['control']}/reset", {}),
    "POST /reset with the pool": lambda sim: _post(f"{sim['control']}/reset", {"pool": "eth-sim"}),
    "POST /reset/all": lambda sim: _post(f"{sim['control']}/reset/all", {}),
    "the time-to-live sweep": _sweep_past_the_time_to_live,
}


def _close_both_ports(control: str) -> None:
    status, body = _set(control, KEY, mode="port_closed")
    ports = (_connect(HTTP_PORT), _connect(WS_PORT))
    assert status == 200, body
    assert ports == (errno.ECONNREFUSED, errno.ECONNREFUSED), "both ports must be closed before they can open again"


class TestThePortOpensAgain:
    @pytest.mark.parametrize("way", list(_WAYS_TO_OPEN))
    def test_each_way_opens_both_ports_on_the_line_after_the_call(self, sim, way):
        """Each way leaves ``port_closed``, so each way opens the ports. With
        ``mode="down"`` the provider then answers as ``down`` answers: HTTP 503
        on an open connection."""
        _close_both_ports(sim["control"])
        answer = _WAYS_TO_OPEN[way](sim)
        ports = (_connect(HTTP_PORT), _connect(WS_PORT))
        if answer is not None:
            assert answer[0] == 200, answer[1]
        assert ports == (0, 0), f"{way} must open both ports; connect_ex gave {ports}"
        status, _ = _post(f"http://127.0.0.1:{HTTP_PORT}", _CHAIN_ID_CALL)
        assert status == (503 if way == "mode=down" else 200)

    def test_a_port_that_opened_again_serves_the_same_provider(self, sim):
        """A port that opened again has a new server object. That server needs
        the listener of the first one: the listener holds the provider, and the
        ``ws`` listener holds the subscription registry of the simulator."""
        control = sim["control"]
        _close_both_ports(control)
        status, body = _set(control, KEY, mode="success")
        assert status == 200, body

        status, reply = _post(f"http://127.0.0.1:{HTTP_PORT}", _CHAIN_ID_CALL)
        assert status == 200 and "result" in reply, reply
        assert _rows(control, "eth-sim", "1") == [("eth_chainId", "success", HTTP_PORT)]

        with WsClient("127.0.0.1", WS_PORT, "/ws") as client:
            client.send_json(_SUBSCRIBE)
            sub_id = client.recv_json(timeout=2.0)["result"]
            entries = _subscriptions(control)
        assert entries == [(sub_id, "eth-sim", "1")]

    def test_closing_and_opening_ten_times_keeps_working(self, sim):
        control = sim["control"]
        for round_number in range(10):
            status, body = _set(control, KEY, mode="port_closed")
            ports = (_connect(HTTP_PORT), _connect(WS_PORT))
            assert (status, ports) == (200, (errno.ECONNREFUSED, errno.ECONNREFUSED)), (round_number, body)
            status, body = _set(control, KEY, mode="success")
            ports = (_connect(HTTP_PORT), _connect(WS_PORT))
            assert (status, ports) == (200, (0, 0)), (round_number, body)

    def test_a_close_and_an_open_at_the_same_moment_both_answer_200(self, sim):
        """Two callers start together, 20 times: one closes the ports and one
        opens them. Each caller gets the answer for its own write, so both get
        200. After each round the ports are in the state of the stored mode."""
        control = sim["control"]
        writes = {
            "close": lambda: _set(control, KEY, mode="port_closed"),
            "open": lambda: _set(control, KEY, mode="success"),
        }
        for round_number in range(20):
            replies = {}
            together = threading.Barrier(2)

            def call(name: str) -> None:
                together.wait(timeout=10)
                replies[name] = writes[name]()

            callers = [threading.Thread(target=call, args=(name,)) for name in writes]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join()

            assert sorted(replies) == sorted(writes), f"round {round_number}: a caller got no answer"
            for name, (status, body) in replies.items():
                assert status == 200, f"round {round_number}: the caller of {name!r} got {status}: {body}"
            stored = _scenario(control, KEY)["mode"]
            accepted = (_connect(HTTP_PORT) == 0, _connect(WS_PORT) == 0)
            assert accepted == (stored != "port_closed", stored != "port_closed"), (
                f"round {round_number}: the stored mode is {stored!r}, and the http port and the ws port "
                f"accept a connection: {accepted}"
            )

    def test_ready_leaves_the_closed_ports_out_and_stays_200(self, sim):
        """The readiness probe of the pod reads /ready. A port that a scenario
        closed must not fail it: the pod would leave its Service, and each
        router would lose each provider."""
        control = sim["control"]
        _, baseline = _get(f"{control}/ready")
        assert baseline["closed_by_scenario"] == []
        _close_both_ports(control)
        ready_status, ready = _get(f"{control}/ready")
        assert ready_status == 200, ready
        assert ready["closed_by_scenario"] == sorted([HTTP_PORT, WS_PORT])
        assert ready["expected"] == baseline["expected"] - 2, "exactly the two closed ports leave the check"
        assert ready["listening"] == ready["expected"]

        status, body = _post(f"{control}/reset", {})
        assert status == 200, body
        ready_status, ready = _get(f"{control}/ready")
        assert (ready_status, ready["closed_by_scenario"], ready["expected"]) == (200, [], baseline["expected"])


# ── stop() ───────────────────────────────────────────────────────────────────

# The simulator of the last test, on this file's own block of ports. The block
# is below the range that the kernel gives to client sockets.
_STOP_CONTROL = 29751
_STOP_HTTP, _STOP_WS = 28751, 28752
_STOP_ROWS = [
    (
        "stop-sim",
        "eth",
        "1",
        "StopPrimaryProvider1",
        False,
        "",
        (("jsonrpc", "http", _STOP_HTTP), ("jsonrpc", "ws", _STOP_WS)),
    ),
]


def test_stop_reaches_a_server_that_opened_again():
    """A port that closed and opened again has a new server object. ``stop()``
    must stop that server too. If ``stop()`` stops only the first server of the
    port, the port answers after the stop."""
    shipped = topology.TOPOLOGY
    topology.TOPOLOGY = _STOP_ROWS
    try:
        simulator = server_module.SimulatorServer(
            host="127.0.0.1", control_port=_STOP_CONTROL, scenario_ttl_s=0, cache_ports={}, resp_proxy_ports={}
        )
    finally:
        topology.TOPOLOGY = shipped
    control = f"http://127.0.0.1:{_STOP_CONTROL}"
    simulator.start()
    try:
        simulator.wait_ready(20.0)
        status, body = _set(control, "stop-sim:1", mode="port_closed")
        assert status == 200, body
        status, body = _set(control, "stop-sim:1", mode="success")
        assert status == 200, body
        status, reply = _post(f"http://127.0.0.1:{_STOP_HTTP}", _CHAIN_ID_CALL)
        assert status == 200 and "result" in reply, "the port that opened again must answer before the stop"
    finally:
        simulator.stop()
    with pytest.raises((*_CONNECTION_ERRORS, TimeoutError)):
        _post(f"http://127.0.0.1:{_STOP_HTTP}", _CHAIN_ID_CALL, timeout=1.5)
    assert (_connect(_STOP_HTTP), _connect(_STOP_WS)) == (errno.ECONNREFUSED, errno.ECONNREFUSED)
