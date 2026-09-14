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
