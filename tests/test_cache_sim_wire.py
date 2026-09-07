"""Round-trip tests for the cache-sim's gRPC listener, on localhost.

These boot a real gRPC server on a loopback port and call it with a raw-bytes
client, the way the router does. No external network and no fixtures beyond a
free port, so they run anywhere the unit tests do.

They exist because the decision layer's tests cannot see the one thing that
would break silently in production: whether the bytes actually travel. A method
registered under a slightly wrong service path answers UNIMPLEMENTED, the router
counts that as a miss, and every test built on it passes while the secondary is
never really reached.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time

import pytest

grpc = pytest.importorskip("grpc", reason="grpcio is an optional dependency")

from provider_simulator.cache_sim import CacheEntry, CacheSim  # noqa: E402
from provider_simulator.listeners.cache_grpc import serve  # noqa: E402

GET_RELAY = "/smartrouter.pairing.RelayerCache/GetRelay"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Listener:
    """A cache-sim serving on a loopback port for the life of one test."""

    def __init__(self, sim: CacheSim) -> None:
        self.sim = sim
        self.port = _free_port()
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        # The Event must be created on the loop that awaits it.
        self._stop = asyncio.Event()
        self._loop.create_task(serve(self.sim, self.port, host="127.0.0.1", stop=self._stop))
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()

    def __enter__(self) -> _Listener:
        self._thread.start()
        self._ready.wait(timeout=5)
        _wait_until_listening(self.port)
        return self

    def __exit__(self, *_: object) -> None:
        """Stop the server, THEN the loop.

        Stopping the loop alone leaves the port open: the gRPC server holds its
        own listening socket, and abandoning the serve() coroutine never closes
        it. Every test would then leak a listening socket for the life of the
        pytest process. test_the_port_is_closed_after_teardown pins this.
        """
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        _wait_until_closed(self.port)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    @property
    def target(self) -> str:
        return f"127.0.0.1:{self.port}"


def _wait_until_listening(port: int, timeout_s: float = 5.0) -> None:
    """Poll the port until it accepts, rather than sleeping a guessed interval."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
    raise AssertionError(f"cache-sim listener never accepted on port {port}")


def _wait_until_closed(port: int, timeout_s: float = 5.0) -> None:
    """Poll until the port stops accepting, so teardown is observed not assumed."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return
    raise AssertionError(f"cache-sim listener still accepting on port {port} after teardown")


def call_get_relay(target: str, body: dict, timeout_s: float = 5.0) -> bytes:
    """Call GetRelay with raw JSON bytes, the way the router's codec does."""
    with grpc.insecure_channel(target) as channel:
        rpc = channel.unary_unary(
            GET_RELAY,
            request_serializer=lambda b: b,
            response_deserializer=lambda b: b,
        )
        return rpc(json.dumps(body).encode(), timeout=timeout_s)


def a_lookup(block: int = 18_000_000, chain_id: str = "ETH1") -> dict:
    return {
        "request_hash": base64.b64encode(bytes.fromhex("deadbeef")).decode(),
        "block_hash": None,
        "finalized": False,
        "requested_block": block,
        "shared_state_id": "",
        "chain_id": chain_id,
        "seen_block": 0,
        "blocks_hashes_to_heights": None,
    }


class TestTheBytesTravel:
    def test_a_staged_entry_comes_back_over_the_wire(self) -> None:
        sim = CacheSim("internal")
        sim.stage(mode="hit", entry=CacheEntry(data=b'{"result":"0x1"}', status_code=200))
        with _Listener(sim) as listener:
            raw = call_get_relay(listener.target, a_lookup())

        decoded = json.loads(raw)
        assert decoded["status_code"] == 200
        assert base64.b64decode(decoded["reply"]["data"]) == b'{"result":"0x1"}'

    def test_a_miss_travels_as_a_successful_call_with_no_reply(self) -> None:
        sim = CacheSim("internal")
        with _Listener(sim) as listener:
            raw = call_get_relay(listener.target, a_lookup())
        assert json.loads(raw) == {"reply": None}

    def test_the_call_is_recorded_with_the_key_the_caller_asked_under(self) -> None:
        sim = CacheSim("internal")
        with _Listener(sim) as listener:
            call_get_relay(listener.target, a_lookup(block=18_500_000))

        assert sim.call_count() == 1
        assert sim.calls()[0]["key"] == "rel:t:ETH1:deadbeef:18500000"


class TestFailingOnPurpose:
    def test_error_mode_reaches_the_caller_as_that_status(self) -> None:
        sim = CacheSim("internal")
        sim.stage(mode="error", error_status="UNAVAILABLE", error_message="zone down")
        with _Listener(sim) as listener, pytest.raises(grpc.RpcError) as excinfo:
            call_get_relay(listener.target, a_lookup())
        assert excinfo.value.code() == grpc.StatusCode.UNAVAILABLE

    def test_a_hang_exceeds_a_routers_lookup_budget(self) -> None:
        """The router bounds the lookup and counts an overrun as a miss. Here
        the deadline stands in for that budget, so a hang must expire it."""
        sim = CacheSim("internal")
        sim.stage(mode="hang")
        with _Listener(sim) as listener, pytest.raises(grpc.RpcError) as excinfo:
            call_get_relay(listener.target, a_lookup(), timeout_s=0.3)
        assert excinfo.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED

    def test_a_hang_is_still_recorded_as_a_call(self) -> None:
        """The router gave up, but it did ask. A test asserting the secondary
        was never consulted must not be satisfied by a timeout."""
        sim = CacheSim("internal")
        sim.stage(mode="hang")
        with _Listener(sim) as listener:
            with pytest.raises(grpc.RpcError):
                call_get_relay(listener.target, a_lookup(), timeout_s=0.3)
            assert sim.call_count() == 1

    def test_malformed_bytes_arrive_unchanged(self) -> None:
        sim = CacheSim("internal")
        sim.stage(mode="malformed", malformed_body="{ not a reply")
        with _Listener(sim) as listener:
            raw = call_get_relay(listener.target, a_lookup())
        assert raw == b"{ not a reply"
        with pytest.raises(ValueError):
            json.loads(raw)


class TestOnlyGetRelayIsServed:
    def test_a_method_the_secondary_path_never_calls_is_unimplemented(self) -> None:
        """SetRelay is not registered on purpose. An unexpected write must fail
        loudly rather than be quietly accepted."""
        sim = CacheSim("internal")
        with _Listener(sim) as listener:
            with grpc.insecure_channel(listener.target) as channel:
                rpc = channel.unary_unary(
                    "/smartrouter.pairing.RelayerCache/SetRelay",
                    request_serializer=lambda b: b,
                    response_deserializer=lambda b: b,
                )
                with pytest.raises(grpc.RpcError) as excinfo:
                    rpc(b"{}", timeout=5)
        assert excinfo.value.code() == grpc.StatusCode.UNIMPLEMENTED

    def test_a_write_that_was_refused_is_not_counted_as_a_lookup(self) -> None:
        sim = CacheSim("internal")
        with _Listener(sim) as listener:
            with grpc.insecure_channel(listener.target) as channel:
                rpc = channel.unary_unary(
                    "/smartrouter.pairing.RelayerCache/SetRelay",
                    request_serializer=lambda b: b,
                    response_deserializer=lambda b: b,
                )
                with pytest.raises(grpc.RpcError):
                    rpc(b"{}", timeout=5)
            assert sim.call_count() == 0


class TestTeardownActuallyStops:
    """A leaked listener is invisible until the suite runs out of sockets.

    Stopping the event loop is not enough — the gRPC server owns the listening
    socket, and abandoning the serve() coroutine leaves it open for the life of
    the process. This ran green against the leaking version because nothing
    looked; it fails against it now.
    """

    def test_the_port_is_closed_after_teardown(self) -> None:
        sim = CacheSim("internal")
        with _Listener(sim) as listener:
            port = listener.port
            call_get_relay(listener.target, a_lookup())

        with socket.socket() as s:
            s.settimeout(0.5)
            assert s.connect_ex(("127.0.0.1", port)) != 0, (
                f"port {port} still accepts after teardown — the gRPC server was "
                f"never stopped, so every wire test leaks a listening socket"
            )

    def test_repeated_listeners_leave_nothing_behind(self) -> None:
        ports = []
        for _ in range(3):
            sim = CacheSim("internal")
            with _Listener(sim) as listener:
                ports.append(listener.port)
                call_get_relay(listener.target, a_lookup())

        still_open = []
        for port in ports:
            with socket.socket() as s:
                s.settimeout(0.5)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    still_open.append(port)
        assert not still_open, f"leaked listeners on {still_open}"
