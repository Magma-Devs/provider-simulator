"""``mode="port_closed"`` on an ``http`` port and on a ``ws`` port, over real sockets.

A closed port is not a reply. ``mode="down"`` accepts the connection and
answers HTTP 503. ``mode="port_closed"`` closes the port: the simulator refuses
a new connection, and each open connection ends.

``TestTheServerOfOneProviderPort`` has the tests of ``_ProviderHTTPServer``, the
server class that can close its port. Each test makes a server on a port that
the system gives, with a small handler class of this file. These tests use no
simulator.
"""

from __future__ import annotations

import errno
import socket
import socketserver
import threading
import time

import pytest

import server as server_module

# The address of a server on a port that the system gives.
_ANY_PORT = ("127.0.0.1", 0)


# ── helpers ──────────────────────────────────────────────────────────────────


def _connect(port: int) -> int:
    """Try one TCP connection. 0 when the port accepts it, else the errno."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(2.0)
    try:
        return probe.connect_ex(("127.0.0.1", port))
    finally:
        probe.close()


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
