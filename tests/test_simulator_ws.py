"""
test_simulator_ws.py — WebSocket transport test suite.

Runs against the shared in-process simulator (see conftest.py). The eth-sim
pool's ws endpoints listen on 18557-18559; the sibling http endpoints of the
SAME providers listen on 18545-18547. Under the pool:pid model a ws endpoint
and its http sibling belong to one provider — a scenario block with no
``transports`` filter covers both, and ``transports: ["ws"]`` scopes it to
the ws wire only.

The autouse clean_state fixture calls /reset/all and clears the subscription
registry before and after every test so scenarios don't leak between tests.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import socket
import sys
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from ws_client import WsClient

from provider_simulator.listeners import ws_protocol
from provider_simulator.topology import port_of

_WS_HOST = "127.0.0.1"
# eth-sim providers 1-3 — each serves http and ws on separate ports, so the
# same pid appears in both maps with different port numbers.
_PRIMARY_PIDS = ("1", "2", "3")
_WS_PORTS = {pid: port_of("eth-sim", pid, transport="ws") for pid in _PRIMARY_PIDS}
_HTTP_URLS = {pid: f"http://127.0.0.1:{port_of('eth-sim', pid)}" for pid in _PRIMARY_PIDS}


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
    """Reset scenario config, history, and the WS subscription registry
    before and after every test."""
    sim["server"].subscriptions.clear()
    _control(sim, "POST", "/reset/all")
    yield
    sim["server"].subscriptions.clear()
    _control(sim, "POST", "/reset/all")


# ── Handshake & path routing ──────────────────────────────────────────────────


class TestHandshake:

    def test_ws_path_completes_handshake_with_101(self, sim):
        """GET /ws with a valid Upgrade request returns 101 Switching Protocols."""
        c = WsClient(_WS_HOST, _WS_PORTS["1"], "/ws")
        c.connect()
        # We only assert the handshake succeeded.
        c.close()

    def test_root_path_returns_404(self, sim):
        """GET / on a WS port is rejected with 404 — only /ws accepts the upgrade."""
        s = socket.create_connection((_WS_HOST, _WS_PORTS["1"]), timeout=2)
        s.sendall(
            b"GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        data = s.recv(1024)
        s.close()
        assert b" 404 " in data.split(b"\r\n", 1)[0]

    def test_missing_upgrade_header_returns_400(self, sim):
        """GET /ws without Upgrade: websocket returns 400."""
        s = socket.create_connection((_WS_HOST, _WS_PORTS["1"]), timeout=2)
        s.sendall(b"GET /ws HTTP/1.1\r\nHost: x\r\n\r\n")
        data = s.recv(1024)
        s.close()
        assert b" 400 " in data.split(b"\r\n", 1)[0]


# ── Request/response over WS ──────────────────────────────────────────────────


class TestRequestResponse:

    def test_eth_blocknumber_over_ws_matches_http(self, sim):
        """An eth_blockNumber request over WS must return the same default value
        as it does over HTTP JSON-RPC — the ws endpoint serves the same chain
        through the same listener flow as the http endpoint."""
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            reply = c.recv_json(timeout=2.0)
        assert reply["jsonrpc"] == "2.0"
        assert reply["id"] == 1
        assert reply["result"].startswith("0x")


class TestWsBlocksBehind:

    def test_blocks_behind_shifts_block_height_over_ws(self, sim):
        """blocks_behind=100 shifts the eth_blockNumber result on the ws wire.

        Baseline-then-shifted on the same connection: the difference between
        the two replies must be exactly the configured shift, independent of
        the head constant's literal value.
        """
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            baseline = int(c.recv_json(timeout=2.0)["result"], 16)
            _control(
                sim,
                "POST",
                "/scenario",
                {"providers": {"eth-sim:1": {"blocks_behind": 100}}},
            )
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 2})
            shifted = int(c.recv_json(timeout=2.0)["result"], 16)
        assert baseline - shifted == 100, (
            f"expected the ws-served head to drop by exactly 100, " f"got baseline={baseline} shifted={shifted}"
        )


class TestPingPong:

    def test_client_ping_gets_pong_with_same_payload(self, sim):
        """Reader auto-pongs incoming pings. Payload is echoed verbatim."""
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            # Send a PING frame manually (WsClient only exposes TEXT helpers).
            payload = b"ping-payload"
            c.sock.sendall(ws_protocol.encode_frame(ws_protocol.OPCODE_PING, payload, mask=True))
            frame = c.recv_raw(timeout=1.0)
        assert frame.opcode == ws_protocol.OPCODE_PONG
        assert frame.payload == payload


# ── Subscription lifecycle ─────────────────────────────────────────────────────


class TestSubscriptionLifecycle:

    def test_eth_subscribe_returns_sub_id_string(self, sim):
        """eth_subscribe must return a result that is a '0x'-prefixed string
        with exactly 32 hex characters after the prefix (16 raw bytes)."""
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            c.send_json(
                {
                    "jsonrpc": "2.0",
                    "method": "eth_subscribe",
                    "params": ["newHeads"],
                    "id": 1,
                }
            )
            reply = c.recv_json(timeout=2.0)
        assert reply["jsonrpc"] == "2.0"
        assert reply["id"] == 1
        sub_id = reply["result"]
        assert isinstance(sub_id, str)
        assert sub_id.startswith("0x")
        assert len(sub_id) == 2 + 32  # "0x" + 32 hex chars

    def test_eth_unsubscribe_returns_true_when_known(self, sim):
        """Unsubscribing a known sub_id returns True; re-unsubscribing returns False."""
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            # Subscribe first.
            c.send_json(
                {
                    "jsonrpc": "2.0",
                    "method": "eth_subscribe",
                    "params": ["newHeads"],
                    "id": 1,
                }
            )
            sub_reply = c.recv_json(timeout=2.0)
            sub_id = sub_reply["result"]

            # Unsubscribe — should succeed.
            c.send_json(
                {
                    "jsonrpc": "2.0",
                    "method": "eth_unsubscribe",
                    "params": [sub_id],
                    "id": 2,
                }
            )
            unsub_reply = c.recv_json(timeout=2.0)
            assert unsub_reply["id"] == 2
            assert unsub_reply["result"] is True

            # Re-unsubscribe the same sub_id — must return False.
            c.send_json(
                {
                    "jsonrpc": "2.0",
                    "method": "eth_unsubscribe",
                    "params": [sub_id],
                    "id": 3,
                }
            )
            unsub2_reply = c.recv_json(timeout=2.0)
            assert unsub2_reply["id"] == 3
            assert unsub2_reply["result"] is False


class TestWsEmit:

    def test_emit_delivers_event_frame_to_subscriber(self, sim):
        """POST /ws/emit with a live sub_id delivers a wrapped event frame
        on the matching connection within 1s."""
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"], "id": 1})
            sub_id = c.recv_json(timeout=2.0)["result"]

            status, _ = _control(
                sim,
                "POST",
                "/ws/emit",
                {
                    "subscription_id": sub_id,
                    "event": {"number": "0x1312D02", "hash": "0xfeed"},
                },
            )
            assert status == 200

            event_frame = c.recv_json(timeout=1.0)
        assert event_frame["method"] == "eth_subscription"
        assert event_frame["params"]["subscription"] == sub_id
        assert event_frame["params"]["result"]["number"] == "0x1312D02"

    def test_emit_unknown_sub_id_returns_404(self, sim):
        """POST /ws/emit with a sub_id that doesn't exist returns 404."""
        try:
            _control(
                sim,
                "POST",
                "/ws/emit",
                {
                    "subscription_id": "0xdeadbeefdeadbeefdeadbeefdeadbeef",
                    "event": {"number": "0x1"},
                },
            )
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
        with (
            WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c1,
            WsClient(_WS_HOST, _WS_PORTS["2"], "/ws") as c2,
        ):
            c1.send_json({"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"], "id": 1})
            sid1 = c1.recv_json(timeout=2.0)["result"]
            c2.send_json({"jsonrpc": "2.0", "method": "accountSubscribe", "params": [], "id": 1})
            sid2 = c2.recv_json(timeout=2.0)["result"]

            status, body = _control(sim, "GET", "/ws/subscriptions")
        assert status == 200
        ids = {s["subscription_id"] for s in body["subscriptions"]}
        assert ids == {sid1, sid2}


class TestConnectionCleanup:

    def test_client_close_clears_registry(self, sim):
        """When the client closes the socket, the subscription is removed."""
        c = WsClient(_WS_HOST, _WS_PORTS["1"], "/ws")
        c.connect()
        c.send_json({"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"], "id": 1})
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
        c = WsClient(_WS_HOST, _WS_PORTS["1"], "/ws")
        c.connect()
        c.send_json({"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"], "id": 1})
        sub_id = c.recv_json(timeout=2.0)["result"]
        c.close()

        deadline = time.monotonic() + 2.0
        last_status = None
        while time.monotonic() < deadline:
            try:
                _control(sim, "POST", "/ws/emit", {"subscription_id": sub_id, "event": {}})
                last_status = 200
            except urllib.request.HTTPError as e:
                last_status = e.code
                if e.code == 404:
                    break
            time.sleep(0.05)
        assert last_status == 404


# ── Post-handshake fault primitives ───────────────────────────────────────────
#
# Content faults meant for the ws wire only are scoped with
# ``transports: ["ws"]`` — the sibling http endpoint of the same provider
# stays healthy (pinned explicitly in TestWsPerMethodFaultOverrides below).


class TestPostHandshakeFaults:

    def test_mode_error_returns_jsonrpc_error_frame(self, sim):
        """With mode=error set after handshake, every request frame yields a JSON-RPC error reply."""
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            _control(
                sim,
                "POST",
                "/scenario",
                {
                    "providers": {
                        "eth-sim:1": {
                            "mode": "error",
                            "error_code": -32601,
                            "error_message": "Method not found",
                            "transports": ["ws"],
                        }
                    }
                },
            )
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 7})
            reply = c.recv_json(timeout=2.0)
        assert reply["error"]["code"] == -32601
        assert reply["error"]["message"] == "Method not found"

    def test_rate_limit_returns_429_prose_frame_post_handshake(self, sim):
        """mode=rate_limit set after handshake returns a prose frame, not a
        JSON-RPC error envelope — matching the HTTP shape (see
        tests/test_simulator.py::test_rate_limit_returns_429). A WS frame
        carries no HTTP status, so the prose text is the only signal."""
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            _control(
                sim,
                "POST",
                "/scenario",
                {
                    "providers": {
                        "eth-sim:1": {
                            "mode": "rate_limit",
                            "transports": ["ws"],
                        }
                    }
                },
            )
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 8})
            frame = c.recv_raw(timeout=2.0)
        body = frame.payload.decode("utf-8")
        assert not body.lstrip().startswith("{"), f"rate_limit frame must be prose, not JSON; got {body!r}"

    def test_hang_yields_no_reply_within_1s(self, sim):
        """mode=hang set after handshake: the reader records history but does not enqueue a reply."""
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            _control(
                sim,
                "POST",
                "/scenario",
                {
                    "providers": {
                        "eth-sim:1": {
                            "mode": "hang",
                            "transports": ["ws"],
                        }
                    }
                },
            )
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 9})
            with pytest.raises(socket.timeout):
                c.recv_json(timeout=1.0)

    def test_latency_ms_delays_reply(self, sim):
        """latency_ms=200 inserts at least 200ms between request and reply."""
        _control(
            sim,
            "POST",
            "/scenario",
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "latency_ms": 200,
                    }
                }
            },
        )
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            t0 = time.monotonic()
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 10})
            c.recv_json(timeout=2.0)
            elapsed = time.monotonic() - t0
        assert elapsed >= 0.2

    def test_down_set_after_connect_closes_connection_on_next_request(self, sim):
        """mode=down set after a connection is already up closes it on the next request."""
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            # First request succeeds.
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            c.recv_json(timeout=2.0)
            # Flip to down (ws wire only — the provider stays reachable over
            # http, which pins the transports filter on a live connection).
            _control(
                sim,
                "POST",
                "/scenario",
                {
                    "providers": {
                        "eth-sim:1": {
                            "mode": "down",
                            "transports": ["ws"],
                        }
                    }
                },
            )
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 2})
            with pytest.raises((socket.timeout, ConnectionError, ws_protocol.FrameParseError)):
                c.recv_json(timeout=1.0)

    def test_error_probability_one_always_errors(self, sim):
        """error_probability=1.0 forces an error on every request."""
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            _control(
                sim,
                "POST",
                "/scenario",
                {
                    "providers": {
                        "eth-sim:1": {
                            "mode": "success",
                            "error_probability": 1.0,
                            "error_code": -32007,
                            "error_message": "Forced",
                            "transports": ["ws"],
                        }
                    }
                },
            )
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            reply = c.recv_json(timeout=2.0)
        assert reply["error"]["code"] == -32007

    def test_drop_connection_before_headers_closes_socket_immediately(self, sim):
        """drop_connection before_headers set after handshake: no reply frame, socket closed."""
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            _control(
                sim,
                "POST",
                "/scenario",
                {
                    "providers": {
                        "eth-sim:1": {
                            "mode": "drop_connection",
                            "drop_at": "before_headers",
                            "transports": ["ws"],
                        }
                    }
                },
            )
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            with pytest.raises((ConnectionError, ws_protocol.FrameParseError, socket.timeout)):
                c.recv_json(timeout=1.0)

    def test_drop_connection_mid_body_sends_partial_payload(self, sim):
        """drop_connection mid_body set after handshake: client receives partial WS frame then EOF."""
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            _control(
                sim,
                "POST",
                "/scenario",
                {
                    "providers": {
                        "eth-sim:1": {
                            "mode": "drop_connection",
                            "drop_at": "mid_body",
                            "transports": ["ws"],
                        }
                    }
                },
            )
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            # mid_body declares a 100-byte payload but sends only 50 + close.
            # parse_frame will block trying to read the missing bytes and
            # ultimately raise FrameParseError (peer closed before complete frame).
            with pytest.raises((ws_protocol.FrameParseError, ConnectionError, socket.timeout)):
                c.recv_raw(timeout=1.0)


# ── Pre-handshake faults ──────────────────────────────────────────────────────


def _raw_ws_upgrade(host, port):
    """Send a valid WS upgrade request and return the raw response bytes."""
    s = socket.create_connection((host, port), timeout=2)
    s.sendall(
        b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        b"Sec-WebSocket-Version: 13\r\n\r\n"
    )
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
        _control(
            sim,
            "POST",
            "/scenario",
            {"providers": {"eth-sim:1": {"mode": "down", "transports": ["ws"]}}},
        )
        data = _raw_ws_upgrade(_WS_HOST, _WS_PORTS["1"])
        assert b" 503 " in data.split(b"\r\n", 1)[0]

    def test_mode_rate_limit_blocks_upgrade_with_429(self, sim):
        _control(
            sim,
            "POST",
            "/scenario",
            {"providers": {"eth-sim:1": {"mode": "rate_limit", "transports": ["ws"]}}},
        )
        data = _raw_ws_upgrade(_WS_HOST, _WS_PORTS["1"])
        assert b" 429 " in data.split(b"\r\n", 1)[0]

    def test_mode_error_blocks_upgrade_with_400(self, sim):
        """mode=error pre-handshake: the http_status 200 default becomes 400 so
        the response is non-200 + non-101 (200 with no Upgrade would be
        confusing)."""
        _control(
            sim,
            "POST",
            "/scenario",
            {"providers": {"eth-sim:1": {"mode": "error", "transports": ["ws"]}}},
        )
        data = _raw_ws_upgrade(_WS_HOST, _WS_PORTS["1"])
        assert b" 400 " in data.split(b"\r\n", 1)[0]

    def test_mode_hang_pre_handshake_sleeps_then_closes(self, sim):
        """mode=hang pre-handshake: the upgrade hangs (no 101 within 1s) then
        the server closes the socket. We don't wait the full 30s — assert no
        bytes arrive within a short window and then close."""
        _control(
            sim,
            "POST",
            "/scenario",
            {"providers": {"eth-sim:1": {"mode": "hang", "transports": ["ws"]}}},
        )
        s = socket.create_connection((_WS_HOST, _WS_PORTS["1"]), timeout=2)
        s.sendall(
            b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        s.settimeout(1.0)
        with pytest.raises(socket.timeout):
            s.recv(1024)
        s.close()


# ── Corruption modes on reply frames ──────────────────────────────────────────


class TestCorruptionModes:

    def test_truncated_chops_trailing_bytes_from_payload(self, sim):
        _control(
            sim,
            "POST",
            "/scenario",
            {"providers": {"eth-sim:1": {"corruption_mode": "truncated", "transports": ["ws"]}}},
        )
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            frame = c.recv_raw(timeout=2.0)
        # Truncation chops the last 10 bytes from the JSON payload — the
        # resulting bytes will fail json.loads but the frame itself is valid.
        with pytest.raises(json.JSONDecodeError):
            json.loads(frame.payload.decode())

    def test_missing_field_strips_top_level_key(self, sim):
        _control(
            sim,
            "POST",
            "/scenario",
            {
                "providers": {
                    "eth-sim:1": {
                        "corruption_mode": "missing_field",
                        "missing_field": "result",
                        "transports": ["ws"],
                    }
                }
            },
        )
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            reply = c.recv_json(timeout=2.0)
        assert "result" not in reply
        assert reply["jsonrpc"] == "2.0"  # other fields still present

    def test_invalid_json_replaces_payload_with_garbage(self, sim):
        _control(
            sim,
            "POST",
            "/scenario",
            {"providers": {"eth-sim:1": {"corruption_mode": "invalid_json", "transports": ["ws"]}}},
        )
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            frame = c.recv_raw(timeout=2.0)
        with pytest.raises(json.JSONDecodeError):
            json.loads(frame.payload.decode("utf-8", errors="replace"))

    def test_wrong_type_swaps_target_field_type(self, sim):
        """wrong_type swaps a string result for an int (or vice versa).
        Default target is "result" — same semantics as the http endpoint."""
        _control(
            sim,
            "POST",
            "/scenario",
            {"providers": {"eth-sim:1": {"corruption_mode": "wrong_type", "transports": ["ws"]}}},
        )
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            reply = c.recv_json(timeout=2.0)
        # Default eth_blockNumber result is a hex string; wrong_type turns it
        # into an int.
        assert isinstance(reply["result"], int)


# ── transports filter round-trip through /scenario ────────────────────────────


class TestTransportsFilterRoundTrip:

    def test_transports_ws_round_trips_through_scenario(self, sim):
        """POST /scenario accepts transports=["ws"] and GET /scenario echoes it."""
        _control(
            sim,
            "POST",
            "/scenario",
            {"providers": {"eth-sim:1": {"transports": ["ws"]}}},
        )
        _, body = _control(sim, "GET", "/scenario")
        assert body["providers"]["eth-sim:1"]["transports"] == ["ws"]


# ── All four subscribe-method envelopes ───────────────────────────────────────


class TestAllSubscribeMethods:

    @pytest.mark.parametrize(
        "method,expected_envelope_method",
        [
            ("eth_subscribe", "eth_subscription"),
            ("subscribe", None),  # tendermint uses id-based correlation
            ("accountSubscribe", "accountNotification"),
            ("logsSubscribe", "logsNotification"),
        ],
    )
    def test_subscribe_then_emit_delivers_chain_correct_envelope(self, sim, method, expected_envelope_method):
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": method, "params": [], "id": 1})
            sub_id = c.recv_json(timeout=2.0)["result"]

            _control(
                sim,
                "POST",
                "/ws/emit",
                {
                    "subscription_id": sub_id,
                    "event": {"x": 1},
                },
            )
            event = c.recv_json(timeout=1.0)

        if expected_envelope_method is None:
            # Tendermint envelope: no "method" field, result.query carries it.
            assert "method" not in event
            assert event["result"]["query"].startswith("tm.event=")
        else:
            assert event["method"] == expected_envelope_method
            assert event["params"]["result"]["x"] == 1


# ── Two concurrent subscriptions on one connection ────────────────────────────


class TestConcurrentSubscriptions:

    def test_two_subs_on_one_connection_receive_their_own_events(self, sim):
        """Each subscription gets its own pushed events, not the other's."""
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"], "id": 1})
            sid1 = c.recv_json(timeout=2.0)["result"]
            c.send_json({"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["logs"], "id": 2})
            sid2 = c.recv_json(timeout=2.0)["result"]
            assert sid1 != sid2

            _control(sim, "POST", "/ws/emit", {"subscription_id": sid1, "event": {"tag": "A"}})
            _control(sim, "POST", "/ws/emit", {"subscription_id": sid2, "event": {"tag": "B"}})

            got = []
            for _ in range(2):
                got.append(c.recv_json(timeout=1.0))

        # Match by subscription field in the envelope.
        by_sub = {e["params"]["subscription"]: e["params"]["result"]["tag"] for e in got}
        assert by_sub == {sid1: "A", sid2: "B"}


class TestLavaHeaderCapture:

    def test_lava_headers_from_upgrade_request_recorded_in_history(self, sim):
        """lava-* headers sent on the HTTP Upgrade request must be captured
        and recorded in /history for every frame that arrives on the
        connection — the WS adapter reuses the same lava-header capture
        pattern as the request/response adapters."""
        # Build the raw Upgrade request by hand so we can attach lava-* headers.
        s = socket.create_connection((_WS_HOST, _WS_PORTS["1"]), timeout=2)
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
        payload = json.dumps({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}).encode()
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
        _, body = _control(sim, "GET", "/history?pool=eth-sim&pid=1&method=eth_blockNumber&transport=ws")
        assert body["count"] >= 1
        entry = body["history"][-1]
        assert entry["lava_headers"].get("lava-stateful-api") == "true"
        assert entry["lava_headers"].get("lava-consumer-relay") == "7"
        assert entry["transport"] == "ws"
        assert entry["port"] == _WS_PORTS["1"]


class TestFaultCorruptionConsistency:

    def test_error_frame_respects_corruption_truncated(self, sim):
        """mode=error + corruption_mode=truncated must produce a truncated
        error frame, mirroring how the http endpoint applies corruption to
        fault-path replies. Without this, the WS transport silently diverges
        from HTTP on combined fault+corruption scenarios."""
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            _control(
                sim,
                "POST",
                "/scenario",
                {
                    "providers": {
                        "eth-sim:1": {
                            "mode": "error",
                            "error_code": -32099,
                            "error_message": "Forced for corruption test",
                            "corruption_mode": "truncated",
                            "transports": ["ws"],
                        }
                    }
                },
            )
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            frame = c.recv_raw(timeout=2.0)
        # Without corruption the payload would be a valid JSON-RPC error envelope.
        # With truncated, the last 10 bytes are chopped → JSON parse must fail.
        with pytest.raises(json.JSONDecodeError):
            json.loads(frame.payload.decode("utf-8"))


class TestLavaProviderAddressHeader:
    """The WS smoke tests assert that the handshake response carries a
    Lava-Provider-Address header so the consumer can identify which provider
    answered the upgrade. Each ws endpoint sends "sim-provider-<pool:pid>"
    naming its provider."""

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
            s.sendall(ws_protocol.encode_frame(ws_protocol.OPCODE_CLOSE, b"", mask=True))
        except OSError:
            pass
        s.close()
        return buf

    @pytest.mark.parametrize("pid", ["1", "2", "3"])
    def test_lava_provider_address_header_present_in_upgrade_response(self, sim, pid):
        resp = self._raw_handshake(_WS_HOST, _WS_PORTS[pid])
        assert b" 101 " in resp.split(b"\r\n", 1)[0], f"no 101 in: {resp!r}"
        expected = f"Lava-Provider-Address: sim-provider-eth-sim:{pid}".encode()
        assert expected in resp, f"missing {expected!r} in: {resp!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Per-method FAULT overrides on WS
#
# A string-keyed entry in ``responses`` can carry ``mode`` / ``latency_ms``
# fault keys. Eligible modes: down, hang, drop_connection, rate_limit,
# success. ``mode == "error"`` is rejected at /scenario time, matching
# JSON-RPC. Composition order mirrors JSON-RPC: latency FIRST, then fault.
# Per-key fallback also mirrors JSON-RPC: a partial per-method entry inherits
# provider-wide fault keys it doesn't override. The block's ``transports``
# filter scopes the overrides like everything else.
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
        _control(
            sim,
            "POST",
            "/scenario",
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "transports": ["ws"],
                        "responses": {
                            "eth_blockNumber": {"mode": "down"},
                        },
                    }
                }
            },
        )
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            with pytest.raises((ConnectionError, ws_protocol.FrameParseError, socket.timeout, OSError)):
                c.recv_json(timeout=1.0)

    def test_per_method_eth_subscribe_mode_down_closes_before_registration(self, sim):
        """``eth_subscribe: {mode: down}`` closes the WS connection on the
        subscribe attempt — *before* any subscription is registered. We
        assert:

          1. The client never receives a sub_id (connection is dropped).
          2. ``/ws/subscriptions`` shows no entries from the dropped attempt.

        This pins the fault-check-before-subscribe order: the per-method
        merge has to happen before the subscription dispatch, otherwise
        eth_subscribe would silently succeed despite the override.
        """
        _control(
            sim,
            "POST",
            "/scenario",
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "transports": ["ws"],
                        "responses": {
                            "eth_subscribe": {"mode": "down"},
                        },
                    }
                }
            },
        )
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"], "id": 1})
            with pytest.raises((ConnectionError, ws_protocol.FrameParseError, socket.timeout, OSError)):
                c.recv_json(timeout=1.0)

        # No subscription should have been registered for the dropped
        # attempt — the down branch returns before the registration path.
        _, subs = _control(sim, "GET", "/ws/subscriptions")
        assert subs["subscriptions"] == [], (
            f"down override should drop before subscribe registers, " f"got {subs['subscriptions']!r}"
        )

    def test_per_method_other_methods_unaffected_by_down_override(self, sim):
        """Non-overridden methods on the same provider serve normally."""
        _control(
            sim,
            "POST",
            "/scenario",
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "responses": {
                            "eth_blockNumber": {"mode": "down"},
                        },
                    }
                }
            },
        )
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            c.send_json(
                {
                    "jsonrpc": "2.0",
                    "method": "eth_getBlockByNumber",
                    "params": ["latest", False],
                    "id": 1,
                }
            )
            reply = c.recv_json(timeout=2.0)
        assert "result" in reply
        assert "error" not in reply

    def test_per_method_mode_rate_limit_emits_429_prose_frame(self, sim):
        """Per-method ``mode: rate_limit`` emits a prose frame, not a
        JSON-RPC error envelope (see test_rate_limit_returns_429_prose_frame_post_handshake)."""
        _control(
            sim,
            "POST",
            "/scenario",
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "transports": ["ws"],
                        "responses": {
                            "eth_blockNumber": {"mode": "rate_limit"},
                        },
                    }
                }
            },
        )
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            frame = c.recv_raw(timeout=2.0)
        body = frame.payload.decode("utf-8")
        assert not body.lstrip().startswith("{"), f"rate_limit frame must be prose, not JSON; got {body!r}"

    def test_per_method_latency_ms_isolates_to_named_method(self, sim):
        """Per-method ``latency_ms`` only delays the named method."""
        _control(
            sim,
            "POST",
            "/scenario",
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "latency_ms": 0,
                        "responses": {
                            "eth_getBlockByNumber": {"latency_ms": 500},
                        },
                    }
                }
            },
        )
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            t0 = time.monotonic()
            c.send_json(
                {
                    "jsonrpc": "2.0",
                    "method": "eth_getBlockByNumber",
                    "params": ["latest", False],
                    "id": 1,
                }
            )
            c.recv_json(timeout=2.0)
            elapsed_overridden_ms = (time.monotonic() - t0) * 1000
            assert (
                elapsed_overridden_ms >= 480
            ), f"overridden method should sleep ~500ms, elapsed={elapsed_overridden_ms:.0f}ms"

            t1 = time.monotonic()
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 2})
            c.recv_json(timeout=2.0)
            elapsed_other_ms = (time.monotonic() - t1) * 1000
            assert elapsed_other_ms < 200, f"non-overridden method should not sleep, elapsed={elapsed_other_ms:.0f}ms"

    def test_per_key_fallback_inherits_provider_wide_latency(self, sim):
        """A partial per-method entry inherits provider-wide latency_ms it doesn't override."""
        _control(
            sim,
            "POST",
            "/scenario",
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "latency_ms": 100,
                        "transports": ["ws"],
                        "responses": {
                            "eth_blockNumber": {"mode": "rate_limit"},
                        },
                    }
                }
            },
        )
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            t0 = time.monotonic()
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            frame = c.recv_raw(timeout=2.0)
            elapsed_ms = (time.monotonic() - t0) * 1000
        body = frame.payload.decode("utf-8")
        assert not body.lstrip().startswith("{"), f"rate_limit frame must be prose, not JSON; got {body!r}"
        assert elapsed_ms >= 80, f"provider-wide latency_ms=100 should still apply, elapsed={elapsed_ms:.0f}ms"

    def test_composition_order_latency_first_then_fault(self, sim):
        """Per-method ``{latency_ms: 200, mode: rate_limit}`` → 429 frame after ~200ms."""
        _control(
            sim,
            "POST",
            "/scenario",
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "transports": ["ws"],
                        "responses": {
                            "eth_blockNumber": {"latency_ms": 200, "mode": "rate_limit"},
                        },
                    }
                }
            },
        )
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            t0 = time.monotonic()
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            frame = c.recv_raw(timeout=2.0)
            elapsed_ms = (time.monotonic() - t0) * 1000
        body = frame.payload.decode("utf-8")
        assert not body.lstrip().startswith("{"), f"rate_limit frame must be prose, not JSON; got {body!r}"
        assert elapsed_ms >= 180, f"per-method latency should fire before fault, elapsed={elapsed_ms:.0f}ms"

    def test_per_method_mode_error_rejected_with_400(self, sim):
        """Per-method ``mode: error`` is rejected at /scenario time."""
        status, body = _ctrl_post_raw(
            sim,
            {
                "providers": {
                    "eth-sim:1": {
                        "responses": {
                            "eth_blockNumber": {"mode": "error"},
                        },
                    }
                }
            },
        )
        assert status == 400, f"expected 400 on per-method mode=error, got {status}"
        assert "error" in body
        # Message should reference the offending key for diagnosability.
        assert "mode" in body["error"].lower() or "error" in body["error"].lower()

    def test_ws_and_http_endpoints_isolated_by_transports_filter(self, sim):
        """Per-endpoint scoping on ONE provider: an override scoped to
        ``transports: ["ws"]`` fires on the ws endpoint but not the http
        endpoint of the same provider — and the reverse for
        ``transports: ["http"]``. This is the pool:pid replacement for the
        old chain_family gate, expressed per wire instead of per config tag."""
        # Block A — ws-scoped fault. WS should 429; http should succeed.
        _control(
            sim,
            "POST",
            "/scenario",
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "transports": ["ws"],
                        "responses": {
                            "eth_blockNumber": {"mode": "rate_limit"},
                        },
                    }
                }
            },
        )

        # The http endpoint should NOT see the ws-scoped fault.
        rpc_req = urllib.request.Request(
            _HTTP_URLS["1"],
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber"}).encode(),
        )
        try:
            with urllib.request.urlopen(rpc_req, timeout=5) as resp:
                rpc_code = resp.status
        except urllib.error.HTTPError as e:
            rpc_code = e.code
        assert rpc_code == 200, f"http endpoint must ignore a ws-scoped fault; got {rpc_code}"

        # The ws endpoint SHOULD see it.
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            frame = c.recv_raw(timeout=2.0)
        ws_body = frame.payload.decode("utf-8")
        assert not ws_body.lstrip().startswith("{"), f"rate_limit frame must be prose, not JSON; got {ws_body!r}"

        # Block B — http-scoped fault. http should 429; ws should succeed.
        _control(
            sim,
            "POST",
            "/scenario",
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "transports": ["http"],
                        "responses": {
                            "eth_blockNumber": {"mode": "rate_limit"},
                        },
                    }
                }
            },
        )

        try:
            with urllib.request.urlopen(rpc_req, timeout=5) as resp:
                rpc_code_2 = resp.status
        except urllib.error.HTTPError as e:
            rpc_code_2 = e.code
        assert rpc_code_2 == 429, f"http endpoint must fire an http-scoped fault; got {rpc_code_2}"

        # The ws endpoint should NOT see the http-scoped fault — success body.
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            reply_2 = c.recv_json(timeout=2.0)
        assert "result" in reply_2, f"ws endpoint must ignore an http-scoped fault; got {reply_2!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Cross-pool isolation — faults on other pools never reach the eth-sim ws
# endpoints. Under the old bare-pid model every transport shared pid "1"'s
# state, so a btc/tm down also refused the WS upgrade; the pool:pid model
# abolishes that. A down on the ws endpoint's OWN provider still refuses the
# upgrade — with or without a transports filter.
# ─────────────────────────────────────────────────────────────────────────────


class TestWsCrossPoolIsolation:

    def test_ws_handshake_killed_by_eth_down_fault(self, sim):
        """A provider-wide down on eth-sim:1 (no transports filter) covers
        BOTH its endpoints: the WS upgrade refuses with 503 — the router
        must see the whole provider as unreachable, or it never blocks it."""
        _control(sim, "POST", "/scenario", {"providers": {"eth-sim:1": {"mode": "down"}}})
        data = _raw_ws_upgrade(_WS_HOST, _WS_PORTS["1"])
        assert (
            b" 503 " in data.split(b"\r\n", 1)[0]
        ), f"WS upgrade should refuse with 503 under a provider-wide down; got {data[:80]!r}"

    def test_ws_handshake_unaffected_by_btc_error_fault(self, sim):
        """mode=error on btc-sim:1 must not 400 the WS upgrade of eth-sim:1
        — different pools share nothing."""
        _control(
            sim,
            "POST",
            "/scenario",
            {
                "providers": {
                    "btc-sim:1": {
                        "mode": "error",
                        "error_code": -32000,
                        "error_message": "BTC error stub",
                    }
                }
            },
        )
        data = _raw_ws_upgrade(_WS_HOST, _WS_PORTS["1"])
        assert b" 101 " in data.split(b"\r\n", 1)[0], f"WS upgrade should complete (101); got {data[:80]!r}"

    def test_ws_reader_loop_unaffected_by_btc_error_fault(self, sim):
        """mode=error on btc-sim:1 set AFTER handshake must not produce an
        error frame on eth-sim:1's reader loop. The WS success-path response
        should arrive instead."""
        with WsClient(_WS_HOST, _WS_PORTS["1"], "/ws") as c:
            _control(
                sim,
                "POST",
                "/scenario",
                {
                    "providers": {
                        "btc-sim:1": {
                            "mode": "error",
                            "error_code": -32000,
                            "error_message": "BTC error stub",
                        }
                    }
                },
            )
            c.send_json({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1})
            reply = c.recv_json(timeout=2.0)
        assert "result" in reply, f"WS reader should ignore a btc-sim error; got {reply!r}"
        assert "error" not in reply

    def test_ws_unaffected_by_rest_rate_limit_fault(self, sim):
        """A lava-sim-rest rate_limit must not 429 the WS upgrade."""
        _control(
            sim,
            "POST",
            "/scenario",
            {"providers": {"lava-sim-rest:1": {"mode": "rate_limit"}}},
        )
        data = _raw_ws_upgrade(_WS_HOST, _WS_PORTS["1"])
        assert b" 101 " in data.split(b"\r\n", 1)[0]

    def test_ws_fault_still_fires_when_scoped_to_ws(self, sim):
        """Sanity check: a down scoped to transports=["ws"] on eth-sim:1
        must still 503 the WS upgrade — the filter must not swallow the ws
        endpoint's own faults."""
        _control(
            sim,
            "POST",
            "/scenario",
            {"providers": {"eth-sim:1": {"mode": "down", "transports": ["ws"]}}},
        )
        data = _raw_ws_upgrade(_WS_HOST, _WS_PORTS["1"])
        assert b" 503 " in data.split(b"\r\n", 1)[0]

    def test_ws_handshake_unaffected_by_btc_down_fault(self, sim):
        """mode=down on btc-sim:1 downs only btc-sim:1 — eth-sim:1's WS
        upgrade completes."""
        _control(sim, "POST", "/scenario", {"providers": {"btc-sim:1": {"mode": "down"}}})
        data = _raw_ws_upgrade(_WS_HOST, _WS_PORTS["1"])
        assert b" 101 " in data.split(b"\r\n", 1)[0], f"WS upgrade must ignore a btc-sim down; got {data[:80]!r}"

    def test_ws_handshake_unaffected_by_tendermintrpc_down_fault(self, sim):
        """mode=down on lava-sim-tm:1 downs only that provider — eth-sim:1's
        WS upgrade completes."""
        _control(sim, "POST", "/scenario", {"providers": {"lava-sim-tm:1": {"mode": "down"}}})
        data = _raw_ws_upgrade(_WS_HOST, _WS_PORTS["1"])
        assert b" 101 " in data.split(b"\r\n", 1)[0], f"WS upgrade must ignore a lava-sim-tm down; got {data[:80]!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Sequenced faults within one provider — every targeted endpoint consumes the
# fail_first_n window (http and ws alike), and recovery is visible on both
# ─────────────────────────────────────────────────────────────────────────────


class TestWsSequencedFaults:

    def test_ws_upgrade_down_clears_after_window_consumed(self, sim):
        """A provider-wide sequenced down (fail_first_n=3, no transports
        filter) covers both endpoints of eth-sim:1, and every targeted call
        — ws upgrades and http requests alike — consumes the window. The
        refused upgrade burns 1, two http calls burn 2 more, and the next
        upgrade completes with then_mode=success."""
        _control(
            sim,
            "POST",
            "/scenario",
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "down",
                        "fail_first_n": 3,
                        "then_mode": "success",
                    }
                }
            },
        )

        data = _raw_ws_upgrade(_WS_HOST, _WS_PORTS["1"])
        assert (
            b" 503 " in data.split(b"\r\n", 1)[0]
        ), f"WS upgrade must refuse while the down window is open; got {data[:80]!r}"

        for i in (2, 3):
            req = urllib.request.Request(
                _HTTP_URLS["1"],
                method="POST",
                headers={"Content-Type": "application/json"},
                data=json.dumps({"jsonrpc": "2.0", "id": i, "method": "eth_blockNumber"}).encode(),
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    eth_code = resp.status
            except urllib.error.HTTPError as e:
                eth_code = e.code
            assert eth_code == 503, f"http call {i} is inside the down window; got {eth_code}"

        data = _raw_ws_upgrade(_WS_HOST, _WS_PORTS["1"])
        assert (
            b" 101 " in data.split(b"\r\n", 1)[0]
        ), f"WS upgrade must complete once the window is consumed; got {data[:80]!r}"


# ─────────────────────────────────────────────────────────────────────────────
# What WebSocket does for the upgrade request, for a subscribe frame and an
# unsubscribe frame, and for a pushed event
#
# The package provider_simulator/listeners/ serves each of the three, and it
# writes the history row. The upgrade request: JsonRpcWsListener.decide_upgrade.
# A subscribe frame and an unsubscribe frame: Listener.serve, with the content
# of JsonRpcWsListener.build_content. A pushed event: WsSubscriptions.emit. The
# first two also decide a fault. The tests below record the reply and the row
# of each one. Each expected value is written out by hand. A change of that
# code can make a value fail. Then the value is not edited to pass: the change
# is a change of behaviour, or it is a defect.
# ─────────────────────────────────────────────────────────────────────────────

# The sample key of RFC 6455, section 1.3. The RFC gives the accept value
# s3pPLMBiTxaQ9kYGzzhZRbK+xOo= for it.
_SAMPLE_KEY = "dGhlIHNhbXBsZSBub25jZQ=="

# The reply to an upgrade that succeeds on provider 1 with the sample key.
_HANDSHAKE_OF_PROVIDER_1 = (
    b"HTTP/1.1 101 Switching Protocols\r\n"
    b"Upgrade: websocket\r\n"
    b"Connection: Upgrade\r\n"
    b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n"
    b"Lava-Provider-Address: sim-provider-eth-sim:1\r\n"
    b"\r\n"
)

_LAVA_HEADERS = {"lava-consumer-relay": "7", "lava-stateful-api": "true"}
_RATE_LIMIT_TEXT = b"Rate limit exceeded. Reduce your request rate, or use an API key for a higher limit."
_PORT = _WS_PORTS["1"]
_SUBSCRIBE = {"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"], "id": 7}
_BLOCK_NUMBER = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 8}


def _set_scenario(sim, block, pid="1"):
    """Set one scenario block on a provider of the pool eth-sim."""
    status, _ = _control(sim, "POST", "/scenario", {"providers": {f"eth-sim:{pid}": block}})
    assert status == 200


def _rows(sim, pid="1"):
    """The history rows of the ws endpoint of one provider, oldest first."""
    _, body = _control(sim, "GET", f"/history?pool=eth-sim&pid={pid}&transport=ws")
    return body["history"]


def _rows_when_complete(sim, count, pid="1", timeout_s=2.0):
    """The rows of the ws endpoint, read when `count` rows exist and none is in flight."""
    deadline = time.monotonic() + timeout_s
    while True:
        rows = _rows(sim, pid)
        if len(rows) >= count and all(row["status"] != "in_flight" for row in rows):
            return rows
        if time.monotonic() > deadline:
            pytest.fail(f"expected {count} complete rows in {timeout_s} s, got {rows!r}")
        time.sleep(0.02)


def _every_row(sim):
    """Each history row of the simulator, of every pool and every transport."""
    _, body = _control(sim, "GET", "/history")
    return body["history"]


def _facts(row):
    """The method, the status, the latency and the request id of a row."""
    return (row["method"], row["status"], row["latency_ms"], row["request_id"])


def _endpoint(row):
    """The endpoint that a row names."""
    return (row["interface"], row["transport"], row["port"])


def _subscriptions(sim):
    _, body = _control(sim, "GET", "/ws/subscriptions")
    return body["subscriptions"]


def _emit(sim, body):
    """POST /ws/emit. Return the status and the body, for a refusal too."""
    request = urllib.request.Request(
        f"{sim['control']}/ws/emit",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(body).encode(),
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as refusal:
        return refusal.code, json.loads(refusal.read())


def _request_upgrade(port=_PORT, path="/ws", upgrade_headers=True, lava_headers=None):
    """Open a connection and send one request for an upgrade. Return the socket."""
    lines = [f"GET {path} HTTP/1.1", "Host: x"]
    if upgrade_headers:
        lines += [
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {_SAMPLE_KEY}",
            "Sec-WebSocket-Version: 13",
        ]
    lines += [f"{name}: {value}" for name, value in (lava_headers or {}).items()]
    sock = socket.create_connection((_WS_HOST, port), timeout=3)
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
    return sock


def _read_until_the_close(sock, timeout_s=3.0):
    """Read each byte that arrives until the simulator closes the connection.

    A reset counts as a close. A connection that stays open for `timeout_s`
    fails the test, and the failure shows the bytes that arrived.
    """
    sock.settimeout(timeout_s)
    received = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except ConnectionError:
            return received
        except socket.timeout:
            pytest.fail(f"the connection stayed open for {timeout_s} s, and these bytes arrived: {received!r}")
        if not chunk:
            return received
        received += chunk


def _refusal(sock):
    """Read a refused upgrade to its end. Return its HTTP status and its JSON body."""
    try:
        raw = _read_until_the_close(sock)
    finally:
        sock.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    try:
        return int(head.split(b" ", 2)[1]), json.loads(body)
    except (IndexError, ValueError):
        pytest.fail(f"expected a refusal with an HTTP status and a JSON body, and these bytes arrived: {raw!r}")


def _read_the_handshake(sock):
    """Read the reply to an upgrade request, up to the end of its headers."""
    head = b""
    while not head.endswith(b"\r\n\r\n"):
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
    return head


@contextlib.contextmanager
def _websocket(port=_PORT, lava_headers=None):
    """Open a WebSocket with the sample key. Give the socket after the handshake."""
    sock = _request_upgrade(port, lava_headers=lava_headers)
    try:
        head = _read_the_handshake(sock)
        assert head.startswith(b"HTTP/1.1 101 "), f"the upgrade did not succeed: {head!r}"
        yield sock
    finally:
        try:
            sock.sendall(ws_protocol.encode_frame(ws_protocol.OPCODE_CLOSE, b"", mask=True))
        except OSError:
            pass
        sock.close()


def _send_frame(sock, message):
    """Send one JSON-RPC message as a text frame."""
    sock.sendall(ws_protocol.encode_frame(ws_protocol.OPCODE_TEXT, json.dumps(message).encode(), mask=True))


def _payload(sock, timeout_s=3.0):
    """The payload bytes of the next frame. The frame must be a text frame."""
    sock.settimeout(timeout_s)
    try:
        frame = ws_protocol.parse_frame(sock.recv)
    except socket.timeout:
        pytest.fail(f"expected a frame in {timeout_s} s, and no frame arrived")
    assert (
        frame.opcode == ws_protocol.OPCODE_TEXT
    ), f"expected a text frame, and a frame with the opcode {frame.opcode} arrived: {frame.payload!r}"
    return frame.payload


def _reply(sock, timeout_s=3.0):
    """The next frame, read as JSON."""
    payload = _payload(sock, timeout_s)
    try:
        return json.loads(payload)
    except ValueError:
        pytest.fail(f"expected a JSON reply, and this payload arrived: {payload!r}")


def _assert_no_frame(sock, wait_s=0.5):
    """No byte arrives in `wait_s`, and the connection stays open."""
    sock.settimeout(wait_s)
    try:
        arrived = sock.recv(1)
    except socket.timeout:
        return
    except ConnectionError as reset:
        pytest.fail(f"expected an open connection for {wait_s} s, and the simulator reset it: {reset!r}")
    if arrived:
        pytest.fail(f"expected no byte in {wait_s} s, and this byte arrived: {arrived!r}")
    pytest.fail(f"expected an open connection for {wait_s} s, and the simulator closed it")


def _subscribe(sock, method="eth_subscribe", frame_id=1):
    """Send one subscribe frame. Return the subscription id of the reply."""
    _send_frame(sock, {"jsonrpc": "2.0", "method": method, "params": ["newHeads"], "id": frame_id})
    reply = _reply(sock)
    assert (
        isinstance(reply, dict) and "result" in reply
    ), f"expected a subscription id, and this reply arrived: {reply!r}"
    return reply["result"]


class TestTheUpgradeRequest:
    """The upgrade request that opens a WebSocket: a refusal, and an upgrade
    that succeeds.

    The upgrade is not a request of the request flow.
    `JsonRpcWsListener.decide_upgrade` asks the fault policy, and it writes the
    row of a refusal. The adapter `_WsHandler` performs the decision. An
    upgrade that succeeds writes no row.
    """

    @pytest.mark.parametrize(
        "block, status, body, method, row_status",
        [
            pytest.param({"mode": "down"}, 503, {"error": "provider down"}, "*", "down", id="down"),
            pytest.param(
                {"mode": "rate_limit"}, 429, {"error": "rate limited"}, "ws_upgrade", "rate_limit", id="rate-limit"
            ),
            pytest.param(
                {"mode": "rate_limit", "rate_limit_body": "slow down"},
                429,
                {"error": "rate limited"},
                "ws_upgrade",
                "rate_limit",
                id="rate-limit-with-a-rate-limit-body",
            ),
            pytest.param(
                {"mode": "error"},
                400,
                {"error": "Internal error"},
                "ws_upgrade",
                "error",
                id="error-with-the-default-message",
            ),
            pytest.param(
                {"mode": "error", "error_message": "refused by the test", "error_code": -32099, "http_status": 503},
                400,
                {"error": "refused by the test"},
                "ws_upgrade",
                "error",
                id="error-with-a-message-a-code-and-an-http-status",
            ),
            pytest.param(
                {"mode": "success", "error_probability": 1.0},
                400,
                {"error": "Internal error"},
                "ws_upgrade",
                "error",
                id="error-probability-of-one",
            ),
        ],
    )
    def test_a_refused_upgrade_gets_its_status_and_its_body_and_writes_one_row(
        self, sim, block, status, body, method, row_status
    ):
        """Each fault that answers refuses the upgrade with its own HTTP status
        and its own JSON body. The refusal writes one complete row. Its method
        is `*` for `down` and `ws_upgrade` for each other fault. The row has
        `latency_ms` 0, no request id, and the lava headers of the upgrade
        request."""
        _set_scenario(sim, {**block, "transports": ["ws"]})

        answer = _refusal(_request_upgrade(lava_headers=_LAVA_HEADERS))

        assert answer == (status, body)
        rows = _rows(sim)
        assert [_facts(row) for row in rows] == [(method, row_status, 0, None)]
        assert _endpoint(rows[0]) == ("jsonrpc", "ws", _PORT)
        assert rows[0]["lava_headers"] == _LAVA_HEADERS

    def test_a_hung_upgrade_gets_no_byte_and_writes_one_row(self, sim):
        """A `hang` provider sends no byte for the upgrade. The row is written
        before the wait, so a test can read it while the upgrade hangs."""
        _set_scenario(sim, {"mode": "hang", "transports": ["ws"]})

        sock = _request_upgrade(lava_headers=_LAVA_HEADERS)
        try:
            _assert_no_frame(sock)
            rows = _rows_when_complete(sim, 1)
        finally:
            sock.close()

        assert [_facts(row) for row in rows] == [("ws_upgrade", "hang", 0, None)]
        assert _endpoint(rows[0]) == ("jsonrpc", "ws", _PORT)
        assert rows[0]["lava_headers"] == _LAVA_HEADERS

    def test_a_hung_upgrade_ends_after_30_seconds_with_a_closed_connection_and_no_byte(self, sim):
        """The adapter holds a hung upgrade for 30 seconds. Then it closes the
        connection, and it sends no byte. This is the one slow test of this
        part of the file."""
        _set_scenario(sim, {"mode": "hang", "transports": ["ws"]})

        sock = _request_upgrade()
        started = time.monotonic()
        try:
            received = _read_until_the_close(sock, timeout_s=40.0)
        finally:
            sock.close()
        waited = time.monotonic() - started

        assert received == b""
        assert 29.0 <= waited <= 35.0, f"the upgrade hung for {waited:.1f} s"

    @pytest.mark.parametrize(
        "drop_at, reply",
        [
            pytest.param(None, b"", id="no-drop-point-in-the-scenario"),
            pytest.param("before_headers", b"", id="before-headers"),
            pytest.param("after_headers", _HANDSHAKE_OF_PROVIDER_1, id="after-headers"),
            pytest.param("mid_body", b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: webso", id="mid-body"),
        ],
    )
    def test_a_dropped_upgrade_gets_the_bytes_of_its_drop_point_and_writes_one_row(self, sim, drop_at, reply):
        """`drop_connection` closes the connection of the upgrade. The drop
        point says which bytes arrive first: none, the complete 101 reply, or
        the first 48 bytes of it."""
        block = {"mode": "drop_connection", "transports": ["ws"]}
        if drop_at is not None:
            block["drop_at"] = drop_at
        _set_scenario(sim, block)

        sock = _request_upgrade(lava_headers=_LAVA_HEADERS)
        try:
            received = _read_until_the_close(sock)
        finally:
            sock.close()

        assert received == reply
        rows = _rows(sim)
        assert [_facts(row) for row in rows] == [("ws_upgrade", "drop_connection", 0, None)]
        assert _endpoint(rows[0]) == ("jsonrpc", "ws", _PORT)
        assert rows[0]["lava_headers"] == _LAVA_HEADERS

    def test_an_upgrade_that_succeeds_gets_the_101_reply_and_writes_no_row(self, sim):
        """An upgrade with no fault gets the 101 reply with the provider name,
        and the simulator writes no history row for it."""
        sock = _request_upgrade(lava_headers=_LAVA_HEADERS)
        try:
            head = _read_the_handshake(sock)
        finally:
            sock.close()

        assert head == _HANDSHAKE_OF_PROVIDER_1
        assert _every_row(sim) == []

    @pytest.mark.parametrize("mode", ["success", "down"])
    def test_a_request_for_another_path_gets_404_and_writes_no_row(self, sim, mode):
        """The adapter answers a wrong path itself, before it asks the listener
        for a decision. So a `down` provider answers 404 too, and no row is
        written."""
        _set_scenario(sim, {"mode": mode, "transports": ["ws"]})

        answer = _refusal(_request_upgrade(path="/"))

        assert answer == (404, {"error": "not found"})
        assert _every_row(sim) == []

    @pytest.mark.parametrize("mode", ["success", "down"])
    def test_a_request_with_no_upgrade_headers_gets_400_and_writes_no_row(self, sim, mode):
        """The adapter answers a request that is no upgrade itself, before it
        asks the listener for a decision. So a `down` provider answers 400 too,
        and no row is written."""
        _set_scenario(sim, {"mode": mode, "transports": ["ws"]})

        answer = _refusal(_request_upgrade(upgrade_headers=False))

        assert answer == (400, {"error": "bad WS upgrade request"})
        assert _every_row(sim) == []

    def test_a_refused_upgrade_does_not_wait_for_latency_ms(self, sim):
        """The upgrade applies no `latency_ms`: the refusal comes at once, and
        its row records 0."""
        _set_scenario(sim, {"mode": "rate_limit", "latency_ms": 1500, "transports": ["ws"]})

        started = time.monotonic()
        answer = _refusal(_request_upgrade())
        waited = time.monotonic() - started

        assert answer == (429, {"error": "rate limited"})
        assert waited < 1.0, f"the refusal took {waited:.2f} s"
        assert [_facts(row) for row in _rows(sim)] == [("ws_upgrade", "rate_limit", 0, None)]

    def test_an_upgrade_that_succeeds_does_not_wait_for_latency_ms(self, sim):
        """The upgrade applies no `latency_ms`: the 101 reply comes at once."""
        _set_scenario(sim, {"mode": "success", "latency_ms": 1500, "transports": ["ws"]})

        started = time.monotonic()
        sock = _request_upgrade()
        try:
            head = _read_the_handshake(sock)
        finally:
            sock.close()
        waited = time.monotonic() - started

        assert head == _HANDSHAKE_OF_PROVIDER_1
        assert waited < 1.0, f"the handshake took {waited:.2f} s"

    @pytest.mark.parametrize(
        "corruption_mode", ["truncated", "invalid_json", "empty_response", "null_body", "missing_field", "wrong_type"]
    )
    def test_the_upgrade_applies_no_corruption(self, sim, corruption_mode):
        """A `corruption_mode` does not change the reply of the upgrade: not
        the body of a refusal, and not the 101 reply."""
        corruption = {"corruption_mode": corruption_mode, "missing_field": "error", "transports": ["ws"]}

        _set_scenario(sim, {"mode": "rate_limit", **corruption})
        assert _refusal(_request_upgrade()) == (429, {"error": "rate limited"})

        _set_scenario(sim, {"mode": "success", **corruption})
        sock = _request_upgrade()
        try:
            assert _read_the_handshake(sock) == _HANDSHAKE_OF_PROVIDER_1
        finally:
            sock.close()

    @pytest.mark.parametrize("key", ["ws_upgrade", "*"])
    def test_the_upgrade_reads_no_per_method_override(self, sim, key):
        """An entry of `responses` does not reach the upgrade, also when its
        key is the method name of the row of a refused upgrade."""
        _set_scenario(sim, {"mode": "success", "transports": ["ws"], "responses": {key: {"mode": "down"}}})
        sock = _request_upgrade()
        try:
            assert _read_the_handshake(sock) == _HANDSHAKE_OF_PROVIDER_1
        finally:
            sock.close()
        assert _every_row(sim) == []

        _set_scenario(sim, {"mode": "rate_limit", "transports": ["ws"], "responses": {key: {"mode": "success"}}})
        assert _refusal(_request_upgrade()) == (429, {"error": "rate limited"})

    @pytest.mark.parametrize(
        "scope",
        [
            pytest.param({"transports": ["http"]}, id="a-transports-filter"),
            pytest.param({"ports": [port_of("eth-sim", "1")]}, id="a-ports-filter"),
        ],
    )
    def test_a_filter_that_does_not_name_the_ws_endpoint_leaves_the_upgrade_alone(self, sim, scope):
        """A `down` with a filter that names the http endpoint only does not
        reach the upgrade: it succeeds, and no row is written."""
        _set_scenario(sim, {"mode": "down", **scope})

        sock = _request_upgrade()
        try:
            head = _read_the_handshake(sock)
        finally:
            sock.close()

        assert head == _HANDSHAKE_OF_PROVIDER_1
        assert _every_row(sim) == []

    def test_the_upgrade_performs_no_pause(self, sim):
        """`pause_at` holds a reply of the http endpoint of the provider. The
        upgrade of its ws endpoint is not held."""
        _set_scenario(sim, {"mode": "success", "pause_at": "mid_body", "pause_ms": 1500})

        started = time.monotonic()
        sock = _request_upgrade()
        try:
            head = _read_the_handshake(sock)
        finally:
            sock.close()
        waited = time.monotonic() - started

        assert head == _HANDSHAKE_OF_PROVIDER_1
        assert waited < 1.0, f"the handshake took {waited:.2f} s"

    def test_each_upgrade_uses_one_count_of_the_fail_first_n_window(self, sim):
        """With `fail_first_n` 2 on the ws endpoint, two upgrades are refused
        and each one writes its row. The third upgrade succeeds and writes no
        row."""
        _set_scenario(sim, {"mode": "rate_limit", "fail_first_n": 2, "then_mode": "success", "transports": ["ws"]})

        first = _refusal(_request_upgrade())
        second = _refusal(_request_upgrade())
        sock = _request_upgrade()
        try:
            third = _read_the_handshake(sock)
        finally:
            sock.close()

        assert first == (429, {"error": "rate limited"})
        assert second == (429, {"error": "rate limited"})
        assert third == _HANDSHAKE_OF_PROVIDER_1
        assert [_facts(row) for row in _rows(sim)] == [("ws_upgrade", "rate_limit", 0, None)] * 2


class TestSubscribeAndUnsubscribeFrames:
    """A subscribe frame and an unsubscribe frame.

    These frames go through the request flow, `Listener.serve`, as a frame such
    as `eth_blockNumber` does. `JsonRpcWsListener.build_content` gives their success
    content from the subscription registry, and it asks no chain for them.
    """

    def test_each_frame_writes_one_success_row_with_its_method_and_its_request_id(self, sim):
        """Three frames: a subscribe frame, an unsubscribe frame that removes
        the subscription, and an unsubscribe frame that removes nothing. Each
        one writes one `success` row with the method and the id of the frame.
        Each row carries the lava headers of the upgrade request."""
        with _websocket(lava_headers=_LAVA_HEADERS) as sock:
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"], "id": 41})
            subscription_id = _reply(sock)["result"]
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_unsubscribe", "params": [subscription_id], "id": 42})
            removed = _reply(sock)
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_unsubscribe", "params": [subscription_id], "id": 43})
            not_removed = _reply(sock)
            rows = _rows(sim)

        assert re.fullmatch(r"0x[0-9a-f]{32}", subscription_id), subscription_id
        assert removed == {"jsonrpc": "2.0", "id": 42, "result": True}
        assert not_removed == {"jsonrpc": "2.0", "id": 43, "result": False}
        assert [_facts(row) for row in rows] == [
            ("eth_subscribe", "success", 0, 41),
            ("eth_unsubscribe", "success", 0, 42),
            ("eth_unsubscribe", "success", 0, 43),
        ]
        for row in rows:
            assert _endpoint(row) == ("jsonrpc", "ws", _PORT)
            assert row["lava_headers"] == _LAVA_HEADERS

    @pytest.mark.parametrize(
        "subscribe, unsubscribe",
        [
            pytest.param("eth_subscribe", "eth_unsubscribe", id="eth"),
            pytest.param("subscribe", "unsubscribe", id="tendermint"),
            pytest.param("accountSubscribe", "accountUnsubscribe", id="solana-account"),
            pytest.param("logsSubscribe", "logsUnsubscribe", id="solana-logs"),
            pytest.param("accountSubscribe", "eth_unsubscribe", id="an-unsubscribe-method-of-another-chain"),
        ],
    )
    def test_each_subscribe_method_and_each_unsubscribe_method_writes_a_row_with_its_own_name(
        self, sim, subscribe, unsubscribe
    ):
        """Each of the four subscribe methods registers a subscription, and each
        of the four unsubscribe methods removes a subscription of its own
        connection. The method name of each row is the method of its frame."""
        with _websocket() as sock:
            subscription_id = _subscribe(sock, subscribe, frame_id=1)
            _send_frame(sock, {"jsonrpc": "2.0", "method": unsubscribe, "params": [subscription_id], "id": 2})
            removed = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert removed == {"jsonrpc": "2.0", "id": 2, "result": True}
        assert [_facts(row) for row in rows] == [(subscribe, "success", 0, 1), (unsubscribe, "success", 0, 2)]
        assert subscriptions == []

    def test_a_text_id_goes_into_the_reply_and_into_the_row(self, sim):
        """The id of a subscribe frame can be text. The reply and the row carry
        it as it is."""
        with _websocket() as sock:
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"], "id": "sub-a"})
            reply = _reply(sock)
            rows = _rows(sim)

        assert reply["id"] == "sub-a"
        assert [_facts(row) for row in rows] == [("eth_subscribe", "success", 0, "sub-a")]

    def test_a_frame_with_no_id_gets_a_reply_with_the_id_null_and_a_row_with_no_request_id(self, sim):
        """A subscribe frame and an unsubscribe frame with no id: the reply has
        the id `null`, and the row has no request id. An unsubscribe frame with
        no params removes nothing."""
        with _websocket() as sock:
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"]})
            subscribed = _reply(sock)
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_unsubscribe"})
            not_removed = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert subscribed["id"] is None
        assert not_removed == {"jsonrpc": "2.0", "id": None, "result": False}
        assert [_facts(row) for row in rows] == [
            ("eth_subscribe", "success", 0, None),
            ("eth_unsubscribe", "success", 0, None),
        ]
        assert [entry["subscription_id"] for entry in subscriptions] == [subscribed["result"]]

    def test_a_subscribe_frame_waits_for_latency_ms_and_its_row_records_it(self, sim):
        """The reply of a subscribe frame comes after the latency of the
        scenario, and the row records that latency."""
        _set_scenario(sim, {"mode": "success", "latency_ms": 300, "transports": ["ws"]})

        with _websocket() as sock:
            started = time.monotonic()
            _send_frame(sock, _SUBSCRIBE)
            reply = _reply(sock)
            waited = time.monotonic() - started
            rows = _rows(sim)

        assert re.fullmatch(r"0x[0-9a-f]{32}", reply["result"]), reply
        assert waited >= 0.3, f"the reply came after {waited:.2f} s"
        assert [_facts(row) for row in rows] == [("eth_subscribe", "success", 300, 7)]

    def test_the_row_and_the_subscription_exist_while_the_provider_still_waits(self, sim):
        """The row of a subscribe frame is complete, and the subscription is
        registered, before the provider waits for the latency. A test can read
        both while the reply is still held back."""
        _set_scenario(sim, {"mode": "success", "latency_ms": 1500, "transports": ["ws"]})

        with _websocket() as sock:
            _send_frame(sock, _SUBSCRIBE)
            rows = _rows_when_complete(sim, 1, timeout_s=1.0)
            subscriptions = _subscriptions(sim)
            _assert_no_frame(sock, wait_s=0.2)
            reply = _reply(sock)

        assert [_facts(row) for row in rows] == [("eth_subscribe", "success", 1500, 7)]
        assert [entry["subscription_id"] for entry in subscriptions] == [reply["result"]]

    def test_a_subscribe_frame_of_a_down_provider_closes_the_connection_and_its_row_names_no_frame(self, sim):
        """A provider-wide `down` on a subscribe frame: no reply, and the
        connection closes with no wait. A `down` provider does not read the
        frame. So the row has the method `*`, no request id and `latency_ms`
        0, as the `down` row of each other frame."""
        with _websocket(lava_headers=_LAVA_HEADERS) as sock:
            _set_scenario(sim, {"mode": "down", "latency_ms": 1500, "transports": ["ws"]})
            started = time.monotonic()
            _send_frame(sock, _SUBSCRIBE)
            received = _read_until_the_close(sock)
            waited = time.monotonic() - started

        assert received == b""
        assert waited < 1.0, f"the connection closed after {waited:.2f} s"
        rows = _rows(sim)
        assert [_facts(row) for row in rows] == [("*", "down", 0, None)]
        assert _endpoint(rows[0]) == ("jsonrpc", "ws", _PORT)
        assert rows[0]["lava_headers"] == _LAVA_HEADERS
        assert _subscriptions(sim) == []

    def test_a_subscribe_frame_of_a_hung_provider_gets_no_reply_and_the_connection_stays_open(self, sim):
        """A `hang` on a subscribe frame: no reply, and no subscription. The
        provider does not wait for the configured latency, so the row records
        0. The connection stays open: it answers the next frame when the fault
        is gone."""
        with _websocket() as sock:
            _set_scenario(sim, {"mode": "hang", "latency_ms": 250, "transports": ["ws"]})
            _send_frame(sock, _SUBSCRIBE)
            _assert_no_frame(sock)
            rows = _rows_when_complete(sim, 1)
            subscriptions = _subscriptions(sim)
            _set_scenario(sim, {"mode": "success", "latency_ms": 0, "transports": ["ws"]})
            _send_frame(sock, _BLOCK_NUMBER)
            next_reply = _reply(sock)

        assert [_facts(row) for row in rows] == [("eth_subscribe", "hang", 0, 7)]
        assert subscriptions == []
        assert next_reply["id"] == 8

    @pytest.mark.parametrize(
        "block, text",
        [
            pytest.param({"mode": "rate_limit"}, _RATE_LIMIT_TEXT, id="the-default-text"),
            pytest.param({"mode": "rate_limit", "rate_limit_body": "slow down"}, b"slow down", id="rate-limit-body"),
        ],
    )
    def test_a_subscribe_frame_of_a_rate_limited_provider_gets_the_text_of_the_rate_limit(self, sim, block, text):
        """`rate_limit` on a subscribe frame: the reply is a text frame with
        the rate-limit text, and no subscription is registered."""
        with _websocket() as sock:
            _set_scenario(sim, {**block, "latency_ms": 100, "transports": ["ws"]})
            _send_frame(sock, _SUBSCRIBE)
            payload = _payload(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert payload == text
        assert [_facts(row) for row in rows] == [("eth_subscribe", "rate_limit", 100, 7)]
        assert subscriptions == []

    @pytest.mark.parametrize(
        "block, frame, reply, expected_row",
        [
            pytest.param(
                {"mode": "error"},
                _SUBSCRIBE,
                {"jsonrpc": "2.0", "id": 7, "error": {"code": -32000, "message": "Internal error"}},
                ("eth_subscribe", "error", 0, 7),
                id="error-with-the-default-code-and-message",
            ),
            pytest.param(
                {"mode": "error", "error_code": -32601, "error_message": "Method not found"},
                _SUBSCRIBE,
                {"jsonrpc": "2.0", "id": 7, "error": {"code": -32601, "message": "Method not found"}},
                ("eth_subscribe", "error", 0, 7),
                id="error-with-a-code-and-a-message",
            ),
            pytest.param(
                {"mode": "success", "error_probability": 1.0, "error_code": -32007, "error_message": "Forced"},
                _SUBSCRIBE,
                {"jsonrpc": "2.0", "id": 7, "error": {"code": -32007, "message": "Forced"}},
                ("eth_subscribe", "error", 0, 7),
                id="a-provider-wide-error-probability-of-one",
            ),
            pytest.param(
                {"mode": "error"},
                {"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"]},
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "Internal error"}},
                ("eth_subscribe", "error", 0, None),
                id="a-frame-with-no-id-gets-the-id-1-in-the-error",
            ),
            pytest.param(
                {"mode": "error"},
                {"jsonrpc": "2.0", "method": "eth_unsubscribe", "params": ["0x00"], "id": 9},
                {"jsonrpc": "2.0", "id": 9, "error": {"code": -32000, "message": "Internal error"}},
                ("eth_unsubscribe", "error", 0, 9),
                id="an-unsubscribe-frame",
            ),
        ],
    )
    def test_a_subscription_frame_of_a_provider_with_an_error_gets_the_error_reply(
        self, sim, block, frame, reply, expected_row
    ):
        """`error`, or an `error_probability` of the provider, on a subscribe
        frame or an unsubscribe frame. The reply is the JSON-RPC error of the
        scenario, and no subscription is registered."""
        with _websocket() as sock:
            _set_scenario(sim, {**block, "transports": ["ws"]})
            _send_frame(sock, frame)
            answer = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert answer == reply
        assert [_facts(row) for row in rows] == [expected_row]
        assert subscriptions == []

    def test_an_unsubscribe_frame_of_a_provider_with_an_error_removes_nothing(self, sim):
        """`error` on an unsubscribe frame that names a subscription of its own
        connection: the reply is the JSON-RPC error, and the subscription stays."""
        with _websocket() as sock:
            subscription_id = _subscribe(sock, frame_id=1)
            _set_scenario(sim, {"mode": "error", "transports": ["ws"]})
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_unsubscribe", "params": [subscription_id], "id": 2})
            answer = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert answer == {"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "Internal error"}}
        assert [_facts(row) for row in rows] == [("eth_subscribe", "success", 0, 1), ("eth_unsubscribe", "error", 0, 2)]
        assert [entry["subscription_id"] for entry in subscriptions] == [subscription_id]

    @pytest.mark.parametrize(
        "drop_at, received",
        [
            pytest.param(None, b"", id="no-drop-point-in-the-scenario"),
            pytest.param("before_headers", b"", id="before-headers"),
            pytest.param("after_headers", bytes([0x81, 100]), id="after-headers"),
            pytest.param("mid_body", bytes([0x81, 100]) + b"X" * 50, id="mid-body"),
        ],
    )
    def test_a_dropped_subscribe_frame_gets_the_bytes_of_its_drop_point(self, sim, drop_at, received):
        """`drop_connection` on a subscribe frame closes the connection. The
        drop point says which bytes arrive first: none, a frame header that
        declares 100 payload bytes, or that header and 50 bytes."""
        block = {"mode": "drop_connection", "transports": ["ws"]}
        if drop_at is not None:
            block["drop_at"] = drop_at

        with _websocket() as sock:
            _set_scenario(sim, block)
            _send_frame(sock, _SUBSCRIBE)
            arrived = _read_until_the_close(sock)

        assert arrived == received
        assert [_facts(row) for row in _rows(sim)] == [("eth_subscribe", "drop_connection", 0, 7)]
        assert _subscriptions(sim) == []

    @pytest.mark.parametrize(
        "corruption_mode, subscribe_payload, unsubscribe_payload",
        [
            pytest.param(
                "truncated",
                b'{"jsonrpc": "2.0", "id": 7, "result": "0x<the first 24 digits of the id>',
                b'{"jsonrpc": "2.0", "id": 9, "resu',
                id="truncated",
            ),
            pytest.param("invalid_json", b"}{ {{ not valid json", b"}{ {{ not valid json", id="invalid-json"),
            pytest.param("empty_response", b"", b"", id="empty-response"),
            pytest.param("null_body", b"null", b"null", id="null-body"),
            pytest.param(
                "missing_field", b'{"jsonrpc": "2.0", "id": 7}', b'{"jsonrpc": "2.0", "id": 9}', id="missing-field"
            ),
            pytest.param(
                "wrong_type",
                b'{"jsonrpc": "2.0", "id": 7, "result": 12345}',
                b'{"jsonrpc": "2.0", "id": 9, "result": 1}',
                id="wrong-type",
            ),
        ],
    )
    def test_a_corruption_mode_changes_the_reply_of_a_subscribe_and_of_an_unsubscribe_frame(
        self, sim, corruption_mode, subscribe_payload, unsubscribe_payload
    ):
        """A `corruption_mode` reaches the success reply of a subscribe frame
        and of an unsubscribe frame. The subscription is registered, the
        unsubscribe frame removes it, and both rows stay `success`. The caller
        cannot read the id from a corrupted reply, so the test reads it from
        GET /ws/subscriptions. A `truncated` reply loses its last 10 bytes: 24
        of the 32 digits of the id stay."""
        _set_scenario(
            sim,
            {"mode": "success", "corruption_mode": corruption_mode, "missing_field": "result", "transports": ["ws"]},
        )

        with _websocket() as sock:
            _send_frame(sock, _SUBSCRIBE)
            subscribed = _payload(sock)
            registered = [entry["subscription_id"] for entry in _subscriptions(sim)]
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_unsubscribe", "params": registered[:1], "id": 9})
            removed = _payload(sock)
            rows = _rows(sim)
            left = _subscriptions(sim)

        assert len(registered) == 1 and re.fullmatch(r"0x[0-9a-f]{32}", registered[0]), registered
        first_24_digits = registered[0][2:26].encode()
        assert subscribed == subscribe_payload.replace(b"<the first 24 digits of the id>", first_24_digits)
        assert removed == unsubscribe_payload
        assert [_facts(row) for row in rows] == [
            ("eth_subscribe", "success", 0, 7),
            ("eth_unsubscribe", "success", 0, 9),
        ]
        assert left == []

    @pytest.mark.parametrize(
        "block, payload, row_status",
        [
            pytest.param(
                {"mode": "error", "corruption_mode": "invalid_json"},
                b"}{ {{ not valid json",
                "error",
                id="invalid-json-on-an-error-reply",
            ),
            pytest.param(
                {"mode": "rate_limit", "corruption_mode": "empty_response"},
                b"",
                "rate_limit",
                id="empty-response-on-a-rate-limit-reply",
            ),
        ],
    )
    def test_a_corruption_mode_changes_the_fault_reply_of_a_subscribe_frame(self, sim, block, payload, row_status):
        """A `corruption_mode` reaches the reply of a fault on a subscribe
        frame: the `error` reply and the `rate_limit` reply. The row keeps the
        status of the fault."""
        with _websocket() as sock:
            _set_scenario(sim, {**block, "transports": ["ws"]})
            _send_frame(sock, _SUBSCRIBE)
            arrived = _payload(sock)
            rows = _rows(sim)

        assert arrived == payload
        assert [_facts(row) for row in rows] == [("eth_subscribe", row_status, 0, 7)]

    def test_a_per_method_rate_limit_reaches_the_subscribe_frame_only(self, sim):
        """An entry of `responses` for `eth_subscribe` with `mode: rate_limit`.
        The subscribe frame gets the rate-limit text, and another frame of the
        same connection gets its result."""
        _set_scenario(
            sim, {"mode": "success", "transports": ["ws"], "responses": {"eth_subscribe": {"mode": "rate_limit"}}}
        )

        with _websocket() as sock:
            _send_frame(sock, _SUBSCRIBE)
            payload = _payload(sock)
            _send_frame(sock, _BLOCK_NUMBER)
            other = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert payload == _RATE_LIMIT_TEXT
        assert other["id"] == 8 and other["result"].startswith("0x")
        assert [_facts(row)[:2] for row in rows] == [("eth_subscribe", "rate_limit"), ("eth_blockNumber", "success")]
        assert subscriptions == []

    def test_a_per_method_down_with_a_latency_closes_at_once_and_its_row_records_the_latency(self, sim):
        """An entry of `responses` for `eth_subscribe` with `mode: down` and a
        latency: the connection closes with no wait and with no reply. The row
        records the method, the id of the frame and the latency of the entry."""
        _set_scenario(
            sim,
            {
                "mode": "success",
                "transports": ["ws"],
                "responses": {"eth_subscribe": {"mode": "down", "latency_ms": 1500}},
            },
        )

        with _websocket() as sock:
            started = time.monotonic()
            _send_frame(sock, _SUBSCRIBE)
            received = _read_until_the_close(sock)
            waited = time.monotonic() - started

        assert received == b""
        assert waited < 1.0, f"the connection closed after {waited:.2f} s"
        assert [_facts(row) for row in _rows(sim)] == [("eth_subscribe", "down", 1500, 7)]
        assert _subscriptions(sim) == []

    def test_a_per_method_hang_gives_no_reply_to_the_subscribe_frame(self, sim):
        """An entry of `responses` for `eth_subscribe` with `mode: hang`: no
        reply and no subscription, and the row has the status `hang`."""
        _set_scenario(sim, {"mode": "success", "transports": ["ws"], "responses": {"eth_subscribe": {"mode": "hang"}}})

        with _websocket() as sock:
            _send_frame(sock, _SUBSCRIBE)
            _assert_no_frame(sock)
            rows = _rows_when_complete(sim, 1)
            subscriptions = _subscriptions(sim)

        assert [_facts(row) for row in rows] == [("eth_subscribe", "hang", 0, 7)]
        assert subscriptions == []

    def test_a_per_method_drop_point_reaches_the_subscribe_frame(self, sim):
        """An entry of `responses` for `eth_subscribe` with `drop_connection`
        and the drop point `mid_body`. The frame header that declares 100
        bytes arrives with 50 bytes, and the connection closes."""
        _set_scenario(
            sim,
            {
                "mode": "success",
                "transports": ["ws"],
                "responses": {"eth_subscribe": {"mode": "drop_connection", "drop_at": "mid_body"}},
            },
        )

        with _websocket() as sock:
            _send_frame(sock, _SUBSCRIBE)
            arrived = _read_until_the_close(sock)

        assert arrived == bytes([0x81, 100]) + b"X" * 50
        assert [_facts(row) for row in _rows(sim)] == [("eth_subscribe", "drop_connection", 0, 7)]

    def test_a_per_method_latency_delays_the_subscribe_frame_only(self, sim):
        """An entry of `responses` for `eth_subscribe` with a latency: the
        subscribe frame waits and its row records the latency. The row of
        another frame of the same connection records 0."""
        _set_scenario(
            sim, {"mode": "success", "transports": ["ws"], "responses": {"eth_subscribe": {"latency_ms": 300}}}
        )

        with _websocket() as sock:
            started = time.monotonic()
            _send_frame(sock, _SUBSCRIBE)
            _reply(sock)
            waited = time.monotonic() - started
            _send_frame(sock, _BLOCK_NUMBER)
            _reply(sock)
            rows = _rows(sim)

        assert waited >= 0.3, f"the reply came after {waited:.2f} s"
        assert [_facts(row) for row in rows] == [
            ("eth_subscribe", "success", 300, 7),
            ("eth_blockNumber", "success", 0, 8),
        ]

    def test_a_per_method_error_code_and_message_reach_the_subscribe_frame_only(self, sim):
        """Under a provider-wide `error`, an entry of `responses` for
        `eth_subscribe` gives the subscribe frame its own error code and its
        own message. Another frame gets the code and the message of the
        provider."""
        with _websocket() as sock:
            _set_scenario(
                sim,
                {
                    "mode": "error",
                    "error_code": -32000,
                    "error_message": "of the provider",
                    "transports": ["ws"],
                    "responses": {"eth_subscribe": {"error_code": -32601, "error_message": "of this method"}},
                },
            )
            _send_frame(sock, _SUBSCRIBE)
            of_the_method = _reply(sock)
            _send_frame(sock, _BLOCK_NUMBER)
            of_the_provider = _reply(sock)

        assert of_the_method == {"jsonrpc": "2.0", "id": 7, "error": {"code": -32601, "message": "of this method"}}
        assert of_the_provider == {"jsonrpc": "2.0", "id": 8, "error": {"code": -32000, "message": "of the provider"}}

    def test_a_per_method_error_probability_reaches_a_subscribe_frame(self, sim):
        """The request flow reads `error_probability` from an entry of
        `responses`. The entry for `eth_subscribe` gives the subscribe frame
        the error reply, and no subscription is registered. The same entry for
        `eth_blockNumber` gives that frame the error reply too."""
        _set_scenario(
            sim,
            {
                "mode": "success",
                "transports": ["ws"],
                "responses": {
                    "eth_subscribe": {"error_probability": 1.0},
                    "eth_blockNumber": {"error_probability": 1.0},
                },
            },
        )

        with _websocket() as sock:
            _send_frame(sock, _SUBSCRIBE)
            subscribed = _reply(sock)
            _send_frame(sock, _BLOCK_NUMBER)
            other = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert subscribed == {"jsonrpc": "2.0", "id": 7, "error": {"code": -32000, "message": "Internal error"}}
        assert other == {"jsonrpc": "2.0", "id": 8, "error": {"code": -32000, "message": "Internal error"}}
        assert [_facts(row) for row in rows] == [("eth_subscribe", "error", 0, 7), ("eth_blockNumber", "error", 0, 8)]
        assert subscriptions == []

    def test_a_canned_body_answers_a_subscribe_frame_and_registers_no_subscription(self, sim):
        """The request flow reads a canned `body` from an entry of `responses`.
        The entry for `eth_subscribe` answers the subscribe frame with that
        body, with the id of the body and not the id of the frame. No
        subscription is registered, and the row is a `success` row with the id
        of the frame. The same entry for `eth_blockNumber` gives the canned
        body too."""
        canned = {"jsonrpc": "2.0", "id": 1, "result": "0xcanned"}
        _set_scenario(
            sim,
            {
                "mode": "success",
                "transports": ["ws"],
                "responses": {"eth_subscribe": {"body": canned}, "eth_blockNumber": {"body": canned}},
            },
        )

        with _websocket() as sock:
            _send_frame(sock, _SUBSCRIBE)
            subscribed = _reply(sock)
            _send_frame(sock, _BLOCK_NUMBER)
            other = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert subscribed == canned
        assert other == canned
        assert _facts(rows[0]) == ("eth_subscribe", "success", 0, 7)
        assert subscriptions == []

    @pytest.mark.parametrize(
        "responses, other_reply",
        [
            pytest.param(
                {"eth_subscribe": {"result": "0xcanned"}, "eth_blockNumber": {"result": "0xcanned"}},
                ("result", "0xcanned"),
                id="result",
            ),
            pytest.param(
                {"eth_subscribe": {"error_stub": "revert"}, "eth_blockNumber": {"error_stub": "revert"}},
                ("error", 3, "execution reverted"),
                id="error-stub",
            ),
            pytest.param(
                {
                    "eth_subscribe": {"error": {"code": -32099, "message": "canned"}},
                    "eth_blockNumber": {"error": {"code": -32099, "message": "canned"}},
                },
                ("error", -32099, "canned"),
                id="error",
            ),
            pytest.param({"default": {"result": "0xcanned"}}, ("result", "0xcanned"), id="the-entry-default"),
        ],
    )
    def test_a_content_key_of_responses_is_not_read_for_a_subscribe_frame(self, sim, responses, other_reply):
        """A chain reads the content keys `result`, `error_stub` and `error` of
        an entry of `responses`, and the entry `default`. The listener asks no
        chain for a subscribe frame: the subscribe registers a subscription and
        answers its id. The same key reaches `eth_blockNumber` on the same
        connection."""
        _set_scenario(sim, {"mode": "success", "transports": ["ws"], "responses": responses})

        with _websocket() as sock:
            _send_frame(sock, _SUBSCRIBE)
            subscribed = _reply(sock)
            _send_frame(sock, _BLOCK_NUMBER)
            other = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        if "result" in other:
            other_facts = ("result", other["result"])
        else:
            other_facts = ("error", other["error"]["code"], other["error"]["message"])
        assert set(subscribed) == {"jsonrpc", "id", "result"}
        assert re.fullmatch(r"0x[0-9a-f]{32}", subscribed["result"]), subscribed
        assert other_facts == other_reply
        assert _facts(rows[0]) == ("eth_subscribe", "success", 0, 7)
        assert [entry["subscription_id"] for entry in subscriptions] == [subscribed["result"]]

    def test_a_subscribe_frame_performs_no_pause(self, sim):
        """`pause_at` holds a reply of the http endpoint of the provider. The
        reply of a subscribe frame is not held."""
        _set_scenario(sim, {"mode": "success", "pause_at": "mid_body", "pause_ms": 1500})

        with _websocket() as sock:
            started = time.monotonic()
            _send_frame(sock, _SUBSCRIBE)
            subscribed = _reply(sock)
            waited = time.monotonic() - started

        assert re.fullmatch(r"0x[0-9a-f]{32}", subscribed["result"]), subscribed
        assert waited < 1.0, f"the reply came after {waited:.2f} s"

    def test_a_provider_wide_down_comes_before_a_per_method_success_for_a_subscribe_frame(self, sim):
        """The request flow checks a provider-wide `down` before it reads an
        entry of `responses`. So an entry with `mode: success` for
        `eth_subscribe` does not make the subscribe succeed. The frame gets no
        reply, the connection closes, and the row has the method `*`."""
        with _websocket() as sock:
            _set_scenario(
                sim, {"mode": "down", "transports": ["ws"], "responses": {"eth_subscribe": {"mode": "success"}}}
            )
            _send_frame(sock, _SUBSCRIBE)
            received = _read_until_the_close(sock)

        assert received == b""
        assert [_facts(row) for row in _rows(sim)] == [("*", "down", 0, None)]
        assert _subscriptions(sim) == []

    @pytest.mark.parametrize(
        "scope",
        [
            pytest.param({"transports": ["http"]}, id="a-transports-filter"),
            pytest.param({"ports": [port_of("eth-sim", "1")]}, id="a-ports-filter"),
        ],
    )
    def test_a_filter_that_does_not_name_the_ws_endpoint_holds_everything_back(self, sim, scope):
        """A filter that names the http endpoint only: the mode, the latency
        and the entry of `responses` do not reach a subscribe frame."""
        _set_scenario(
            sim,
            {"mode": "rate_limit", "latency_ms": 1500, "responses": {"eth_subscribe": {"mode": "down"}}, **scope},
        )

        with _websocket() as sock:
            started = time.monotonic()
            _send_frame(sock, _SUBSCRIBE)
            subscribed = _reply(sock)
            waited = time.monotonic() - started
            rows = _rows(sim)

        assert re.fullmatch(r"0x[0-9a-f]{32}", subscribed["result"]), subscribed
        assert waited < 1.0, f"the reply came after {waited:.2f} s"
        assert [_facts(row) for row in rows] == [("eth_subscribe", "success", 0, 7)]

    def test_each_subscribe_frame_uses_one_count_of_the_fail_first_n_window(self, sim):
        """With `fail_first_n` 2 on the ws endpoint, the first two subscribe
        frames get the fault and the third one succeeds."""
        with _websocket() as sock:
            _set_scenario(sim, {"mode": "rate_limit", "fail_first_n": 2, "then_mode": "success", "transports": ["ws"]})
            answers = []
            for frame_id in (1, 2, 3):
                _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"], "id": frame_id})
                answers.append(_payload(sock))
            rows = _rows(sim)

        assert answers[:2] == [_RATE_LIMIT_TEXT, _RATE_LIMIT_TEXT]
        assert re.fullmatch(r"0x[0-9a-f]{32}", json.loads(answers[2])["result"]), answers[2]
        assert [_facts(row) for row in rows] == [
            ("eth_subscribe", "rate_limit", 0, 1),
            ("eth_subscribe", "rate_limit", 0, 2),
            ("eth_subscribe", "success", 0, 3),
        ]


class TestAPushedEvent:
    """An event that the control API pushes with POST /ws/emit.

    `WsSubscriptions.emit` puts the frame on the queue of the connection and
    writes the row. It asks no fault policy.
    """

    @pytest.mark.parametrize(
        "subscribe, row_method",
        [
            pytest.param("eth_subscribe", "eth_subscription push", id="eth"),
            pytest.param("subscribe", "tendermint_event push", id="tendermint"),
            pytest.param("accountSubscribe", "solana_account push", id="solana-account"),
            pytest.param("logsSubscribe", "solana_logs push", id="solana-logs"),
        ],
    )
    def test_a_pushed_event_writes_one_row_with_the_subscription_id_as_its_request_id(self, sim, subscribe, row_method):
        """One pushed event writes one `success` row. Its method is the
        envelope of the subscribe method and the word `push`. Its request id is
        the subscription id. It names the ws endpoint of the provider, and it
        carries no lava header, also when the upgrade request had some."""
        with _websocket(lava_headers=_LAVA_HEADERS) as sock:
            subscription_id = _subscribe(sock, subscribe)
            answer = _emit(sim, {"subscription_id": subscription_id, "event": {"tag": "A"}})
            _reply(sock)
            rows = _rows(sim)

        assert answer == (200, {"status": "emitted", "subscription_id": subscription_id})
        assert [_facts(row) for row in rows] == [
            (subscribe, "success", 0, 1),
            (row_method, "success", 0, subscription_id),
        ]
        assert _endpoint(rows[1]) == ("jsonrpc", "ws", _PORT)
        assert rows[1]["lava_headers"] == {}

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"event": "text"}, id="a-text"),
            pytest.param({"event": 5}, id="a-number"),
            pytest.param({"event": ["a"]}, id="a-list"),
            pytest.param({"event": None}, id="null"),
            pytest.param({}, id="no-event-key"),
        ],
    )
    def test_an_event_that_is_no_object_is_pushed_as_an_empty_object(self, sim, body):
        """The control API accepts an event that is a text, a number, a list or
        `null`, and a body with no event. Each one reaches the caller as an
        empty object, and it writes its row."""
        with _websocket() as sock:
            subscription_id = _subscribe(sock)
            answer = _emit(sim, {"subscription_id": subscription_id, **body})
            frame = _reply(sock)
            rows = _rows(sim)

        assert answer == (200, {"status": "emitted", "subscription_id": subscription_id})
        assert frame == {
            "jsonrpc": "2.0",
            "method": "eth_subscription",
            "params": {"subscription": subscription_id, "result": {}},
        }
        assert _facts(rows[1]) == ("eth_subscription push", "success", 0, subscription_id)

    def test_an_event_for_an_unknown_subscription_gets_404_and_writes_no_row(self, sim):
        """An event for a subscription id that does not exist: the control API
        answers 404, and no row is written."""
        answer = _emit(sim, {"subscription_id": "0x" + "ab" * 16, "event": {"tag": "A"}})

        assert answer == (404, {"error": "no active subscription '0xabababababababababababababababab'"})
        assert _every_row(sim) == []

    def test_an_event_for_a_subscription_that_was_removed_gets_404_and_writes_no_row(self, sim):
        """After an unsubscribe frame removed the subscription, an event for
        its id gets 404 and writes no row."""
        with _websocket() as sock:
            subscription_id = _subscribe(sock)
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_unsubscribe", "params": [subscription_id], "id": 2})
            _reply(sock)
            status, _ = _emit(sim, {"subscription_id": subscription_id, "event": {"tag": "A"}})
            rows = _rows(sim)

        assert status == 404
        assert [_facts(row)[0] for row in rows] == ["eth_subscribe", "eth_unsubscribe"]

    @pytest.mark.parametrize("mode", ["down", "hang", "rate_limit", "error", "drop_connection"])
    def test_an_event_reaches_its_subscriber_under_each_mode_of_the_provider(self, sim, mode):
        """A pushed event asks no fault policy. A fault that is set after the
        subscribe does not stop the event, and the row is a `success` row."""
        with _websocket() as sock:
            subscription_id = _subscribe(sock)
            _set_scenario(sim, {"mode": mode, "latency_ms": 1500, "transports": ["ws"]})
            started = time.monotonic()
            answer = _emit(sim, {"subscription_id": subscription_id, "event": {"tag": "A"}})
            frame = _reply(sock)
            waited = time.monotonic() - started
            rows = _rows(sim)

        assert answer[0] == 200
        assert frame["params"] == {"subscription": subscription_id, "result": {"tag": "A"}}
        assert waited < 1.0, f"the event came after {waited:.2f} s"
        assert _facts(rows[1]) == ("eth_subscription push", "success", 0, subscription_id)


class TestASubscriptionBelongsToOneConnection:
    """The registry that GET /ws/subscriptions shows, and the connection that
    owns each subscription."""

    def test_ws_subscriptions_shows_one_entry_for_each_subscribe_with_its_pool_and_its_provider(self, sim):
        """Each subscribe adds one entry. The entry names the subscription id,
        the pool, the provider id and the subscribe method."""
        with _websocket(_WS_PORTS["2"]) as sock:
            first = _subscribe(sock, "accountSubscribe", frame_id=1)
            second = _subscribe(sock, "eth_subscribe", frame_id=2)
            entries = _subscriptions(sim)

        assert sorted(entries, key=lambda entry: entry["method"]) == [
            {"subscription_id": first, "pool": "eth-sim", "pid": "2", "method": "accountSubscribe", "queue_depth": 0},
            {"subscription_id": second, "pool": "eth-sim", "pid": "2", "method": "eth_subscribe", "queue_depth": 0},
        ]

    def test_an_unsubscribe_from_another_connection_answers_false_and_removes_nothing(self, sim):
        """A connection cannot remove the subscription of another connection of
        the same endpoint. The owner still gets a pushed event, and the owner
        can remove the subscription."""
        with _websocket() as owner, _websocket() as other:
            subscription_id = _subscribe(owner)
            unsubscribe = {"jsonrpc": "2.0", "method": "eth_unsubscribe", "params": [subscription_id], "id": 9}

            _send_frame(other, unsubscribe)
            from_the_other = _reply(other)
            after_the_other = [entry["subscription_id"] for entry in _subscriptions(sim)]
            emit_status, _ = _emit(sim, {"subscription_id": subscription_id, "event": {"tag": "A"}})
            event = _reply(owner)

            _send_frame(owner, unsubscribe)
            from_the_owner = _reply(owner)
            after_the_owner = _subscriptions(sim)

        assert from_the_other == {"jsonrpc": "2.0", "id": 9, "result": False}
        assert after_the_other == [subscription_id]
        assert emit_status == 200
        assert event["params"] == {"subscription": subscription_id, "result": {"tag": "A"}}
        assert from_the_owner == {"jsonrpc": "2.0", "id": 9, "result": True}
        assert after_the_owner == []

    def test_a_close_removes_the_subscriptions_of_its_own_connection_only(self, sim):
        """When a connection closes, the simulator removes each subscription of
        that connection. A subscription of another connection stays."""
        with _websocket() as other:
            kept = _subscribe(other)
            with _websocket() as owner:
                _subscribe(owner, frame_id=1)
                _subscribe(owner, frame_id=2)
                assert len(_subscriptions(sim)) == 3

            deadline = time.monotonic() + 2.0
            while len(_subscriptions(sim)) != 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            left = [entry["subscription_id"] for entry in _subscriptions(sim)]

        assert left == [kept]

    @pytest.mark.parametrize(
        "down_closes_it",
        [
            pytest.param(False, id="the-client-closes-the-socket"),
            pytest.param(True, id="a-down-provider-closes-the-connection"),
        ],
    )
    def test_a_connection_that_ends_with_no_close_frame_loses_its_subscriptions(self, sim, down_closes_it):
        """A connection can end with no close frame: the client closes the
        socket, or a `down` provider closes the connection. The simulator then
        removes each subscription of that connection."""
        sock = _request_upgrade()
        try:
            head = _read_the_handshake(sock)
            assert head.startswith(b"HTTP/1.1 101 "), f"the upgrade did not succeed: {head!r}"
            _subscribe(sock, frame_id=1)
            _subscribe(sock, frame_id=2)
            before = len(_subscriptions(sim))
            received = b""
            if down_closes_it:
                _set_scenario(sim, {"mode": "down", "transports": ["ws"]})
                _send_frame(sock, _BLOCK_NUMBER)
                received = _read_until_the_close(sock)
        finally:
            sock.close()

        deadline = time.monotonic() + 2.0
        while _subscriptions(sim) and time.monotonic() < deadline:
            time.sleep(0.02)

        assert before == 2
        assert received == b""
        assert _subscriptions(sim) == []


class TestFramesOutsideTheSubscribeCode:
    """Frames that are not a subscribe frame or an unsubscribe frame: a frame
    that the adapter does not give to `Listener.serve`, and a frame of another
    method."""

    @pytest.mark.parametrize(
        "opcode, payload",
        [
            pytest.param(ws_protocol.OPCODE_TEXT, b"this is not JSON", id="a-text-frame-that-is-not-json"),
            pytest.param(ws_protocol.OPCODE_TEXT, b"\xff\xfe\xfd", id="a-text-frame-that-is-not-utf-8"),
            pytest.param(
                ws_protocol.OPCODE_BINARY,
                b'{"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"], "id": 1}',
                id="a-binary-frame-with-a-subscribe-request",
            ),
        ],
    )
    def test_a_frame_that_the_adapter_cannot_read_gets_no_reply_and_no_row(self, sim, opcode, payload):
        """The adapter drops a text frame that is not JSON and a frame that is
        not text. It sends no reply and writes no row, and it registers no
        subscription. The connection stays open and answers the next frame."""
        with _websocket() as sock:
            sock.sendall(ws_protocol.encode_frame(opcode, payload, mask=True))
            _assert_no_frame(sock)
            rows_after_the_frame = _every_row(sim)
            subscriptions = _subscriptions(sim)
            _send_frame(sock, _BLOCK_NUMBER)
            next_reply = _reply(sock)
            rows = _rows(sim)

        assert rows_after_the_frame == []
        assert subscriptions == []
        assert next_reply["id"] == 8
        assert [_facts(row) for row in rows] == [("eth_blockNumber", "success", 0, 8)]

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b"5", id="a-number"),
            pytest.param(b"null", id="null"),
            pytest.param(b'"eth_subscribe"', id="a-text"),
            pytest.param(b"{}", id="an-object-with-no-method"),
        ],
    )
    def test_a_json_frame_with_no_method_goes_to_the_request_flow(self, sim, payload):
        """A text frame that is JSON and names no method goes to the request
        flow. The eth chain answers it as the method `unknown`, and the row has
        that method."""
        with _websocket() as sock:
            sock.sendall(ws_protocol.encode_frame(ws_protocol.OPCODE_TEXT, payload, mask=True))
            reply = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert reply == {"jsonrpc": "2.0", "id": 1, "result": "0x1"}
        assert [_facts(row) for row in rows] == [("unknown", "success", 0, 1)]
        assert subscriptions == []

    def test_a_list_of_requests_gets_the_batch_error_and_registers_no_subscription(self, sim):
        """A text frame that is a JSON list goes to the request flow, also when
        the list holds a subscribe request. The reply is the batch error, the
        row has the method `batch`, and no subscription is registered."""
        with _websocket() as sock:
            _send_frame(sock, [_SUBSCRIBE])
            reply = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert reply == {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "batch requests are not supported"},
        }
        assert [_facts(row) for row in rows] == [("batch", "error", 0, None)]
        assert subscriptions == []

    @pytest.mark.parametrize(
        "block, expected_row",
        [
            pytest.param({"mode": "down", "latency_ms": 250}, ("*", "down", 0, None), id="down"),
            pytest.param({"mode": "hang", "latency_ms": 250}, ("eth_blockNumber", "hang", 0, 8), id="hang"),
        ],
    )
    def test_the_down_row_and_the_hang_row_of_another_frame_record_no_latency(self, sim, block, expected_row):
        """A frame such as `eth_blockNumber` goes to `Listener.serve`. Its `down`
        row has the method `*`, no request id and `latency_ms` 0, and its `hang`
        row records 0. The rows of a subscribe frame follow the same rule: the
        tests of `TestSubscribeAndUnsubscribeFrames` hold them."""
        with _websocket() as sock:
            _set_scenario(sim, {**block, "transports": ["ws"]})
            _send_frame(sock, _BLOCK_NUMBER)
            rows = _rows_when_complete(sim, 1)

        assert [_facts(row) for row in rows] == [expected_row]

    def test_a_per_method_down_with_a_latency_closes_another_frame_at_once(self, sim):
        """An entry of `responses` with `mode: down` and a latency, for a frame
        that goes to `Listener.serve`. The row records the method, the id and
        the latency. The WebSocket adapter closes the connection with no wait."""
        _set_scenario(
            sim,
            {
                "mode": "success",
                "transports": ["ws"],
                "responses": {"eth_blockNumber": {"mode": "down", "latency_ms": 1500}},
            },
        )

        with _websocket() as sock:
            started = time.monotonic()
            _send_frame(sock, _BLOCK_NUMBER)
            received = _read_until_the_close(sock)
            waited = time.monotonic() - started

        assert received == b""
        assert waited < 1.0, f"the connection closed after {waited:.2f} s"
        assert [_facts(row) for row in _rows(sim)] == [("eth_blockNumber", "down", 1500, 8)]

    @pytest.mark.parametrize(
        "method",
        [
            pytest.param(["eth_subscribe"], id="a-list"),
            pytest.param({"name": "eth_subscribe"}, id="an-object"),
        ],
    )
    def test_a_frame_whose_method_is_a_list_or_an_object_closes_the_connection(self, sim, method):
        """This test records a defect and does not judge it. A JSON frame whose
        `method` is a list or an object stops the reader of the connection. The
        caller gets no byte, and the connection closes. No row is written, and
        no subscription is registered."""
        with _websocket() as sock:
            _send_frame(sock, {"jsonrpc": "2.0", "method": method, "params": [], "id": 1})
            received = _read_until_the_close(sock)

        assert received == b""
        assert _every_row(sim) == []
        assert _subscriptions(sim) == []


class TestMoreOfWhatWebSocketDoesToday:
    """Four behaviours that no test above holds: the `down` row and the `hang`
    row of an unsubscribe frame, a canned `body` for a subscribe method under
    a provider-wide `rate_limit`, an event for a full queue, and an
    unsubscribe frame whose `params` has a wrong shape."""

    @pytest.mark.parametrize(
        "mode, expected_row",
        [
            pytest.param("down", ("*", "down", 0, None), id="down"),
            pytest.param("hang", ("eth_unsubscribe", "hang", 0, 9), id="hang"),
        ],
    )
    def test_the_row_of_an_unsubscribe_frame_of_a_down_or_a_hung_provider(self, sim, mode, expected_row):
        """A provider-wide `down` or `hang` on an unsubscribe frame. The `down`
        row has the method `*` and no request id. The `hang` row has the
        method and the id of the frame. The provider does not wait for the
        configured latency, so each row records 0."""
        with _websocket() as sock:
            _set_scenario(sim, {"mode": mode, "latency_ms": 250, "transports": ["ws"]})
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_unsubscribe", "params": ["0x00"], "id": 9})
            rows = _rows_when_complete(sim, 1)

        assert [_facts(row) for row in rows] == [expected_row]

    def test_a_canned_body_for_a_subscribe_method_under_a_provider_wide_rate_limit(self, sim):
        """An entry of `responses` for `eth_subscribe` with a canned `body`,
        under a provider-wide `rate_limit`. The request flow reads the canned
        body before the fault. So the subscribe frame gets the canned body,
        and its row has the status `success`. No subscription is registered."""
        canned = {"jsonrpc": "2.0", "id": 1, "result": "0xcanned"}
        with _websocket() as sock:
            _set_scenario(
                sim, {"mode": "rate_limit", "transports": ["ws"], "responses": {"eth_subscribe": {"body": canned}}}
            )
            _send_frame(sock, _SUBSCRIBE)
            payload = _payload(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert payload == b'{"jsonrpc": "2.0", "id": 1, "result": "0xcanned"}'
        assert [_facts(row) for row in rows] == [("eth_subscribe", "success", 0, 7)]
        assert subscriptions == []

    def test_an_event_for_a_full_queue_gets_503_and_writes_no_row(self, sim):
        """An event for a subscription whose queue is full: the control API
        answers 503, and no row is written. The event before it gets 200 and
        writes its row."""
        subscription_id = "0xcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"
        sim["server"].subscriptions.register(
            subscription_id, "eth-sim", "1", "eth_subscribe", out_queue=queue.Queue(maxsize=1)
        )

        first = _emit(sim, {"subscription_id": subscription_id, "event": {"tag": "A"}})
        rows_after_the_first = _rows(sim)
        second = _emit(sim, {"subscription_id": subscription_id, "event": {"tag": "B"}})
        rows = _rows(sim)

        the_push_row = ("eth_subscription push", "success", 0, subscription_id)
        assert first == (200, {"status": "emitted", "subscription_id": subscription_id})
        assert [_facts(row) for row in rows_after_the_first] == [the_push_row]
        assert second == (503, {"error": "subscription '0xcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd' queue full"})
        assert [_facts(row) for row in rows] == [the_push_row]

    @pytest.mark.parametrize(
        "params",
        [
            pytest.param({"id": "0x00"}, id="an-object"),
            pytest.param(5, id="a-number"),
            pytest.param([["0x00"]], id="a-list-whose-first-item-is-a-list"),
            pytest.param([{"id": "0x00"}], id="a-list-whose-first-item-is-an-object"),
        ],
    )
    def test_an_unsubscribe_frame_with_params_of_a_wrong_shape_closes_the_connection_and_leaves_its_row_in_flight(
        self, sim, params
    ):
        """This test records a defect and does not judge it. An unsubscribe
        frame whose `params` has a wrong shape stops the reader of the
        connection. The caller gets no byte, and the connection closes. The
        row of the frame stays `in_flight`."""
        with _websocket() as sock:
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_unsubscribe", "params": params, "id": 9})
            received = _read_until_the_close(sock)

        assert received == b""
        assert [_facts(row) for row in _rows(sim)] == [("*", "in_flight", 0, None)]
