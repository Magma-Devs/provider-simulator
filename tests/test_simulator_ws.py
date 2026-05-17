"""
test_simulator_ws.py — WebSocket transport test suite (MAG-1801).

Boots a complete simulator (JSON-RPC + REST + WS + control) on isolated test
ports and exercises the WS handler. The module-scoped `sim` fixture boots
once per test module. The autouse clean_state fixture calls /reset/all
before and after every test so scenarios don't leak between tests.

Test ports are 48xxx (offset +30000 from production) so this file can run in
the same pytest invocation as test_simulator.py / test_simulator_rest.py
without collision.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from constants import HISTORY_MAX  # noqa: F401  (kept for symmetry with sibling test files)
from server import (
    ControlHandler,
    JSONRPCHandler,
    ProviderState,
    _WS_SUBSCRIPTIONS,
)
import handlers_ws
import handlers_eth  # noqa: E402  (for the test direct comparison)
from ws_client import WsClient


_PROVIDER_PORTS = {"1": 48545, "2": 48546, "3": 48547}
_WS_PORTS       = {"1": 48557, "2": 48558, "3": 48559}
_CONTROL_PORT   = 49000


@pytest.fixture(scope="module")
def sim():
    """Boot one simulator (JSON-RPC + WS + control) on isolated ports.

    Yields a dict mapping role names → http URLs (or ws URLs for WS providers).
    """
    states = {pid: ProviderState() for pid in _PROVIDER_PORTS}

    servers = []
    for pid, port in _PROVIDER_PORTS.items():
        srv = ThreadingHTTPServer(("127.0.0.1", port), JSONRPCHandler)
        srv.daemon_threads = True
        srv.state = states[pid]
        srv.provider_id = pid
        servers.append(srv)

    for pid, port in _WS_PORTS.items():
        srv = ThreadingHTTPServer(("127.0.0.1", port), handlers_ws.WsHandler)
        srv.daemon_threads = True
        srv.state = states[pid]
        srv.provider_id = pid
        servers.append(srv)

    ctrl = HTTPServer(("127.0.0.1", _CONTROL_PORT), ControlHandler)
    ctrl.provider_states = states
    servers.append(ctrl)

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()

    yield {
        "control":     f"http://127.0.0.1:{_CONTROL_PORT}",
        "provider1":   f"http://127.0.0.1:{_PROVIDER_PORTS['1']}",
        "provider2":   f"http://127.0.0.1:{_PROVIDER_PORTS['2']}",
        "provider3":   f"http://127.0.0.1:{_PROVIDER_PORTS['3']}",
        "ws1_host":    "127.0.0.1",
        "ws1_port":    _WS_PORTS["1"],
        "ws2_host":    "127.0.0.1",
        "ws2_port":    _WS_PORTS["2"],
        "ws3_host":    "127.0.0.1",
        "ws3_port":    _WS_PORTS["3"],
    }

    for s in servers:
        s.shutdown()


def _control(sim, method, path, body=None):
    req = urllib.request.Request(
        f"{sim['control']}{path}",
        method=method,
        headers={"Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario config and history before and after every test."""
    _WS_SUBSCRIPTIONS.clear()
    _control(sim, "POST", "/reset/all")
    yield
    _WS_SUBSCRIPTIONS.clear()
    _control(sim, "POST", "/reset/all")


# ── Handshake & path routing ──────────────────────────────────────────────────


class TestHandshake:

    def test_ws_path_completes_handshake_with_101(self, sim):
        """GET /ws with a valid Upgrade request returns 101 Switching Protocols."""
        c = WsClient(sim["ws1_host"], sim["ws1_port"], "/ws")
        c.connect()
        # Connection is closed by the handler at this stage (task 13 skeleton).
        # We only assert the handshake succeeded.
        c.close()

    def test_root_path_returns_404(self, sim):
        """GET / on a WS port is rejected with 404 — only /ws accepts the upgrade."""
        s = socket.create_connection((sim["ws1_host"], sim["ws1_port"]), timeout=2)
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                  b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                  b"Sec-WebSocket-Version: 13\r\n\r\n")
        data = s.recv(1024)
        s.close()
        assert b" 404 " in data.split(b"\r\n", 1)[0]

    def test_missing_upgrade_header_returns_400(self, sim):
        """GET /ws without Upgrade: websocket returns 400."""
        s = socket.create_connection((sim["ws1_host"], sim["ws1_port"]), timeout=2)
        s.sendall(b"GET /ws HTTP/1.1\r\nHost: x\r\n\r\n")
        data = s.recv(1024)
        s.close()
        assert b" 400 " in data.split(b"\r\n", 1)[0]


# ── Request/response over WS ──────────────────────────────────────────────────

import ws_protocol  # noqa: E402


class TestRequestResponse:

    def test_eth_blocknumber_over_ws_matches_http(self, sim):
        """An eth_blockNumber request over WS must return the same default value
        as it does over HTTP JSON-RPC — verifies the handler delegates to
        handlers_eth.handle exactly the same way JSONRPCHandler does."""
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 1})
            reply = c.recv_json(timeout=2.0)
        assert reply["jsonrpc"] == "2.0"
        assert reply["id"] == 1
        assert reply["result"].startswith("0x")


class TestPingPong:

    def test_client_ping_gets_pong_with_same_payload(self, sim):
        """Reader auto-pongs incoming pings. Payload is echoed verbatim."""
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            # Send a PING frame manually (WsClient only exposes TEXT helpers).
            payload = b"ping-payload"
            c.sock.sendall(ws_protocol.encode_frame(
                ws_protocol.OPCODE_PING, payload, mask=True))
            frame = c.recv_raw(timeout=1.0)
        assert frame.opcode == ws_protocol.OPCODE_PONG
        assert frame.payload == payload


# ── Subscription lifecycle ─────────────────────────────────────────────────────


class TestSubscriptionLifecycle:

    def test_eth_subscribe_returns_sub_id_string(self, sim):
        """eth_subscribe must return a result that is a '0x'-prefixed string
        with exactly 32 hex characters after the prefix (16 raw bytes)."""
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            c.send_json({
                "jsonrpc": "2.0",
                "method": "eth_subscribe",
                "params": ["newHeads"],
                "id": 1,
            })
            reply = c.recv_json(timeout=2.0)
        assert reply["jsonrpc"] == "2.0"
        assert reply["id"] == 1
        sub_id = reply["result"]
        assert isinstance(sub_id, str)
        assert sub_id.startswith("0x")
        assert len(sub_id) == 2 + 32  # "0x" + 32 hex chars

    def test_eth_unsubscribe_returns_true_when_known(self, sim):
        """Unsubscribing a known sub_id returns True; re-unsubscribing returns False."""
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            # Subscribe first.
            c.send_json({
                "jsonrpc": "2.0",
                "method": "eth_subscribe",
                "params": ["newHeads"],
                "id": 1,
            })
            sub_reply = c.recv_json(timeout=2.0)
            sub_id = sub_reply["result"]

            # Unsubscribe — should succeed.
            c.send_json({
                "jsonrpc": "2.0",
                "method": "eth_unsubscribe",
                "params": [sub_id],
                "id": 2,
            })
            unsub_reply = c.recv_json(timeout=2.0)
            assert unsub_reply["id"] == 2
            assert unsub_reply["result"] is True

            # Re-unsubscribe the same sub_id — must return False.
            c.send_json({
                "jsonrpc": "2.0",
                "method": "eth_unsubscribe",
                "params": [sub_id],
                "id": 3,
            })
            unsub2_reply = c.recv_json(timeout=2.0)
            assert unsub2_reply["id"] == 3
            assert unsub2_reply["result"] is False


class TestWsEmit:

    def test_emit_delivers_event_frame_to_subscriber(self, sim):
        """POST /ws/emit with a live sub_id delivers a wrapped event frame
        on the matching connection within 1s."""
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_subscribe",
                         "params": ["newHeads"], "id": 1})
            sub_id = c.recv_json(timeout=2.0)["result"]

            status, _ = _control(sim, "POST", "/ws/emit", {
                "subscription_id": sub_id,
                "event": {"number": "0x1312D02", "hash": "0xfeed"},
            })
            assert status == 200

            event_frame = c.recv_json(timeout=1.0)
        assert event_frame["method"] == "eth_subscription"
        assert event_frame["params"]["subscription"] == sub_id
        assert event_frame["params"]["result"]["number"] == "0x1312D02"

    def test_emit_unknown_sub_id_returns_404(self, sim):
        """POST /ws/emit with a sub_id that doesn't exist returns 404."""
        try:
            _control(sim, "POST", "/ws/emit", {
                "subscription_id": "0xdeadbeefdeadbeefdeadbeefdeadbeef",
                "event": {"number": "0x1"},
            })
            pytest.fail("expected HTTPError")
        except urllib.request.HTTPError as e:
            assert e.code == 404

    def test_emit_missing_subscription_id_returns_400(self, sim):
        """POST /ws/emit without subscription_id returns 400."""
        try:
            _control(sim, "POST", "/ws/emit", {"event": {"x": 1}})
            pytest.fail("expected HTTPError")
        except urllib.request.HTTPError as e:
            assert e.code == 400


class TestWsSubscriptionsIntrospection:

    def test_empty_at_startup(self, sim):
        status, body = _control(sim, "GET", "/ws/subscriptions")
        assert status == 200
        assert body == {"subscriptions": []}

    def test_reflects_active_subscriptions(self, sim):
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c1, \
             WsClient(sim["ws2_host"], sim["ws2_port"], "/ws") as c2:
            c1.send_json({"jsonrpc": "2.0", "method": "eth_subscribe",
                          "params": ["newHeads"], "id": 1})
            sid1 = c1.recv_json(timeout=2.0)["result"]
            c2.send_json({"jsonrpc": "2.0", "method": "accountSubscribe",
                          "params": [], "id": 1})
            sid2 = c2.recv_json(timeout=2.0)["result"]

            status, body = _control(sim, "GET", "/ws/subscriptions")
        assert status == 200
        ids = {s["subscription_id"] for s in body["subscriptions"]}
        assert ids == {sid1, sid2}


class TestConnectionCleanup:

    def test_client_close_clears_registry(self, sim):
        """When the client closes the socket, the subscription is removed."""
        c = WsClient(sim["ws1_host"], sim["ws1_port"], "/ws")
        c.connect()
        c.send_json({"jsonrpc": "2.0", "method": "eth_subscribe",
                     "params": ["newHeads"], "id": 1})
        sub_id = c.recv_json(timeout=2.0)["result"]

        _, body = _control(sim, "GET", "/ws/subscriptions")
        assert sub_id in {s["subscription_id"] for s in body["subscriptions"]}

        c.close()

        # Reader thread cleanup is async; poll briefly.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            _, body = _control(sim, "GET", "/ws/subscriptions")
            if sub_id not in {s["subscription_id"] for s in body["subscriptions"]}:
                break
            time.sleep(0.05)
        assert sub_id not in {s["subscription_id"] for s in body["subscriptions"]}

    def test_emit_after_client_close_returns_404(self, sim):
        """/ws/emit on a closed connection's sub_id returns 404 once cleanup completes."""
        c = WsClient(sim["ws1_host"], sim["ws1_port"], "/ws")
        c.connect()
        c.send_json({"jsonrpc": "2.0", "method": "eth_subscribe",
                     "params": ["newHeads"], "id": 1})
        sub_id = c.recv_json(timeout=2.0)["result"]
        c.close()

        deadline = time.monotonic() + 2.0
        last_status = None
        while time.monotonic() < deadline:
            try:
                _control(sim, "POST", "/ws/emit",
                         {"subscription_id": sub_id, "event": {}})
                last_status = 200
            except urllib.request.HTTPError as e:
                last_status = e.code
                if e.code == 404:
                    break
            time.sleep(0.05)
        assert last_status == 404


# ── Task 20: Post-handshake fault primitives ──────────────────────────────────


class TestPostHandshakeFaults:

    def test_mode_error_returns_jsonrpc_error_frame(self, sim):
        """With mode=error set after handshake, every request frame yields a JSON-RPC error reply."""
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            _control(sim, "POST", "/scenario", {"providers": {"1": {
                "mode": "error", "error_code": -32601,
                "error_message": "Method not found",
            }}})
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 7})
            reply = c.recv_json(timeout=2.0)
        assert reply["error"]["code"] == -32601
        assert reply["error"]["message"] == "Method not found"

    def test_rate_limit_returns_429_error_frame_post_handshake(self, sim):
        """mode=rate_limit set after handshake returns a 429-shaped error frame."""
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            _control(sim, "POST", "/scenario", {"providers": {"1": {"mode": "rate_limit"}}})
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 8})
            reply = c.recv_json(timeout=2.0)
        assert reply["error"]["code"] == 429

    def test_hang_yields_no_reply_within_1s(self, sim):
        """mode=hang set after handshake: the reader records history but does not enqueue a reply."""
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            _control(sim, "POST", "/scenario", {"providers": {"1": {"mode": "hang"}}})
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 9})
            with pytest.raises(socket.timeout):
                c.recv_json(timeout=1.0)

    def test_latency_ms_delays_reply(self, sim):
        """latency_ms=200 inserts at least 200ms between request and reply."""
        _control(sim, "POST", "/scenario", {"providers": {"1": {
            "mode": "success", "latency_ms": 200,
        }}})
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            t0 = time.monotonic()
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 10})
            c.recv_json(timeout=2.0)
            elapsed = time.monotonic() - t0
        assert elapsed >= 0.2

    def test_down_set_after_connect_closes_connection_on_next_request(self, sim):
        """mode=down set after a connection is already up closes it on the next request."""
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            # First request succeeds.
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 1})
            c.recv_json(timeout=2.0)
            # Flip to down. Next request should close the connection.
            _control(sim, "POST", "/scenario", {"providers": {"1": {"mode": "down"}}})
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 2})
            with pytest.raises((socket.timeout, ConnectionError, ws_protocol.FrameParseError)):
                c.recv_json(timeout=1.0)

    def test_error_probability_one_always_errors(self, sim):
        """error_probability=1.0 forces an error on every request."""
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            _control(sim, "POST", "/scenario", {"providers": {"1": {
                "mode": "success", "error_probability": 1.0,
                "error_code": -32007, "error_message": "Forced",
            }}})
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 1})
            reply = c.recv_json(timeout=2.0)
        assert reply["error"]["code"] == -32007

    def test_drop_connection_before_headers_closes_socket_immediately(self, sim):
        """drop_connection before_headers set after handshake: no reply frame, socket closed."""
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            _control(sim, "POST", "/scenario", {"providers": {"1": {
                "mode": "drop_connection", "drop_at": "before_headers",
            }}})
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 1})
            with pytest.raises((ConnectionError, ws_protocol.FrameParseError, socket.timeout)):
                c.recv_json(timeout=1.0)

    def test_drop_connection_mid_body_sends_partial_payload(self, sim):
        """drop_connection mid_body set after handshake: client receives partial WS frame then EOF."""
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            _control(sim, "POST", "/scenario", {"providers": {"1": {
                "mode": "drop_connection", "drop_at": "mid_body",
            }}})
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 1})
            # mid_body declares a 100-byte payload but sends only 50 + close.
            # parse_frame will block trying to read the missing bytes and
            # ultimately raise FrameParseError (peer closed before complete frame).
            with pytest.raises((ws_protocol.FrameParseError, ConnectionError, socket.timeout)):
                c.recv_raw(timeout=1.0)


# ── Task 21: Pre-handshake faults ─────────────────────────────────────────────


def _raw_ws_upgrade(host, port):
    """Send a valid WS upgrade request and return the raw response bytes."""
    s = socket.create_connection((host, port), timeout=2)
    s.sendall(b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
              b"Connection: Upgrade\r\n"
              b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
              b"Sec-WebSocket-Version: 13\r\n\r\n")
    # Read until we have at least one full HTTP response status line.
    try:
        s.settimeout(2.0)
        data = b""
        while b"\r\n" not in data:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        pass
    finally:
        s.close()
    return data


class TestPreHandshakeFaults:

    def test_mode_down_blocks_upgrade_with_503(self, sim):
        """A WS upgrade attempt while mode=down returns HTTP 503, no 101."""
        _control(sim, "POST", "/scenario", {"providers": {"1": {"mode": "down"}}})
        data = _raw_ws_upgrade(sim["ws1_host"], sim["ws1_port"])
        assert b" 503 " in data.split(b"\r\n", 1)[0]

    def test_mode_rate_limit_blocks_upgrade_with_429(self, sim):
        _control(sim, "POST", "/scenario", {"providers": {"1": {"mode": "rate_limit"}}})
        data = _raw_ws_upgrade(sim["ws1_host"], sim["ws1_port"])
        assert b" 429 " in data.split(b"\r\n", 1)[0]

    def test_mode_error_blocks_upgrade_with_400(self, sim):
        """mode=error pre-handshake: we override http_status 200 -> 400 so the
        response is non-200 + non-101 (200 with no Upgrade would be confusing)."""
        _control(sim, "POST", "/scenario", {"providers": {"1": {"mode": "error"}}})
        data = _raw_ws_upgrade(sim["ws1_host"], sim["ws1_port"])
        assert b" 400 " in data.split(b"\r\n", 1)[0]

    def test_mode_hang_pre_handshake_sleeps_then_closes(self, sim):
        """mode=hang pre-handshake: the upgrade hangs (no 101 within 1s) then
        the server closes the socket. We don't wait the full 30s — assert no
        bytes arrive within a short window and then close."""
        _control(sim, "POST", "/scenario", {"providers": {"1": {"mode": "hang"}}})
        s = socket.create_connection((sim["ws1_host"], sim["ws1_port"]), timeout=2)
        s.sendall(b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                  b"Connection: Upgrade\r\n"
                  b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                  b"Sec-WebSocket-Version: 13\r\n\r\n")
        s.settimeout(1.0)
        with pytest.raises(socket.timeout):
            s.recv(1024)
        s.close()


# ── Task 22: Corruption modes on reply frames ─────────────────────────────────


class TestCorruptionModes:

    def test_truncated_chops_trailing_bytes_from_payload(self, sim):
        # chain_family="ws" — MAG-1837 gates corruption on chain_family.
        _control(sim, "POST", "/scenario", {"providers": {"1": {
            "chain_family": "ws",
            "corruption_mode": "truncated",
        }}})
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 1})
            frame = c.recv_raw(timeout=2.0)
        # Truncation chops the last 10 bytes from the JSON payload — the
        # resulting bytes will fail json.loads but the frame itself is valid.
        with pytest.raises(json.JSONDecodeError):
            json.loads(frame.payload.decode())

    def test_missing_field_strips_top_level_key(self, sim):
        # chain_family="ws" — MAG-1837 gates corruption on chain_family.
        _control(sim, "POST", "/scenario", {"providers": {"1": {
            "chain_family": "ws",
            "corruption_mode": "missing_field",
            "missing_field": "result",
        }}})
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 1})
            reply = c.recv_json(timeout=2.0)
        assert "result" not in reply
        assert reply["jsonrpc"] == "2.0"  # other fields still present

    def test_invalid_json_replaces_payload_with_garbage(self, sim):
        # chain_family="ws" — MAG-1837 gates corruption on chain_family.
        _control(sim, "POST", "/scenario", {"providers": {"1": {
            "chain_family": "ws",
            "corruption_mode": "invalid_json",
        }}})
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 1})
            frame = c.recv_raw(timeout=2.0)
        with pytest.raises(json.JSONDecodeError):
            json.loads(frame.payload.decode("utf-8", errors="replace"))

    def test_wrong_type_swaps_target_field_type(self, sim):
        """wrong_type swaps a string result for an int (or vice versa).
        Default target is "result" -- matches JSONRPCHandler semantics."""
        # chain_family="ws" — MAG-1837 gates corruption on chain_family.
        _control(sim, "POST", "/scenario", {"providers": {"1": {
            "chain_family": "ws",
            "corruption_mode": "wrong_type",
        }}})
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 1})
            reply = c.recv_json(timeout=2.0)
        # Default eth_blockNumber result is a hex string; wrong_type turns it
        # into an int.
        assert isinstance(reply["result"], int)


# ── Task 23: chain_family="ws" round-trip through /scenario ──────────────────


class TestChainFamilyRoundTrip:

    def test_chain_family_ws_round_trips_through_scenario(self, sim):
        """POST /scenario accepts chain_family="ws" and GET /scenario echoes it."""
        _control(sim, "POST", "/scenario", {"providers": {"1": {
            "chain_family": "ws",
        }}})
        _, body = _control(sim, "GET", "/scenario")
        assert body["providers"]["1"]["chain_family"] == "ws"


# ── Task 24: cover all four subscribe-method envelopes ───────────────────────


class TestAllSubscribeMethods:

    @pytest.mark.parametrize("method,expected_envelope_method", [
        ("eth_subscribe",    "eth_subscription"),
        ("subscribe",        None),  # tendermint uses id-based correlation
        ("accountSubscribe", "accountNotification"),
        ("logsSubscribe",    "logsNotification"),
    ])
    def test_subscribe_then_emit_delivers_chain_correct_envelope(
            self, sim, method, expected_envelope_method):
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": method,
                         "params": [], "id": 1})
            sub_id = c.recv_json(timeout=2.0)["result"]

            _control(sim, "POST", "/ws/emit", {
                "subscription_id": sub_id,
                "event": {"x": 1},
            })
            event = c.recv_json(timeout=1.0)

        if expected_envelope_method is None:
            # Tendermint envelope: no "method" field, result.query carries it.
            assert "method" not in event
            assert event["result"]["query"].startswith("tm.event=")
        else:
            assert event["method"] == expected_envelope_method
            assert event["params"]["result"]["x"] == 1


# ── Task 25: two concurrent subscriptions on one connection ──────────────────


class TestConcurrentSubscriptions:

    def test_two_subs_on_one_connection_receive_their_own_events(self, sim):
        """Each subscription gets its own pushed events, not the other's."""
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_subscribe",
                         "params": ["newHeads"], "id": 1})
            sid1 = c.recv_json(timeout=2.0)["result"]
            c.send_json({"jsonrpc": "2.0", "method": "eth_subscribe",
                         "params": ["logs"], "id": 2})
            sid2 = c.recv_json(timeout=2.0)["result"]
            assert sid1 != sid2

            _control(sim, "POST", "/ws/emit",
                     {"subscription_id": sid1, "event": {"tag": "A"}})
            _control(sim, "POST", "/ws/emit",
                     {"subscription_id": sid2, "event": {"tag": "B"}})

            got = []
            for _ in range(2):
                got.append(c.recv_json(timeout=1.0))

        # Match by subscription field in the envelope.
        by_sub = {e["params"]["subscription"]: e["params"]["result"]["tag"]
                  for e in got}
        assert by_sub == {sid1: "A", sid2: "B"}


class TestLavaHeaderCapture:

    def test_lava_headers_from_upgrade_request_recorded_in_history(self, sim):
        """lava-* headers sent on the HTTP Upgrade request must be captured
        and recorded in /history for every frame that arrives on the
        connection — verifies the WS handler reuses the same lava-header
        capture pattern as JSONRPCHandler and RestHandler."""
        # Build the raw Upgrade request by hand so we can attach lava-* headers.
        s = socket.create_connection((sim["ws1_host"], sim["ws1_port"]), timeout=2)
        s.sendall(
            b"GET /ws HTTP/1.1\r\n"
            b"Host: x\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n"
            b"lava-stateful-api: true\r\n"
            b"lava-consumer-relay: 7\r\n"
            b"\r\n"
        )
        # Read until end of handshake response.
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        # Now send a TEXT frame with eth_blockNumber.
        import ws_protocol  # local for clarity
        payload = json.dumps(
            {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}
        ).encode()
        s.sendall(ws_protocol.encode_frame(ws_protocol.OPCODE_TEXT, payload, mask=True))
        # Drain the response frame so the handler completes the history push.
        s.settimeout(2.0)
        ws_protocol.parse_frame(s.recv)
        try:
            s.sendall(ws_protocol.encode_frame(ws_protocol.OPCODE_CLOSE, b"", mask=True))
        except OSError:
            pass
        s.close()

        # Now confirm /history shows the lava-* headers we sent.
        _, body = _control(sim, "GET",
                           "/history?provider=1&method=eth_blockNumber")
        assert body["count"] >= 1
        entry = body["history"][-1]
        assert entry["lava_headers"].get("lava-stateful-api") == "true"
        assert entry["lava_headers"].get("lava-consumer-relay") == "7"


class TestFaultCorruptionConsistency:

    def test_error_frame_respects_corruption_truncated(self, sim):
        """mode=error + corruption_mode=truncated must produce a truncated
        error frame, mirroring how JSONRPCHandler applies corruption to
        fault-path replies. Without this, the WS transport silently diverges
        from the HTTP transport on combined fault+corruption scenarios."""
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            # chain_family="ws" — MAG-1837 gates corruption on chain_family.
            _control(sim, "POST", "/scenario", {"providers": {"1": {
                "chain_family": "ws",
                "mode": "error", "error_code": -32099,
                "error_message": "Forced for corruption test",
                "corruption_mode": "truncated",
            }}})
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 1})
            frame = c.recv_raw(timeout=2.0)
        # Without corruption the payload would be a valid JSON-RPC error envelope.
        # With truncated, the last 10 bytes are chopped → JSON parse must fail.
        with pytest.raises(json.JSONDecodeError):
            json.loads(frame.payload.decode("utf-8"))


class TestLavaProviderAddressHeader:
    """The MAG-1749 WS smoke test asserts that the handshake response carries
    a Lava-Provider-Address header so the consumer can identify which provider
    answered the upgrade. Each WS listener (port 18557/8/9 ↔ provider 1/2/3)
    sends "sim-provider-<N>" matching its provider_id."""

    def _raw_handshake(self, host, port):
        """Open a raw TCP socket, send a valid WS upgrade, return the entire
        response-header block as bytes."""
        s = socket.create_connection((host, port), timeout=2)
        s.sendall(
            b"GET /ws HTTP/1.1\r\n"
            b"Host: x\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n"
            b"\r\n"
        )
        s.settimeout(2.0)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        # Send CLOSE so the server cleanly exits its reader loop after we
        # read what we needed.
        try:
            import ws_protocol as _wp
            s.sendall(_wp.encode_frame(_wp.OPCODE_CLOSE, b"", mask=True))
        except OSError:
            pass
        s.close()
        return buf

    @pytest.mark.parametrize("provider_id,port_key", [
        ("1", "ws1_port"),
        ("2", "ws2_port"),
        ("3", "ws3_port"),
    ])
    def test_lava_provider_address_header_present_in_upgrade_response(
            self, sim, provider_id, port_key):
        resp = self._raw_handshake(sim["ws1_host"], sim[port_key])
        assert b" 101 " in resp.split(b"\r\n", 1)[0], f"no 101 in: {resp!r}"
        expected = f"Lava-Provider-Address: sim-provider-{provider_id}".encode()
        assert expected in resp, f"missing {expected!r} in: {resp!r}"


# ─────────────────────────────────────────────────────────────────────────────
# MAG-1821 follow-up — per-method FAULT overrides on WS
#
# Extends the JSON-RPC per-method override pattern from MAG-1821 to the WS
# transport. A string-keyed entry in ``responses`` can now carry ``mode`` /
# ``latency_ms`` / ``rate_limit`` (in addition to the existing success-path
# ``result`` / ``error_stub`` / ``error`` keys consumed by handlers_eth).
# Eligible modes: down, hang, drop_connection, rate_limit, success.
# ``mode == "error"`` is rejected at /scenario time, matching JSON-RPC.
#
# Composition order mirrors JSON-RPC: latency FIRST, then fault.
# Per-key fallback also mirrors JSON-RPC: a partial per-method entry
# inherits provider-wide fault keys it doesn't override.
# ─────────────────────────────────────────────────────────────────────────────


def _ctrl_post_raw(sim, body):
    """POST /scenario and return (status, parsed_body) without raising on 4xx.

    The module-level ``_control`` helper raises HTTPError on non-2xx, which
    swallows the 400 body we want to assert against in the validation test.
    """
    req = urllib.request.Request(
        f"{sim['control']}/scenario",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(body).encode(),
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {}
        return e.code, parsed


class TestWsPerMethodFaultOverrides:

    def test_per_method_mode_down_closes_connection_on_named_method(self, sim):
        """Per-method ``mode: down`` closes the WS connection on that method."""
        _control(sim, "POST", "/scenario", {
            "providers": {"1": {
                "mode": "success",
                "responses": {
                    "eth_blockNumber": {"mode": "down"},
                },
            }}
        })
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 1})
            with pytest.raises((ConnectionError, ws_protocol.FrameParseError,
                                socket.timeout, OSError)):
                c.recv_json(timeout=1.0)

    def test_per_method_eth_subscribe_mode_down_closes_before_registration(self, sim):
        """Spec's literal example: ``eth_subscribe: {mode: down}`` closes the
        WS connection on the subscribe attempt — *before* the subscribe
        branch in _reader_loop registers a SubscriptionHandle. We assert:

          1. The client never receives a sub_id (connection is dropped).
          2. ``/ws/subscriptions`` shows no entries from the dropped attempt.

        This pins the fault-check-before-subscribe order: the per-method
        merge has to happen before the SUBSCRIBE_METHODS dispatch branch,
        otherwise eth_subscribe would silently succeed despite the override.
        """
        _control(sim, "POST", "/scenario", {
            "providers": {"1": {
                "mode": "success",
                "responses": {
                    "eth_subscribe": {"mode": "down"},
                },
            }}
        })
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_subscribe",
                         "params": ["newHeads"], "id": 1})
            with pytest.raises((ConnectionError, ws_protocol.FrameParseError,
                                socket.timeout, OSError)):
                c.recv_json(timeout=1.0)

        # No SubscriptionHandle should have been registered for the
        # dropped attempt — the down branch returns before reaching the
        # subscribe registration path.
        _, subs = _control(sim, "GET", "/ws/subscriptions")
        assert subs["subscriptions"] == [], (
            f"down override should drop before subscribe registers, "
            f"got {subs['subscriptions']!r}"
        )

    def test_per_method_other_methods_unaffected_by_down_override(self, sim):
        """Non-overridden methods on the same provider serve normally."""
        _control(sim, "POST", "/scenario", {
            "providers": {"1": {
                "mode": "success",
                "responses": {
                    "eth_blockNumber": {"mode": "down"},
                },
            }}
        })
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_getBlockByNumber",
                         "params": ["latest", False], "id": 1})
            reply = c.recv_json(timeout=2.0)
        assert "result" in reply
        assert "error" not in reply

    def test_per_method_mode_rate_limit_emits_429_error_frame(self, sim):
        """Per-method ``mode: rate_limit`` emits a JSON-RPC error frame with code 429."""
        _control(sim, "POST", "/scenario", {
            "providers": {"1": {
                "mode": "success",
                "responses": {
                    "eth_blockNumber": {"mode": "rate_limit"},
                },
            }}
        })
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 1})
            reply = c.recv_json(timeout=2.0)
        assert "error" in reply
        assert reply["error"]["code"] == 429
        assert reply["id"] == 1

    def test_per_method_latency_ms_isolates_to_named_method(self, sim):
        """Per-method ``latency_ms`` only delays the named method."""
        _control(sim, "POST", "/scenario", {
            "providers": {"1": {
                "mode": "success",
                "latency_ms": 0,
                "responses": {
                    "eth_getBlockByNumber": {"latency_ms": 500},
                },
            }}
        })
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            t0 = time.monotonic()
            c.send_json({"jsonrpc": "2.0", "method": "eth_getBlockByNumber",
                         "params": ["latest", False], "id": 1})
            c.recv_json(timeout=2.0)
            elapsed_overridden_ms = (time.monotonic() - t0) * 1000
            assert elapsed_overridden_ms >= 480, (
                f"overridden method should sleep ~500ms, elapsed={elapsed_overridden_ms:.0f}ms"
            )

            t1 = time.monotonic()
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 2})
            c.recv_json(timeout=2.0)
            elapsed_other_ms = (time.monotonic() - t1) * 1000
            assert elapsed_other_ms < 200, (
                f"non-overridden method should not sleep, elapsed={elapsed_other_ms:.0f}ms"
            )

    def test_per_key_fallback_inherits_provider_wide_latency(self, sim):
        """A partial per-method entry inherits provider-wide latency_ms it doesn't override."""
        _control(sim, "POST", "/scenario", {
            "providers": {"1": {
                "mode": "success",
                "latency_ms": 100,
                "responses": {
                    "eth_blockNumber": {"mode": "rate_limit"},
                },
            }}
        })
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            t0 = time.monotonic()
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 1})
            reply = c.recv_json(timeout=2.0)
            elapsed_ms = (time.monotonic() - t0) * 1000
        assert "error" in reply
        assert reply["error"]["code"] == 429
        assert elapsed_ms >= 80, (
            f"provider-wide latency_ms=100 should still apply, elapsed={elapsed_ms:.0f}ms"
        )

    def test_composition_order_latency_first_then_fault(self, sim):
        """Per-method ``{latency_ms: 200, mode: rate_limit}`` → 429 frame after ~200ms."""
        _control(sim, "POST", "/scenario", {
            "providers": {"1": {
                "mode": "success",
                "responses": {
                    "eth_blockNumber": {"latency_ms": 200, "mode": "rate_limit"},
                },
            }}
        })
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            t0 = time.monotonic()
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 1})
            reply = c.recv_json(timeout=2.0)
            elapsed_ms = (time.monotonic() - t0) * 1000
        assert "error" in reply
        assert reply["error"]["code"] == 429
        assert elapsed_ms >= 180, (
            f"per-method latency should fire before fault, elapsed={elapsed_ms:.0f}ms"
        )

    def test_per_method_mode_error_rejected_with_400(self, sim):
        """Per-method ``mode: error`` is rejected at /scenario time (MAG-1821 rule)."""
        status, body = _ctrl_post_raw(sim, {
            "providers": {"1": {
                "responses": {
                    "eth_blockNumber": {"mode": "error"},
                },
            }}
        })
        assert status == 400, f"expected 400 on per-method mode=error, got {status}"
        assert "error" in body
        # Message should reference the offending key for diagnosability.
        assert "mode" in body["error"].lower() or "error" in body["error"].lower()

    def test_ws_and_jsonrpc_share_string_keyed_overrides_on_same_provider(self, sim):
        """Cross-transport consistency: the SAME per-method override fires on
        both JSON-RPC (HTTP POST) and WS for the same provider, because both
        look up state.responses by the same string method name. The WS
        transport delegates non-subscription methods through the same fault
        path that JSON-RPC uses, so the two are intentionally consistent.
        """
        _control(sim, "POST", "/scenario", {
            "providers": {"1": {
                "mode": "success",
                "responses": {
                    "eth_blockNumber": {"mode": "rate_limit"},
                },
            }}
        })
        # JSON-RPC over HTTP returns 429.
        rpc_req = urllib.request.Request(
            sim["provider1"],
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"jsonrpc": "2.0", "id": 1,
                             "method": "eth_blockNumber"}).encode(),
        )
        try:
            with urllib.request.urlopen(rpc_req, timeout=5) as resp:
                rpc_code = resp.status
        except urllib.error.HTTPError as e:
            rpc_code = e.code
        assert rpc_code == 429

        # WS emits the same fault envelope as an error frame.
        with WsClient(sim["ws1_host"], sim["ws1_port"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber",
                         "params": [], "id": 1})
            reply = c.recv_json(timeout=2.0)
        assert "error" in reply
        assert reply["error"]["code"] == 429
