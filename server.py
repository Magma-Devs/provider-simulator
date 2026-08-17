"""Provider-simulator bootstrap + the socket adapters.

Everything the simulator *decides* lives in the ``provider_simulator`` package:
the topology, the per-provider domain objects, the fault policy, the chain
response builders, the per-transport Listeners, and the control API. This
module is the wire layer on top of them — it binds one socket per endpoint in
the registry and PERFORMS each listener's plan:

- HTTP request/response endpoints (jsonrpc / rest / tendermintrpc) run a
  ``BaseHTTPRequestHandler`` that builds a ``RawRequest``, calls
  ``Listener.serve()``, and turns the returned ``ServeResult`` into wire bytes
  (via ``listeners.wire.serialize``), a hang, or a connection drop.
- gRPC endpoints run an async servicer that performs ``GrpcListener.plan()``:
  abort with a status code, or build the protobuf from the plan's data.
- WebSocket endpoints do the RFC 6455 handshake (refusing the upgrade when the
  fault policy says so), then serve each TEXT frame through the provider's
  ``JsonRpcListener``; the subscription lifecycle (eth_subscribe / emit /
  unsubscribe) is handled here because it is per-connection wire state.
- The control API (port 19000) dispatches each route to a ``ControlApi``
  method and writes its (status, dict) result as JSON.

The process entry point is ``run.py`` — never execute this file as
``__main__``: loading it a second time under a different module name would
duplicate module state (one WS-subscription registry per copy) and desync the
control API from the listeners.
"""

import json
import logging
import os
import queue
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import stubs_ws
from constants import CONTROL_PORT
from provider_simulator import fault_policy
from provider_simulator.control_api import ControlApi
from provider_simulator.domain.registry import Registry, build_registry
from provider_simulator.listeners import (
    JsonRpcListener,
    Listener,
    RawRequest,
    RestListener,
    ServeResult,
    TendermintListener,
    wire,
    ws_protocol,
)
from provider_simulator.listeners.rest import allowed_verbs
from provider_simulator.listeners.ws import WsSubscriptions

_log = logging.getLogger(__name__)

# Verdict kind -> history status label (for paths served outside Listener.serve).
_STATUS_LABEL = {
    "down": "down",
    "hang": "hang",
    "drop": "drop_connection",
    "rate_limit": "rate_limit",
    "error": "error",
}


class _SimThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer for every listener.

    request_queue_size is the OS listen() backlog — how many freshly-arrived
    connections the kernel holds before a worker thread accepts them. The
    stdlib default of 5 is far too shallow for the burst of concurrent relays
    the router fans out under load; 128 gives ample headroom.

    daemon_threads keeps per-request worker threads from blocking interpreter
    shutdown.
    """

    request_queue_size = 128
    daemon_threads = True

    # Wiring attached by SimulatorServer.start() before serving begins.
    listener: Listener
    subscriptions: "_WireSubscriptions"
    control: ControlApi
    registry: Registry


# ── HTTP request/response adapter (jsonrpc / rest / tendermintrpc) ────────────


class _HttpListenerHandler(BaseHTTPRequestHandler):
    """Performs a Listener's ServeResult on a plain HTTP socket.

    Subclasses pick the verbs they answer, whether ``missing_field`` corruption
    uses dotted paths (REST / Tendermint address nested keys; JSON-RPC targets a
    flat top-level field), and the partial bytes a ``mid_body`` drop sends.
    """

    # Socket timeout honoured by BaseHTTPRequestHandler: caps the otherwise
    # unbounded body read so a stalled client can't pin a worker thread.
    timeout = 30
    dotted = False
    mid_body_bytes = b'{"jsonrpc":"2.0",'

    server: _SimThreadingHTTPServer  # narrowed for the wiring attributes

    def _run(self, verb: str) -> None:
        listener = self.server.listener
        provider, endpoint = listener.provider, listener.endpoint
        lava = {k: v for k, v in self.headers.items() if k.lower().startswith("lava-")}
        # Record the arrival BEFORE the body read: a client that cancels while
        # sending the body still leaves an in_flight history row.
        entry = provider.log.record_arrival(endpoint.interface, endpoint.transport, endpoint.port, lava_headers=lava)
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length > 0 else b""
        parsed = urlparse(self.path)
        raw = RawRequest(
            body=body,
            headers=dict(self.headers.items()),
            verb=verb,
            path=parsed.path,
            query=parse_qs(parsed.query),
        )
        result = listener.serve(raw, entry=entry)
        self._perform(result)

    def _perform(self, result: ServeResult) -> None:
        if result.action == "hang":
            # Long enough for any reasonable client read timeout to fire,
            # finite so the worker thread eventually unwinds.
            time.sleep(30)
            self._close()
            return
        if result.latency_ms > 0:
            time.sleep(result.latency_ms / 1000.0)
        if result.action == "no_body":
            self.send_response(result.status)
            self.end_headers()
            return
        if result.action == "drop":
            self._drop(result.drop_at)
            return
        status, raw, emit_body = wire.serialize(
            result.status,
            result.body,
            result.corruption_mode,
            result.missing_field,
            dotted=self.dotted,
        )
        if not emit_body:
            raw = b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        # suppress_body: the listener sized a body but told us to withhold it
        # (an HTTP HEAD). Content-Length above still announces the size a
        # body-carrying request would have received.
        if raw and not result.suppress_body:
            self.wfile.write(raw)

    def _drop(self, drop_at: str) -> None:
        try:
            if drop_at == "after_headers":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100")  # promise a body never sent
                self.end_headers()
            elif drop_at == "mid_body":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(self.mid_body_bytes)  # ~half a body
                self.wfile.flush()
            # before_headers (default): no bytes at all
        except OSError:
            pass  # client may already be gone
        self._close()

    def _close(self) -> None:
        try:
            self.connection.close()
        except OSError:
            pass

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        """Suppress the default per-request stdout logging."""


class _JsonRpcHttpHandler(_HttpListenerHandler):
    def do_POST(self):
        self._run("POST")


class _TendermintHttpHandler(_HttpListenerHandler):
    dotted = True

    def do_GET(self):
        self._run("GET")

    def do_POST(self):
        self._run("POST")


class _RestHttpHandler(_HttpListenerHandler):
    dotted = True
    mid_body_bytes = b'{"block":'

    def do_GET(self):
        self._run("GET")

    def do_POST(self):
        self._run("POST")

    def do_PUT(self):
        self._run("PUT")

    def do_DELETE(self):
        self._run("DELETE")

    def do_HEAD(self):
        # HEAD reaches the listener as HEAD: it borrows the GET route there and
        # comes back with a plan that says "headers only". History records the
        # HEAD, and dropping the body is the plan's instruction, not a rule the
        # socket layer invents.
        self._run("HEAD")

    def do_OPTIONS(self):
        # RFC 7231: answer with the verbs registered for this path. Not routed
        # through the listener — OPTIONS is a wire-metadata query, not a call.
        path = urlparse(self.path).path
        allowed = allowed_verbs(path)
        if not allowed:
            self._send_json(404, {"code": "not_found", "method": "OPTIONS", "path": path})
            return
        if "GET" in allowed and "HEAD" not in allowed:
            allowed.append("HEAD")  # HEAD is implied by GET
        allowed.append("OPTIONS")
        self.send_response(204)
        self.send_header("Allow", ", ".join(allowed))
        self.send_header("Content-Length", "0")
        self.end_headers()


# ── WebSocket adapter ─────────────────────────────────────────────────────────

# Sentinel placed on a connection's out_queue to tell its writer thread to exit.
_WS_SENTINEL = object()


def _ws_writer_loop(connection, out_queue: "queue.Queue") -> None:
    """Drain out_queue and sendall each frame. Exits on the sentinel or when
    the peer disconnects."""
    while True:
        item = out_queue.get()
        if item is _WS_SENTINEL:
            return
        try:
            connection.sendall(item)
        except OSError:
            return


def _ws_text_frame(payload_obj, corruption_mode=None, missing_field=None) -> bytes:
    """Encode a dict as a WS TEXT frame, applying the same corruption
    transforms the HTTP adapters apply (empty_response = zero-payload frame)."""
    _status, raw, emit_body = wire.serialize(200, payload_obj, corruption_mode, missing_field, dotted=False)
    return ws_protocol.encode_frame(ws_protocol.OPCODE_TEXT, raw if emit_body else b"", mask=False)


class _WireSubscriptions(WsSubscriptions):
    """WsSubscriptions that also owns the wire side of an emitted event.

    The control API pushes an event at a subscription id; this subclass wraps
    it in the subscription's chain envelope, enqueues the ready frame BYTES
    (the writer thread only ever sends bytes), and records the push in the
    owning provider's history — so a /history read shows control-plane pushes
    next to the served calls.
    """

    def __init__(self, registry: Registry) -> None:
        super().__init__()
        self._registry = registry

    def emit(self, sub_id: str, event: object) -> str:
        sub = self.get(sub_id)
        if sub is None or sub.closed:
            return "unknown"
        envelope = stubs_ws.SUBSCRIBE_METHODS.get(sub.method, {}).get("envelope", "eth_subscription")
        payload = event if isinstance(event, dict) else {}
        frame = _ws_text_frame(stubs_ws.build_event_frame(envelope, sub_id, payload))
        try:
            sub.out_queue.put_nowait(frame)
        except queue.Full:
            return "full"
        try:
            provider = self._registry.provider(sub.pool, sub.pid)
        except KeyError:
            return "emitted"
        ws_endpoint = next((ep for ep in provider.endpoints if ep.transport == "ws"), None)
        provider.log.push(
            f"{envelope} push",
            "success",
            0,
            interface=ws_endpoint.interface if ws_endpoint else "jsonrpc",
            transport="ws",
            port=ws_endpoint.port if ws_endpoint else 0,
            request_id=sub_id,
            lava_headers={},
        )
        return "emitted"


class _WsHandler(BaseHTTPRequestHandler):
    """WebSocket endpoint: HTTP upgrade handshake + per-frame JSON-RPC.

    A WS endpoint is still ``(jsonrpc, ws, port)``, so frames are served by the
    provider's JsonRpcListener — same chain, same fault policy as the http
    endpoint. What is WS-specific lives here: the handshake (a faulted provider
    refuses the upgrade), the frame codec, the reader/writer thread pair, and
    the subscription lifecycle.
    """

    timeout = 30

    server: _SimThreadingHTTPServer

    def do_GET(self):
        listener = self.server.listener
        provider, endpoint = listener.provider, listener.endpoint

        # Only /ws accepts the upgrade — wrong-path mistakes fail loudly.
        if urlparse(self.path).path != "/ws":
            self._send_simple_error(404, "not found")
            return
        if (
            self.headers.get("Upgrade", "").lower() != "websocket"
            or "upgrade" not in self.headers.get("Connection", "").lower()
            or self.headers.get("Sec-WebSocket-Version") != "13"
            or not self.headers.get("Sec-WebSocket-Key")
        ):
            self._send_simple_error(400, "bad WS upgrade request")
            return

        client_key = self.headers["Sec-WebSocket-Key"]
        lava = {k: v for k, v in self.headers.items() if k.lower().startswith("lava-")}

        # Fault evaluation BEFORE completing the handshake, so a faulted
        # provider refuses the upgrade the way it refuses an HTTP request.
        scenario = provider.scenario.snapshot()
        verdict = fault_policy.decide(scenario, endpoint, provider)
        if verdict.kind != "none":
            self._refuse_upgrade(verdict, scenario, client_key, lava)
            return

        try:
            self.connection.sendall(
                ws_protocol.build_handshake_response(
                    client_key,
                    extra_headers={"Lava-Provider-Address": f"sim-provider-{provider.key}"},
                )
            )
        except OSError:
            return

        out_queue: "queue.Queue" = queue.Queue(maxsize=1000)
        writer = threading.Thread(
            target=_ws_writer_loop,
            args=(self.connection, out_queue),
            daemon=True,
            name=f"ws-writer-{provider.key}",
        )
        writer.start()
        try:
            self._reader_loop(listener, out_queue, lava)
        finally:
            try:
                out_queue.put_nowait(_WS_SENTINEL)
            except queue.Full:
                pass
            try:
                self.connection.close()
            except OSError:
                pass
            writer.join(timeout=1.0)

    def _refuse_upgrade(self, verdict, scenario: dict, client_key: str, lava: dict) -> None:
        """Refuse the WS upgrade per the fault verdict, recording history the
        way the flat WS handler did (method ``"*"`` for down — the provider is
        dead before it reads anything — ``ws_upgrade`` for everything else)."""
        provider = self.server.listener.provider
        endpoint = self.server.listener.endpoint

        def _record(method: str, status: str) -> None:
            provider.log.push(
                method,
                status,
                0,
                interface=endpoint.interface,
                transport=endpoint.transport,
                port=endpoint.port,
                lava_headers=lava,
            )

        if verdict.kind == "down":
            _record("*", "down")
            self._send_simple_error(503, "provider down")
            return
        if verdict.kind == "rate_limit":
            _record("ws_upgrade", "rate_limit")
            self._send_simple_error(429, "rate limited")
            return
        if verdict.kind == "error":
            # 200-without-101 is non-spec for WS upgrades; 4xx is the cleanest
            # "upgrade refused" a client can read.
            _record("ws_upgrade", "error")
            self._send_simple_error(400, scenario.get("error_message", "Internal error"))
            return
        if verdict.kind == "hang":
            _record("ws_upgrade", "hang")
            time.sleep(30)
            try:
                self.connection.close()
            except OSError:
                pass
            return
        # drop
        _record("ws_upgrade", "drop_connection")
        try:
            if verdict.drop_at == "after_headers":
                # Complete the 101 (with Lava-Provider-Address), then close.
                self.connection.sendall(
                    ws_protocol.build_handshake_response(
                        client_key,
                        extra_headers={"Lava-Provider-Address": f"sim-provider-{provider.key}"},
                    )
                )
            elif verdict.drop_at == "mid_body":
                # The 101 has no body — "mid_body" maps to mid-header here.
                self.connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: webso")
            # before_headers (default): silent close, no bytes.
        except OSError:
            pass
        try:
            self.connection.close()
        except OSError:
            pass

    def _reader_loop(self, listener: Listener, out_queue: "queue.Queue", lava: dict) -> None:
        subscriptions = self.server.subscriptions
        connection_subs: set[str] = set()
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
                        out_queue.put_nowait(
                            ws_protocol.encode_frame(ws_protocol.OPCODE_PONG, frame.payload, mask=False)
                        )
                    except queue.Full:
                        return
                    continue
                if frame.opcode != ws_protocol.OPCODE_TEXT:
                    continue  # ignore binary etc.

                try:
                    body = json.loads(frame.payload.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                method = body.get("method") if isinstance(body, dict) else None

                if method in stubs_ws.SUBSCRIBE_METHODS or method in stubs_ws.UNSUBSCRIBE_METHODS:
                    action = self._serve_subscription_frame(
                        listener, subscriptions, out_queue, body, lava, connection_subs
                    )
                else:
                    result = listener.serve(RawRequest(body=frame.payload, headers=lava))
                    action = self._perform_frame(result, out_queue)
                if action == "close":
                    return
        finally:
            # Tear down every subscription still registered to this connection.
            for sub_id in list(connection_subs):
                subscriptions.unregister(sub_id)

    def _serve_subscription_frame(
        self,
        listener: Listener,
        subscriptions: "_WireSubscriptions",
        out_queue: "queue.Queue",
        body: dict,
        lava: dict,
        connection_subs: "set[str]",
    ) -> str:
        """Subscribe/unsubscribe are transport-level calls: the fault ladder
        still applies (a faulted provider can't accept a subscription), but the
        success response is the subscription lifecycle, not a chain response."""
        provider, endpoint = listener.provider, listener.endpoint
        method = body.get("method")
        req_id = body.get("id")
        entry = provider.log.record_arrival(endpoint.interface, endpoint.transport, endpoint.port, lava_headers=lava)
        scenario = provider.scenario.snapshot()
        targeted, mode = fault_policy.resolve_mode(scenario, endpoint, provider)
        merged = scenario
        if targeted:
            # Per-method fault overrides apply to subscription methods too —
            # e.g. `eth_subscribe: {mode: down}` must drop the connection
            # BEFORE any subscription is registered.
            method_cfg = (scenario.get("responses") or {}).get(method)
            if isinstance(method_cfg, dict):
                merged = dict(scenario)
                merged["mode"] = mode
                for key in ("mode", "latency_ms", "drop_at", "error_code", "error_message"):
                    if key in method_cfg:
                        merged[key] = method_cfg[key]
                mode = merged["mode"]
        latency = merged.get("latency_ms", 0) if targeted else 0
        verdict = fault_policy.ladder(mode, merged) if targeted else fault_policy.NONE_VERDICT

        if verdict.kind == "down":
            provider.log.finalize(entry, method=str(method), status="down", latency_ms=latency, request_id=req_id)
            return "close"
        if verdict.kind != "none":
            result = listener.build_fault(verdict, body)
            result.latency_ms = latency
            if result.action == "respond" and targeted:
                result.corruption_mode = scenario.get("corruption_mode")
                result.missing_field = scenario.get("missing_field")
            provider.log.finalize(
                entry,
                method=str(method),
                status=_STATUS_LABEL[verdict.kind],
                latency_ms=latency,
                request_id=req_id,
            )
            return self._perform_frame(result, out_queue)

        if method in stubs_ws.SUBSCRIBE_METHODS:
            sub_id = "0x" + secrets.token_hex(16)
            subscriptions.register(sub_id, provider.pool.name, provider.pid, method, out_queue=out_queue)
            connection_subs.add(sub_id)
            response = {"jsonrpc": "2.0", "id": req_id, "result": sub_id}
        else:
            params = body.get("params") or []
            target_id = params[0] if params else None
            removed = False
            if target_id and target_id in connection_subs and subscriptions.unregister(target_id):
                connection_subs.discard(target_id)
                removed = True
            response = {"jsonrpc": "2.0", "id": req_id, "result": removed}

        provider.log.finalize(entry, method=str(method), status="success", latency_ms=latency, request_id=req_id)
        if latency > 0:
            time.sleep(latency / 1000.0)
        try:
            out_queue.put_nowait(_ws_text_frame(response))
        except queue.Full:
            return "close"
        return "continue"

    def _perform_frame(self, result: ServeResult, out_queue: "queue.Queue") -> str:
        """Perform a ServeResult as a WS frame action. Returns ``"continue"``
        (keep reading) or ``"close"`` (caller must exit the reader loop)."""
        if result.action == "no_body":
            return "close"  # a downed provider kills the connection
        if result.action == "hang":
            # No reply frame; pushed events on other subscriptions still flow.
            return "continue"
        if result.latency_ms > 0:
            time.sleep(result.latency_ms / 1000.0)
        if result.action == "drop":
            try:
                if result.drop_at == "after_headers":
                    # Frame header declaring 100 payload bytes, none sent.
                    self.connection.sendall(bytes([0x81, 100]))
                elif result.drop_at == "mid_body":
                    # Header declaring 100 bytes, send 50, close.
                    self.connection.sendall(bytes([0x81, 100]) + b"X" * 50)
                # before_headers: nothing — fall through to close
            except OSError:
                pass
            return "close"
        try:
            out_queue.put_nowait(_ws_text_frame(result.body, result.corruption_mode, result.missing_field))
        except queue.Full:
            return "close"
        return "continue"

    def _send_simple_error(self, status: int, message: str) -> None:
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
        """Suppress the default per-request stdout logging."""


# ── Control API adapter (port 19000) ──────────────────────────────────────────


class _ControlHandler(BaseHTTPRequestHandler):
    """HTTP surface for the ControlApi routes. Parsing and route dispatch only —
    every decision lives in ControlApi; /ready is the exception because probing
    live TCP ports is a socket concern."""

    timeout = 30

    server: _SimThreadingHTTPServer

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError as exc:
            self._reply(400, {"error": f"request body is not valid JSON: {exc}"})
            return
        if not isinstance(body, dict):
            self._reply(400, {"error": f"request body must be a JSON object, got {type(body).__name__}"})
            return

        control = self.server.control
        if self.path == "/scenario":
            status, payload = control.apply_scenario(body)
        elif self.path == "/reset":
            status, payload = control.reset()
        elif self.path == "/history/clear":
            status, payload = control.clear_history()
        elif self.path == "/reset/all":
            status, payload = control.reset_all()
        elif self.path == "/advance":
            status, payload = control.advance(body)
        elif self.path == "/ws/emit":
            status, payload = control.ws_emit(body)
        else:
            status, payload = 404, {"error": "unknown path"}
        self._reply(status, payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        control = self.server.control
        if path == "/health":
            status, payload = control.health()
        elif path == "/ready":
            status, payload = self._ready()
        elif path == "/scenario":
            status, payload = control.get_scenario()
        elif path == "/stats":
            status, payload = control.get_stats()
        elif path == "/topology":
            status, payload = control.get_topology()
        elif path == "/history":
            status, payload = control.get_history(query)
        elif path == "/ws/subscriptions":
            status, payload = control.ws_subscriptions()
        else:
            status, payload = 404, {"error": "unknown path"}
        self._reply(status, payload)

    def _ready(self) -> tuple[int, dict]:
        """Real readiness: every registry port accepts a TCP connection — not
        just "the python process started". Wired to the chart's readinessProbe
        so the router's earliest relays can't race the listener binds (a
        connection-refused there poisons the router's pairing pool)."""
        ports = self.server.registry.ports()
        missing = []
        for port in ports:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.settimeout(0.1)
            try:
                if probe.connect_ex(("127.0.0.1", port)) != 0:
                    missing.append(port)
            finally:
                probe.close()
        if missing:
            return 503, {
                "status": "not_ready",
                "listening": len(ports) - len(missing),
                "expected": len(ports),
                "missing_ports": missing,
            }
        return 200, {"status": "ready", "listening": len(ports), "expected": len(ports)}

    def _reply(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        """Suppress the default per-request stdout logging."""


# ── gRPC adapter ──────────────────────────────────────────────────────────────


def _run_grpc_in_thread(grpc_listener, port: int, host: str) -> None:
    """Run one gRPC endpoint: an asyncio event loop on this (daemon) thread
    hosting an async servicer that performs the listener's GrpcPlan.

    All gRPC imports are local so a missing grpcio never breaks the HTTP-only
    simulator (the caller downgraded gRPC to a warning at bootstrap).
    """
    import asyncio
    import datetime

    import grpc
    from grpc_reflection.v1alpha import reflection

    # Importing the package splices cosmos_pb2/ onto sys.path so the generated
    # stubs' absolute imports resolve; it must run before the `from cosmos...`
    # imports below.
    import cosmos_pb2  # noqa: F401  isort: split

    from cosmos.base.tendermint.v1beta1 import query_pb2, query_pb2_grpc  # isort: skip
    from tendermint.types import block_pb2, types_pb2  # isort: skip

    from provider_simulator.chains.lava import GRPC_LATEST_BLOCK, LAVA_SIM_CHAIN_ID

    def _merged(data: dict) -> dict:
        # A per-method `responses` result override arrives as {"result": {...}};
        # its keys shadow the plan's defaults.
        merged = dict(data)
        result = merged.pop("result", None)
        if isinstance(result, dict):
            merged.update(result)
        return merged

    def build_latest_block(data: dict):
        merged = _merged(data)
        now = datetime.datetime.now(datetime.timezone.utc)
        header = types_pb2.Header(
            chain_id=merged.get("chain_id", LAVA_SIM_CHAIN_ID),
            height=merged.get("height", GRPC_LATEST_BLOCK),
        )
        header.time.seconds = int(now.timestamp())
        header.time.nanos = now.microsecond * 1000
        block = block_pb2.Block(header=header)
        block_id = types_pb2.BlockID(hash=b"\xab" * 32)
        return query_pb2.GetLatestBlockResponse(block_id=block_id, block=block)

    def build_node_info(data: dict):
        merged = _merged(data)
        resp = query_pb2.GetNodeInfoResponse()
        resp.default_node_info.network = merged.get("network", LAVA_SIM_CHAIN_ID)
        resp.default_node_info.moniker = merged.get("moniker", "lava-sim-grpc-provider")
        resp.default_node_info.version = merged.get("version", "sim-1.0")
        resp.application_version.name = "lava-sim"
        resp.application_version.app_name = merged.get("app_name", "lava-sim-app")
        resp.application_version.version = merged.get("app_version", "sim-1.0")
        return resp

    class _Servicer(query_pb2_grpc.ServiceServicer):
        async def GetLatestBlock(self, request, context):
            return await self._perform("GetLatestBlock", context, build_latest_block)

        async def GetNodeInfo(self, request, context):
            return await self._perform("GetNodeInfo", context, build_node_info)

        async def _perform(self, method: str, context, build_fn):
            metadata = context.invocation_metadata() or []
            lava = {k: v for (k, v) in metadata if k.lower().startswith("lava-")}
            plan = grpc_listener.plan(method, lava)

            if plan.action == "abort":
                if plan.hang:
                    # Long enough for the client deadline to fire, finite so
                    # the asyncio task doesn't leak.
                    await asyncio.sleep(30)
                elif plan.latency_ms > 0:
                    await asyncio.sleep(plan.latency_ms / 1000.0)
                if plan.drop_at in ("after_headers", "mid_body"):
                    # Half-open response: metadata out, then the abort. Unary
                    # RPCs can't stream mid-body, so both variants collapse here.
                    try:
                        await context.send_initial_metadata([])
                    except Exception:
                        pass
                await context.abort(grpc.StatusCode[plan.status_code], plan.message)
                return None  # unreachable — abort raises

            response = build_fn(plan.data)
            if plan.corruption_mode == "missing_field" and plan.missing_field:
                # proto3 fields are clearable; the receiver sees the field unset.
                if response.DESCRIPTOR.fields_by_name.get(plan.missing_field):
                    response.ClearField(plan.missing_field)
            if plan.latency_ms > 0:
                await asyncio.sleep(plan.latency_ms / 1000.0)
            return response

    async def _serve() -> None:
        server = grpc.aio.server()
        query_pb2_grpc.add_ServiceServicer_to_server(_Servicer(), server)
        # Server reflection lets grpcurl discover services without a proto
        # bundle — a dev/test convenience worth the negligible surface.
        service_names = (
            query_pb2.DESCRIPTOR.services_by_name["Service"].full_name,
            reflection.SERVICE_NAME,
        )
        reflection.enable_server_reflection(service_names, server)
        bind = f"[::]:{port}" if host == "0.0.0.0" else f"{host}:{port}"
        server.add_insecure_port(bind)
        _log.info("grpc provider bound on %s", bind)
        await server.start()
        await server.wait_for_termination()

    asyncio.run(_serve())


# ── Scenario TTL sweep ────────────────────────────────────────────────────────


def _scenario_ttl_sweep(registry: Registry, ttl_s: int, interval_s: float = 120.0) -> None:
    """Background daemon: revert any provider whose scenario hasn't been
    written to in > ttl_s seconds back to defaults. Prevents stale state
    (e.g. a leftover mode=hang from a prior test session) from surviving into
    the next session. Only non-default state is reverted — providers already
    in mode='success' are skipped."""
    while True:
        time.sleep(interval_s)
        now = time.time()
        for provider in registry.all_providers():
            age = now - provider.scenario.last_write_at
            if age <= ttl_s:
                continue
            if provider.scenario.snapshot().get("mode") == "success":
                continue
            provider.scenario.reset()
            _log.info(f"[ttl-sweep] reverted provider {provider.key} (idle {age:.0f}s > {ttl_s}s TTL)")


# ── Bootstrap ─────────────────────────────────────────────────────────────────

_HTTP_ADAPTERS: dict = {
    ("jsonrpc", "http"): (_JsonRpcHttpHandler, JsonRpcListener),
    ("jsonrpc", "ws"): (_WsHandler, JsonRpcListener),
    ("rest", "http"): (_RestHttpHandler, RestListener),
    ("tendermintrpc", "http"): (_TendermintHttpHandler, TendermintListener),
}


class SimulatorServer:
    """The whole simulator as one object: registry + one bound socket per
    endpoint + the control API. ``start()`` binds and serves on daemon
    threads; ``stop()`` shuts the HTTP servers down (gRPC loops and the TTL
    sweep are daemon threads that die with the process).

    Tests construct this directly (with ``host="127.0.0.1"`` and the TTL sweep
    disabled) to run the real server in-process on the real ports.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        control_port: int = CONTROL_PORT,
        scenario_ttl_s: "int | None" = None,
    ) -> None:
        self.host = host
        self.control_port = control_port
        self.registry = build_registry()
        self.subscriptions = _WireSubscriptions(self.registry)
        self.control = ControlApi(self.registry, self.subscriptions)
        if scenario_ttl_s is None:
            scenario_ttl_s = int(os.environ.get("SIM_SCENARIO_TTL_SECONDS", "900"))
        self.scenario_ttl_s = scenario_ttl_s
        self.grpc_enabled = False
        self._servers: list[_SimThreadingHTTPServer] = []
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        grpc_endpoints = []
        for provider in self.registry.all_providers():
            for endpoint in provider.endpoints:
                if endpoint.interface == "grpc":
                    grpc_endpoints.append((provider, endpoint))
                    continue
                handler_cls, listener_cls = _HTTP_ADAPTERS[(endpoint.interface, endpoint.transport)]
                srv = _SimThreadingHTTPServer((self.host, endpoint.port), handler_cls)
                srv.listener = listener_cls(provider, endpoint)
                srv.subscriptions = self.subscriptions
                self._servers.append(srv)

        ctrl = _SimThreadingHTTPServer((self.host, self.control_port), _ControlHandler)
        ctrl.control = self.control
        ctrl.registry = self.registry
        self._servers.append(ctrl)

        self._threads = [threading.Thread(target=srv.serve_forever, daemon=True) for srv in self._servers]

        # gRPC endpoints — optional dependency: a missing grpcio downgrades
        # them to a warning instead of killing the HTTP-only simulator.
        if grpc_endpoints:
            try:
                from provider_simulator.listeners.grpc import GrpcListener

                for provider, endpoint in grpc_endpoints:
                    self._threads.append(
                        threading.Thread(
                            target=_run_grpc_in_thread,
                            args=(GrpcListener(provider, endpoint), endpoint.port, self.host),
                            daemon=True,
                            name=f"grpc-{provider.key}",
                        )
                    )
                self.grpc_enabled = True
            except ImportError as exc:
                _log.warning(f"  gRPC listeners DISABLED — grpcio import failed: {exc}")

        if self.scenario_ttl_s > 0:
            self._threads.append(
                threading.Thread(
                    target=_scenario_ttl_sweep,
                    args=(self.registry, self.scenario_ttl_s),
                    daemon=True,
                    name="scenario-ttl-sweep",
                )
            )

        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        # shutdown() waits for each serve_forever loop to notice the flag
        # (up to its 0.5s poll interval) — run them concurrently so stopping
        # ~40 listeners takes one poll interval, not the sum of them.
        stoppers = [threading.Thread(target=srv.shutdown) for srv in self._servers]
        for stopper in stoppers:
            stopper.start()
        for stopper in stoppers:
            stopper.join()

    def wait_ready(self, timeout_s: float = 10.0) -> None:
        """Block until every registry port accepts a TCP connection (or raise).
        Callers that race the bind (tests, scripted boots) use this instead of
        a sleep."""
        deadline = time.monotonic() + timeout_s
        pending = set(self.registry.ports())
        probe_host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
        while pending:
            for port in sorted(pending):
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                probe.settimeout(0.2)
                try:
                    if probe.connect_ex((probe_host, port)) == 0:
                        pending.discard(port)
                finally:
                    probe.close()
            if pending and time.monotonic() > deadline:
                raise TimeoutError(f"listeners not ready after {timeout_s}s: {sorted(pending)}")


def _log_topology(registry: Registry, control_port: int) -> None:
    _log.info("Provider simulator started")
    for pool in registry.pools.values():
        for provider in pool.providers.values():
            endpoints = ", ".join(f"{ep.interface}/{ep.transport} :{ep.port}" for ep in provider.endpoints)
            _log.info(f"  {provider.key:<20} ({pool.chain:<6}) → {endpoints}")
    _log.info(f"  control API  → :{control_port}")
    _log.info("  GET /stats   → call counts per provider")
    _log.info("  GET /history → ordered call log (who was tried first)")


def main():
    """Start the simulator and block until interrupted."""
    # format="%(message)s" keeps output identical to a bare print() — the
    # simulator's stdout is scraped in its bare-text shape by tests and
    # `kubectl logs`.
    logging.basicConfig(level=os.environ.get("SIM_LOG_LEVEL", "INFO"), format="%(message)s")

    server = SimulatorServer()
    server.start()
    if server.scenario_ttl_s > 0:
        _log.info(
            f"[ttl-sweep] started — scenario TTL = {server.scenario_ttl_s}s "
            f"(set SIM_SCENARIO_TTL_SECONDS=0 to disable)"
        )
    _log_topology(server.registry, server.control_port)

    try:
        for thread in server._threads:
            thread.join()
    except KeyboardInterrupt:
        server.stop()


# Entry point lives in run.py — see the docstring there. server.py is a library
# module; running it directly would load it twice (as __main__ and as `server`)
# and split module-level state across the two copies.
