"""The proxy that controls the ROUTER's access to its store.

Three things this file proves, and the third is the one that has gone wrong
before in this project:

1. With the gate open, bytes reach the store and come back.
2. Each of the two cut-off kinds does what its name says — one holds the
   connection, the other ends it.
3. **Cutting off reaches connections that already exist.** A client holds a pool
   of open connections. Acting only on the next one leaves the router using the
   ones it already had, and the cut-off does nothing while appearing to work.

The same shape was measured on the secondary cache: a staged change travelled to
a new pod while the router kept reading its established connection to the old
one, and the test read the answer of a process it never staged into.
"""

from __future__ import annotations

import socket
import socketserver
import threading
import time

import pytest

from provider_simulator import resp_proxy as resp_proxy_module
from provider_simulator.resp_proxy import ERROR, FORWARDING, TIMEOUT, RespProxy, UnknownCutOffKind
from provider_simulator.resp_store import RespStore, RespStoreError
from tests.resp_fake_store import FakeRespStore

# Long enough for a held read to be unambiguous, short enough not to slow a
# suite. The proxy re-checks its gate every 50ms, so this is several polls.
_HOLD_SECONDS = 0.4


class _ProxyRunner:
    """Runs one proxy on a free port for the length of a test."""

    def __init__(self, proxy: RespProxy) -> None:
        self.proxy = proxy

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                proxy.serve(self.request)

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = _Server(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def store():
    with FakeRespStore() as fake:
        yield fake


@pytest.fixture
def proxy(store):
    return RespProxy("127.0.0.1", store.port, name="primary")


@pytest.fixture
def runner(proxy):
    running = _ProxyRunner(proxy)
    yield running
    running.stop()


@pytest.fixture
def through_proxy(runner):
    """A reader that reaches the store the way the ROUTER does — via the proxy."""
    return RespStore("127.0.0.1", runner.port, timeout=1.0)


# ── open ──────────────────────────────────────────────────────────────────────


def test_a_new_proxy_is_forwarding(proxy):
    assert proxy.state() == FORWARDING


def test_bytes_reach_the_store_and_come_back(store, through_proxy):
    store.put("sr:chaintip:ETH1", "20000000")
    assert through_proxy.get("sr:chaintip:ETH1") == "20000000"


def test_the_store_records_the_command_the_proxy_carried(store, through_proxy):
    """Proves the proxy forwarded rather than answering something itself."""
    through_proxy.ping()
    assert ["PING"] in store.commands


# ── cut off: error ────────────────────────────────────────────────────────────


def test_an_error_cut_off_stops_a_new_connection_reaching_the_store(store, proxy, through_proxy):
    store.put("sr:chaintip:ETH1", "20000000")
    proxy.cut_off(ERROR)
    with pytest.raises(RespStoreError):
        through_proxy.get("sr:chaintip:ETH1")


def test_an_error_cut_off_closes_a_connection_that_already_exists(store, runner, proxy):
    """The trap this whole file is written around.

    The connection is opened and used BEFORE the cut-off, so it is exactly the
    established connection a router would be holding in its pool.

    **What this proves, exactly.** The connection ends. It does NOT prove which
    of the proxy's two mechanisms ended it — the sweep over live connections in
    ``cut_off``, or the pump loop noticing the gate on its next poll. Measured:
    with the sweep removed this test still passes, because the poll closes the
    connection within 50ms and the read below waits a second.

    That is worth saying rather than leaving implied. The test below is the one
    that proves the sweep itself ran; this one proves the outcome a router would
    see. Both are wanted, and neither stands in for the other.
    """
    conn = socket.create_connection(("127.0.0.1", runner.port), timeout=1.0)
    try:
        conn.sendall(b"*1\r\n$4\r\nPING\r\n")
        assert conn.recv(64) == b"+PONG\r\n"

        proxy.cut_off(ERROR)

        conn.settimeout(1.0)
        # An ended connection reads as empty, or raises. Both say it is gone;
        # neither is a reply.
        try:
            assert conn.recv(64) == b""
        except OSError:
            pass
    finally:
        conn.close()


def test_an_error_cut_off_counts_the_connections_it_closed(runner, proxy):
    """How a test proves the cut-off reached what already existed, rather than
    only the next connection."""
    conn = socket.create_connection(("127.0.0.1", runner.port), timeout=1.0)
    try:
        conn.sendall(b"*1\r\n$4\r\nPING\r\n")
        conn.recv(64)
        assert proxy.counters()["closed_by_cut_off"] == 0
        proxy.cut_off(ERROR)
        assert proxy.counters()["closed_by_cut_off"] >= 1
    finally:
        conn.close()


# ── cut off: timeout ──────────────────────────────────────────────────────────


def test_a_timeout_cut_off_holds_a_new_connection_open_and_sends_nothing(store, runner, proxy):
    """Held, not closed. The difference is the whole reason both kinds exist.

    The router counts a store it could not finish with separately from one it
    could not reach, so a proxy that closed here would make one of those two
    counters untestable.
    """
    proxy.cut_off(TIMEOUT)
    conn = socket.create_connection(("127.0.0.1", runner.port), timeout=1.0)
    try:
        conn.sendall(b"*1\r\n$4\r\nPING\r\n")
        conn.settimeout(_HOLD_SECONDS)
        with pytest.raises((TimeoutError, socket.timeout)):
            conn.recv(64)
    finally:
        conn.close()


def test_a_timeout_cut_off_never_opens_a_connection_to_the_store(store, runner, proxy):
    """ "The router cannot reach the store" has to be true of the store as well.

    Opening the upstream eagerly would leave a real connection to the store
    sitting there while the test believed the router was cut off from it.
    """
    proxy.cut_off(TIMEOUT)
    conn = socket.create_connection(("127.0.0.1", runner.port), timeout=1.0)
    try:
        conn.sendall(b"*1\r\n$4\r\nPING\r\n")
        time.sleep(_HOLD_SECONDS)
        assert store.commands == []
    finally:
        conn.close()


def test_a_timeout_cut_off_stops_a_connection_that_already_exists(store, runner, proxy):
    """The existing-connection case again, for the other kind."""
    conn = socket.create_connection(("127.0.0.1", runner.port), timeout=1.0)
    try:
        conn.sendall(b"*1\r\n$4\r\nPING\r\n")
        assert conn.recv(64) == b"+PONG\r\n"

        proxy.cut_off(TIMEOUT)

        conn.sendall(b"*1\r\n$4\r\nPING\r\n")
        conn.settimeout(_HOLD_SECONDS)
        with pytest.raises((TimeoutError, socket.timeout)):
            conn.recv(64)
    finally:
        conn.close()


# ── restore ───────────────────────────────────────────────────────────────────


def test_restoring_lets_a_new_connection_through_again(store, proxy, through_proxy):
    store.put("sr:chaintip:ETH1", "20000000")
    proxy.cut_off(ERROR)
    with pytest.raises(RespStoreError):
        through_proxy.get("sr:chaintip:ETH1")
    proxy.restore()
    assert through_proxy.get("sr:chaintip:ETH1") == "20000000"


def test_restoring_resumes_a_connection_held_by_a_timeout_cut_off(store, runner, proxy):
    """A held connection is still there, so it carries on rather than being
    re-opened. That is what makes the held kind a pause and not a failure."""
    proxy.cut_off(TIMEOUT)
    conn = socket.create_connection(("127.0.0.1", runner.port), timeout=2.0)
    try:
        conn.sendall(b"*1\r\n$4\r\nPING\r\n")
        time.sleep(0.15)
        proxy.restore()
        assert conn.recv(64) == b"+PONG\r\n"
    finally:
        conn.close()


def test_restore_reports_the_state_it_returned_to(proxy):
    proxy.cut_off(ERROR)
    assert proxy.restore() == FORWARDING
    assert proxy.state() == FORWARDING


# ── refusing a kind nobody can act on ─────────────────────────────────────────


def test_an_unknown_cut_off_kind_raises_rather_than_defaulting(proxy):
    """A typo must fail loudly. Allowed to fall through to a default it would
    silently become one kind while the test believed it asked for the other, and
    the test would pass having measured the wrong half."""
    with pytest.raises(UnknownCutOffKind):
        proxy.cut_off("timout")
    assert proxy.state() == FORWARDING


def test_asking_to_cut_off_with_forwarding_is_refused(proxy):
    """A real state, and still not a cut-off. The mistake it hides — meaning to
    restore and calling cut_off — would leave the store reachable while the test
    waited for a failure that never comes."""
    with pytest.raises(UnknownCutOffKind):
        proxy.cut_off(FORWARDING)


# ── what a test can read about the proxy ──────────────────────────────────────


def test_the_proxy_reports_its_target_and_state(proxy, store):
    reported = proxy.as_dict()
    assert reported["name"] == "primary"
    assert reported["state"] == FORWARDING
    assert reported["target"] == f"127.0.0.1:{store.port}"


def test_counters_move_when_a_connection_is_carried(store, through_proxy, proxy):
    assert proxy.counters()["accepted"] == 0
    through_proxy.ping()
    assert proxy.counters()["accepted"] == 1
    assert proxy.counters()["forwarded"] == 1


def test_counters_can_be_zeroed_without_changing_the_gate(proxy, through_proxy):
    through_proxy.ping()
    proxy.cut_off(TIMEOUT)
    proxy.reset_counters()
    assert proxy.counters()["accepted"] == 0
    assert proxy.state() == TIMEOUT


def test_a_held_connection_with_a_pending_request_does_not_burn_a_core(store, runner, proxy):
    """The held path must WAIT, not spin.

    ``_peer_gone`` blocks for the poll interval only while nothing is pending.
    The normal case here is the opposite: the router has already sent a request
    and is waiting for a reply, so the socket is readable, the peek returns at
    once, and a loop with no wait would spin on bytes it is deliberately not
    reading. One held connection would consume a whole core, and a router pool
    holds several.

    Measured as processor time rather than by reading the loop, because "there
    is a sleep in the code" and "this connection is not burning a core" are
    different claims and only the second one matters. The threshold is loose on
    purpose: a spin costs roughly the whole wall-clock period, and sleeping
    costs almost nothing, so anything in between still fails.
    """
    proxy.cut_off(TIMEOUT)
    conn = socket.create_connection(("127.0.0.1", runner.port), timeout=1.0)
    try:
        conn.sendall(b"*1\r\n$4\r\nPING\r\n")
        time.sleep(0.1)  # let the held loop get going
        before = time.process_time()
        time.sleep(_HOLD_SECONDS)
        spent = time.process_time() - before
    finally:
        conn.close()
    assert spent < _HOLD_SECONDS / 2, (
        f"the held path used {spent:.3f}s of processor time over {_HOLD_SECONDS}s of "
        f"waiting; it is spinning on a readable socket instead of waiting"
    )


def test_the_counters_do_not_claim_to_partition_accepted(store, through_proxy, proxy):
    """One ordinary connection increments three of them, so they are not parts
    of a whole. An earlier version named one of them 'held', which read as a
    live state while counting every connection the proxy ever took on."""
    through_proxy.ping()
    counters = proxy.counters()
    assert counters["accepted"] == 1
    assert counters["carried"] == 1
    assert counters["forwarded"] == 1
    assert counters["closed_by_cut_off"] == 0


def test_a_reply_larger_than_one_socket_buffer_arrives_whole(proxy, runner):
    """The write deadline must be a write deadline, not the gate-poll interval.

    A socket carries one timeout. The poll interval and the send deadline were
    the same value, so a reply that could not be written in 50 milliseconds
    raised and the connection was torn down. Measured before the fix: a 4 MB
    reply arrived truncated at about 540 KB, and the cut moved when the interval
    moved, which is what proved the cause.

    A cache reply can be this large — a full block, or a page of logs — so this
    is the ordinary case rather than an extreme one. The client waits before
    reading, which is what makes the proxy's write block.
    """
    payload = b"x" * (4 * 1024 * 1024)
    big = b"$" + str(len(payload)).encode() + b"\r\n" + payload + b"\r\n"

    class _Slow(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.recv(65536)
            self.request.sendall(big)
            time.sleep(2)

    class _Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    upstream = _Server(("127.0.0.1", 0), _Slow)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    slow_proxy = RespProxy("127.0.0.1", upstream.server_address[1])
    slow_runner = _ProxyRunner(slow_proxy)
    try:
        conn = socket.create_connection(("127.0.0.1", slow_runner.port), timeout=15)
        try:
            conn.sendall(b"*1\r\n$4\r\nPING\r\n")
            time.sleep(1.0)  # make the proxy's write block on a full buffer
            conn.settimeout(10)
            received = 0
            while received < len(big):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                received += len(chunk)
        finally:
            conn.close()
    finally:
        slow_runner.stop()
        upstream.shutdown()
    assert received == len(big), f"the reply was cut short: {received} of {len(big)} bytes"


# ── a slow client must not become everybody's wait ────────────────────────────

# How long a control call may take while some client is being slow. The ticket
# measured 0.00 s with nothing unusual happening and 27.9 s with one stuck
# client, so anything between those two separates the two worlds. A second is
# far above the real cost and far below the fault.
_CONTROL_CALL_BOUND_SECONDS = 1.0

# A write deadline short enough for a suite. The real one is thirty seconds,
# which is a delivery budget for a 4 MB cache reply and not a number a test
# should sit through. Only the WAIT changes; the ordering under test does not.
_SHORT_SEND_DEADLINE = 2.0


class _RecordingSocket:
    """A socket that remembers when each write began and ended.

    ``RespProxy.serve`` takes a socket, so a test can hand it one of these. No
    production code changes and nothing private is reached into.

    The end is stamped in a ``finally``. A write that ends by RAISING has still
    ended, and the first version of this recorder stamped only success — so a
    write that hit its deadline looked like it was still running for ever, and
    it reported a violation against correct code.
    """

    def __init__(self, wrapped: socket.socket) -> None:
        self._wrapped = wrapped
        self._lock = threading.Lock()
        self.started = 0
        self.finished = 0

    def sendall(self, data):  # noqa: ANN001, ANN201 - mirrors socket.sendall
        with self._lock:
            self.started += 1
        try:
            return self._wrapped.sendall(data)
        finally:
            with self._lock:
                self.finished += 1

    def in_flight(self) -> int:
        with self._lock:
            return self.started - self.finished

    def __getattr__(self, name):  # noqa: ANN001, ANN204 - pass the rest through
        return getattr(self._wrapped, name)


def _wait_until(condition, why: str, limit: float = 5.0) -> None:
    """Block until ``condition()`` is true, or fail saying what never happened.

    A fixed sleep only ESTIMATES when the proxy reached its blocked write. This
    asks. Without it a loaded machine runs the control call before the write
    ever started, and the test passes having exercised nothing.
    """
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"{why} within {limit}s")


def _stuck_delivery(monkeypatch):
    """A proxy with one delivery that cannot finish, and the recorder watching it.

    An upstream that answers with more than the socket pair can swallow, and a
    client that never reads. Returns the proxy, the recorder and a closer.

    A socket pair rather than a listening server, because ``serve`` takes a
    socket and that is what lets a test hand it a recorder.
    """
    monkeypatch.setattr(resp_proxy_module, "_SEND_TIMEOUT_SECONDS", _SHORT_SEND_DEADLINE)

    payload = b"x" * (8 * 1024 * 1024)
    big = b"$" + str(len(payload)).encode() + b"\r\n" + payload + b"\r\n"

    class _BigReply(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.recv(65536)
            try:
                self.request.sendall(big)
            except OSError:
                # Teardown ends the connection while this write is deliberately
                # blocked, so the disconnect is expected. Left unhandled it
                # surfaces as a thread-exception warning, or as a failure where
                # warnings are fatal.
                pass

    class _Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    upstream = _Server(("127.0.0.1", 0), _BigReply)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    proxy = RespProxy("127.0.0.1", upstream.server_address[1])

    router_side, client_side = socket.socketpair()
    # A small receive buffer on the client makes the delivery block sooner, so
    # the window these tests need is reached in well under a second.
    client_side.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
    recorder = _RecordingSocket(router_side)
    threading.Thread(target=proxy.serve, args=(recorder,), daemon=True).start()
    client_side.sendall(b"*1\r\n$4\r\nPING\r\n")
    _wait_until(lambda: recorder.in_flight() > 0, "the delivery never started")

    def close() -> None:
        client_side.close()
        router_side.close()
        upstream.shutdown()

    return proxy, recorder, close


def test_a_client_that_stopped_reading_does_not_freeze_the_control_calls(monkeypatch):
    """Reading the proxy's state is a question about the proxy, not about its
    slowest connection.

    Scenario. One client asks for something large and then stops reading. Its
    reply cannot be written, so the delivery sits there until the write deadline
    gives up on it. Meanwhile a test asks the proxy a simple question, such as
    what its gate is set to.

    What is tested. That the question is answered anyway. The proxy has one
    lock, and the delivery must not hold it across the write. When it did, every
    control path waited behind the slowest client for the whole deadline —
    reading the state, reading the counters, restoring, and accepting the next
    connection.

    ``cut_off`` is the deliberate exception and is not covered here. It waits
    for a delivery already in progress, because it must not report the gate
    closed while bytes are still crossing it. That promise has its own test,
    ``test_cutting_off_does_not_return_while_a_write_is_still_running``.

    Setup. ``_stuck_delivery`` arranges a write that cannot finish and does not
    return until the recorder confirms it started. That confirmation is the
    point: a fixed sleep only estimates it, and on a loaded machine the control
    call would run first and the test would pass having exercised nothing.

    Assertions and why. The baseline first, so a failure below is about the
    stuck client rather than about the instrument. Then the control call on its
    own thread with a bounded wait — answered inside it means a slow client
    costs other callers nothing. The thread is used so a broken proxy costs the
    suite one second rather than the whole deadline.

    How to read a failure. "did not answer" means the lock is held across the
    write — look at the delivery path in ``resp_proxy``.
    """
    proxy, recorder, close = _stuck_delivery(monkeypatch)
    answer: dict[str, object] = {}

    def ask_the_proxy_what_its_gate_is() -> None:
        answer["state"] = proxy.state()

    try:
        assert recorder.in_flight() > 0, "no delivery is running; nothing is being measured"

        baseline = time.monotonic()
        proxy.state()
        baseline_took = time.monotonic() - baseline

        asking = threading.Thread(target=ask_the_proxy_what_its_gate_is, daemon=True)
        asking.start()
        asking.join(_CONTROL_CALL_BOUND_SECONDS)
        answered = not asking.is_alive()
    finally:
        close()

    assert answered, (
        f"a control call did not answer within {_CONTROL_CALL_BOUND_SECONDS}s "
        f"while one client had stopped reading its reply. The proxy's lock is "
        f"held across the delivery, so every caller waits for the slowest "
        f"client (the same call with nothing stuck: {baseline_took:.4f}s)"
    )
    assert answer["state"] == FORWARDING, f"the control call answered but with the wrong gate: {answer['state']!r}"


def test_cutting_off_does_not_return_while_a_write_is_still_running(monkeypatch):
    """After ``cut_off`` returns, nothing further is written. Including a write
    that was already running when it was called.

    Scenario. A client asks for something large and stops reading, so the
    delivery cannot finish. A test cuts the router off while it is still running.

    What is tested. That ``cut_off`` does not answer until the delivery has
    stopped. A call that returned early would tell its caller the router can no
    longer reach the store while bytes were still moving, and every measurement
    in the cut-off tests rests on that call meaning what it says.

    **Say what the contract is NOT.** It is not "no bytes cross a closed gate".
    Bytes in a running write DO cross — they cross before ``cut_off`` returns,
    because it waits for them. The promise is about what is true once it has
    answered.

    Assertions and why. First that a write was actually running when the cut-off
    was called — without it the test proves nothing and would pass on anything.
    Then that none is running once ``cut_off`` has returned.

    How to read a failure. "still running" means the delivery no longer holds
    the cut-off back, which is what happens when the gate check and the write
    stop being one step.
    """
    proxy, recorder, close = _stuck_delivery(monkeypatch)
    try:
        running_before = recorder.in_flight()
        assert running_before > 0, (
            "no write was running when the cut-off was called, so this test "
            "proves nothing — the arrangement failed to fill the socket buffers"
        )
        proxy.cut_off(TIMEOUT)
        running_after = recorder.in_flight()
    finally:
        close()

    assert running_after == 0, (
        f"cut_off returned while {running_after} write(s) were still running. "
        f"It must not answer until the delivery has stopped, or a caller is "
        f"told the gate is closed while bytes are still crossing it "
        f"(running when it was called: {running_before})"
    )


def test_restoring_while_a_cut_off_waits_does_not_let_it_claim_a_closed_gate(monkeypatch):
    """Two control calls that both move the gate must not interleave.

    Scenario. A cut-off is waiting for a delivery to finish. While it waits,
    something else restores the gate. Both calls return.

    What is tested. That the cut-off does not answer "cut off" on a gate that
    has since been reopened. It waits with the lock released, so another gate
    move can land inside it — and a caller told the gate is closed, on a gate
    that is open, has been told something untrue. Writes go on happening after
    that answer.

    Assertions and why. First that the cut-off really was waiting — without it
    the two calls ran in sequence and nothing was tested. Then that the gate
    matches what the cut-off returned.

    How to read a failure. A returned kind that does not match the gate means
    gate moves are interleaving and have to be serialised against each other.
    Reads and the accept path must NOT be serialised with them; that is the
    fault this whole section exists to fix.
    """
    proxy, recorder, close = _stuck_delivery(monkeypatch)
    when: dict[str, float] = {}

    def cut_it_off() -> None:
        proxy.cut_off(TIMEOUT)
        when["cut_off_returned"] = time.monotonic()

    try:
        cutting = threading.Thread(target=cut_it_off, daemon=True)
        cutting.start()
        cutting.join(0.3)
        assert cutting.is_alive(), (
            "the cut-off returned before the delivery finished, so it never "
            "waited and this test proves nothing about interleaving"
        )

        # This is the interleaving attempt. Serialised, it cannot complete until
        # the cut-off has. Not serialised, it returns straight away -- and the
        # cut-off then answers "cut off" on a gate this call has reopened.
        proxy.restore()
        when["restore_returned"] = time.monotonic()

        cutting.join(_SHORT_SEND_DEADLINE + 3.0)
        assert not cutting.is_alive(), "the cut-off never returned"
    finally:
        close()

    assert when["restore_returned"] >= when["cut_off_returned"], (
        f"the restore completed {when['cut_off_returned'] - when['restore_returned']:.3f}s "
        f"BEFORE the cut-off returned, so it landed inside the cut-off's wait. "
        f"The cut-off then answered on a gate the restore had already reopened, "
        f"and writes continue after that answer. Gate moves must be serialised "
        f"against each other -- but never against reads or the accept path, "
        f"which is the fault this section exists to fix"
    )


def test_a_held_connection_is_released_when_its_client_goes_away(store, runner, proxy):
    """The peer check must see a hang-up with bytes still unread.

    A held connection normally has an unread request sitting in it — that is
    what the held path creates. The first version peeked and called the peer
    gone only on an empty read, so those same bytes came back for ever and the
    answer was always "still here". The guard was blind in the only case it saw,
    and each abandoned connection kept a thread and a file descriptor until the
    gate was restored or the process ended.
    """
    proxy.cut_off(TIMEOUT)
    conn = socket.create_connection(("127.0.0.1", runner.port), timeout=5)
    conn.sendall(b"*1\r\n$4\r\nPING\r\n")
    time.sleep(0.2)
    assert proxy.live_connections() == 1, "the connection should be held while the client is there"
    conn.close()

    deadline = time.monotonic() + 3.0
    while proxy.live_connections() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert proxy.live_connections() == 0, "a held connection outlived the client that abandoned it"


def test_a_held_connection_that_sent_nothing_is_also_released(store, runner, proxy):
    """The case the old check DID handle, kept so the fix cannot lose it."""
    proxy.cut_off(TIMEOUT)
    conn = socket.create_connection(("127.0.0.1", runner.port), timeout=5)
    time.sleep(0.2)
    conn.close()

    deadline = time.monotonic() + 3.0
    while proxy.live_connections() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert proxy.live_connections() == 0


# ── reclaiming a connection the client abandoned without closing ──────────────
#
# A client can stop using a connection without closing it. The proxy then sits
# on an ESTABLISHED socket whose peer will never speak again, and nothing in the
# carry loop ever gives it up: the loop only ends when a side closes, errors, or
# the gate shuts. Measured on the local cluster — a cut-off left connections
# behind at two every ten seconds, and they were still there an hour later,
# ESTABLISHED at both ends, while fresh connections came and went normally.
#
# A real Redis has ``timeout`` for exactly this. These five tests are that
# setting.
#
# Two of them have teeth against the reclaimer itself, and three are controls on
# the ways it could go wrong -- because a reclaimer that also takes connections
# somebody still wants is a worse bug than the leak it fixes. Said plainly
# because an earlier version of this comment called the controls the stronger
# half, and one of them passes against a reclaimer that does nothing at all.


def _idle_proxy(store, idle_seconds):
    """A proxy that gives up on a silent connection after ``idle_seconds``."""
    proxy = RespProxy("127.0.0.1", store.port, name="primary", idle_seconds=idle_seconds)
    return proxy, _ProxyRunner(proxy)


def _wait_for_live(proxy, want, timeout=5.0):
    deadline = time.monotonic() + timeout
    while proxy.live_connections() != want and time.monotonic() < deadline:
        time.sleep(0.02)
    return proxy.live_connections()


def test_a_connection_abandoned_without_being_closed_is_reclaimed(store):
    """The leak, in one test.

    The client stays alive and keeps the socket open. It simply never speaks
    again — which is what the router does to every connection whose operation
    timed out while it could not reach the store. Nothing closes, so no close
    can be noticed, and before this the proxy carried it for the life of the
    process.
    """
    proxy, runner = _idle_proxy(store, 0.6)
    try:
        conn = socket.create_connection(("127.0.0.1", runner.port), timeout=5)
        conn.sendall(b"*1\r\n$4\r\nPING\r\n")
        conn.recv(65536)
        assert _wait_for_live(proxy, 1) == 1, "the connection should be carried while it is in use"

        # The client does nothing further and does NOT close.
        assert _wait_for_live(proxy, 0) == 0, (
            "a connection whose client stopped speaking was carried past the idle limit; "
            f"counters={proxy.counters()}"
        )
        assert proxy.counters()["closed_when_idle"] == 1, f"the reclaim was not counted: {proxy.counters()}"
        conn.close()
    finally:
        runner.stop()


def test_a_connection_still_in_use_is_never_reclaimed(store):
    """The regression this fix could cause, guarded.

    Activity has to push the limit out. A reclaimer that counts from the
    connection's birth rather than from its last byte would close a connection
    in continuous use, and the router would see its store drop it mid-run.
    """
    proxy, runner = _idle_proxy(store, 1.5)
    try:
        conn = socket.create_connection(("127.0.0.1", runner.port), timeout=5)
        # Two things have to be true at once here, and getting one of them right
        # alone breaks the test in opposite directions.
        #
        # Each gap must sit well under the limit, or an overrun on a loaded
        # runner fails the test claiming the router dropped a live connection.
        # 0.35s against 1.5s is under a quarter of the budget; an earlier version
        # left 0.35s against 0.6s, which is more than half.
        #
        # And the gaps must TOTAL more than the limit, or a clock that never
        # resets is never caught: six gaps span 2.1s against a 1.5s limit. A
        # version of this test ran four gaps against a 2.0s limit and passed with
        # the reset deleted, which is the third time in this change that widening
        # a margin quietly removed what the test was for.
        for _ in range(6):
            conn.sendall(b"*1\r\n$4\r\nPING\r\n")
            assert conn.recv(65536), "the store stopped answering mid-test"
            time.sleep(0.35)
        assert proxy.live_connections() == 1, (
            "a connection in continuous use was reclaimed; the idle clock is not "
            f"being reset by traffic. counters={proxy.counters()}"
        )
        assert proxy.counters()["closed_when_idle"] == 0
        conn.close()
    finally:
        runner.stop()


def test_a_connection_idle_for_less_than_the_limit_is_kept_and_still_works(store):
    """Quiet is not abandoned, and the proof is that it still carries bytes.

    Counting it as live is the weaker claim — a connection can be in the set and
    already useless. This sends a command down the same socket after the quiet
    period and requires the store's answer to come back.
    """
    proxy, runner = _idle_proxy(store, 3.0)
    try:
        conn = socket.create_connection(("127.0.0.1", runner.port), timeout=5)
        conn.sendall(b"*1\r\n$4\r\nPING\r\n")
        conn.recv(65536)
        time.sleep(0.7)  # quiet, but inside the limit
        assert proxy.live_connections() == 1, "a connection inside the idle limit was reclaimed"

        conn.sendall(b"*1\r\n$4\r\nPING\r\n")
        assert conn.recv(65536), "the kept connection no longer reaches the store"
        conn.close()
    finally:
        runner.stop()


def test_a_connection_held_longer_than_the_idle_limit_survives_the_restore(store):
    """The quiet of a cut-off must not be counted against the connection.

    ``timeout`` holds the connection open and moves no bytes — that is the whole
    definition of the kind. So a cut-off longer than the idle limit leaves every
    held connection looking abandoned, and a clock that kept running through it
    would reclaim them all the moment the gate reopened. The router, still
    waiting on its read, would see the connection END rather than resume: a
    ``timeout`` turned into an ``error`` at the instant of recovery, which is
    precisely the distinction the two kinds exist to keep apart.

    **The connection is QUIET before the cut-off, and that is the whole design of
    this test.** Two earlier versions passed with the protection deleted. The
    first asserted during the cut-off, where the idle check is never reached at
    all — the held branch loops before it. The second left an unread request in
    the socket, and forwarding that request after the restore stamped the clock
    a moment before it was read, so the connection rescued itself. Only a
    connection with nothing pending exposes the missing stamp.

    Each version was caught by planting the mutation rather than by reasoning:
    four others went red and this one stayed green, twice.
    """
    proxy, runner = _idle_proxy(store, 1.0)
    try:
        conn = socket.create_connection(("127.0.0.1", runner.port), timeout=5)
        conn.sendall(b"*1\r\n$4\r\nPING\r\n")
        assert conn.recv(65536), "the store did not answer before the cut-off"
        assert _wait_for_live(proxy, 1) == 1

        proxy.cut_off(TIMEOUT)
        time.sleep(3.0)  # three times the idle limit, all of it cut off, nothing pending
        proxy.restore()
        time.sleep(0.3)  # several polls, so a reclaim would have happened by now

        assert proxy.live_connections() == 1, (
            "a connection was reclaimed for being quiet during a cut-off; a timeout has "
            f"been turned into an error at the moment of recovery. counters={proxy.counters()}"
        )
        assert (
            proxy.counters()["closed_when_idle"] == 0
        ), f"a held connection was counted as abandoned: {proxy.counters()}"
        # The strong claim: it is not merely counted, it still carries bytes.
        conn.sendall(b"*1\r\n$4\r\nPING\r\n")
        conn.settimeout(5)
        assert conn.recv(65536), "the connection survived the restore but no longer reaches the store"
        conn.close()
    finally:
        runner.stop()


def test_a_cut_off_landing_just_before_the_limit_does_not_reclaim(store):
    """The race between the gate and the reclaimer, which the other four miss.

    `_carry` reads the gate at the top of its loop and checks the idle limit at
    the bottom, with a whole pump in between — two poll intervals, and longer
    under lock contention. A cut-off landing inside that window is invisible to
    the iteration already in flight. So a connection one poll short of its limit
    can be reclaimed AFTER `cut_off(TIMEOUT)` has returned: the router reads a
    CLOSE where the test staged a HANG, which is the `error` kind arriving under
    the name of the `timeout` kind.

    The other four tests cannot see this. Each one either leaves the connection
    busy right up to the cut-off, so it is nowhere near its limit, or never
    approaches the boundary at all. This one aims at the boundary deliberately.

    Found by an adversarial review round that drove it rather than arguing it:
    40 trials, reclaimed after the cut-off returned in 40 of them. Three trials
    here, because one was enough to catch it every time.
    """
    for trial in range(3):
        proxy, runner = _idle_proxy(store, 1.0)
        try:
            conn = socket.create_connection(("127.0.0.1", runner.port), timeout=5)
            conn.sendall(b"*1\r\n$4\r\nPING\r\n")
            assert conn.recv(65536), "the store did not answer"
            assert _wait_for_live(proxy, 1) == 1

            # Quiet until just short of the limit, so the cut-off lands inside
            # the window between the gate being read and the limit being checked.
            time.sleep(0.95)
            proxy.cut_off(TIMEOUT)

            # Several polls of the held branch, which is where the reclaim would
            # have happened.
            time.sleep(0.5)
            assert proxy.counters()["closed_when_idle"] == 0, (
                f"trial {trial}: a connection was reclaimed while the gate was cut off. "
                f"The router would read a close where the test asked for a hang. "
                f"counters={proxy.counters()}"
            )
            assert (
                proxy.live_connections() == 1
            ), f"trial {trial}: the held connection is gone; counters={proxy.counters()}"
            conn.close()
        finally:
            runner.stop()


def test_zero_turns_the_reclaimer_off(store):
    """The documented off switch, which nothing else here exercises.

    Every other test in this block passes a positive limit, so a regression that
    made zero mean "reclaim immediately" — the natural reading of
    ``now - last_active > 0`` if the guard above it were dropped — would put the
    leak back for anyone who had deliberately turned the reclaimer off, and no
    test would notice.

    The connection here is quiet far longer than any limit the rest of the file
    uses, so a reclaimer that is on at all takes it.
    """
    proxy, runner = _idle_proxy(store, 0)
    try:
        conn = socket.create_connection(("127.0.0.1", runner.port), timeout=5)
        conn.sendall(b"*1\r\n$4\r\nPING\r\n")
        assert conn.recv(65536), "the store did not answer"
        assert _wait_for_live(proxy, 1) == 1

        time.sleep(2.0)  # longer than every positive limit used in this file

        assert proxy.live_connections() == 1, f"zero did not turn the reclaimer off; counters={proxy.counters()}"
        assert proxy.counters()["closed_when_idle"] == 0

        # Still usable, not merely counted.
        conn.sendall(b"*1\r\n$4\r\nPING\r\n")
        conn.settimeout(5)
        assert conn.recv(65536), "the connection was kept but no longer reaches the store"
        conn.close()
    finally:
        runner.stop()
