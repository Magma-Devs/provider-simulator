"""
HTTP JSON-RPC Provider Simulator

Three independent JSON-RPC servers (ports 18545 / 18546 / 18547)
plus one control API (port 19000).

Each provider's behaviour is changed at runtime via POST /scenario.

Supported modes per provider:
  success           — returns {"jsonrpc":"2.0","result":"..."} with optional latency
  error             — returns {"jsonrpc":"2.0","error":{"code":…,"message":"…"}}
                      Configurable via error_code (default -32000),
                      error_message (default "Internal error"),
                      and http_status (default 200).
  rate_limit        — returns HTTP 429
  down              — returns HTTP 503 (router treats provider as unavailable)
  error_probability — randomly returns error on X% of requests (0.0–1.0)

Control API:
  POST /scenario   {"providers": {"1": {"mode": "error", "error_code": -32601,
                     "error_message": "Method not found", "http_status": 200}}}
  POST /reset      {}
  GET  /scenario   → current state of all providers
  GET  /health     → {"status": "ok"}
  GET  /stats      → call counts and per-status breakdown per provider
  GET  /history    → ordered call log — supports filtering:
                       ?last=60          last 60 seconds
                       ?from=<ts>        from unix timestamp
                       ?to=<ts>          to unix timestamp
                       ?provider=1       single provider (1/2/3)
                       ?method=eth_call  specific RPC method
                       ?status=error     success | error | rate_limit | down
                     params are combinable: ?last=120&provider=2&status=error
"""

import datetime
import json
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import handlers_btc
import handlers_eth
from constants import HISTORY_MAX, PROVIDER_PORTS, CONTROL_PORT


# ── Provider state ────────────────────────────────────────────────────────────



@dataclass
class ProviderState:
    mode: str = "success"               # success | error | rate_limit | down
    latency_ms: int = 0
    error_probability: float = 0.0
    error_code: int = -32000            # JSON-RPC error code when mode="error"
    error_message: str = "Internal error"  # JSON-RPC error message when mode="error"
    http_status: int = 200              # HTTP status code for error responses (200 = JSON-RPC body error)
    responses: Dict[str, Any] = field(default_factory=dict)
    corruption_mode: Optional[str] = None     # one of: None, "truncated", "missing_field", "invalid_json", "empty_response", "wrong_type"
    missing_field: Optional[str] = None       # field-name slot — which top-level field to target when corruption_mode is "missing_field" (omit it) or "wrong_type" (swap its type). Defaults to "result" for wrong_type when unset.
    blocks_behind: int = 0    # 0 = current head; positive = behind; negative = ahead
    drop_at: str = "before_headers"   # one of: "before_headers", "after_headers", "mid_body"; only applies when mode="drop_connection"
    chain_family: str = "eth"   # one of: "eth", "btc"; selects which chain-specific handler module dispatches the success-branch response. Default "eth" preserves backward-compat — pre-MAG-1716 /scenario payloads (and the existing 155 ETH tests) keep working without touching the new field.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # call history — each entry: {ts, method, status, latency_ms}
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAX), repr=False)
    # all-time counters — never capped, survives history ring-buffer rollover
    total_calls: int = 0
    calls_by_status: Dict[str, int] = field(default_factory=dict, repr=False)

    def snapshot(self) -> dict:
        """Return a thread-safe copy of the mutable config fields.
        Used by JSONRPCHandler at the start of every request so the handler works
        on a stable snapshot even if a test updates the state mid-request."""
        with self.lock:
            return {
                "mode":              self.mode,
                "latency_ms":        self.latency_ms,
                "error_probability": self.error_probability,
                "error_code":        self.error_code,
                "error_message":     self.error_message,
                "http_status":       self.http_status,
                "corruption_mode":   self.corruption_mode,
                "missing_field":     self.missing_field,
                "blocks_behind":     self.blocks_behind,
                "drop_at":           self.drop_at,
                "chain_family":      self.chain_family,
            }

    def update(self, cfg: dict) -> None:
        """Apply a partial config dict received from POST /scenario.
        Only keys present in cfg are updated; omitted keys keep their current value.
        Acquires the lock so updates are atomic and safe to call from any thread."""
        with self.lock:
            self.mode              = cfg.get("mode",              self.mode)
            self.latency_ms        = cfg.get("latency_ms",        self.latency_ms)
            self.error_probability = cfg.get("error_probability", self.error_probability)
            self.error_code        = cfg.get("error_code",        self.error_code)
            self.error_message     = cfg.get("error_message",     self.error_message)
            self.http_status       = cfg.get("http_status",       self.http_status)
            self.corruption_mode   = cfg.get("corruption_mode",   self.corruption_mode)
            self.missing_field     = cfg.get("missing_field",     self.missing_field)
            self.blocks_behind     = cfg.get("blocks_behind",     self.blocks_behind)
            self.drop_at           = cfg.get("drop_at",           self.drop_at)
            self.chain_family      = cfg.get("chain_family",      self.chain_family)
            if "responses" in cfg:
                self.responses = cfg["responses"]

    def reset_scenario(self) -> None:
        """Reset only the scenario config fields back to startup defaults (mode, latency, responses).
        Does NOT touch the call history or counters.
        Called by POST /reset — use between test scenarios to put providers back to healthy."""
        with self.lock:
            self.mode              = "success"
            self.latency_ms        = 0
            self.error_probability = 0.0
            self.error_code        = -32000
            self.error_message     = "Internal error"
            self.http_status       = 200
            self.responses         = {}
            self.corruption_mode   = None
            self.missing_field     = None
            self.blocks_behind     = 0
            self.drop_at           = "before_headers"
            self.chain_family      = "eth"

    def clear_history(self) -> None:
        """Wipe the in-memory call buffer and reset all-time counters to zero.
        Does NOT touch the scenario config (mode, latency, responses).
        Called by POST /history/clear — use before a specific request to isolate its history."""
        with self.lock:
            self.history.clear()
            self.total_calls       = 0
            self.calls_by_status   = {}

    def push_call_to_buffer(self, method: str, status: str, latency_ms: int,
                             request_id: object = None, lava_headers: dict = None) -> None:
        """Push one call record into the in-memory ring-buffer and update all-time counters.

        Storage is entirely in RAM — nothing is written to disk or any logging framework.
        The ring-buffer (deque) automatically drops the oldest entry once it reaches
        HISTORY_MAX (200) entries. All-time counters (total_calls, calls_by_status)
        are never capped and survive buffer rollovers.

        Args:
            method:       JSON-RPC method name, e.g. "eth_blockNumber". Use "*" for
                          requests that were rejected before the body was parsed (mode=down).
            status:       Outcome string — "success" | "error" | "rate_limit" | "down".
            latency_ms:   Simulated delay that was injected before the response, in ms.
                          0 when no latency was configured or the request was rejected early.
            request_id:   The JSON-RPC ``id`` field from the request body (echoed back in
                          the response). ``None`` for down-mode rejections where the body
                          is never parsed.
            lava_headers: Dict of all ``lava-*`` HTTP request headers provided by the router.
                          ``{}`` if no lava headers were sent.
        """
        now = time.time()
        with self.lock:
            self.history.append({
                "ts":            now,
                "time":          datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.") + f"{int(now % 1 * 1000):03d} UTC",
                "request_id":    request_id,
                "method":        method,
                "status":        status,
                "latency_ms":    latency_ms,
                "lava_headers":  lava_headers or {},
            })
            self.total_calls += 1
            self.calls_by_status[status] = self.calls_by_status.get(status, 0) + 1

    def stats(self) -> dict:
        """Return a thread-safe snapshot of the all-time call counters for this provider.
        Counters are never reset (unlike the ring-buffer which is cleared on reset()).
        Used by GET /stats to show cumulative traffic since the pod started."""
        with self.lock:
            return {
                "total_requests_all_time":    self.total_calls,
                "total_calls":                self.total_calls,  # alias for convenience
                "requests_by_status_all_time": dict(self.calls_by_status),
                "calls_by_status":             dict(self.calls_by_status),  # alias for convenience
                "history_ring_buffer_entries": len(self.history),  # max = HISTORY_MAX
            }

    def get_history(self) -> list:
        """Return a thread-safe copy of the in-memory ring-buffer as a plain list.
        The returned list is a snapshot — mutations to it do not affect the buffer.
        Used by ControlHandler.do_GET() to build the /history response."""
        with self.lock:
            return list(self.history)


# ── Fault-injection helper (chain-agnostic) ───────────────────────────────────
#
# Extracted from JSONRPCHandler.do_POST (MAG-1777). Both the JSON-RPC handler
# and the REST handler call this with their parsed-request context. The helper
# evaluates the same 5 fault primitives in the same order, records the outcome
# in history, and returns a structured dict the caller turns into a wire
# response in its chain's native shape (JSON-RPC envelope vs REST JSON object).
#
# Why a dict and not Optional[Tuple[int, dict]]: down / hang / drop_connection
# need wire-level actions (no body / sleep+close / partial-write+close) that a
# raw (status, body) tuple can't express. The dict's "kind" field tells the
# caller which wire action to perform; rate_limit and error carry status +
# error_code + error_message so the caller composes a chain-appropriate body.
# History accounting lives in the helper so callers don't duplicate it.


def _apply_fault(
    state: "ProviderState",
    snap: Dict[str, Any],
    method: str,
    req_id: Any,
    lava_headers: Dict[str, str],
    t_start: float,
) -> Optional[Dict[str, Any]]:
    """Evaluate post-parse fault primitives and emit history.

    Args:
        state:        The live ProviderState (used to push history records).
        snap:         ProviderState.snapshot() taken at request start; the
                      evaluation uses the snapshot so a mid-request /scenario
                      update can't change the outcome of an in-flight request.
        method:       Resolved method name. For JSON-RPC this is the body
                      "method" field; for REST it's "<VERB> <path_template>"
                      (built by the caller). Used only for history accounting,
                      not for fault decisions.
        req_id:       JSON-RPC id (or X-Request-Id / sim sequence number for
                      REST). Echoed back in the response by the caller when
                      relevant; None for down-mode rejections where no body
                      is parsed.
        lava_headers: Captured lava-* request headers; stored on the history
                      entry for later /history filtering.
        t_start:      time.monotonic() value at request entry, used to compute
                      latency on fault outcomes that count time-to-emit.

    Returns:
        None when no fault triggered — caller proceeds to chain-specific
        success-path handlers (handlers_eth / handlers_btc / handlers_rest).
        Otherwise a dict describing the fault:

          {"kind": "down"}
            Caller MUST emit 503 with no body. History already recorded with
            method="*" (down is pre-body-parse, so no method is known).

          {"kind": "hang"}
            Caller MUST sleep 30s then close the connection. History recorded
            with status="hang", latency_ms=0.

          {"kind": "drop", "drop_at": str}
            Caller MUST perform the partial-write dance per drop_at
            ("before_headers" / "after_headers" / "mid_body") and close.
            History recorded.

          {"kind": "rate_limit", "status": 429, "error_code": 429,
           "error_message": "Too many requests"}
            Caller composes a chain-appropriate body and sends it. History
            recorded.

          {"kind": "error", "status": int, "error_code": int,
           "error_message": str}
            Caller composes a chain-appropriate error body. History recorded.
    """
    # 1. Outage — fires before any body parse. method is "*" because we never
    #    look at the request body in down mode.
    if snap["mode"] == "down":
        state.push_call_to_buffer("*", "down", 0,
                                  request_id=None, lava_headers=lava_headers)
        return {"kind": "down"}

    # 2. Hang — accept request, sleep "forever". 30s is long enough for any
    #    reasonable client read timeout to fire; finite so the thread eventually
    #    exits and we don't leak threads if the client disconnects.
    if snap["mode"] == "hang":
        state.push_call_to_buffer(method, "hang", 0,
                                  request_id=req_id, lava_headers=lava_headers)
        return {"kind": "hang"}

    # 3. Drop connection — close socket at one of three points.
    if snap["mode"] == "drop_connection":
        drop_at = snap.get("drop_at", "before_headers")
        state.push_call_to_buffer(method, "drop_connection",
                                  _elapsed_ms(t_start),
                                  request_id=req_id, lava_headers=lava_headers)
        return {"kind": "drop", "drop_at": drop_at}

    # 4. Rate limit — HTTP 429.
    if snap["mode"] == "rate_limit":
        state.push_call_to_buffer(method, "rate_limit", _elapsed_ms(t_start),
                                  request_id=req_id, lava_headers=lava_headers)
        return {
            "kind": "rate_limit",
            "status": 429,
            "error_code": 429,
            "error_message": "Too many requests",
        }

    # 5. Probabilistic / forced error — configurable code, message, HTTP status.
    if snap["mode"] == "error" or random.random() < snap["error_probability"]:
        state.push_call_to_buffer(method, "error", _elapsed_ms(t_start),
                                  request_id=req_id, lava_headers=lava_headers)
        return {
            "kind": "error",
            "status": snap.get("http_status", 200),
            "error_code": snap.get("error_code", -32000),
            "error_message": snap.get("error_message", "Internal error"),
        }

    return None


def _elapsed_ms(t_start: float) -> int:
    """Return the integer milliseconds elapsed since t_start (time.monotonic())."""
    return int((time.monotonic() - t_start) * 1000)


# ── JSON-RPC handler ──────────────────────────────────────────────────────────

class JSONRPCHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        """Handle every incoming JSON-RPC POST request for one simulated provider.

        Decision flow (evaluated in order, first match wins):
          1. mode == "down"          → 503, no body parsed (via _apply_fault).
          2. latency_ms > 0          → sleep before continuing.
          3. mode == "hang"          → sleep 30s, close (via _apply_fault).
          4. mode == "drop_connection" → partial write + close (via _apply_fault).
          5. mode == "rate_limit"    → 429 JSON-RPC error (via _apply_fault).
          6. mode == "error" or
             random() < error_prob   → JSON-RPC error body (via _apply_fault).
          7. custom response defined → return configured result.
          8. default stub            → return METHOD_DEFAULTS value.

        Every branch (via _apply_fault or in-line success path) calls
        push_call_to_buffer so the outcome is always recorded in the in-memory
        ring-buffer regardless of which path was taken.
        """
        t_start = time.monotonic()
        state: ProviderState = self.server.state
        snap = state.snapshot()

        # Capture all lava-* headers from the router
        lava_headers = {
            k: v for k, v in self.headers.items()
            if k.lower().startswith("lava-")
        }

        # Pre-parse fault check: down mode doesn't read the body.
        # We evaluate this before body-parse via a two-step _apply_fault call
        # (down first with method="*"/req_id=None, then the post-parse faults
        # with the parsed method/req_id).
        if snap["mode"] == "down":
            fault = _apply_fault(state, snap, "*", None, lava_headers, t_start)
            self._emit_jsonrpc_fault(fault, req_id=None,
                                     corruption_mode=snap.get("corruption_mode"),
                                     missing_field=snap.get("missing_field"))
            return

        # Latency injection — applies before any post-parse fault evaluation.
        if snap["latency_ms"] > 0:
            time.sleep(snap["latency_ms"] / 1000.0)

        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}
        req_id = body.get("id", 1)
        method = body.get("method", "unknown")

        # Post-parse fault evaluation. _apply_fault records history internally.
        fault = _apply_fault(state, snap, method, req_id, lava_headers, t_start)
        if fault is not None:
            self._emit_jsonrpc_fault(fault, req_id=req_id,
                                     corruption_mode=snap.get("corruption_mode"),
                                     missing_field=snap.get("missing_field"))
            return

        # Success — delegate the chain-specific success path to a handler module.
        #
        # Fault branches above (down / hang / drop / rate-limit / forced or
        # probabilistic error) are chain-agnostic and stay in _apply_fault.
        # Only the method-lookup + result-shape logic is chain-specific — we
        # pick the handler module based on snap["chain_family"]. Default "eth"
        # preserves backward-compat for every payload that doesn't set
        # chain_family.
        #
        # The handler returns the status + response envelope; this layer is
        # responsible for I/O (corruption hooks, history accounting).
        if snap.get("chain_family") == "btc":
            status, response_body = handlers_btc.handle(state, body, snap, lava_headers)
        else:
            status, response_body = handlers_eth.handle(state, body, snap, lava_headers)
        emit_status = "error" if "error" in response_body else "success"
        self._reply(status, response_body,
                    corruption_mode=snap.get("corruption_mode"),
                    missing_field=snap.get("missing_field"))
        state.push_call_to_buffer(method, emit_status, _elapsed_ms(t_start),
                                  request_id=req_id, lava_headers=lava_headers)

    def _emit_jsonrpc_fault(self, fault: Dict[str, Any], req_id: Any,
                             corruption_mode: Optional[str] = None,
                             missing_field: Optional[str] = None) -> None:
        """Translate a fault dict from ``_apply_fault`` into a JSON-RPC wire reply.

        Each fault "kind" maps to a specific wire action:

        - ``down`` — emit HTTP 503 with no body. Mirrors the router-treats-as-
          unavailable semantic.
        - ``hang`` — sleep 30s, then close the socket. The 30s upper bound is
          long enough for any reasonable client read timeout while still being
          finite so we don't leak threads when the client disconnects.
        - ``drop`` — close the socket at one of three points (before_headers /
          after_headers / mid_body). Exceptions during the partial write are
          swallowed because the client may have already disconnected.
        - ``rate_limit`` — HTTP 429 with a JSON-RPC error envelope.
        - ``error`` — caller-configured HTTP status with a JSON-RPC error
          envelope. The id field is echoed from the request body when known.
        """
        kind = fault["kind"]

        if kind == "down":
            self.send_response(503)
            self.end_headers()
            return

        if kind == "hang":
            time.sleep(30)
            try:
                self.connection.close()
            except Exception:
                pass
            return

        if kind == "drop":
            drop_at = fault.get("drop_at", "before_headers")
            try:
                if drop_at == "after_headers":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "100")  # promise body we won't send
                    self.end_headers()
                elif drop_at == "mid_body":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "100")  # promise body
                    self.end_headers()
                    self.wfile.write(b'{"jsonrpc":"2.0",')  # ~half a body
                    self.wfile.flush()
                # before_headers (default) — fall through, no headers sent
            except Exception:
                pass  # client may have already disconnected, ignore
            try:
                self.connection.close()
            except Exception:
                pass
            return

        # rate_limit / error — JSON-RPC error envelope.
        self._reply(
            fault["status"],
            {"jsonrpc": "2.0", "id": req_id,
             "error": {"code": fault["error_code"],
                       "message": fault["error_message"]}},
            corruption_mode=corruption_mode,
            missing_field=missing_field,
        )

    @staticmethod
    def _elapsed_ms(t_start: float) -> int:
        """Return the integer milliseconds elapsed since t_start (from time.monotonic())."""
        return _elapsed_ms(t_start)

    def _reply(self, status: int, data: dict,
               corruption_mode: Optional[str] = None,
               missing_field: Optional[str] = None):
        """Serialise data as JSON and write a complete HTTP response.
        If corruption_mode is set, alter the body before/after serialization."""
        # Apply structural corruption (modify the dict before serialization)
        if corruption_mode == "missing_field" and missing_field:
            data = {k: v for k, v in data.items() if k != missing_field}
        elif corruption_mode == "empty_response":
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return  # no body
        elif corruption_mode == "wrong_type":
            # Swap the type of a target field so a caller that expects e.g. a
            # hex-string sees an int (or vice versa). Target field comes from
            # the missing_field slot (reused for "which field to corrupt");
            # default to "result" since that's the JSON-RPC success-shape
            # carrier and the most common test target.
            target_field = missing_field or "result"
            if target_field in data:
                current = data[target_field]
                if isinstance(current, bool):
                    # Order matters: bool is a subclass of int — check first.
                    data[target_field] = 1 if current else 0
                elif isinstance(current, str):
                    data[target_field] = 12345
                elif isinstance(current, (int, float)):
                    data[target_field] = "wrong_type_value"
                else:
                    # dict / list / None — fall through to a string sentinel.
                    data[target_field] = "wrong_type_value"

        body = json.dumps(data).encode()

        # Apply byte-level corruption (after serialization)
        if corruption_mode == "truncated" and len(body) > 10:
            body = body[:-10]
        elif corruption_mode == "invalid_json":
            body = b"}{ {{ not valid json"

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        """Suppress the default per-request stdout logging from BaseHTTPRequestHandler."""
        pass


# ── Control API handler ───────────────────────────────────────────────────────

class ControlHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        """Handle POST requests on the control API.

        Routes:
          POST /scenario       — update per-provider config from the request body.
                                 Body: {"providers": {"1": {...}, "2": {...}}}
          POST /reset          — reset scenario config only (mode, latency, responses → defaults).
                                 Does NOT clear history.
          POST /history/clear  — wipe call history and counters only.
                                 Does NOT touch scenario config.
          POST /reset/all      — reset scenario config AND clear history.

        Returns 404 for any unrecognised path.
        """
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}

        if self.path == "/scenario":
            for pid, cfg in body.get("providers", {}).items():
                state = self.server.provider_states.get(str(pid))
                if state:
                    state.update(cfg)
            self._reply(200, {"status": "ok"})

        elif self.path == "/reset":
            for state in self.server.provider_states.values():
                state.reset_scenario()
            self._reply(200, {"status": "scenario reset"})

        elif self.path == "/history/clear":
            for state in self.server.provider_states.values():
                state.clear_history()
            self._reply(200, {"status": "history cleared"})

        elif self.path == "/reset/all":
            for state in self.server.provider_states.values():
                state.reset_scenario()
                state.clear_history()
            self._reply(200, {"status": "scenario reset and history cleared"})

        else:
            self._reply(404, {"error": "unknown path"})

    def do_GET(self):
        """Handle GET requests on the control API.

        Routes:
          GET /health    — liveness probe, always returns {"status": "ok"}.
          GET /scenario  — current snapshot of all provider configs.
          GET /stats     — all-time call counters and per-status breakdown per provider.
          GET /history   — merged, time-sorted call buffer across all providers.
                           Supports query params: last, from, to, provider, method, status,
                           request_id.
                           Every entry includes a call_order field (1 = first attempted)
                           and a request_id field (echoes the JSON-RPC id from the request).

        Returns 404 for any unrecognised path.
        """
        if self.path == "/health":
            self._reply(200, {"status": "ok"})

        elif self.path == "/scenario":
            self._reply(200, {
                "providers": {pid: s.snapshot()
                              for pid, s in self.server.provider_states.items()}
            })

        elif self.path == "/stats":
            # Per-provider call counts and status breakdown.
            # Use this to see if one provider is being skipped or hammered.
            self._reply(200, {
                "providers": {pid: s.stats()
                              for pid, s in self.server.provider_states.items()}
            })

        elif self.path == "/history" or self.path.startswith("/history?"):
            # Supported query params (all optional, combinable):
            #   ?from=<unix_ts>         — include only calls at or after this timestamp
            #   ?to=<unix_ts>           — include only calls at or before this timestamp
            #   ?last=<seconds>         — shorthand: calls in the last N seconds
            #   ?provider=<id>          — filter to a single provider (1, 2, or 3)
            #   ?method=<name>          — filter to a specific RPC method
            #   ?status=<name>          — filter by status (success, error, rate_limit, down)
            #   ?request_id=<id>        — filter by the JSON-RPC id echoed in the request
            #   ?lava_header_*=<value>  — filter by lava header name (e.g. lava_header_lava_stateful_api=true)
            #
            # Each entry in the response includes:
            #   call_order        — 1-based position in the merged timeline (sorted by ts).
            #                       call_order=1 is the provider the router tried FIRST,
            #                       call_order=2 is the second attempt, etc.
            #   correlation_group — groups calls by (request_id, method) within 50ms window.
            #                       calls from same relay have same correlation_group.
            #   request_id        — the JSON-RPC id from the request body (None for down-mode)
            #   lava_headers      — dict of all lava-* headers sent by the router (empty dict if none)
            #
            # Examples:
            #   /history?last=60
            #   /history?from=1774534600&to=1774534700
            #   /history?last=120&provider=2
            #   /history?last=60&status=error
            #   /history?request_id=42
            #   /history?last=60&lava_header_lava_stateful_api=true
            qs = parse_qs(urlparse(self.path).query)

            t_from        = float(qs["from"][0])      if "from"       in qs else None
            t_to          = float(qs["to"][0])         if "to"         in qs else None
            last_secs     = float(qs["last"][0])       if "last"       in qs else None
            f_provider    = qs["provider"][0]          if "provider"   in qs else None
            f_method      = qs["method"][0]            if "method"     in qs else None
            f_status      = qs["status"][0]            if "status"     in qs else None
            f_request_id  = qs["request_id"][0]        if "request_id" in qs else None
            
            # Extract lava header filters: ?lava_header_lava_stateful_api=true becomes {"lava-stateful-api": "true"}
            f_lava_headers = {}
            for param in qs:
                if param.startswith("lava_header_"):
                    header_name = param.replace("lava_header_", "").replace("_", "-")
                    header_value = qs[param][0]
                    f_lava_headers[header_name] = header_value

            if last_secs is not None:
                t_from = time.time() - last_secs

            all_calls = []
            for pid, s in self.server.provider_states.items():
                if f_provider and pid != f_provider:
                    continue
                for entry in s.get_history():
                    if t_from        and entry["ts"] < t_from:                      continue
                    if t_to          and entry["ts"] > t_to:                        continue
                    if f_method      and entry["method"] != f_method:               continue
                    if f_status      and entry["status"] != f_status:               continue
                    if f_request_id  and str(entry.get("request_id")) != f_request_id: continue
                    # Check lava header filters (all must match)
                    if f_lava_headers:
                        entry_headers = entry.get("lava_headers", {})
                        if not all(entry_headers.get(k) == v for k, v in f_lava_headers.items()):
                            continue
                    all_calls.append({"provider": pid, **entry})

            all_calls.sort(key=lambda x: x["ts"])
            
            # Assign correlation_group: group calls by (request_id, method) within 50ms window
            correlation_map = {}  # (request_id, method) → (last_ts, group_id)
            group_counter = 0
            
            for entry in all_calls:
                key = (entry.get("request_id"), entry["method"])
                
                if key in correlation_map:
                    last_ts, group_id = correlation_map[key]
                    if entry["ts"] - last_ts < 0.050:  # 50ms window
                        entry["correlation_group"] = group_id
                    else:
                        # New relay started (same request_id+method but >50ms apart)
                        group_counter += 1
                        entry["correlation_group"] = group_counter
                        correlation_map[key] = (entry["ts"], group_counter)
                else:
                    # First call with this (request_id, method)
                    group_counter += 1
                    entry["correlation_group"] = group_counter
                    correlation_map[key] = (entry["ts"], group_counter)
            
            # Assign call_order within the merged timeline
            for i, entry in enumerate(all_calls, start=1):
                entry["call_order"] = i
            self._reply(200, {"count": len(all_calls), "history": all_calls})

        else:
            self._reply(404, {"error": "unknown path"})

    def _reply(self, status: int, data: dict):
        """Serialise data as JSON and write a complete HTTP response with correct headers.

        Args:
            status: HTTP status code (e.g. 200, 404).
            data:   Response payload — will be JSON-encoded and sent as the body.
        """
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        """Suppress the default per-request stdout logging from BaseHTTPRequestHandler."""
        pass


# ── Server startup ────────────────────────────────────────────────────────────



def main():
    """Start all simulator servers and block until interrupted.

    Spins up four HTTPServer instances in daemon threads:
      - Three JSONRPCHandler servers (ports 18545 / 18546 / 18547), one per provider.
      - One ControlHandler server (port 19000) for scenario config and history queries.

    All four servers share the same dict of ProviderState objects so control API
    changes are immediately visible to the JSON-RPC handlers.

    Blocks on thread.join() and shuts all servers down cleanly on KeyboardInterrupt.
    """
    states = {pid: ProviderState() for pid in PROVIDER_PORTS}

    servers = []
    for pid, port in PROVIDER_PORTS.items():
        # ThreadingHTTPServer so a slow/hanging request on one provider doesn't
        # block its own subsequent requests or the other providers' threads.
        srv = ThreadingHTTPServer(("0.0.0.0", port), JSONRPCHandler)
        srv.daemon_threads = True
        srv.state       = states[pid]
        srv.provider_id = pid          # available as self.server.provider_id in handler
        servers.append(srv)

    ctrl = HTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    ctrl.provider_states = states
    servers.append(ctrl)

    print("Provider simulator started")
    for pid, port in PROVIDER_PORTS.items():
        print(f"  provider {pid} → :{port}")
    print(f"  control API  → :{CONTROL_PORT}")
    print(f"  GET /stats   → call counts per provider")
    print(f"  GET /history → ordered call log (who was tried first)")

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        for s in servers:
            s.shutdown()


if __name__ == "__main__":
    main()

