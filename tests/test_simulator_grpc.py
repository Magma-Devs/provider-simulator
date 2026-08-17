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

Run with:
  pytest tests/test_simulator_grpc.py -v
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request

import grpc
import pytest

# Splice cosmos_pb2 onto sys.path so the generated stubs resolve. Must run
# before the `from cosmos...` import below — isort must not reorder these
# two (see cosmos_pb2/__init__.py's own docstring for why import order here
# matters).
import cosmos_pb2  # noqa: F401  isort: split

from cosmos.base.tendermint.v1beta1 import query_pb2, query_pb2_grpc  # isort: skip

from provider_simulator.chains.lava import GRPC_LATEST_BLOCK
from provider_simulator.topology import port_of

# Primary tier only — pids 4-6 of each pool are the backup listeners, covered
# by test_simulator_backup_listeners.py.
_PRIMARY_PIDS = ("1", "2", "3")
_GRPC_ADDRS = {pid: f"127.0.0.1:{port_of('lava-sim-grpc', pid, 'grpc', 'http2')}" for pid in _PRIMARY_PIDS}
_ETH_URLS = {pid: f"http://127.0.0.1:{port_of('eth-sim', pid)}" for pid in _PRIMARY_PIDS}


# ── HTTP helpers for the control plane ──────────────────────────────────────


def _post(url: str, body: dict) -> tuple[int, dict]:
    """POST JSON body, return (status_code, parsed_response_body)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
            return e.code, json.loads(raw) if raw else {}
        except (ConnectionResetError, OSError):
            return e.code, {}


def _get(url: str) -> tuple[int, dict]:
    """GET url, return (status_code, parsed_response_body)."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else {}


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
        """gRPC has no JSON-RPC id equivalent — history entries record
        request_id=None for every gRPC request."""
        _call_get_latest_block(_GRPC_ADDRS["1"])
        _, hist = _get(_ctrl(sim, "/history?pool=lava-sim-grpc&pid=1"))
        assert hist["history"][-1]["request_id"] is None


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
