"""
handlers_ws.py — WebSocket transport for the provider simulator (MAG-1801).

Peer to JSONRPCHandler / RestHandler / handlers_grpc. Subclasses
BaseHTTPRequestHandler to do the HTTP Upgrade handshake, then hijacks the raw
socket (self.connection) and runs the WS frame loop for the lifetime of the
connection.

Each connection uses TWO threads:
  - Reader (the BaseHTTPRequestHandler thread itself): blocks on
    self.connection.recv → parses frames → handles incoming requests.
  - Writer (spawned on successful handshake): blocks on out_queue.get() →
    self.connection.sendall(bytes).

Communication between the threads is one queue.Queue + a sentinel object that
tells the writer to exit. External threads (POST /ws/emit on the control
server) push event frames onto the same queue.

Fault primitives are reused from server._apply_fault — the helper returns
a wire-action dict that this handler interprets into WS-shaped responses
(error frame, close, no-reply, etc.) via _emit_ws_fault.
"""

from __future__ import annotations

import json
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, Optional, Set
from urllib.parse import urlparse

import handlers_btc
import handlers_eth
import stubs_ws
import ws_protocol


# Sentinel placed on out_queue to tell the writer thread to exit cleanly.
_SENTINEL_CLOSE = object()


# Module-level imports of server-side helpers. Lazy-imported inside functions
# to avoid a circular import at module load time (server imports handlers_ws,
# handlers_ws needs server.* symbols).


def _server_helpers():
    """Return the (`_apply_fault`, `_elapsed_ms`, `_resolve_method_config`,
    `_corruption_for`, `_missing_field_for`) helpers from server.py.

    server.py imports handlers_ws at top level for main(), so importing it
    back at module load would be a cycle. We resolve on first use, which is
    after both modules have finished loading.

    ``_resolve_method_config`` was added in the MAG-1821 follow-up so the
    WS reader loop can pre-merge per-method overrides into the snap before
    the latency / fault evaluation runs, matching the JSON-RPC dispatcher.

    ``_corruption_for`` / ``_missing_field_for`` were added in MAG-1837 so
    the WS reader loop can gate corruption_mode on chain_family="ws" the
    same way the JSON-RPC / REST / Tendermint handlers gate their own
    transports.
    """
    import server
    return (
        server._apply_fault,
        server._elapsed_ms,
        server._resolve_method_config,
        server._corruption_for,
        server._missing_field_for,
    )


def _writer_loop(connection, out_queue: "queue.Queue") -> None:
    """Drain out_queue and sendall each item to the socket.

    Exits cleanly when:
      - _SENTINEL_CLOSE is popped (reader signalled close).
      - sendall raises OSError (peer disconnected).
    """
    while True:
        item = out_queue.get()
        if item is _SENTINEL_CLOSE:
            return
        try:
            connection.sendall(item)
        except OSError:
            return


def _text_frame(payload_obj: Dict[str, Any],
                corruption_mode: Optional[str] = None,
                missing_field: Optional[str] = None) -> bytes:
    """Encode a Python dict as a WS TEXT frame, applying corruption per snap.

    Corruption modes mirror JSONRPCHandler._reply (server.py):
      - truncated     : chop last 10 bytes from the serialised JSON
      - missing_field : drop top-level key named in missing_field
      - invalid_json  : replace body with non-JSON bytes
      - empty_response: emit zero-payload TEXT frame
      - wrong_type    : swap target field's type (str->int, bool->int)
    """
    if corruption_mode == "missing_field" and missing_field:
        payload_obj = {k: v for k, v in payload_obj.items() if k != missing_field}
    elif corruption_mode == "empty_response":
        return ws_protocol.encode_frame(ws_protocol.OPCODE_TEXT, b"", mask=False)
    elif corruption_mode == "wrong_type":
        target = missing_field or "result"
        if target in payload_obj:
            cur = payload_obj[target]
            if isinstance(cur, bool):
                payload_obj[target] = 1 if cur else 0
            elif isinstance(cur, str):
                payload_obj[target] = 12345
            elif isinstance(cur, (int, float)):
                payload_obj[target] = "wrong_type_value"
            else:
                payload_obj[target] = "wrong_type_value"

    raw = json.dumps(payload_obj).encode("utf-8")
    if corruption_mode == "truncated" and len(raw) > 10:
        raw = raw[:-10]
    elif corruption_mode == "invalid_json":
        raw = b"}{ {{ not valid json"
    return ws_protocol.encode_frame(ws_protocol.OPCODE_TEXT, raw, mask=False)


def _emit_ws_fault(fault: Dict[str, Any], req_id: Any,
                   out_queue: "queue.Queue",
                   connection,
                   corruption_mode: Optional[str] = None,
                   missing_field: Optional[str] = None) -> str:
    """Translate a fault dict (from server._apply_fault) into a WS wire action.

    Returns one of:
      "continue"   -- fault produced a reply frame; reader loop continues with next frame.
      "close"      -- caller MUST exit the reader loop (connection should close).

    corruption_mode / missing_field are threaded through to the error-frame
    encoder so a /scenario combining mode=error with corruption_mode=truncated
    behaves identically over WS as it does over HTTP JSON-RPC (the JSONRPCHandler
    applies corruption to fault-path replies via _emit_jsonrpc_fault → _reply).
    """
    kind = fault["kind"]

    if kind == "down":
        return "close"

    if kind == "hang":
        # Don't enqueue a reply; other subscriptions on this connection
        # still receive pushed events.
        return "continue"

    if kind == "drop":
        drop_at = fault.get("drop_at", "before_headers")
        try:
            if drop_at == "before_headers":
                pass  # nothing — fall through to close
            elif drop_at == "after_headers":
                # Frame header declaring 100 bytes of payload, zero payload sent.
                connection.sendall(bytes([0x81, 100]))
            elif drop_at == "mid_body":
                # Frame header declaring 100 bytes, send 50, close.
                connection.sendall(bytes([0x81, 100]) + (b"X" * 50))
        except OSError:
            pass
        return "close"

    # rate_limit / error: encode JSON-RPC error frame, stay open. Corruption
    # hooks apply to the error envelope just like they do to success replies.
    try:
        out_queue.put_nowait(_text_frame(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": fault["error_code"],
                    "message": fault["error_message"],
                },
            },
            corruption_mode=corruption_mode,
            missing_field=missing_field,
        ))
    except queue.Full:
        return "close"
    return "continue"


class WsHandler(BaseHTTPRequestHandler):
    """WebSocket transport handler. One instance per connection.

    self.server.state is the ProviderState for this listener port (set in
    server.py:main when the ThreadingHTTPServer is created).
    self.server.provider_id is the string "1" / "2" / "3".
    """

    def do_GET(self):
        # Path enforcement: only /ws accepts the upgrade. Everything else
        # (root, /metrics, /health, etc.) returns 404 to make wrong-path
        # mistakes loud instead of confusing handshake errors.
        parsed_path = urlparse(self.path).path
        if parsed_path != "/ws":
            self._send_simple_error(404, "not found")
            return

        # Validate the canonical WS upgrade headers.
        if (self.headers.get("Upgrade", "").lower() != "websocket"
            or "upgrade" not in self.headers.get("Connection", "").lower()
            or self.headers.get("Sec-WebSocket-Version") != "13"
            or not self.headers.get("Sec-WebSocket-Key")):
            self._send_simple_error(400, "bad WS upgrade request")
            return

        client_key = self.headers["Sec-WebSocket-Key"]

        # Capture lava-* request headers from the upgrade request — recorded
        # in /history for every frame that arrives on this connection.
        lava_headers = {
            k: v for k, v in self.headers.items()
            if k.lower().startswith("lava-")
        }

        # Pre-handshake fault evaluation. We do this before completing the
        # handshake so down / rate_limit / error / hang prevent the upgrade
        # entirely — matching how the JSON-RPC handler refuses HTTP requests
        # in the same states. drop_connection variants are handled identically
        # to the JSON-RPC handler's drop_at semantics adapted to HTTP/1.1.
        state = self.server.state
        snap = state.snapshot()

        # Cross-transport isolation — mirrors MAG-1838's jsonrpc_owns_snap
        # gate. ``ProviderState`` is shared across all transports for the
        # same provider id, so a fault authored for one transport leaks
        # onto every other transport that reads ``snap["mode"]`` without
        # a chain_family check. The WS handler owns chain_family="ws";
        # for any other value the pre-handshake fault ladder is skipped
        # and the handshake completes normally. Surfaced in the 2026-05-18
        # suite triage as one of the leak paths feeding the ~37 spurious
        # failures.
        #
        # Exception (MAG-2092): mode="down" is honored on every transport
        # because reachability is provider-wide; per-transport isolation
        # only applies to content modes (error / corrupt / hang /
        # rate_limit / latency / drop_connection). Without this exemption
        # an ETH provider in mode=down would still complete the WS upgrade
        # at port 18557-59, hiding router-side bugs that depend on the
        # provider being unreachable across every node-url (e.g.
        # MAG-2061).
        ws_owns_snap = snap.get("chain_family") == "ws"
        raw_mode = snap["mode"]
        ws_mode = raw_mode if (ws_owns_snap or raw_mode == "down") else "success"

        if ws_mode == "down":
            state.push_call_to_buffer("*", "down", 0,
                                      request_id=None,
                                      lava_headers=lava_headers)
            self._send_simple_error(503, "provider down")
            return

        if ws_mode == "rate_limit":
            state.push_call_to_buffer("ws_upgrade", "rate_limit", 0,
                                      request_id=None,
                                      lava_headers=lava_headers)
            self._send_simple_error(429, "rate limited")
            return

        if ws_mode == "error":
            # Override http_status 200 -> 400 here because 200-without-101 is
            # non-spec for WS upgrades. 4xx is the cleanest "upgrade refused"
            # signal a client can read.
            state.push_call_to_buffer("ws_upgrade", "error", 0,
                                      request_id=None,
                                      lava_headers=lava_headers)
            self._send_simple_error(400, snap["error_message"])
            return

        if ws_mode == "hang":
            state.push_call_to_buffer("ws_upgrade", "hang", 0,
                                      request_id=None,
                                      lava_headers=lava_headers)
            time.sleep(30)
            try:
                self.connection.close()
            except OSError:
                pass
            return

        if ws_mode == "drop_connection":
            drop_at = snap.get("drop_at", "before_headers")
            state.push_call_to_buffer("ws_upgrade", "drop_connection", 0,
                                      request_id=None,
                                      lava_headers=lava_headers)
            try:
                if drop_at == "after_headers":
                    # Complete the 101 (with Lava-Provider-Address) then close.
                    self.connection.sendall(
                        ws_protocol.build_handshake_response(
                            client_key, extra_headers=self._lava_extra_headers()))
                elif drop_at == "mid_body":
                    # Send just the status line + truncate before the headers
                    # finish. The 101 reply has no body — "mid_body" maps to
                    # mid-header here.
                    self.connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                                            b"Upgrade: webso")  # truncated
                # before_headers (default): silent close, no bytes.
            except OSError:
                pass
            try:
                self.connection.close()
            except OSError:
                pass
            return

        # Complete the handshake.
        try:
            self.connection.sendall(
                ws_protocol.build_handshake_response(
                    client_key, extra_headers=self._lava_extra_headers()))
        except OSError:
            return

        # Spawn writer.
        out_queue: "queue.Queue[Any]" = queue.Queue(maxsize=1000)
        writer = threading.Thread(
            target=_writer_loop,
            args=(self.connection, out_queue),
            daemon=True,
            name=f"ws-writer-{self.server.provider_id}",
        )
        writer.start()

        # Run reader in this thread.
        try:
            self._reader_loop(out_queue, lava_headers)
        finally:
            try:
                out_queue.put_nowait(_SENTINEL_CLOSE)
            except queue.Full:
                pass
            try:
                self.connection.close()
            except OSError:
                pass
            writer.join(timeout=1.0)

    def _reader_loop(self, out_queue: "queue.Queue", lava_headers: Dict[str, str]) -> None:
        state = self.server.state
        provider_id = self.server.provider_id
        apply_fault, elapsed_ms, resolve_method_config, corruption_for, missing_field_for = _server_helpers()

        connection_subs: Set[str] = set()

        try:
            while True:
                try:
                    frame = ws_protocol.parse_frame(self.connection.recv)
                except (ws_protocol.FrameParseError, OSError):
                    return

                if frame.opcode == ws_protocol.OPCODE_CLOSE:
                    return
                if frame.opcode == ws_protocol.OPCODE_PING:
                    try:
                        out_queue.put_nowait(ws_protocol.encode_frame(
                            ws_protocol.OPCODE_PONG, frame.payload, mask=False))
                    except queue.Full:
                        return
                    continue
                if frame.opcode != ws_protocol.OPCODE_TEXT:
                    continue  # ignore binary etc.

                try:
                    body = json.loads(frame.payload.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                method = body.get("method", "unknown")
                req_id = body.get("id")
                snap = state.snapshot()
                t_start = time.monotonic()

                # Merge per-method overrides into the snap (MAG-1821
                # follow-up). When no override applies for this method,
                # ``method_snap is snap`` and behaviour matches pre-follow-up
                # exactly. Per-key fallback (provider-wide fault keys
                # inherited by partial per-method entries) is handled inside
                # _resolve_method_config.
                method_snap = resolve_method_config(method, snap, state.responses)

                # Cross-transport isolation — mirrors MAG-1838's
                # jsonrpc_owns_snap gate. The WS reader-loop fault path
                # was missing a chain_family check, so a fault authored
                # for chain_family="eth"/"btc"/"rest"/etc. fired on WS
                # requests. Gate on the WS snap so the fault ladder only
                # runs when the snap is WS-authored; otherwise fall through
                # to the success-path response below.
                #
                # Exception (MAG-2092): mode="down" is honored on every
                # transport because reachability is provider-wide;
                # per-transport isolation only applies to content modes
                # (error / corrupt / hang / rate_limit / latency /
                # drop_connection). A live WS connection whose provider
                # is set to mode=down mid-stream still closes via the
                # down branch in _emit_ws_fault.
                ws_owns_snap = snap.get("chain_family") == "ws"
                run_fault_ladder = ws_owns_snap or method_snap["mode"] == "down"

                # MAG-1832 — fault evaluation BEFORE the latency sleep so
                # apply_fault's internal push_call_to_buffer fires before
                # any cancel window opens. Fault path sleeps AFTER the
                # record but before the wire emit so timing is unchanged.
                fault = apply_fault(state, method_snap, method, req_id,
                                    lava_headers, t_start) if run_fault_ladder else None
                if fault is not None:
                    if method_snap["latency_ms"] > 0:
                        time.sleep(method_snap["latency_ms"] / 1000.0)
                    # MAG-1837 — gate corruption_mode on chain_family="ws"
                    # so a corruption authored for JSON-RPC / REST / etc.
                    # doesn't reach the WS frame encoder.
                    action = _emit_ws_fault(
                        fault, req_id, out_queue, self.connection,
                        corruption_mode=corruption_for(snap, "ws"),
                        missing_field=missing_field_for(snap, "ws"),
                    )
                    if action == "close":
                        return
                    continue

                # Success-path branches below all follow the JSON-RPC pattern:
                # record history with the configured latency BEFORE the sleep
                # so a client disconnect mid-sleep still leaves a trace
                # (MAG-1832), then sleep, then enqueue the response frame.

                # Subscribe request — register and return sub_id.
                if method in stubs_ws.SUBSCRIBE_METHODS:
                    sub_id = self._register_subscription(provider_id, method, out_queue)
                    connection_subs.add(sub_id)
                    response = {"jsonrpc": "2.0", "id": req_id, "result": sub_id}
                    state.push_call_to_buffer(
                        method,
                        "success",
                        method_snap["latency_ms"],
                        request_id=req_id,
                        lava_headers=lava_headers,
                    )
                    if method_snap["latency_ms"] > 0:
                        time.sleep(method_snap["latency_ms"] / 1000.0)
                    try:
                        out_queue.put_nowait(_text_frame(response))
                    except queue.Full:
                        return
                    continue

                # Unsubscribe request — remove from registry and return bool result.
                if method in stubs_ws.UNSUBSCRIBE_METHODS:
                    from server import _unregister_ws_subscription
                    params = body.get("params") or []
                    target_id = params[0] if params else None
                    removed = False
                    if target_id and target_id in connection_subs:
                        handle = _unregister_ws_subscription(target_id)
                        if handle is not None:
                            connection_subs.discard(target_id)
                            removed = True
                    response = {"jsonrpc": "2.0", "id": req_id, "result": removed}
                    state.push_call_to_buffer(
                        method,
                        "success",
                        method_snap["latency_ms"],
                        request_id=req_id,
                        lava_headers=lava_headers,
                    )
                    if method_snap["latency_ms"] > 0:
                        time.sleep(method_snap["latency_ms"] / 1000.0)
                    try:
                        out_queue.put_nowait(_text_frame(response))
                    except queue.Full:
                        return
                    continue

                # Non-subscription request → delegate to existing chain handler.
                # MAG-1837 — gate corruption_mode on chain_family="ws" so the
                # WS frame encoder doesn't apply a corruption authored for a
                # different transport.
                if snap.get("chain_family") == "btc":
                    _, response = handlers_btc.handle(state, body, snap, lava_headers)
                else:
                    _, response = handlers_eth.handle(state, body, snap, lava_headers)
                state.push_call_to_buffer(
                    method,
                    "error" if "error" in response else "success",
                    method_snap["latency_ms"],
                    request_id=req_id,
                    lava_headers=lava_headers,
                )
                if method_snap["latency_ms"] > 0:
                    time.sleep(method_snap["latency_ms"] / 1000.0)
                try:
                    out_queue.put_nowait(_text_frame(
                        response,
                        corruption_mode=corruption_for(snap, "ws"),
                        missing_field=missing_field_for(snap, "ws"),
                    ))
                except queue.Full:
                    return
        finally:
            # Clean up all subscriptions still active for this connection.
            from server import _unregister_ws_subscription
            for sub_id in list(connection_subs):
                handle = _unregister_ws_subscription(sub_id)
                if handle is not None:
                    handle.closed.set()

    def _register_subscription(self, provider_id: str, method: str,
                                out_queue: "queue.Queue") -> str:
        from server import SubscriptionHandle, _register_ws_subscription
        sub_id = "0x" + secrets.token_hex(16)
        meta = stubs_ws.SUBSCRIBE_METHODS[method]
        handle = SubscriptionHandle(
            sub_id=sub_id,
            provider_id=provider_id,
            method=method,
            chain=meta["chain"],
            envelope=meta["envelope"],
            out_queue=out_queue,
            closed=threading.Event(),
        )
        _register_ws_subscription(handle)
        return sub_id

    def _lava_extra_headers(self) -> Dict[str, str]:
        """Return the extra response headers attached to a successful WS
        upgrade. Lava-Provider-Address identifies which of the three
        simulated providers answered, matching what real Lava providers
        send so the MAG-1749 smoke test can assert presence without
        coupling to a real-provider bech32 format."""
        return {"Lava-Provider-Address": f"sim-provider-{self.server.provider_id}"}

    def _send_simple_error(self, status: int, message: str) -> None:
        """Write a tiny HTTP error response with no body and close."""
        try:
            body = json.dumps({"error": message}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *_):
        """Suppress per-request stdout logging."""
        pass
