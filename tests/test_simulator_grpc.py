"""
Integration tests for the gRPC pool of the provider simulator.

Runs against the shared in-process simulator (see conftest.py): the
lava-sim-grpc pool listens on 18548-18550 (grpc over http2) and the eth-sim
pool on 18545-18547. Under the pool:pid model those are SEPARATE providers,
so cross-pool isolation is structural, not gated.

Coverage:
  Happy-path                 — GetLatestBlock / GetNodeInfo respond with a
                                well-formed protobuf.
  Metadata capture            — lava-* request metadata shows up in /history.
  Fault primitives            — hang / status / dropped / corrupt / stale /
                                latency_ms / error_probability all behave
                                correctly over gRPC.
  Cross-pool isolation        — faults on eth-sim / btc-sim / lava-sim-rest
                                never abort the gRPC pool.
  History tracking            — gRPC requests show up in /history exactly
                                like ETH/BTC ones, with the gRPC method name
                                preserved (no JSON-RPC id).
  Request id                  — AllBalances records its ``address`` as the
                                request id of its history row.
  Reflection                  — a served service is found by its symbol.
  Status texts                — the status and the text that a caller reads
                                for each fault, with the history row.
  Waits                       — which statuses wait for latency_ms.
  Per-method errors           — error_stub with each status name, and the
                                per-method error override.
  Reply fields                — the fields of the replies, with a result
                                override and with none.
  Calls with no listener      — a method that is not served, a service that
                                is not registered, a request that is no
                                message.
  Drop points                 — the status and the HTTP/2 frames of each
                                drop point.

Run with:
  pytest tests/test_simulator_grpc.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import grpc
import pytest
from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc

# Splice cosmos_pb2 onto sys.path so the generated stubs resolve. Must run
# before the `from cosmos...` import below — isort must not reorder these
# two (see cosmos_pb2/__init__.py's own docstring for why import order here
# matters).
import cosmos_pb2  # noqa: F401  isort: split

from cosmos.bank.v1beta1 import query_pb2 as bank_query_pb2  # isort: skip
from cosmos.bank.v1beta1 import query_pb2_grpc as bank_query_pb2_grpc  # isort: skip
from cosmos.base.tendermint.v1beta1 import query_pb2, query_pb2_grpc  # isort: skip

import server as server_module
from provider_simulator import topology
from provider_simulator.chains.lava import GRPC_LATEST_BLOCK
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners import grpc as grpc_listener
from provider_simulator.listeners.grpc import SERVED_METHODS, GrpcListener
from provider_simulator.port_gate import PortGate
from provider_simulator.topology import port_of

# Primary tier only — pids 4-6 of each pool are the backup listeners, covered
# by test_simulator_backup_listeners.py.
_PRIMARY_PIDS = ("1", "2", "3")
_GRPC_ADDRS = {pid: f"127.0.0.1:{port_of('lava-sim-grpc', pid, 'grpc', 'http2')}" for pid in _PRIMARY_PIDS}
_ETH_URLS = {pid: f"http://127.0.0.1:{port_of('eth-sim', pid)}" for pid in _PRIMARY_PIDS}
_GRPC_PORT_1 = port_of("lava-sim-grpc", "1", "grpc", "http2")
_GET_LATEST_BLOCK_PATH = "/cosmos.base.tendermint.v1beta1.Service/GetLatestBlock"

# A second simulator, for the one test that needs a gRPC endpoint that does not
# start. Its ports are this file's own block, below the range that the kernel
# gives to client sockets.
_REFUSED_CONTROL = 29721
_REFUSED_GRPC, _REFUSED_REST = 28721, 28722
_REFUSED_ROWS = [
    ("lava-refused-sim", "lava", "1", "LavaRefusedPrimaryProvider1", False, "", (("grpc", "http2", _REFUSED_GRPC),)),
    ("lava-refused-sim", "lava", "2", "LavaRefusedPrimaryProvider2", False, "", (("rest", "http", _REFUSED_REST),)),
]

# Each gRPC status that is not OK, by its name.
_ERROR_STATUS_NAMES = [
    "CANCELLED",
    "UNKNOWN",
    "INVALID_ARGUMENT",
    "DEADLINE_EXCEEDED",
    "NOT_FOUND",
    "ALREADY_EXISTS",
    "PERMISSION_DENIED",
    "RESOURCE_EXHAUSTED",
    "FAILED_PRECONDITION",
    "ABORTED",
    "OUT_OF_RANGE",
    "UNIMPLEMENTED",
    "INTERNAL",
    "UNAVAILABLE",
    "DATA_LOSS",
    "UNAUTHENTICATED",
]


# ── HTTP helpers for the control plane ──────────────────────────────────────


def _parse_body(raw: bytes) -> dict | str:
    """JSON-decode ``raw``, falling back to the decoded text when it isn't
    JSON — the rate_limit fault's prose body is not, by design (see
    provider_simulator/listeners/jsonrpc.py)."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode()


def _post(url: str, body: dict) -> tuple[int, dict | str]:
    """POST JSON body, return (status_code, parsed_response_body)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, _parse_body(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, _parse_body(e.read())
        except (ConnectionResetError, OSError):
            return e.code, {}


def _get(url: str) -> tuple[int, dict | str]:
    """GET url, return (status_code, parsed_response_body)."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, _parse_body(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse_body(e.read())


def _ctrl(sim: dict, path: str) -> str:
    return sim["control"] + path


# ── Function-scoped autouse: clean slate before/after every test ────────────


@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario AND clear history before and after every test."""
    _post(_ctrl(sim, "/reset/all"), {})
    yield
    _post(_ctrl(sim, "/reset/all"), {})


# ── gRPC client helpers ─────────────────────────────────────────────────────


def _set_grpc(sim, pid: str = "1", **extra):
    """Convenience: POST /scenario for one lava-sim-grpc provider."""
    return _post(_ctrl(sim, "/scenario"), {"providers": {f"lava-sim-grpc:{pid}": dict(extra)}})


def _call_get_latest_block(
    address: str, timeout: float = 5.0, metadata: tuple = ()
) -> query_pb2.GetLatestBlockResponse:
    """Open an insecure channel, call GetLatestBlock, return the response.

    ``metadata`` is forwarded as gRPC client metadata so tests can verify
    the metadata-capture path (``lava-guid`` etc.).
    """

    async def _do():
        channel = grpc.aio.insecure_channel(address)
        try:
            stub = query_pb2_grpc.ServiceStub(channel)
            req = query_pb2.GetLatestBlockRequest()
            resp = await asyncio.wait_for(
                stub.GetLatestBlock(req, metadata=metadata),
                timeout=timeout,
            )
            return resp
        finally:
            await channel.close()

    return asyncio.run(_do())


def _call_get_node_info(address: str, timeout: float = 5.0, metadata: tuple = ()) -> query_pb2.GetNodeInfoResponse:
    """Open an insecure channel, call GetNodeInfo, return the response."""

    async def _do():
        channel = grpc.aio.insecure_channel(address)
        try:
            stub = query_pb2_grpc.ServiceStub(channel)
            req = query_pb2.GetNodeInfoRequest()
            resp = await asyncio.wait_for(
                stub.GetNodeInfo(req, metadata=metadata),
                timeout=timeout,
            )
            return resp
        finally:
            await channel.close()

    return asyncio.run(_do())


def _call_all_balances(address: str, account: str, timeout: float = 5.0) -> bank_query_pb2.QueryAllBalancesResponse:
    """Open an insecure channel, call AllBalances for ``account``, return the response."""

    async def _do():
        channel = grpc.aio.insecure_channel(address)
        try:
            stub = bank_query_pb2_grpc.QueryStub(channel)
            req = bank_query_pb2.QueryAllBalancesRequest(address=account)
            return await asyncio.wait_for(stub.AllBalances(req), timeout=timeout)
        finally:
            await channel.close()

    return asyncio.run(_do())


def _ask_reflection(address: str, request: reflection_pb2.ServerReflectionRequest, timeout: float = 5.0):
    """Send one request to the server reflection service, return its reply."""

    async def _do():
        channel = grpc.aio.insecure_channel(address)
        try:
            stub = reflection_pb2_grpc.ServerReflectionStub(channel)
            call = stub.ServerReflectionInfo(iter([request]))
            return await asyncio.wait_for(call.read(), timeout=timeout)
        finally:
            await channel.close()

    return asyncio.run(_do())


def _free_port() -> int:
    """A local TCP port that nothing listens on now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _status_of(call, *args, **kwargs) -> tuple[grpc.StatusCode, str]:
    """Run one of the ``_call_*`` helpers. Return the status that the caller
    gets and the text of that status. A reply message gives ``(OK, "")``."""
    try:
        call(*args, **kwargs)
    except grpc.RpcError as exc:
        return exc.code(), exc.details() or ""
    return grpc.StatusCode.OK, ""


def _call_raw(address: str, path: str, payload: bytes = b"", timeout: float = 5.0) -> bytes:
    """Call one gRPC method by its path, with request bytes that the test
    chooses, and return the reply bytes. A generated stub cannot call a method
    that it does not have, and it cannot send bytes that are no message."""

    async def _do():
        channel = grpc.aio.insecure_channel(address)
        try:
            rpc = channel.unary_unary(path, request_serializer=lambda raw: raw, response_deserializer=lambda raw: raw)
            return await asyncio.wait_for(rpc(payload), timeout=timeout)
        finally:
            await channel.close()

    return asyncio.run(_do())


def _rows(sim, pid: str = "1", **filters: str) -> list[dict]:
    """The history rows of one lava-sim-grpc provider, oldest first."""
    query = "".join(f"&{name}={value}" for name, value in filters.items())
    _, hist = _get(_ctrl(sim, f"/history?pool=lava-sim-grpc&pid={pid}{query}"))
    assert isinstance(hist, dict), f"/history did not answer with a JSON object: {hist!r}"
    return hist["history"]


def _wait_until_accepting(port: int, timeout_s: float = 10.0) -> None:
    """Poll a local port until it accepts a TCP connection."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.02)
    raise AssertionError(f"port {port} did not accept a connection in {timeout_s} s")


# ── One call as HTTP/2 frames ────────────────────────────────────────────────
# A gRPC client reads the same thing for a status that came alone and for a
# status that came after the initial metadata: the status, and no metadata. The
# difference is in the frames that the server sends. So the tests of the drop
# points write one call as HTTP/2 frames by hand, and they read the frames that
# come back.

_H2_DATA, _H2_HEADERS, _H2_RST_STREAM, _H2_SETTINGS = 0x0, 0x1, 0x3, 0x4
_H2_END_STREAM, _H2_ACK, _H2_END_HEADERS = 0x1, 0x1, 0x4
_H2_FRAME_NAMES = {_H2_DATA: "DATA", _H2_HEADERS: "HEADERS", _H2_RST_STREAM: "RST_STREAM"}


def _h2_frame(kind: int, flags: int, stream: int, payload: bytes = b"") -> bytes:
    return len(payload).to_bytes(3, "big") + bytes([kind, flags]) + stream.to_bytes(4, "big") + payload


def _h2_header(name: bytes, value: bytes) -> bytes:
    """One request header as an HPACK literal, with no indexing and no Huffman coding."""
    assert len(name) < 127 and len(value) < 127, "this helper writes one-byte lengths only"
    return b"\x00" + bytes([len(name)]) + name + bytes([len(value)]) + value


def _frames_of_one_call(port: int, path: str, timeout: float = 5.0) -> list[str]:
    """Send one unary gRPC call with an empty request message, as HTTP/2 frames.

    Return the frames that the server sent for that call, in order: "HEADERS",
    "DATA" or "RST_STREAM". The frame that ends the call has "+END_STREAM".
    """
    request_headers = b"".join(
        _h2_header(name, value)
        for name, value in (
            (b":method", b"POST"),
            (b":scheme", b"http"),
            (b":path", path.encode()),
            (b":authority", f"127.0.0.1:{port}".encode()),
            (b"content-type", b"application/grpc"),
            (b"te", b"trailers"),
        )
    )
    frames: list[str] = []
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(
            b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
            + _h2_frame(_H2_SETTINGS, 0, 0)
            + _h2_frame(_H2_HEADERS, _H2_END_HEADERS, 1, request_headers)
            # One gRPC message of zero bytes: a flag byte and a four-byte length.
            + _h2_frame(_H2_DATA, _H2_END_STREAM, 1, b"\x00\x00\x00\x00\x00")
        )
        buffer = b""
        while True:
            while len(buffer) < 9 or len(buffer) < 9 + int.from_bytes(buffer[:3], "big"):
                chunk = sock.recv(65536)
                if not chunk:
                    return frames + ["CONNECTION CLOSED"]
                buffer += chunk
            length = int.from_bytes(buffer[:3], "big")
            kind, flags = buffer[3], buffer[4]
            stream = int.from_bytes(buffer[5:9], "big") & 0x7FFFFFFF
            buffer = buffer[9 + length :]
            if kind == _H2_SETTINGS and not flags & _H2_ACK:
                sock.sendall(_h2_frame(_H2_SETTINGS, _H2_ACK, 0))
            if stream != 1:
                continue
            name = _H2_FRAME_NAMES.get(kind, f"type {kind}")
            if kind == _H2_RST_STREAM:
                return frames + [name]
            if kind in (_H2_DATA, _H2_HEADERS) and flags & _H2_END_STREAM:
                return frames + [name + "+END_STREAM"]
            frames.append(name)


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# One gRPC endpoint in a process of its own, and a reflection lookup for each
# symbol given on the command line. This script must NOT import a bank stub:
# the lookup must find the bank service because the SERVER loaded it.
_REFLECTION_PROBE = """
import asyncio
import os
import sys
import threading
import time

import grpc
from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc

import server
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners.grpc import GrpcListener
from provider_simulator.port_gate import PortGate

port = int(sys.argv[1])
endpoint = Endpoint("grpc", "http2", port)
provider = Pool(name="lava-sim-grpc", chain="lava").add_provider("1", [endpoint])
gate = PortGate(provider, endpoint)
threading.Thread(
    target=server._run_grpc_in_thread,
    args=(GrpcListener(provider, endpoint), port, "127.0.0.1", gate),
    daemon=True,
).start()
deadline = time.monotonic() + 10
while not gate.accepts():
    if time.monotonic() > deadline:
        sys.exit("the gRPC endpoint did not open")
    time.sleep(0.05)


async def lookup(symbol):
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        stub = reflection_pb2_grpc.ServerReflectionStub(channel)
        request = reflection_pb2.ServerReflectionRequest(file_containing_symbol=symbol)
        reply = await asyncio.wait_for(stub.ServerReflectionInfo(iter([request])).read(), timeout=10)
        return reply.WhichOneof("message_response")


for symbol in sys.argv[2:]:
    print(symbol, asyncio.run(lookup(symbol)), flush=True)
# Leave at once. A grpc.aio server still runs on a daemon thread, and the
# teardown of the interpreter must not wait for it.
os._exit(0)
"""


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────


class TestGrpcHappy:
    """The simulator must return a well-formed protobuf for the canonical
    cosmos.base.tendermint.v1beta1.Service unary methods."""

    def test_get_latest_block_returns_valid_proto(self, sim):
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert resp.block.header.chain_id == "lava-sim"
        assert resp.block.header.height > 0

    def test_get_latest_block_height_matches_constant(self, sim):
        """The default head is pinned to GRPC_LATEST_BLOCK so tests can assert
        exact equality."""
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert resp.block.header.height == GRPC_LATEST_BLOCK

    def test_get_latest_block_carries_block_id(self, sim):
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert len(resp.block_id.hash) == 32

    def test_get_node_info_returns_valid_proto(self, sim):
        resp = _call_get_node_info(_GRPC_ADDRS["1"])
        assert resp.default_node_info.network == "lava-sim"
        assert resp.application_version.name == "lava-sim"


# ─────────────────────────────────────────────────────────────────────────────
# Lava metadata capture
# ─────────────────────────────────────────────────────────────────────────────


class TestGrpcMetadataCapture:
    """lava-* request metadata must show up in /history under the recorded
    entry so the router's observable behaviour can be verified."""

    def test_lava_guid_captured(self, sim):
        _call_get_latest_block(_GRPC_ADDRS["1"], metadata=(("lava-guid", "abc123"),))
        _, hist = _get(_ctrl(sim, "/history?pool=lava-sim-grpc&pid=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "GetLatestBlock"
        assert last["lava_headers"].get("lava-guid") == "abc123"
        assert last["interface"] == "grpc"
        assert last["transport"] == "http2"

    def test_multiple_lava_headers_captured(self, sim):
        _call_get_latest_block(
            _GRPC_ADDRS["1"],
            metadata=(
                ("lava-guid", "g1"),
                ("lava-stateful-api", "true"),
                ("authorization", "bearer ignored"),  # non-lava header — should be dropped
            ),
        )
        _, hist = _get(_ctrl(sim, "/history?pool=lava-sim-grpc&pid=1"))
        headers = hist["history"][-1]["lava_headers"]
        assert headers.get("lava-guid") == "g1"
        assert headers.get("lava-stateful-api") == "true"
        # Only lava-* headers are captured — authorization must be absent.
        assert "authorization" not in headers

    def test_history_filter_by_lava_header(self, sim):
        """The existing /history?lava_header_<name>= filter must work for
        gRPC requests, not just JSON-RPC ones."""
        _call_get_latest_block(_GRPC_ADDRS["1"], metadata=(("lava-guid", "match-me"),))
        _call_get_latest_block(_GRPC_ADDRS["1"], metadata=(("lava-guid", "other"),))
        _, hist = _get(_ctrl(sim, "/history?lava_header_lava-guid=match-me"))
        assert hist["count"] == 1

    def test_a_lava_header_that_comes_two_times_keeps_its_last_value(self, sim):
        _call_get_latest_block(_GRPC_ADDRS["1"], metadata=(("lava-guid", "first"), ("lava-guid", "last")))
        assert [row["lava_headers"] for row in _rows(sim)] == [{"lava-guid": "last"}]


# ─────────────────────────────────────────────────────────────────────────────
# Fault: hang
# ─────────────────────────────────────────────────────────────────────────────


class TestGrpcFaultHang:
    """mode=hang must make the client time out — the simulator sleeps 30s
    before responding, the client's deadline fires first."""

    def test_hang_times_out_client(self, sim):
        _set_grpc(sim, "1", mode="hang")
        t0 = time.monotonic()
        with pytest.raises((asyncio.TimeoutError, grpc.RpcError)):
            _call_get_latest_block(_GRPC_ADDRS["1"], timeout=2.0)
        elapsed = time.monotonic() - t0
        # Client deadline is 2s — must fire well before the 30s server sleep.
        assert elapsed < 10, f"hang should time out via client deadline, got {elapsed:.2f}s"


# ─────────────────────────────────────────────────────────────────────────────
# Fault: status (mode=error → grpc.StatusCode mapping)
# ─────────────────────────────────────────────────────────────────────────────


class TestGrpcFaultStatus:
    """mode=error + error_message=<StatusCode name> must abort with that
    exact gRPC status code on the client side."""

    def test_resource_exhausted_status(self, sim):
        _set_grpc(sim, "1", mode="error", error_message="RESOURCE_EXHAUSTED")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(_GRPC_ADDRS["1"])
        assert exc_info.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED

    def test_unavailable_status(self, sim):
        _set_grpc(sim, "1", mode="error", error_message="UNAVAILABLE")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(_GRPC_ADDRS["1"])
        assert exc_info.value.code() == grpc.StatusCode.UNAVAILABLE

    def test_internal_status(self, sim):
        _set_grpc(sim, "1", mode="error", error_message="INTERNAL")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(_GRPC_ADDRS["1"])
        assert exc_info.value.code() == grpc.StatusCode.INTERNAL

    def test_integer_error_code_fallback(self, sim):
        """When error_message isn't a recognised name, error_code (integer)
        is consulted next. 8 = RESOURCE_EXHAUSTED per the gRPC spec."""
        _set_grpc(sim, "1", mode="error", error_message="not-a-grpc-name", error_code=8)
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(_GRPC_ADDRS["1"])
        assert exc_info.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED

    def test_unknown_fallback(self, sim):
        """Unmatched name AND unmatched int → UNKNOWN."""
        _set_grpc(sim, "1", mode="error", error_message="garbage", error_code=-999)
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(_GRPC_ADDRS["1"])
        assert exc_info.value.code() == grpc.StatusCode.UNKNOWN


# ─────────────────────────────────────────────────────────────────────────────
# Fault: dropped connection (3 drop points)
# ─────────────────────────────────────────────────────────────────────────────


class TestGrpcFaultDropped:
    """drop_connection at all three points must surface as a gRPC error
    on the client side (UNAVAILABLE / CANCELLED depending on stage)."""

    def test_drop_before_headers(self, sim):
        _set_grpc(sim, "1", mode="drop_connection", drop_at="before_headers")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(_GRPC_ADDRS["1"])
        # Server aborts UNAVAILABLE before sending initial metadata.
        assert exc_info.value.code() in (
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.CANCELLED,
        )

    def test_drop_after_headers(self, sim):
        _set_grpc(sim, "1", mode="drop_connection", drop_at="after_headers")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(_GRPC_ADDRS["1"])
        assert exc_info.value.code() in (
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.CANCELLED,
        )

    def test_drop_mid_body(self, sim):
        """Unary RPC has no mid-body — the gRPC sim collapses mid_body to
        the after_headers shape until streaming support lands."""
        _set_grpc(sim, "1", mode="drop_connection", drop_at="mid_body")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(_GRPC_ADDRS["1"])
        assert exc_info.value.code() in (
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.CANCELLED,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Fault: corruption (5 variants)
# ─────────────────────────────────────────────────────────────────────────────


class TestGrpcFaultCorrupt:
    """corruption_mode applies to gRPC responses the same way it applies
    to JSON-RPC ones — wire-level breakage surfaces as a client error.

    Note: ``invalid_proto``, ``empty_response``, ``truncated``, and
    ``wrong_type`` all surface as a status-code abort because the proto
    runtime won't let us emit a partial / mismatched message at the
    Python layer. ``missing_field`` is the only variant that returns a
    valid-on-wire-but-incomplete response.
    """

    def test_invalid_proto(self, sim):
        _set_grpc(sim, "1", corruption_mode="invalid_proto")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(_GRPC_ADDRS["1"])
        assert exc_info.value.code() == grpc.StatusCode.UNKNOWN

    def test_empty_response(self, sim):
        _set_grpc(sim, "1", corruption_mode="empty_response")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(_GRPC_ADDRS["1"])
        assert exc_info.value.code() == grpc.StatusCode.UNKNOWN

    def test_truncated(self, sim):
        _set_grpc(sim, "1", corruption_mode="truncated")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(_GRPC_ADDRS["1"])
        assert exc_info.value.code() == grpc.StatusCode.UNKNOWN

    def test_missing_field(self, sim):
        """missing_field=block clears the ``block`` field — client sees a
        valid response with no ``block`` payload."""
        _set_grpc(sim, "1", corruption_mode="missing_field", missing_field="block")
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        # block field is cleared — protobuf's default for a cleared message
        # field is the zero-value singleton, height 0 and chain_id empty.
        assert resp.block.header.height == 0
        assert resp.block.header.chain_id == ""

    def test_wrong_type(self, sim):
        """wrong_type surfaces as an INTERNAL status abort because the
        proto runtime can't emit a type-mismatched wire message from
        Python — the simulator surfaces the corruption as a status."""
        _set_grpc(sim, "1", corruption_mode="wrong_type", missing_field="block")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(_GRPC_ADDRS["1"])
        assert exc_info.value.code() == grpc.StatusCode.INTERNAL


# ─────────────────────────────────────────────────────────────────────────────
# Fault: stale head (blocks_behind shifts height)
# ─────────────────────────────────────────────────────────────────────────────


class TestGrpcFaultStale:
    """blocks_behind=N must decrement ``block.header.height`` by N."""

    def test_blocks_behind_shifts_height(self, sim):
        _set_grpc(sim, "1", blocks_behind=100)
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert resp.block.header.height == GRPC_LATEST_BLOCK - 100

    def test_blocks_behind_zero_is_default_head(self, sim):
        _set_grpc(sim, "1", blocks_behind=0)
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert resp.block.header.height == GRPC_LATEST_BLOCK

    def test_large_blocks_behind(self, sim):
        _set_grpc(sim, "1", blocks_behind=1_000_000)
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert resp.block.header.height == GRPC_LATEST_BLOCK - 1_000_000


# ─────────────────────────────────────────────────────────────────────────────
# Fault: latency (provider-wide latency_ms floor)
# ─────────────────────────────────────────────────────────────────────────────


class TestGrpcFaultLatency:
    """latency_ms on the provider block delays the reply on the gRPC wire."""

    def test_latency_ms_delays_reply(self, sim):
        """latency_ms=300 inserts at least 300ms between request and reply."""
        _set_grpc(sim, "1", latency_ms=300)
        t0 = time.monotonic()
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        elapsed = time.monotonic() - t0
        assert resp.block.header.height == GRPC_LATEST_BLOCK  # reply is still valid
        assert elapsed >= 0.28, f"latency floor not paid: elapsed={elapsed:.3f}s"


# ─────────────────────────────────────────────────────────────────────────────
# Fault: error_probability (probabilistic error on mode=success)
# ─────────────────────────────────────────────────────────────────────────────


class TestGrpcFaultErrorProbability:
    """error_probability rolls on the shared fault ladder for gRPC calls too.

    On gRPC an error verdict is a status abort. The default error_message
    ("Internal error") names no grpc.StatusCode and the default error_code
    (-32000) is no valid status int, so the abort resolves to UNKNOWN.
    """

    def test_error_probability_1_always_errors(self, sim):
        """error_probability=1.0 on mode=success aborts every one of 5 calls."""
        _set_grpc(sim, "1", mode="success", error_probability=1.0)
        errored = 0
        for _ in range(5):
            try:
                _call_get_latest_block(_GRPC_ADDRS["1"])
            except grpc.RpcError as e:
                assert e.code() == grpc.StatusCode.UNKNOWN
                errored += 1
        assert errored == 5, f"expected 5/5 aborts at probability 1.0, got {errored}/5"

    def test_error_probability_0_never_errors(self, sim):
        """error_probability=0.0 on mode=success answers every one of 5 calls."""
        _set_grpc(sim, "1", mode="success", error_probability=0.0)
        succeeded = 0
        for _ in range(5):
            resp = _call_get_latest_block(_GRPC_ADDRS["1"])
            if resp.block.header.height == GRPC_LATEST_BLOCK:
                succeeded += 1
        assert succeeded == 5, f"expected 5/5 replies at probability 0.0, got {succeeded}/5"


# ─────────────────────────────────────────────────────────────────────────────
# Addressing — grpc + eth providers in the same scenario body
# ─────────────────────────────────────────────────────────────────────────────


class TestGrpcAddressing:
    """lava-sim-grpc providers round-trip through /scenario, and one body can
    configure gRPC and ETH providers side by side."""

    def test_grpc_provider_visible_in_scenario(self, sim):
        _set_grpc(sim, "1", latency_ms=0)
        _, body = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["lava-sim-grpc:1"]["mode"] == "success"
        assert "chain_family" not in body["providers"]["lava-sim-grpc:1"]

    def test_mixed_eth_and_grpc(self, sim):
        """lava-sim-grpc:1 healthy + eth-sim:2 rate-limited — each pool
        independently configured in the same /scenario call."""
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "lava-sim-grpc:1": {"latency_ms": 0},
                    "eth-sim:2": {"mode": "rate_limit"},
                }
            },
        )

        # gRPC side: GetLatestBlock returns valid proto.
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert resp.block.header.height > 0

        # ETH side: rate-limited HTTP/JSON-RPC request returns 429.
        status, _ = _post(_ETH_URLS["2"], {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
        assert status == 429

    def test_reset_restores_grpc_defaults(self, sim):
        _set_grpc(sim, "1", mode="error", error_message="UNAVAILABLE")
        _post(_ctrl(sim, "/reset"), {})
        _, body = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["lava-sim-grpc:1"]["mode"] == "success"
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert resp.block.header.height > 0


# ─────────────────────────────────────────────────────────────────────────────
# History tracking — gRPC requests show up in /history
# ─────────────────────────────────────────────────────────────────────────────


class TestGrpcHistoryTracking:
    """gRPC requests must be recorded in /history with the same shape as
    HTTP requests, so cross-transport correlations work."""

    def test_grpc_request_recorded(self, sim):
        _call_get_latest_block(_GRPC_ADDRS["1"])
        _, hist = _get(_ctrl(sim, "/history?pool=lava-sim-grpc&pid=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "GetLatestBlock"
        assert last["status"] == "success"

    def test_grpc_history_filter_by_method(self, sim):
        _call_get_latest_block(_GRPC_ADDRS["1"])
        _call_get_node_info(_GRPC_ADDRS["1"])
        _, hist = _get(_ctrl(sim, "/history?method=GetLatestBlock"))
        assert hist["count"] >= 1
        assert all(e["method"] == "GetLatestBlock" for e in hist["history"])

    def test_grpc_error_status_recorded(self, sim):
        """Fault-injected gRPC requests show up with status=error."""
        _set_grpc(sim, "1", mode="error", error_message="RESOURCE_EXHAUSTED")
        with pytest.raises(grpc.RpcError):
            _call_get_latest_block(_GRPC_ADDRS["1"])
        _, hist = _get(_ctrl(sim, "/history?pool=lava-sim-grpc&pid=1&status=error"))
        assert hist["count"] >= 1
        assert hist["history"][-1]["method"] == "GetLatestBlock"

    def test_grpc_request_id_is_none(self, sim):
        """A gRPC method with an empty request has no request id: the row of
        GetLatestBlock records request_id=None. AllBalances is the method that
        carries one, in its ``address``."""
        _call_get_latest_block(_GRPC_ADDRS["1"])
        _, hist = _get(_ctrl(sim, "/history?pool=lava-sim-grpc&pid=1"))
        assert hist["history"][-1]["request_id"] is None


# ─────────────────────────────────────────────────────────────────────────────
# The request id — AllBalances carries it in the field ``address``
# ─────────────────────────────────────────────────────────────────────────────


class TestGrpcAllBalances:
    """The bank method that carries a request id. The caller chooses the
    ``address``, and the history row holds it as the request id."""

    def test_all_balances_returns_one_ulava_coin(self, sim):
        resp = _call_all_balances(_GRPC_ADDRS["1"], "lava1-grpc-a")
        assert [(coin.denom, coin.amount) for coin in resp.balances] == [("ulava", "1000000")]

    def test_address_selects_the_rows_of_one_request(self, sim):
        """One request that reached two providers gives two rows under its
        address, and no row of another request."""
        _call_all_balances(_GRPC_ADDRS["1"], "lava1-mag3800-a")
        _call_all_balances(_GRPC_ADDRS["2"], "lava1-mag3800-a")
        _call_all_balances(_GRPC_ADDRS["1"], "lava1-mag3800-b")
        _call_get_latest_block(_GRPC_ADDRS["1"])
        _, hist = _get(_ctrl(sim, "/history?request_id=lava1-mag3800-a&pool=lava-sim-grpc"))
        assert hist["count"] == 2
        assert sorted(e["pid"] for e in hist["history"]) == ["1", "2"]
        assert {e["method"] for e in hist["history"]} == {"AllBalances"}
        assert {e["request_id"] for e in hist["history"]} == {"lava1-mag3800-a"}

    def test_a_failed_call_keeps_the_address_on_its_row(self, sim):
        _set_grpc(sim, "1", mode="error", error_message="RESOURCE_EXHAUSTED")
        with pytest.raises(grpc.RpcError) as excinfo:
            _call_all_balances(_GRPC_ADDRS["1"], "lava1-mag3800-c")
        assert excinfo.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
        _, hist = _get(_ctrl(sim, "/history?request_id=lava1-mag3800-c&pool=lava-sim-grpc"))
        assert hist["count"] == 1
        assert hist["history"][0]["status"] == "error"

    def test_a_down_provider_row_has_no_request_id(self, sim):
        _set_grpc(sim, "1", mode="down")
        with pytest.raises(grpc.RpcError):
            _call_all_balances(_GRPC_ADDRS["1"], "lava1-mag3800-d")
        _, by_id = _get(_ctrl(sim, "/history?request_id=lava1-mag3800-d&pool=lava-sim-grpc"))
        _, by_status = _get(_ctrl(sim, "/history?pool=lava-sim-grpc&pid=1&status=down"))
        assert by_id["count"] == 0
        assert by_status["count"] == 1
        assert by_status["history"][0]["method"] == "*"

    def test_missing_field_corruption_clears_the_balances(self, sim):
        _set_grpc(sim, "1", corruption_mode="missing_field", missing_field="balances")
        resp = _call_all_balances(_GRPC_ADDRS["1"], "lava1-grpc-e")
        assert len(resp.balances) == 0

    def test_result_override_replaces_the_balances(self, sim):
        _set_grpc(sim, "1", responses={"AllBalances": {"result": {"balances": [{"denom": "uatom", "amount": "5"}]}}})
        resp = _call_all_balances(_GRPC_ADDRS["1"], "lava1-grpc-f")
        assert [(coin.denom, coin.amount) for coin in resp.balances] == [("uatom", "5")]

    def test_result_override_with_a_number_amount_gives_the_amount_as_text(self, sim):
        """A JSON number for the amount is an easy slip. The reply holds it as
        text, and the row says success because the caller got a reply."""
        _set_grpc(sim, "1", responses={"AllBalances": {"result": {"balances": [{"denom": "uatom", "amount": 5}]}}})
        resp = _call_all_balances(_GRPC_ADDRS["1"], "lava1-grpc-g")
        assert [(coin.denom, coin.amount) for coin in resp.balances] == [("uatom", "5")]
        _, hist = _get(_ctrl(sim, "/history?request_id=lava1-grpc-g&pool=lava-sim-grpc"))
        assert [e["status"] for e in hist["history"]] == ["success"]

    @pytest.mark.parametrize(
        "balances, expected",
        [
            pytest.param("oops", [], id="balances-is-no-list"),
            pytest.param([5, "x"], [], id="an-item-is-no-object"),
            pytest.param([{"denom": "uatom"}], [("uatom", "")], id="a-coin-has-no-amount"),
        ],
    )
    def test_result_override_that_is_no_list_of_coins_still_gets_a_reply(self, sim, balances, expected):
        """The history row is written before the reply is built. So a
        ``result`` that is no list of coins must not end the call with an
        error: the row would say success while the caller got no reply."""
        _set_grpc(sim, "1", responses={"AllBalances": {"result": {"balances": balances}}})
        resp = _call_all_balances(_GRPC_ADDRS["1"], "lava1-grpc-h")
        assert [(coin.denom, coin.amount) for coin in resp.balances] == expected

    def test_other_bank_methods_answer_unimplemented(self, sim):
        """The simulator serves one method of the bank service. Each other
        method keeps the generated default. A service that is not registered
        gives another text, "Method not found!", so the text is read too."""

        async def _do():
            channel = grpc.aio.insecure_channel(_GRPC_ADDRS["1"])
            try:
                stub = bank_query_pb2_grpc.QueryStub(channel)
                await asyncio.wait_for(stub.TotalSupply(bank_query_pb2.QueryTotalSupplyRequest()), timeout=5.0)
            finally:
                await channel.close()

        with pytest.raises(grpc.RpcError) as excinfo:
            asyncio.run(_do())
        assert excinfo.value.code() == grpc.StatusCode.UNIMPLEMENTED
        assert "Method not implemented!" in excinfo.value.details()


class TestGrpcReflection:
    """The smart-router reads the description of a gRPC method from the
    provider: it asks the provider's reflection for the SYMBOL of the service.
    The list of services is for ``grpcurl list`` only."""

    def test_reflection_lists_the_served_services(self, sim):
        reply = _ask_reflection(_GRPC_ADDRS["1"], reflection_pb2.ServerReflectionRequest(list_services=""))
        assert {service.name for service in reply.list_services_response.service} == {
            "cosmos.bank.v1beta1.Query",
            "cosmos.base.tendermint.v1beta1.Service",
            "grpc.reflection.v1alpha.ServerReflection",
        }

    def test_reflection_finds_the_bank_symbol_because_the_server_loads_it(self, sim):
        """This test module imports the bank stubs for its client, so a lookup
        in this process finds the symbol with any server. The lookup runs in a
        process of its own, where only the server can load the bank stubs.
        The staking service is the control: its stubs are compiled, the
        server does not load them, and the lookup fails."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _REFLECTION_PROBE,
                str(_free_port()),
                "cosmos.bank.v1beta1.Query",
                "cosmos.base.tendermint.v1beta1.Service",
                "cosmos.staking.v1beta1.Query",
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [
            "cosmos.bank.v1beta1.Query file_descriptor_response",
            "cosmos.base.tendermint.v1beta1.Service file_descriptor_response",
            "cosmos.staking.v1beta1.Query error_response",
        ]


class TestGrpcServedMethods:
    """The gRPC listener module writes its servicers by hand and holds the
    table of the served methods. The adapter compares the two before it starts
    a server."""

    def test_the_adapter_refuses_to_start_when_a_served_method_has_no_row(self, sim, monkeypatch):
        without_all_balances = tuple(row for row in SERVED_METHODS if row[1] != "AllBalances")
        monkeypatch.setattr(grpc_listener, "SERVED_METHODS", without_all_balances)
        port = _free_port()
        endpoint = Endpoint("grpc", "http2", port)
        provider = Pool(name="lava-sim-grpc", chain="lava").add_provider("1", [endpoint])
        errors: list[str] = []

        def _run():
            try:
                server_module._run_grpc_in_thread(
                    GrpcListener(provider, endpoint), port, "127.0.0.1", PortGate(provider, endpoint)
                )
            except ValueError as exc:
                errors.append(str(exc))

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=10)
        assert errors, "the adapter started a gRPC server with a served method that has no row"
        assert "Served with no row: ['cosmos.bank.v1beta1.Query/AllBalances']" in errors[0]

    def test_an_endpoint_that_is_refused_leaves_the_simulator_up_and_not_ready(self, monkeypatch):
        """The check runs on the thread of one gRPC endpoint. When it refuses,
        that thread ends and the port of the endpoint never opens. The process
        stays up: the control API answers and each other endpoint serves.
        /ready answers 503 and names the port, so a deployment of such a
        simulator does not report ready."""
        without_all_balances = tuple(row for row in SERVED_METHODS if row[1] != "AllBalances")
        monkeypatch.setattr(grpc_listener, "SERVED_METHODS", without_all_balances)
        thread_errors: list[str] = []
        monkeypatch.setattr(threading, "excepthook", lambda args: thread_errors.append(str(args.exc_value)))
        shipped = topology.TOPOLOGY
        topology.TOPOLOGY = _REFUSED_ROWS
        try:
            second = server_module.SimulatorServer(
                host="127.0.0.1", control_port=_REFUSED_CONTROL, scenario_ttl_s=0, cache_ports={}, resp_proxy_ports={}
            )
        finally:
            topology.TOPOLOGY = shipped
        second.start()
        try:
            _wait_until_accepting(_REFUSED_CONTROL)
            _wait_until_accepting(_REFUSED_REST)
            deadline = time.monotonic() + 10
            while not thread_errors and time.monotonic() < deadline:
                time.sleep(0.02)
            assert len(thread_errors) == 1, thread_errors
            assert "Served with no row: ['cosmos.bank.v1beta1.Query/AllBalances']" in thread_errors[0]

            control = f"http://127.0.0.1:{_REFUSED_CONTROL}"
            status, ready = _get(control + "/ready")
            assert status == 503
            assert (ready["status"], ready["missing_ports"]) == ("not_ready", [_REFUSED_GRPC])
            assert _get(control + "/health")[0] == 200
            rest_status, _ = _get(f"http://127.0.0.1:{_REFUSED_REST}/cosmos/base/tendermint/v1beta1/blocks/latest")
            assert rest_status == 200
        finally:
            second.stop()


# ─────────────────────────────────────────────────────────────────────────────
# What a caller reads today, fault by fault
# ─────────────────────────────────────────────────────────────────────────────
# The classes below record what a real gRPC client reads from the simulator
# today: the status of each fault with its text, the history row of the call,
# and the answers that the gRPC library gives before the listener sees a call.
# A later change moves gRPC into the request flow of Listener.serve. These
# tests then show that a caller reads the same thing after the move.


class TestGrpcStatusTexts:
    """Each fault reaches the caller as a status with a text, and it leaves one
    history row. The texts "provider down" and "connection dropped" have their
    tests in tests/test_simulator_grpc_port_closed.py."""

    @pytest.mark.parametrize(
        "scenario, want_code, want_text, want_row_status",
        [
            pytest.param(
                {"mode": "rate_limit"},
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                "Too many requests",
                "rate_limit",
                id="rate-limit",
            ),
            pytest.param(
                {"mode": "rate_limit", "rate_limit_body": "slow down"},
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                "Too many requests",
                "rate_limit",
                id="rate-limit-body-does-not-change-the-text",
            ),
            pytest.param(
                {"mode": "error"}, grpc.StatusCode.UNKNOWN, "Internal error", "error", id="error-with-no-status-named"
            ),
            pytest.param(
                {"mode": "error", "error_message": "NOT_FOUND"},
                grpc.StatusCode.NOT_FOUND,
                "NOT_FOUND",
                "error",
                id="error-message-names-the-status",
            ),
            pytest.param(
                {"mode": "error", "error_message": "no status has this name", "error_code": 5},
                grpc.StatusCode.NOT_FOUND,
                "no status has this name",
                "error",
                id="error-code-is-the-number-of-the-status",
            ),
            pytest.param(
                {"corruption_mode": "wrong_type"},
                grpc.StatusCode.INTERNAL,
                "wrong_type corruption on response",
                "error",
                id="wrong-type",
            ),
            pytest.param(
                {"corruption_mode": "wrong_type", "missing_field": "block"},
                grpc.StatusCode.INTERNAL,
                "wrong_type corruption on block",
                "error",
                id="wrong-type-names-the-field",
            ),
            pytest.param(
                {"corruption_mode": "invalid_proto"},
                grpc.StatusCode.UNKNOWN,
                "corruption: invalid_proto",
                "error",
                id="invalid-proto",
            ),
            pytest.param(
                {"corruption_mode": "empty_response"},
                grpc.StatusCode.UNKNOWN,
                "corruption: empty_response",
                "error",
                id="empty-response",
            ),
            pytest.param(
                {"corruption_mode": "truncated"},
                grpc.StatusCode.UNKNOWN,
                "corruption: truncated",
                "error",
                id="truncated",
            ),
            pytest.param(
                {"corruption_mode": "null_body"},
                grpc.StatusCode.UNKNOWN,
                "corruption: null_body",
                "error",
                id="null-body",
            ),
            # A corruption acts on a reply message only, so a status stays as it is.
            pytest.param(
                {"mode": "rate_limit", "corruption_mode": "wrong_type"},
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                "Too many requests",
                "rate_limit",
                id="rate-limit-with-a-corruption-stays-rate-limit",
            ),
            pytest.param(
                {"mode": "error", "error_message": "NOT_FOUND", "corruption_mode": "invalid_proto"},
                grpc.StatusCode.NOT_FOUND,
                "NOT_FOUND",
                "error",
                id="error-with-a-corruption-stays-the-error",
            ),
        ],
    )
    def test_a_fault_is_a_status_with_a_text_and_one_row(self, sim, scenario, want_code, want_text, want_row_status):
        status, body = _set_grpc(sim, "1", **scenario)
        assert status == 200, body
        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == (want_code, want_text)
        assert [(row["method"], row["status"]) for row in _rows(sim)] == [("GetLatestBlock", want_row_status)]

    def test_invalid_json_corruption_is_refused_for_a_grpc_provider(self, sim):
        """``invalid_json`` breaks the bytes of a JSON body, and a gRPC reply
        has none. The control API refuses it, and the provider answers as
        before."""
        status, body = _set_grpc(sim, "1", corruption_mode="invalid_json")
        assert status == 400, body
        assert "only gRPC endpoints" in body["error"]
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert (resp.block.header.height, resp.block.header.chain_id) == (GRPC_LATEST_BLOCK, "lava-sim")
        assert [row["status"] for row in _rows(sim)] == ["success"]

    @pytest.mark.timeout(60)
    def test_a_hung_call_ends_after_30_seconds_with_cancelled_and_its_text(self, sim):
        """The adapter holds a hung call for 30 seconds, and then it ends the
        call with CANCELLED and the text "hang timeout". A caller with a shorter
        deadline does not read that status, so this test waits for it. It is the
        one slow test of this file: it takes 30 seconds.

        The scenario also has a latency. The row of a hung call records 0."""
        status, body = _set_grpc(sim, "1", mode="hang", latency_ms=700)
        assert status == 200, body
        started = time.monotonic()
        answer = _status_of(_call_get_latest_block, _GRPC_ADDRS["1"], timeout=45.0)
        elapsed = time.monotonic() - started
        assert answer == (grpc.StatusCode.CANCELLED, "hang timeout")
        assert 29.5 <= elapsed <= 40.0, f"a hung call must end after 30 s, and it ended after {elapsed:.1f} s"
        assert [(row["status"], row["latency_ms"]) for row in _rows(sim)] == [("hang", 0)]


class TestGrpcWhichCallsWait:
    """``latency_ms`` delays a reply message and each status but two. A
    provider-wide ``down`` answers at once, and a hung call waits its own 30
    seconds. The row of each of the two records 0. A per-method ``down`` is
    different: it waits for the latency of its entry, and its row records it."""

    @pytest.mark.parametrize(
        "scenario, want_code, min_s, max_s, want_row_latency_ms",
        [
            pytest.param({"mode": "down"}, grpc.StatusCode.UNAVAILABLE, 0.0, 0.5, 0, id="down-answers-at-once"),
            pytest.param(
                {"mode": "drop_connection"}, grpc.StatusCode.UNAVAILABLE, 0.55, 5.0, 600, id="drop-connection-waits"
            ),
            pytest.param(
                {"mode": "rate_limit"}, grpc.StatusCode.RESOURCE_EXHAUSTED, 0.55, 5.0, 600, id="rate-limit-waits"
            ),
            pytest.param({"mode": "error"}, grpc.StatusCode.UNKNOWN, 0.55, 5.0, 600, id="error-waits"),
            pytest.param(
                {"corruption_mode": "wrong_type"}, grpc.StatusCode.INTERNAL, 0.55, 5.0, 600, id="wrong-type-waits"
            ),
            pytest.param(
                {"responses": {"GetLatestBlock": {"error_stub": "NOT_FOUND"}}},
                grpc.StatusCode.NOT_FOUND,
                0.55,
                5.0,
                600,
                id="error-stub-waits",
            ),
        ],
    )
    def test_which_statuses_wait_for_latency_ms(self, sim, scenario, want_code, min_s, max_s, want_row_latency_ms):
        status, body = _set_grpc(sim, "1", latency_ms=600, **scenario)
        assert status == 200, body
        started = time.monotonic()
        code, _ = _status_of(_call_get_latest_block, _GRPC_ADDRS["1"])
        elapsed = time.monotonic() - started
        assert code == want_code
        assert min_s <= elapsed < max_s, f"the status came after {elapsed:.3f} s with latency_ms=600"
        assert [row["latency_ms"] for row in _rows(sim)] == [want_row_latency_ms]

    def test_the_row_of_a_call_is_complete_while_the_provider_still_waits(self, sim):
        """A test of the smart-router reads the row of a call that the provider
        still holds. So the row must have its method, its status, its latency
        and its request id before the wait, and not after it."""
        status, body = _set_grpc(sim, "1", latency_ms=2000)
        assert status == 200, body
        outcome: dict = {}

        def _held_call():
            started = time.monotonic()
            resp = _call_all_balances(_GRPC_ADDRS["1"], "lava1-held-a")
            outcome["elapsed"] = time.monotonic() - started
            outcome["coins"] = [(coin.denom, coin.amount) for coin in resp.balances]

        caller = threading.Thread(target=_held_call)
        caller.start()
        try:
            rows: list[dict] = []
            deadline = time.monotonic() + 1.5
            while not rows and time.monotonic() < deadline:
                rows = _rows(sim, request_id="lava1-held-a")
                if not rows:
                    time.sleep(0.02)
            still_held = caller.is_alive()
        finally:
            caller.join(timeout=10)
        assert rows, "the call left no row in its first 1.5 seconds, while the provider held it"
        assert still_held, "the call had ended when the row was read, so the read proves nothing"
        assert [(row["method"], row["status"], row["latency_ms"], row["request_id"]) for row in rows] == [
            ("AllBalances", "success", 2000, "lava1-held-a")
        ]
        assert outcome["coins"] == [("ulava", "1000000")]
        assert outcome["elapsed"] >= 1.9, f"the reply came after {outcome['elapsed']:.3f} s with latency_ms=2000"

    def test_a_pause_does_not_delay_a_grpc_call(self, sim):
        """The control API stores a pause for a gRPC provider, and the gRPC
        adapter does not perform it: a pause belongs to the HTTP write path."""
        status, body = _set_grpc(sim, "1", pause_at="mid_body", pause_ms=3000)
        assert status == 200, body
        started = time.monotonic()
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        elapsed = time.monotonic() - started
        assert resp.block.header.height == GRPC_LATEST_BLOCK
        assert elapsed < 1.5, f"the reply came after {elapsed:.3f} s, so the pause of 3 s was not performed"

    def test_a_fault_key_in_a_per_method_override_reaches_the_call_of_that_method(self, sim):
        """The flow merges the fault keys of a per-method override, such as
        ``mode`` and ``latency_ms``, as on JSON-RPC and REST. The call of the
        named method waits for the latency of the entry, and then it gets the
        status of a down provider. Its row names the method: the request was
        read to find the entry. A call of another method is answered at once."""
        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"mode": "down", "latency_ms": 600}})
        assert status == 200, body
        started = time.monotonic()
        answer = _status_of(_call_get_latest_block, _GRPC_ADDRS["1"])
        elapsed = time.monotonic() - started
        assert answer == (grpc.StatusCode.UNAVAILABLE, "provider down")
        assert 0.55 <= elapsed < 5.0, f"the status came after {elapsed:.3f} s with latency_ms=600 in the entry"
        assert _call_get_node_info(_GRPC_ADDRS["1"]).default_node_info.network == "lava-sim"
        assert [(row["method"], row["status"], row["latency_ms"]) for row in _rows(sim)] == [
            ("GetLatestBlock", "down", 600),
            ("GetNodeInfo", "success", 0),
        ]

    def test_fail_first_n_gives_exactly_that_many_down_rows(self, sim):
        status, body = _set_grpc(sim, "1", mode="down", fail_first_n=3, then_mode="success")
        assert status == 200, body
        codes = [_status_of(_call_get_latest_block, _GRPC_ADDRS["1"])[0] for _ in range(5)]
        unavailable, ok = grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.OK
        assert codes == [unavailable, unavailable, unavailable, ok, ok]
        assert [row["status"] for row in _rows(sim)] == ["down", "down", "down", "success", "success"]
        assert len(_rows(sim, status="down")) == 3


class TestGrpcPerMethodErrors:
    """A per-method ``error_stub`` or ``error`` override reaches the caller as a
    status. The smart-router has a rule for each status, and its tests set each
    one with ``error_stub``."""

    @pytest.mark.parametrize("name", _ERROR_STATUS_NAMES)
    def test_an_error_stub_gives_the_status_of_its_name_with_the_name_as_the_text(self, sim, name):
        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"error_stub": name}})
        assert status == 200, body
        code, text = _status_of(_call_get_latest_block, _GRPC_ADDRS["1"])
        assert (code.name, text) == (name, name)
        assert [(row["method"], row["status"]) for row in _rows(sim)] == [("GetLatestBlock", "error")]

    def test_an_error_stub_takes_its_text_from_the_key_message(self, sim):
        override = {"error_stub": "NOT_FOUND", "message": "the block is gone"}
        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": override})
        assert status == 200, body
        want = (grpc.StatusCode.NOT_FOUND, "the block is gone")
        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == want

    def test_an_error_stub_whose_name_is_no_status_gives_unknown(self, sim):
        """``revert`` is the name of an error stub of the eth chain. It names no
        gRPC status, so the caller gets UNKNOWN, with the name as the text."""
        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"error_stub": "revert"}})
        assert status == 200, body
        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == (grpc.StatusCode.UNKNOWN, "revert")

    def test_an_error_stub_in_the_default_entry_reaches_each_method(self, sim):
        status, body = _set_grpc(sim, "1", responses={"default": {"error_stub": "ABORTED"}})
        assert status == 200, body
        want = (grpc.StatusCode.ABORTED, "ABORTED")
        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == want
        assert _status_of(_call_get_node_info, _GRPC_ADDRS["1"]) == want
        assert _status_of(_call_all_balances, _GRPC_ADDRS["1"], "lava1-default-a") == want

    def test_an_error_stub_of_one_method_leaves_the_other_methods_alone(self, sim):
        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"error_stub": "ABORTED"}})
        assert status == 200, body
        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == (grpc.StatusCode.ABORTED, "ABORTED")
        assert _call_get_node_info(_GRPC_ADDRS["1"]).default_node_info.network == "lava-sim"

    @pytest.mark.parametrize(
        "error, want_code, want_text",
        [
            pytest.param(
                {"code": "ABORTED", "message": "from the override"},
                grpc.StatusCode.ABORTED,
                "from the override",
                id="the-name-of-a-status",
            ),
            pytest.param(
                {"code": 7, "message": "by number"},
                grpc.StatusCode.PERMISSION_DENIED,
                "by number",
                id="the-number-of-a-status",
            ),
            pytest.param(
                {"code": "no status has this name"}, grpc.StatusCode.UNKNOWN, "override", id="a-code-that-is-no-status"
            ),
        ],
    )
    def test_an_error_override_gives_its_code_and_its_message(self, sim, error, want_code, want_text):
        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"error": error}})
        assert status == 200, body
        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == (want_code, want_text)
        assert [row["status"] for row in _rows(sim)] == ["error"]

    def test_an_error_stub_that_is_a_number_gives_unknown_with_the_number_as_the_text(self, sim):
        """An ``error_stub`` is the name of a status. A number is no name, so the
        caller gets UNKNOWN. The per-method ``error`` override is the key that
        reads a number."""
        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"error_stub": 5}})
        assert status == 200, body
        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == (grpc.StatusCode.UNKNOWN, "5")
        assert [row["status"] for row in _rows(sim)] == ["error"]

    def test_a_per_method_error_that_is_no_object_ends_the_call_and_leaves_the_row_in_flight(self, sim):
        """A fault of today, recorded as it is. The ``error`` override must be
        an object. With a text in its place the simulator fails before it
        finishes the row: the caller gets UNKNOWN, and the row stays in_flight."""
        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"error": "NOT_FOUND"}})
        assert status == 200, body
        code, text = _status_of(_call_get_latest_block, _GRPC_ADDRS["1"])
        assert code == grpc.StatusCode.UNKNOWN
        assert "AttributeError" in text
        assert [(row["method"], row["status"]) for row in _rows(sim)] == [("*", "in_flight")]

    def test_an_error_stub_applies_when_a_filter_does_not_name_the_endpoint(self, sim):
        """An ``error_stub`` of a per-method override is not a fault of the
        endpoint, so a filter does not hold it back. The endpoint is ``http2``,
        and the filter names ``http``."""
        override = {"GetLatestBlock": {"error_stub": "NOT_FOUND"}}
        status, body = _set_grpc(sim, "1", transports=["http"], responses=override)
        assert status == 200, body
        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == (grpc.StatusCode.NOT_FOUND, "NOT_FOUND")

    def test_an_error_stub_that_is_an_object_gives_unknown_with_the_object_as_the_text(self, sim):
        """The shape of the ``error`` override, given to ``error_stub`` by
        mistake. The control API stores it. The caller gets UNKNOWN, the text
        is the object as text, and the row says error."""
        override = {"GetLatestBlock": {"error_stub": {"code": "NOT_FOUND"}}}
        status, body = _set_grpc(sim, "1", responses=override)
        assert status == 200, body
        answer = _status_of(_call_get_latest_block, _GRPC_ADDRS["1"])
        assert answer == (grpc.StatusCode.UNKNOWN, "{'code': 'NOT_FOUND'}")
        assert [(row["method"], row["status"]) for row in _rows(sim)] == [("GetLatestBlock", "error")]


class TestGrpcReplyFields:
    """The fields of the two reply messages of the tendermint service, with no
    override and with a per-method ``result`` override."""

    def test_get_node_info_has_these_fields(self, sim):
        resp = _call_get_node_info(_GRPC_ADDRS["1"])
        node, application = resp.default_node_info, resp.application_version
        assert (node.network, node.moniker, node.version) == ("lava-sim", "lava-sim-grpc-provider", "sim-1.0")
        assert (application.name, application.app_name, application.version) == ("lava-sim", "lava-sim-app", "sim-1.0")

    def test_a_result_override_sets_the_fields_of_get_latest_block(self, sim):
        result = {"height": 7, "chain_id": "another-chain"}
        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"result": result}})
        assert status == 200, body
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert (resp.block.header.height, resp.block.header.chain_id) == (7, "another-chain")
        assert len(resp.block_id.hash) == 32

    def test_a_result_override_sets_the_fields_of_get_node_info(self, sim):
        result = {"network": "n-1", "moniker": "m-1", "version": "v-1", "app_name": "a-1", "app_version": "av-1"}
        status, body = _set_grpc(sim, "1", responses={"GetNodeInfo": {"result": result}})
        assert status == 200, body
        resp = _call_get_node_info(_GRPC_ADDRS["1"])
        node, application = resp.default_node_info, resp.application_version
        assert (node.network, node.moniker, node.version) == ("n-1", "m-1", "v-1")
        assert (application.name, application.app_name, application.version) == ("lava-sim", "a-1", "av-1")

    def test_a_result_override_in_the_default_entry_reaches_get_latest_block(self, sim):
        status, body = _set_grpc(sim, "1", responses={"default": {"result": {"height": 9}}})
        assert status == 200, body
        assert _call_get_latest_block(_GRPC_ADDRS["1"]).block.header.height == 9

    @pytest.mark.parametrize(
        "result, want_height, want_chain_id",
        [
            pytest.param({"height": 7}, 7, "lava-sim", id="only-the-height"),
            pytest.param({"chain_id": "another-chain"}, GRPC_LATEST_BLOCK, "another-chain", id="only-the-chain-id"),
            pytest.param({}, GRPC_LATEST_BLOCK, "lava-sim", id="an-empty-object"),
            pytest.param("not an object", GRPC_LATEST_BLOCK, "lava-sim", id="no-object"),
        ],
    )
    def test_a_field_that_a_result_override_does_not_set_has_the_default_of_get_latest_block(
        self, sim, result, want_height, want_chain_id
    ):
        """A ``result`` override replaces the data of the chain. The builder of
        the reply then gives its own default to each field that the override
        does not set. A ``result`` that is no object sets no field."""
        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"result": result}})
        assert status == 200, body
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert (resp.block.header.height, resp.block.header.chain_id) == (want_height, want_chain_id)
        assert [row["status"] for row in _rows(sim)] == ["success"]

    @pytest.mark.parametrize(
        "result, want_network",
        [
            pytest.param({"network": "n-1"}, "n-1", id="only-the-network"),
            pytest.param("not an object", "lava-sim", id="no-object"),
        ],
    )
    def test_a_field_that_a_result_override_does_not_set_has_the_default_of_get_node_info(
        self, sim, result, want_network
    ):
        status, body = _set_grpc(sim, "1", responses={"GetNodeInfo": {"result": result}})
        assert status == 200, body
        resp = _call_get_node_info(_GRPC_ADDRS["1"])
        node, application = resp.default_node_info, resp.application_version
        assert (node.network, node.moniker, node.version) == (want_network, "lava-sim-grpc-provider", "sim-1.0")
        assert (application.name, application.app_name, application.version) == ("lava-sim", "lava-sim-app", "sim-1.0")

    def test_a_result_override_applies_when_a_filter_does_not_name_the_endpoint(self, sim):
        """A ``result`` of a per-method override is not a fault of the endpoint,
        so a filter does not hold it back. The endpoint is ``http2``, and the
        filter names ``http``."""
        override = {"GetLatestBlock": {"result": {"height": 7}}}
        status, body = _set_grpc(sim, "1", transports=["http"], responses=override)
        assert status == 200, body
        assert _call_get_latest_block(_GRPC_ADDRS["1"]).block.header.height == 7

    def test_a_missing_field_that_names_no_field_of_the_reply_leaves_the_reply_whole(self, sim):
        status, body = _set_grpc(sim, "1", corruption_mode="missing_field", missing_field="no_such_field")
        assert status == 200, body
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert (resp.block.header.height, resp.block.header.chain_id) == (GRPC_LATEST_BLOCK, "lava-sim")
        assert len(resp.block_id.hash) == 32
        assert [row["status"] for row in _rows(sim)] == ["success"]

    def test_a_result_override_of_a_wrong_type_ends_the_call_after_the_row_says_success(self, sim):
        """A known fault, recorded as it is today. The row of a call is written
        before the reply message is built. A ``height`` that is no number makes
        the build of the reply fail: the caller gets UNKNOWN, and the row says
        success. AllBalances does not have this fault: its reply is built for
        each shape of an override."""
        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"result": {"height": "not a number"}}})
        assert status == 200, body
        code, text = _status_of(_call_get_latest_block, _GRPC_ADDRS["1"])
        assert code == grpc.StatusCode.UNKNOWN
        assert "TypeError" in text
        assert [row["status"] for row in _rows(sim)] == ["success"]


class TestGrpcCallsThatReachNoListener:
    """The gRPC library answers three kinds of call before the listener sees
    them: a method of a served service that the simulator does not serve, a
    method of a service that is not registered, and a request that is no
    message. None of them writes a history row or a count."""

    @pytest.mark.parametrize(
        "method", ["GetSyncing", "GetBlockByHeight", "GetLatestValidatorSet", "GetValidatorSetByHeight", "ABCIQuery"]
    )
    def test_a_method_of_the_tendermint_service_that_is_not_served_answers_unimplemented(self, sim, method):
        """The tendermint service has seven methods. The simulator serves
        GetLatestBlock and GetNodeInfo, and these are the five others."""
        code, text = _status_of(_call_raw, _GRPC_ADDRS["1"], f"/cosmos.base.tendermint.v1beta1.Service/{method}")
        assert code == grpc.StatusCode.UNIMPLEMENTED
        assert "Method not implemented!" in text

    def test_a_method_of_a_service_that_is_not_registered_has_another_text(self, sim):
        """The staking stubs are compiled, and the simulator does not register
        the staking service. The text differs from the text of a method that a
        registered service does not serve, so a test can tell the two apart."""
        answer = _status_of(_call_raw, _GRPC_ADDRS["1"], "/cosmos.staking.v1beta1.Query/Params")
        assert answer == (grpc.StatusCode.UNIMPLEMENTED, "Method not found!")

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("/cosmos.bank.v1beta1.Query/AllBalances", id="all-balances"),
            pytest.param(_GET_LATEST_BLOCK_PATH, id="get-latest-block"),
        ],
    )
    def test_a_request_that_is_no_message_answers_unknown(self, sim, path):
        code, text = _status_of(_call_raw, _GRPC_ADDRS["1"], path, b"\xff\xff\xff")
        assert code == grpc.StatusCode.UNKNOWN
        assert "DecodeError" in text

    def test_none_of_these_calls_writes_a_row_or_a_count(self, sim):
        for path, payload in (
            ("/cosmos.bank.v1beta1.Query/TotalSupply", b""),
            ("/cosmos.base.tendermint.v1beta1.Service/GetSyncing", b""),
            ("/cosmos.staking.v1beta1.Query/Params", b""),
            ("/cosmos.bank.v1beta1.Query/AllBalances", b"\xff\xff\xff"),
        ):
            assert _status_of(_call_raw, _GRPC_ADDRS["1"], path, payload)[0] != grpc.StatusCode.OK
        _, stats = _get(_ctrl(sim, "/stats"))
        assert _rows(sim) == []
        assert stats["providers"]["lava-sim-grpc:1"]["total_calls"] == 0

        # The control: a call that the listener does see writes one row and one count.
        _call_get_latest_block(_GRPC_ADDRS["1"])
        _, stats = _get(_ctrl(sim, "/stats"))
        assert len(_rows(sim)) == 1
        assert stats["providers"]["lava-sim-grpc:1"]["total_calls"] == 1


class TestGrpcDropPoint:
    """``drop_at`` on gRPC. A gRPC handler cannot cut the connection part way,
    so each drop point ends the call with UNAVAILABLE and "connection dropped".
    The drop points differ in one thing: for ``after_headers`` and ``mid_body``
    the provider sends the initial metadata first, and the status after it."""

    @pytest.mark.parametrize("drop_at", ["before_headers", "after_headers", "mid_body"])
    def test_a_grpc_client_reads_the_same_status_for_each_drop_point(self, sim, drop_at):
        """A client cannot tell the drop points apart. That is why the tests
        below read the frames."""
        status, body = _set_grpc(sim, "1", mode="drop_connection", drop_at=drop_at)
        assert status == 200, body
        want = (grpc.StatusCode.UNAVAILABLE, "connection dropped")
        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == want

    def test_a_reply_message_comes_as_headers_then_data_then_trailers(self, sim):
        """The control for the tests below. It shows that ``_frames_of_one_call``
        reads the frames of a call."""
        assert _frames_of_one_call(_GRPC_PORT_1, _GET_LATEST_BLOCK_PATH) == ["HEADERS", "DATA", "HEADERS+END_STREAM"]

    def test_a_status_with_no_drop_point_comes_as_one_frame(self, sim):
        status, body = _set_grpc(sim, "1", mode="down")
        assert status == 200, body
        assert _frames_of_one_call(_GRPC_PORT_1, _GET_LATEST_BLOCK_PATH) == ["HEADERS+END_STREAM"]

    @pytest.mark.parametrize(
        "drop_at, want_frames",
        [
            pytest.param("before_headers", ["HEADERS+END_STREAM"], id="before-headers-sends-the-status-alone"),
            pytest.param("after_headers", ["HEADERS", "HEADERS+END_STREAM"], id="after-headers-sends-metadata-first"),
            pytest.param("mid_body", ["HEADERS", "HEADERS+END_STREAM"], id="mid-body-sends-metadata-first"),
        ],
    )
    def test_two_drop_points_send_the_initial_metadata_before_the_status(self, sim, drop_at, want_frames):
        status, body = _set_grpc(sim, "1", mode="drop_connection", drop_at=drop_at)
        assert status == 200, body
        assert _frames_of_one_call(_GRPC_PORT_1, _GET_LATEST_BLOCK_PATH) == want_frames
        assert [(row["method"], row["status"]) for row in _rows(sim)] == [("GetLatestBlock", "drop_connection")]


# ─────────────────────────────────────────────────────────────────────────────
# Cross-pool fault isolation
# ─────────────────────────────────────────────────────────────────────────────


class TestGrpcCrossPoolIsolation:
    """Under the old bare-pid model, eth pid "1" and grpc pid "1" were ONE
    state object, so faults authored for other transports could reach the
    gRPC port (and a down always did). The pool:pid model abolishes that:
    lava-sim-grpc:1 owns its state alone."""

    def test_grpc_unaffected_by_eth_down_fault(self, sim):
        """mode=down on eth-sim:1 downs only eth-sim:1 — the gRPC pool keeps
        serving the clean stub."""
        _post(_ctrl(sim, "/scenario"), {"providers": {"eth-sim:1": {"mode": "down"}}})
        eth_status, _ = _post(_ETH_URLS["1"], {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
        assert eth_status == 503, f"eth-sim:1 must be down; got {eth_status}"
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert resp.block.header.height > 0, "lava-sim-grpc:1 must ignore an eth-sim down"

    def test_grpc_unaffected_by_eth_hang_fault(self, sim):
        """An eth-sim hang must not make the gRPC port sleep 30s. Client
        deadline is 5s (the helper default); the call should complete in
        well under that."""
        _post(_ctrl(sim, "/scenario"), {"providers": {"eth-sim:1": {"mode": "hang"}}})
        t0 = time.monotonic()
        resp = _call_get_latest_block(_GRPC_ADDRS["1"], timeout=5.0)
        elapsed = time.monotonic() - t0
        assert resp.block.header.height > 0
        assert elapsed < 2.0, f"gRPC should not hang for an eth-sim fault; got {elapsed:.2f}s"

    def test_grpc_unaffected_by_eth_rate_limit_fault(self, sim):
        """An eth-sim rate_limit must not abort the gRPC port with
        RESOURCE_EXHAUSTED."""
        _post(_ctrl(sim, "/scenario"), {"providers": {"eth-sim:1": {"mode": "rate_limit"}}})
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert resp.block.header.height > 0

    def test_grpc_unaffected_by_eth_error_fault(self, sim):
        """An eth-sim error must not abort the gRPC port with the translated
        grpc.StatusCode."""
        _post(
            _ctrl(sim, "/scenario"),
            {"providers": {"eth-sim:1": {"mode": "error", "error_message": "UNAVAILABLE"}}},
        )
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert resp.block.header.height > 0

    def test_grpc_fault_still_fires_on_its_own_pool(self, sim):
        """Sanity check: isolation must not break gRPC-side faults. A down on
        lava-sim-grpc:1 must still abort the gRPC port with UNAVAILABLE."""
        _set_grpc(sim, "1", mode="down")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(_GRPC_ADDRS["1"])
        assert exc_info.value.code() == grpc.StatusCode.UNAVAILABLE

    def test_grpc_unaffected_by_btc_down_fault(self, sim):
        """mode=down on btc-sim:1 downs only btc-sim:1 — the gRPC pool stays
        up."""
        _post(_ctrl(sim, "/scenario"), {"providers": {"btc-sim:1": {"mode": "down"}}})
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert resp.block.header.height > 0, "lava-sim-grpc:1 must ignore a btc-sim down"

    def test_grpc_unaffected_by_rest_down_fault(self, sim):
        """mode=down on lava-sim-rest:1 downs only that provider — the gRPC
        pool stays up (rest and grpc are two separate lava routers)."""
        _post(_ctrl(sim, "/scenario"), {"providers": {"lava-sim-rest:1": {"mode": "down"}}})
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert resp.block.header.height > 0, "lava-sim-grpc:1 must ignore a lava-sim-rest down"


# ─────────────────────────────────────────────────────────────────────────────
# Cross-pool corruption isolation
# ─────────────────────────────────────────────────────────────────────────────


class TestGrpcCrossPoolCorruptionIsolation:
    """Corruption authored for another pool's provider can never reach the
    gRPC pool — and the pool's own corruption still fires."""

    def test_grpc_unaffected_by_eth_corruption_invalid_proto(self, sim):
        """corruption_mode="invalid_proto" on eth-sim:1 must not abort the
        gRPC port with UNKNOWN."""
        _post(
            _ctrl(sim, "/scenario"),
            {"providers": {"eth-sim:1": {"corruption_mode": "invalid_proto"}}},
        )
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        # Clean stub response — chain_id pinned, height > 0.
        assert resp.block.header.chain_id == "lava-sim"
        assert resp.block.header.height > 0

    def test_grpc_unaffected_by_eth_corruption_missing_field(self, sim):
        """corruption_mode="missing_field" on eth-sim:1 must not clear the
        ``block`` field on the gRPC response."""
        _post(
            _ctrl(sim, "/scenario"),
            {"providers": {"eth-sim:1": {"corruption_mode": "missing_field", "missing_field": "block"}}},
        )
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        # block field is populated normally — height should be > 0 and
        # chain_id should be the pinned simulator value.
        assert resp.block.header.chain_id == "lava-sim"
        assert resp.block.header.height > 0

    def test_grpc_unaffected_by_eth_corruption_wrong_type(self, sim):
        """corruption_mode="wrong_type" on eth-sim:1 must not abort the gRPC
        port with INTERNAL."""
        _post(
            _ctrl(sim, "/scenario"),
            {"providers": {"eth-sim:1": {"corruption_mode": "wrong_type", "missing_field": "block"}}},
        )
        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert resp.block.header.chain_id == "lava-sim"
        assert resp.block.header.height > 0

    def test_grpc_corruption_still_fires_on_its_own_pool(self, sim):
        """Sanity check: isolation must not break gRPC-side corruption.
        corruption_mode="invalid_proto" on lava-sim-grpc:1 must still abort
        the gRPC port with UNKNOWN."""
        _set_grpc(sim, "1", corruption_mode="invalid_proto")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(_GRPC_ADDRS["1"])
        assert exc_info.value.code() == grpc.StatusCode.UNKNOWN


# ─────────────────────────────────────────────────────────────────────────────
# Sequenced faults stay inside their pool
# ─────────────────────────────────────────────────────────────────────────────


class TestGrpcSequencedFaultIsolation:

    def test_grpc_healthy_through_eth_down_window(self, sim):
        """A sequenced down (fail_first_n) on eth-sim:1 opens and closes its
        window on eth-sim:1 alone. The gRPC pool serves the clean stub
        before, during, and after — it neither observes nor advances another
        pool's window."""
        _post(
            _ctrl(sim, "/scenario"),
            {"providers": {"eth-sim:1": {"mode": "down", "fail_first_n": 2, "then_mode": "success"}}},
        )

        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert resp.block.header.height > 0, "gRPC must be healthy while eth's window is open"

        for i in (1, 2):
            eth_status, _ = _post(
                _ETH_URLS["1"],
                {"jsonrpc": "2.0", "id": i, "method": "eth_blockNumber", "params": []},
            )
            assert eth_status == 503, f"eth-sim:1 call {i} is inside the down window; got {eth_status}"

        eth_status, _ = _post(_ETH_URLS["1"], {"jsonrpc": "2.0", "id": 3, "method": "eth_blockNumber", "params": []})
        assert eth_status == 200, f"eth-sim:1 must recover after the window; got {eth_status}"

        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
        assert resp.block.header.height > 0, "gRPC must still be healthy after eth's window"
