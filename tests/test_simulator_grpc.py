"""
Unit tests for the gRPC chain dispatch in the provider simulator (MAG-1780).

Mirrors the structure of ``tests/test_simulator_btc.py`` (MAG-1716's BTC
suite) but covers only gRPC-specific behaviour:

  Happy-path                 — GetLatestBlock / GetNodeInfo respond with a
                                well-formed protobuf.
  Metadata capture            — lava-* request metadata shows up in /history.
  Fault primitives            — set_hang / set_status / set_dropped /
                                set_corrupt / set_stale all behave correctly
                                over gRPC.
  Mixed-chain                 — one eth provider + one grpc provider in the
                                same /scenario body, each independently
                                faulted.
  History tracking            — gRPC requests show up in /history exactly
                                like ETH/BTC ones, with the gRPC method name
                                preserved (no JSON-RPC id).

Run with:
  /Users/victoria/smart_router_automation/.venv/bin/python -m pytest \
      tests/test_simulator_grpc.py -v

These tests run against an in-process simulator (a ``ProviderState`` plus a
grpc.aio server on a non-default port). They do not touch the cluster or
the production simulator ports — picked 49548-49550 for the gRPC providers
and 49000 for the control API so they coexist with the BTC suite (38545+)
and the ETH suite (28545+) when run side-by-side.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer, ThreadingHTTPServer
from typing import Any

import grpc
import pytest

# Splice cosmos_pb2 onto sys.path so the generated stubs resolve.
import cosmos_pb2  # noqa: F401
from cosmos.base.tendermint.v1beta1 import query_pb2, query_pb2_grpc

import grpc_server
from server import ControlHandler, JSONRPCHandler, ProviderState


# ── Test ports (distinct from ETH 28545+ / BTC 38545+) ──────────────────────
_GRPC_PORTS    = {"1": 49548, "2": 49549, "3": 49550}
_JSONRPC_PORTS = {"1": 49545, "2": 49546, "3": 49547}
_CONTROL_PORT  = 49000


# ── HTTP helpers for the control plane (no JSON-RPC over HTTP needed) ───────

def _post(url: str, body: dict) -> tuple[int, dict]:
    """POST JSON body, return (status_code, parsed_response_body)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
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


# ── Module-scoped fixture: start all servers once ───────────────────────────

@pytest.fixture(scope="module")
def sim():
    """Start 3 JSON-RPC + 3 gRPC + 1 control servers on dedicated ports.

    The JSON-RPC servers are spawned alongside the gRPC ones so /history
    queries via the control API see both transports — same lifecycle
    contract as the production ``main()``.

    Yields a dict:
      sim["control"]   → http://127.0.0.1:49000
      sim["grpc1"]     → "127.0.0.1:49548"  (use as channel address)
      sim["grpc2"]     → "127.0.0.1:49549"
      sim["grpc3"]     → "127.0.0.1:49550"
    """
    states = {pid: ProviderState() for pid in _GRPC_PORTS}

    servers = []
    for pid, port in _JSONRPC_PORTS.items():
        srv = ThreadingHTTPServer(("127.0.0.1", port), JSONRPCHandler)
        srv.daemon_threads = True
        srv.state = states[pid]
        srv.provider_id = pid
        servers.append(srv)

    ctrl = HTTPServer(("127.0.0.1", _CONTROL_PORT), ControlHandler)
    ctrl.provider_states = states
    servers.append(ctrl)

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]

    # gRPC servers — each on its own asyncio loop in a daemon thread.
    for pid, port in _GRPC_PORTS.items():
        threads.append(threading.Thread(
            target=grpc_server.run_grpc_in_thread,
            args=(port, states[pid]),
            daemon=True,
            name=f"grpc-test-{pid}",
        ))

    for t in threads:
        t.start()

    # Wait long enough for both HTTP and gRPC servers to bind. gRPC needs
    # a touch more time because the asyncio loop has to spin up.
    time.sleep(0.5)

    yield {
        "control": f"http://127.0.0.1:{_CONTROL_PORT}",
        "grpc1":   f"127.0.0.1:{_GRPC_PORTS['1']}",
        "grpc2":   f"127.0.0.1:{_GRPC_PORTS['2']}",
        "grpc3":   f"127.0.0.1:{_GRPC_PORTS['3']}",
    }

    for s in servers:
        s.shutdown()


# ── Function-scoped autouse: clean slate before/after every test ────────────

@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario AND clear history before and after every test."""
    _post(_ctrl(sim, "/reset/all"), {})
    yield
    _post(_ctrl(sim, "/reset/all"), {})


# ── gRPC client helpers ─────────────────────────────────────────────────────

def _set_grpc(sim, pid: str = "1", **extra):
    """Convenience: POST /scenario for one provider with chain_family=grpc."""
    cfg = {"chain_family": "grpc", **extra}
    return _post(_ctrl(sim, "/scenario"), {"providers": {pid: cfg}})


def _call_get_latest_block(address: str, timeout: float = 5.0,
                            metadata: tuple = ()) -> query_pb2.GetLatestBlockResponse:
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
                stub.GetLatestBlock(req, metadata=metadata), timeout=timeout,
            )
            return resp
        finally:
            await channel.close()

    return asyncio.run(_do())


def _call_get_node_info(address: str, timeout: float = 5.0,
                         metadata: tuple = ()) -> query_pb2.GetNodeInfoResponse:
    """Open an insecure channel, call GetNodeInfo, return the response."""
    async def _do():
        channel = grpc.aio.insecure_channel(address)
        try:
            stub = query_pb2_grpc.ServiceStub(channel)
            req = query_pb2.GetNodeInfoRequest()
            resp = await asyncio.wait_for(
                stub.GetNodeInfo(req, metadata=metadata), timeout=timeout,
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
        _set_grpc(sim, "1")
        resp = _call_get_latest_block(sim["grpc1"])
        assert resp.block.header.chain_id == "lava-sim"
        assert resp.block.header.height > 0

    def test_get_latest_block_height_matches_constant(self, sim):
        """The default head is pinned to handlers_grpc.GRPC_LATEST_BLOCK
        so tests can assert exact equality."""
        from handlers_grpc import GRPC_LATEST_BLOCK
        _set_grpc(sim, "1")
        resp = _call_get_latest_block(sim["grpc1"])
        assert resp.block.header.height == GRPC_LATEST_BLOCK

    def test_get_latest_block_carries_block_id(self, sim):
        _set_grpc(sim, "1")
        resp = _call_get_latest_block(sim["grpc1"])
        assert len(resp.block_id.hash) == 32

    def test_get_node_info_returns_valid_proto(self, sim):
        _set_grpc(sim, "1")
        resp = _call_get_node_info(sim["grpc1"])
        assert resp.default_node_info.network == "lava-sim"
        assert resp.application_version.name == "lava-sim"


# ─────────────────────────────────────────────────────────────────────────────
# Lava metadata capture
# ─────────────────────────────────────────────────────────────────────────────

class TestGrpcMetadataCapture:
    """lava-* request metadata must show up in /history under the recorded
    entry so the router's observable behaviour can be verified."""

    def test_lava_guid_captured(self, sim):
        _set_grpc(sim, "1")
        _call_get_latest_block(sim["grpc1"], metadata=(("lava-guid", "abc123"),))
        _, hist = _get(_ctrl(sim, "/history?provider=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "GetLatestBlock"
        assert last["lava_headers"].get("lava-guid") == "abc123"

    def test_multiple_lava_headers_captured(self, sim):
        _set_grpc(sim, "1")
        _call_get_latest_block(sim["grpc1"], metadata=(
            ("lava-guid", "g1"),
            ("lava-stateful-api", "true"),
            ("authorization", "bearer ignored"),  # non-lava header — should be dropped
        ))
        _, hist = _get(_ctrl(sim, "/history?provider=1"))
        headers = hist["history"][-1]["lava_headers"]
        assert headers.get("lava-guid") == "g1"
        assert headers.get("lava-stateful-api") == "true"
        # Only lava-* headers are captured — authorization must be absent.
        assert "authorization" not in headers

    def test_history_filter_by_lava_header(self, sim):
        """The existing /history?lava_header_<name>= filter must work for
        gRPC requests, not just JSON-RPC ones."""
        _set_grpc(sim, "1")
        _call_get_latest_block(sim["grpc1"], metadata=(("lava-guid", "match-me"),))
        _call_get_latest_block(sim["grpc1"], metadata=(("lava-guid", "other"),))
        _, hist = _get(_ctrl(sim, "/history?lava_header_lava_guid=match-me"))
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
            _call_get_latest_block(sim["grpc1"], timeout=2.0)
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
            _call_get_latest_block(sim["grpc1"])
        assert exc_info.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED

    def test_unavailable_status(self, sim):
        _set_grpc(sim, "1", mode="error", error_message="UNAVAILABLE")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(sim["grpc1"])
        assert exc_info.value.code() == grpc.StatusCode.UNAVAILABLE

    def test_internal_status(self, sim):
        _set_grpc(sim, "1", mode="error", error_message="INTERNAL")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(sim["grpc1"])
        assert exc_info.value.code() == grpc.StatusCode.INTERNAL

    def test_integer_error_code_fallback(self, sim):
        """When error_message isn't a recognised name, error_code (integer)
        is consulted next. 8 = RESOURCE_EXHAUSTED per the gRPC spec."""
        _set_grpc(sim, "1", mode="error", error_message="not-a-grpc-name", error_code=8)
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(sim["grpc1"])
        assert exc_info.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED

    def test_unknown_fallback(self, sim):
        """Unmatched name AND unmatched int → UNKNOWN."""
        _set_grpc(sim, "1", mode="error", error_message="garbage", error_code=-999)
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(sim["grpc1"])
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
            _call_get_latest_block(sim["grpc1"])
        # Server aborts UNAVAILABLE before sending initial metadata.
        assert exc_info.value.code() in (
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.CANCELLED,
        )

    def test_drop_after_headers(self, sim):
        _set_grpc(sim, "1", mode="drop_connection", drop_at="after_headers")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(sim["grpc1"])
        assert exc_info.value.code() in (
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.CANCELLED,
        )

    def test_drop_mid_body(self, sim):
        """Unary RPC has no mid-body — the gRPC sim collapses mid_body to
        the after_headers shape until streaming support lands."""
        _set_grpc(sim, "1", mode="drop_connection", drop_at="mid_body")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(sim["grpc1"])
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
            _call_get_latest_block(sim["grpc1"])
        assert exc_info.value.code() == grpc.StatusCode.UNKNOWN

    def test_empty_response(self, sim):
        _set_grpc(sim, "1", corruption_mode="empty_response")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(sim["grpc1"])
        assert exc_info.value.code() == grpc.StatusCode.UNKNOWN

    def test_truncated(self, sim):
        _set_grpc(sim, "1", corruption_mode="truncated")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(sim["grpc1"])
        assert exc_info.value.code() == grpc.StatusCode.UNKNOWN

    def test_missing_field(self, sim):
        """missing_field=block clears the ``block`` field — client sees a
        valid response with no ``block`` payload."""
        _set_grpc(sim, "1", corruption_mode="missing_field", missing_field="block")
        resp = _call_get_latest_block(sim["grpc1"])
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
            _call_get_latest_block(sim["grpc1"])
        assert exc_info.value.code() == grpc.StatusCode.INTERNAL


# ─────────────────────────────────────────────────────────────────────────────
# Fault: stale head (blocks_behind shifts height)
# ─────────────────────────────────────────────────────────────────────────────

class TestGrpcFaultStale:
    """blocks_behind=N must decrement ``block.header.height`` by N."""

    def test_blocks_behind_shifts_height(self, sim):
        from handlers_grpc import GRPC_LATEST_BLOCK
        _set_grpc(sim, "1", blocks_behind=100)
        resp = _call_get_latest_block(sim["grpc1"])
        assert resp.block.header.height == GRPC_LATEST_BLOCK - 100

    def test_blocks_behind_zero_is_default_head(self, sim):
        from handlers_grpc import GRPC_LATEST_BLOCK
        _set_grpc(sim, "1", blocks_behind=0)
        resp = _call_get_latest_block(sim["grpc1"])
        assert resp.block.header.height == GRPC_LATEST_BLOCK

    def test_large_blocks_behind(self, sim):
        from handlers_grpc import GRPC_LATEST_BLOCK
        _set_grpc(sim, "1", blocks_behind=1_000_000)
        resp = _call_get_latest_block(sim["grpc1"])
        assert resp.block.header.height == GRPC_LATEST_BLOCK - 1_000_000


# ─────────────────────────────────────────────────────────────────────────────
# Chain-family routing — eth + grpc providers in the same scenario
# ─────────────────────────────────────────────────────────────────────────────

class TestGrpcChainFamily:
    """``chain_family="grpc"`` shows up in /scenario; gRPC and JSON-RPC
    providers can coexist in the same /scenario body, each handling
    its own transport."""

    def test_chain_family_grpc_visible_in_scenario(self, sim):
        _set_grpc(sim, "1")
        _, body = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["1"]["chain_family"] == "grpc"

    def test_mixed_eth_and_grpc(self, sim):
        """Provider 1 = grpc, Provider 2 = eth (default) — each transport
        independently faulted in the same /scenario call."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {"chain_family": "grpc"},
                "2": {"chain_family": "eth", "mode": "rate_limit"},
            }
        })

        # gRPC side: GetLatestBlock returns valid proto.
        resp = _call_get_latest_block(sim["grpc1"])
        assert resp.block.header.height > 0

        # ETH side: rate-limited HTTP/JSON-RPC request returns 429.
        eth_url = f"http://127.0.0.1:{_JSONRPC_PORTS['2']}"
        status, _ = _post(eth_url, {"jsonrpc": "2.0", "id": 1,
                                       "method": "eth_blockNumber", "params": []})
        assert status == 429

    def test_reset_clears_chain_family_grpc(self, sim):
        _set_grpc(sim, "1")
        _post(_ctrl(sim, "/reset"), {})
        _, body = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["1"]["chain_family"] == "eth"


# ─────────────────────────────────────────────────────────────────────────────
# History tracking — gRPC requests show up in /history
# ─────────────────────────────────────────────────────────────────────────────

class TestGrpcHistoryTracking:
    """gRPC requests must be recorded in /history with the same shape as
    HTTP requests, so cross-transport correlations work."""

    def test_grpc_request_recorded(self, sim):
        _set_grpc(sim, "1")
        _call_get_latest_block(sim["grpc1"])
        _, hist = _get(_ctrl(sim, "/history?provider=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "GetLatestBlock"
        assert last["status"] == "success"

    def test_grpc_history_filter_by_method(self, sim):
        _set_grpc(sim, "1")
        _call_get_latest_block(sim["grpc1"])
        _call_get_node_info(sim["grpc1"])
        _, hist = _get(_ctrl(sim, "/history?method=GetLatestBlock"))
        assert hist["count"] >= 1
        assert all(e["method"] == "GetLatestBlock" for e in hist["history"])

    def test_grpc_error_status_recorded(self, sim):
        """Fault-injected gRPC requests show up with status=error."""
        _set_grpc(sim, "1", mode="error", error_message="RESOURCE_EXHAUSTED")
        with pytest.raises(grpc.RpcError):
            _call_get_latest_block(sim["grpc1"])
        _, hist = _get(_ctrl(sim, "/history?provider=1&status=error"))
        assert hist["count"] >= 1
        assert hist["history"][-1]["method"] == "GetLatestBlock"

    def test_grpc_request_id_is_none(self, sim):
        """gRPC has no JSON-RPC id equivalent — history entries record
        request_id=None for every gRPC request."""
        _set_grpc(sim, "1")
        _call_get_latest_block(sim["grpc1"])
        _, hist = _get(_ctrl(sim, "/history?provider=1"))
        assert hist["history"][-1]["request_id"] is None


# ─────────────────────────────────────────────────────────────────────────────
# MAG-1836: cross-transport fault isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestGrpcCrossTransportFaultIsolation:
    """``ProviderState`` is shared across JSON-RPC and gRPC for the same
    provider id. A fault primitive (``down`` / ``hang`` / ``rate_limit`` /
    ``error``) authored for one transport must not fire on the other.

    Without the chain_family gate in ``_apply_grpc_fault``, a JSON-RPC
    test that sets ``{"chain_family": "eth", "mode": "down"}`` on provider 1
    would also kill the gRPC port for provider 1 — release-smoke gRPC
    tests would fail intermittently depending on test ordering.

    These tests verify the gate by setting non-grpc faults via /scenario
    and asserting the gRPC port still returns a normal stub response.
    """

    def test_grpc_killed_by_eth_down_fault(self, sim):
        """A ``chain_family="eth"`` down fault MUST abort the gRPC port
        with ``UNAVAILABLE``.

        MAG-2092: mode="down" is honored on every transport regardless of
        chain_family because reachability is provider-wide. Without the
        universal-down semantic, an ETH provider in mode=down would keep
        serving gRPC requests, hiding router-side bugs that depend on the
        provider being unreachable across every node-url (e.g. MAG-2061).
        Per-transport isolation still applies to content modes (error /
        corrupt / hang / rate_limit / drop_connection) — see sibling
        hang / rate_limit / error tests below."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"chain_family": "eth", "mode": "down"}}
        })
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(sim["grpc1"])
        assert exc_info.value.code() == grpc.StatusCode.UNAVAILABLE, (
            f"gRPC should abort with UNAVAILABLE under universal-down; got {exc_info.value.code()}"
        )

    def test_grpc_unaffected_by_eth_hang_fault(self, sim):
        """JSON-RPC ``hang`` must not make the gRPC port sleep 30s.

        Client deadline is 5s (the helper default); the call should
        complete in well under that.
        """
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"chain_family": "eth", "mode": "hang"}}
        })
        t0 = time.monotonic()
        resp = _call_get_latest_block(sim["grpc1"], timeout=5.0)
        elapsed = time.monotonic() - t0
        assert resp.block.header.height > 0
        assert elapsed < 2.0, f"gRPC should not hang for eth fault; got {elapsed:.2f}s"

    def test_grpc_unaffected_by_eth_rate_limit_fault(self, sim):
        """JSON-RPC ``rate_limit`` must not abort the gRPC port with
        RESOURCE_EXHAUSTED."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"chain_family": "eth", "mode": "rate_limit"}}
        })
        resp = _call_get_latest_block(sim["grpc1"])
        assert resp.block.header.height > 0

    def test_grpc_unaffected_by_eth_error_fault(self, sim):
        """JSON-RPC ``error`` must not abort the gRPC port with the
        translated grpc.StatusCode."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {
                "chain_family": "eth",
                "mode": "error",
                "error_message": "UNAVAILABLE",
            }}
        })
        resp = _call_get_latest_block(sim["grpc1"])
        assert resp.block.header.height > 0

    def test_grpc_fault_still_fires_when_chain_family_is_grpc(self, sim):
        """Sanity check: the gate must not break gRPC-side faults.

        A ``down`` fault with ``chain_family="grpc"`` must still abort
        the gRPC port with UNAVAILABLE."""
        _set_grpc(sim, "1", mode="down")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(sim["grpc1"])
        assert exc_info.value.code() == grpc.StatusCode.UNAVAILABLE

    def test_grpc_killed_by_btc_down_fault(self, sim):
        """MAG-2092 universal-down: a ``chain_family="btc"`` mode=down
        also aborts the gRPC port with UNAVAILABLE."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"chain_family": "btc", "mode": "down"}}
        })
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(sim["grpc1"])
        assert exc_info.value.code() == grpc.StatusCode.UNAVAILABLE, (
            f"gRPC should abort with UNAVAILABLE under universal-down; got {exc_info.value.code()}"
        )

    def test_grpc_killed_by_rest_down_fault(self, sim):
        """MAG-2092 universal-down: a ``chain_family="rest"`` mode=down
        also aborts the gRPC port with UNAVAILABLE."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"chain_family": "rest", "mode": "down"}}
        })
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(sim["grpc1"])
        assert exc_info.value.code() == grpc.StatusCode.UNAVAILABLE, (
            f"gRPC should abort with UNAVAILABLE under universal-down; got {exc_info.value.code()}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# MAG-1837: cross-transport corruption isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestGrpcCrossTransportCorruptionIsolation:
    """``ProviderState`` is shared across JSON-RPC and gRPC for the same
    provider id, so ``corruption_mode`` is a chain-agnostic field on the
    snap. Without the chain_family gate in ``_handle``, a corruption
    authored for JSON-RPC (chain_family="eth") would also break the gRPC
    port for that provider — release-smoke gRPC tests would see invalid
    protos or abort statuses they never asked for.

    Mirrors ``TestGrpcCrossTransportFaultIsolation`` (MAG-1836) but for
    the corruption ladder instead of the fault ladder.
    """

    def test_grpc_unaffected_by_eth_corruption_invalid_proto(self, sim):
        """JSON-RPC ``corruption_mode="invalid_proto"`` must not abort the
        gRPC port with UNKNOWN. Without the chain_family gate this raises
        an RpcError with StatusCode.UNKNOWN instead of returning a valid
        proto.
        """
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {
                "chain_family": "eth",
                "corruption_mode": "invalid_proto",
            }}
        })
        resp = _call_get_latest_block(sim["grpc1"])
        # Clean stub response — chain_id pinned, height > 0.
        assert resp.block.header.chain_id == "lava-sim"
        assert resp.block.header.height > 0

    def test_grpc_unaffected_by_eth_corruption_missing_field(self, sim):
        """JSON-RPC ``corruption_mode="missing_field"`` must not clear the
        ``block`` field on the gRPC response."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {
                "chain_family": "eth",
                "corruption_mode": "missing_field",
                "missing_field": "block",
            }}
        })
        resp = _call_get_latest_block(sim["grpc1"])
        # block field is populated normally — height should be > 0 and
        # chain_id should be the pinned simulator value.
        assert resp.block.header.chain_id == "lava-sim"
        assert resp.block.header.height > 0

    def test_grpc_unaffected_by_eth_corruption_wrong_type(self, sim):
        """JSON-RPC ``corruption_mode="wrong_type"`` must not abort the
        gRPC port with INTERNAL. Without the gate this raises an RpcError
        instead of returning a clean proto."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {
                "chain_family": "eth",
                "corruption_mode": "wrong_type",
                "missing_field": "block",
            }}
        })
        resp = _call_get_latest_block(sim["grpc1"])
        assert resp.block.header.chain_id == "lava-sim"
        assert resp.block.header.height > 0

    def test_grpc_corruption_still_fires_when_chain_family_is_grpc(self, sim):
        """Sanity check: the gate must not break gRPC-side corruption.

        A ``corruption_mode="invalid_proto"`` with ``chain_family="grpc"``
        must still abort the gRPC port with UNKNOWN."""
        _set_grpc(sim, "1", corruption_mode="invalid_proto")
        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(sim["grpc1"])
        assert exc_info.value.code() == grpc.StatusCode.UNKNOWN


# ─────────────────────────────────────────────────────────────────────────────
# Sequenced faults across transports — the fail_first_n window is consumed on
# the owning JSON-RPC listener and only OBSERVED (never advanced) by gRPC
# ─────────────────────────────────────────────────────────────────────────────

class TestGrpcSequencedFaultObservation:
    """The sequenced fault (fail_first_n / then_mode) counts requests on the
    OWNING JSON-RPC listener only. The gRPC surface never advances that
    window — it observes it: while the window is open, a provider-wide down
    aborts with UNAVAILABLE; once the owning listener has consumed the
    window, the call must succeed instead of aborting forever."""

    def test_grpc_down_clears_after_owning_listener_consumes_window(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {
            "chain_family": "eth", "mode": "down",
            "fail_first_n": 2, "then_mode": "success",
        }}})

        with pytest.raises(grpc.RpcError) as exc_info:
            _call_get_latest_block(sim["grpc1"])
        assert exc_info.value.code() == grpc.StatusCode.UNAVAILABLE, (
            f"gRPC must abort while the down window is open; "
            f"got {exc_info.value.code()}"
        )

        eth_url = f"http://127.0.0.1:{_JSONRPC_PORTS['1']}"
        for i in (1, 2):
            eth_status, _ = _post(eth_url, {"jsonrpc": "2.0", "id": i,
                                            "method": "eth_blockNumber",
                                            "params": []})
            assert eth_status == 503, (
                f"owning ETH call {i} is inside the down window; got {eth_status}"
            )

        resp = _call_get_latest_block(sim["grpc1"])
        assert resp.block.header.height > 0, (
            "gRPC must observe the consumed window and serve the success stub"
        )
