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
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import handlers_btc
import handlers_eth
import handlers_rest
import handlers_ws
from constants import HISTORY_MAX, PROVIDER_PORTS, CONTROL_PORT, GRPC_PROVIDER_PORTS, REST_PORTS, WS_PORTS
from stubs_rest import REST_METHOD_DEFAULTS


# ── Wire-payload normalisation ────────────────────────────────────────────────

def _normalise_responses(raw: Any) -> Dict[Any, Any]:
    """Normalise a ``responses`` wire payload into a dict the handlers can use.

    JSON-RPC tests send ``responses`` as a JSON object keyed by method name:

        {"eth_blockNumber": {"result": "0xff"}}

    REST tests (MAG-1777) cannot use JSON objects because their keys are
    ``(verb, path_template)`` tuples — JSON has no tuple type and no
    non-string object keys. The wire payload for REST is a list of
    ``[[verb, template], cfg]`` pairs:

        [[["GET", "/cosmos/.../blocks/latest"], {"status": 503, "body": {...}}]]

    This helper accepts either shape and re-tuples REST keys. Mixed payloads
    (both string-keyed JSON-RPC entries and list-keyed REST entries in the
    same provider) are NOT supported intentionally — a provider has one
    chain_family at a time.
    """
    if isinstance(raw, list):
        out: Dict[Any, Any] = {}
        for entry in raw:
            # Each entry must be a 2-element [key, cfg] pair. Key is the
            # 2-element [verb, template] list (re-tupled here).
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            key, cfg = entry
            if isinstance(key, (list, tuple)) and len(key) == 2:
                out[(key[0], key[1])] = cfg
            else:
                # Fallback: stringify so a malformed payload doesn't crash
                # state.update — the handler will simply miss the override.
                out[str(key)] = cfg
        return out
    if isinstance(raw, dict):
        return raw
    # Unknown shape — clear responses rather than crash.
    return {}


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
    chain_family: str = "eth"   # one of: "eth", "btc", "grpc", "rest", "ws"; selects which chain-specific handler module dispatches the success-branch response. Default "eth" preserves backward-compat — pre-MAG-1716 /scenario payloads (and the existing ETH tests) keep working without touching the new field. "grpc" routes the success path through handlers_grpc on a separate port (18548/18549/18550); the JSON-RPC dispatcher in JSONRPCHandler.do_POST never sees a "grpc" snap because gRPC providers don't listen on JSON-RPC ports. "rest" routes the success path through handlers_rest on a separate port (18551/18552/18553) for REST-style providers (MAG-1777). "ws" routes traffic through handlers_ws on ports 18557/18558/18559 for WebSocket-style providers with subscription lifecycle (MAG-1801) — the handler delegates non-subscription methods back to handlers_eth.handle / handlers_btc.handle so request/response semantics are identical to HTTP JSON-RPC; subscription frames are wrapped in chain-specific envelopes from stubs_ws.
    # MAG-1791: provider-stale-on-getLogs primitive — head-fresh but logs-indexing-lagged.
    # Models the real production failure mode where providers update eth_blockNumber
    # immediately but index logs in a separate pipeline that can fall behind seconds-to-minutes.
    # None = unaffected (today's behaviour). Set an int = "this provider has only indexed logs
    # up through block <N>"; eth_getLogs queries that touch a higher range return either an
    # empty array (logs_lag_mode="empty") or only logs with blockNumber <= N (mode="partial").
    # eth_blockNumber is unaffected: it keeps reporting current head — that's the whole point
    # of this primitive (head-fresh + logs-lagged is the divergence we want to expose).
    logs_indexed_up_to: Optional[int] = None
    # logs_lag_mode: one of "empty" / "partial". Only consulted when logs_indexed_up_to is set.
    logs_lag_mode: str = "empty"
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
                "logs_indexed_up_to": self.logs_indexed_up_to,
                "logs_lag_mode":      self.logs_lag_mode,
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
            # MAG-1791: backward-compat — missing keys keep current value, so
            # /scenario payloads that don't carry logs_indexed_up_to / logs_lag_mode
            # leave existing provider state untouched (defaults to None / "empty").
            self.logs_indexed_up_to = cfg.get("logs_indexed_up_to", self.logs_indexed_up_to)
            self.logs_lag_mode      = cfg.get("logs_lag_mode",      self.logs_lag_mode)
            if "responses" in cfg:
                self.responses = _normalise_responses(cfg["responses"])

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
            # MAG-1791: reset clears the eth_getLogs stale-indexing primitive
            # so a /reset between tests restores full logs availability.
            self.logs_indexed_up_to = None
            self.logs_lag_mode      = "empty"

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


# ── REST handler (MAG-1777) ───────────────────────────────────────────────────
#
# Peer to JSONRPCHandler. Same ProviderState, same fault primitives (via
# _apply_fault), different verb-routing surface: GET / POST / PUT / DELETE
# instead of POST-only. Each verb method shares the same do_* skeleton:
#
#   1. Capture lava-* request headers.
#   2. Build (verb, path) + parsed query + body (if Content-Length > 0).
#   3. Resolve a request_id — prefer X-Request-Id from the router, else fall
#      back to a sim-side monotonically increasing counter so /history's
#      correlation_group still has a stable key per call.
#   4. Run _apply_fault (snap snapshot is taken inside the helper's caller).
#   5. On no fault, match (verb, path) against the compiled route table.
#      404 if no match.
#   6. Dispatch to handlers_rest.handle for the success-path body.
#   7. Record history with method=f"{verb} {template}" so /history filters
#      stay grep-friendly.


# Module-level sim-side request-id counter. Used when the router doesn't send
# X-Request-Id (e.g. test code calling the simulator directly). Atomic
# increment via a lock so two parallel threads see distinct ids.
_REST_REQUEST_ID_COUNTER = 0
_REST_REQUEST_ID_LOCK = threading.Lock()


# ── WebSocket subscription registry (MAG-1801) ────────────────────────────────
#
# Module-level because subscriptions are per-CONNECTION runtime state, not
# per-provider configuration. ProviderState stays for /scenario-driven config;
# subscriptions live here, indexed by sub_id (32-hex-char string handed back
# to the client when it eth_subscribes). /ws/emit on the control server does a
# (sub_id) → SubscriptionHandle lookup and puts a wire-encoded event frame on
# the matching connection's out_queue. /reset does NOT touch this registry —
# resetting scenario config should not tear down live connections.

import queue


@dataclass
class SubscriptionHandle:
    """One active WS subscription. Created on eth_subscribe / subscribe /
    accountSubscribe / logsSubscribe, removed on the matching unsubscribe or
    on connection close.
    """
    sub_id: str               # 32-hex string handed back to the client
    provider_id: str          # "1" | "2" | "3"
    method: str               # e.g. "newHeads", "logs", "accountSubscribe"
    chain: str                # "eth" | "tendermint" | "solana"
    envelope: str             # one of stubs_ws SUBSCRIBE_METHODS envelope names
    out_queue: "queue.Queue[bytes]"  # frames the writer thread will sendall()
    closed: threading.Event   # set when the reader thread exits


_WS_SUBSCRIPTIONS: Dict[str, SubscriptionHandle] = {}
_WS_SUBSCRIPTIONS_LOCK = threading.Lock()


def _register_ws_subscription(handle: SubscriptionHandle) -> None:
    """Store a fresh subscription handle. Idempotent on sub_id."""
    with _WS_SUBSCRIPTIONS_LOCK:
        _WS_SUBSCRIPTIONS[handle.sub_id] = handle


def _unregister_ws_subscription(sub_id: str) -> Optional[SubscriptionHandle]:
    """Pop and return the handle for sub_id, or None if missing."""
    with _WS_SUBSCRIPTIONS_LOCK:
        return _WS_SUBSCRIPTIONS.pop(sub_id, None)


def _lookup_ws_subscription(sub_id: str) -> Optional[SubscriptionHandle]:
    """Return the live handle for sub_id without removing it."""
    with _WS_SUBSCRIPTIONS_LOCK:
        return _WS_SUBSCRIPTIONS.get(sub_id)


def _all_ws_subscriptions() -> list:
    """Return a snapshot of every active subscription as a list of dicts.
    Used by GET /ws/subscriptions."""
    with _WS_SUBSCRIPTIONS_LOCK:
        return [
            {
                "subscription_id": h.sub_id,
                "provider": h.provider_id,
                "method": h.method,
                "chain": h.chain,
                "queue_depth": h.out_queue.qsize(),
            }
            for h in _WS_SUBSCRIPTIONS.values()
        ]


def _next_sim_request_id() -> int:
    """Return the next sim-side monotonically increasing request id (thread-safe)."""
    global _REST_REQUEST_ID_COUNTER
    with _REST_REQUEST_ID_LOCK:
        _REST_REQUEST_ID_COUNTER += 1
        return _REST_REQUEST_ID_COUNTER


def _compile_route(template: str) -> "re.Pattern[str]":
    """Compile a path template like ``/cosmos/.../blocks/{height}`` into a regex.

    Each ``{var}`` placeholder becomes a named capture group ``(?P<var>[^/]+)``
    so the matcher can peel path params off without a second parse pass. The
    regex is anchored at both ends — partial matches don't count.

    Why hand-rolled and not a third-party router: stdlib-only constraint
    (Q2-A from the MAG-1777 design). 25 LOC of compiled regex covers every
    Cosmos REST path shape we need; no need for a Werkzeug-style mini-framework.
    """
    pattern = re.sub(r"\{([^}/]+)\}", lambda m: rf"(?P<{m.group(1)}>[^/]+)", template)
    return re.compile(rf"^{pattern}$")


def _build_rest_routes() -> List[Tuple[str, "re.Pattern[str]", str]]:
    """Compile every (verb, path_template) key in REST_METHOD_DEFAULTS into a
    matchable route table.

    Returns a list of ``(verb_uppercase, compiled_regex, template_str)`` tuples.
    Module-level so the compile cost is paid once at import time, not per request.
    """
    routes: List[Tuple[str, "re.Pattern[str]", str]] = []
    for (verb, template), _stub in REST_METHOD_DEFAULTS.items():
        routes.append((verb.upper(), _compile_route(template), template))
    return routes


# Compiled once at module import. Re-compile by reloading the module if the
# stub table changes (only happens in development, not at runtime).
_REST_ROUTES: List[Tuple[str, "re.Pattern[str]", str]] = _build_rest_routes()


class RestHandler(BaseHTTPRequestHandler):
    """REST surface for the provider simulator (MAG-1777).

    Shares ``ProviderState`` with ``JSONRPCHandler`` so a /scenario update
    targeting one provider is visible to both handlers regardless of which
    port the test hits. Verb-routing is the structurally new piece:
    BaseHTTPRequestHandler dispatches do_GET / do_POST / etc., and each
    method funnels into a common _handle pipeline that runs fault checks
    and matches the URL against the compiled route table.
    """

    # ── Verb dispatch ─────────────────────────────────────────────────────────

    def do_GET(self):      self._handle("GET")
    def do_POST(self):     self._handle("POST")
    def do_PUT(self):      self._handle("PUT")
    def do_DELETE(self):   self._handle("DELETE")

    def do_HEAD(self):
        """HEAD = GET without the body. Build the GET response, then strip the body.

        The wfile suppression is achieved by overwriting ``_reply`` for this
        single request via the ``_head_mode`` instance flag — keeps the rest
        of the pipeline unchanged.
        """
        self._head_mode = True
        try:
            self._handle("GET")
        finally:
            self._head_mode = False

    def do_OPTIONS(self):
        """OPTIONS returns the set of verbs registered for this path.

        Per RFC 7231, the response carries an ``Allow`` header listing the
        verbs the server accepts for the request URI. If the URI matches no
        registered template the response is 404.
        """
        path = urlparse(self.path).path
        allowed: List[str] = []
        for verb, regex, _template in _REST_ROUTES:
            if regex.match(path):
                if verb not in allowed:
                    allowed.append(verb)
        if not allowed:
            self._reply(404, {"code": "not_found", "method": "OPTIONS", "path": path})
            return
        # HEAD is implied by GET; OPTIONS itself is always allowed.
        if "GET" in allowed and "HEAD" not in allowed:
            allowed.append("HEAD")
        allowed.append("OPTIONS")
        self.send_response(204)
        self.send_header("Allow", ", ".join(allowed))
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── Shared pipeline ───────────────────────────────────────────────────────

    def _handle(self, verb: str) -> None:
        """Run one REST request end-to-end.

        Fault evaluation → (404 if no route match) → handlers_rest.handle
        → wire-reply via _reply. History accounting is delegated to
        _apply_fault for fault branches, and emitted inline for the success
        branch. The method label stored in history is ``f"{verb} {template}"``
        (or ``f"{verb} {path}"`` when no template matched) so /history's
        existing ?method= filter keeps working without code changes on the
        control API side.
        """
        t_start = time.monotonic()
        state: ProviderState = self.server.state
        snap = state.snapshot()

        # Lava-* request headers — used for /history filtering and threaded
        # through to handlers_rest so a future test can assert on header
        # propagation.
        lava_headers = {
            k: v for k, v in self.headers.items()
            if k.lower().startswith("lava-")
        }

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # X-Request-Id wins; fall back to sim-side sequence number so every
        # call still gets a stable correlation_group in /history.
        req_id: Any = self.headers.get("X-Request-Id") or _next_sim_request_id()

        # Latency injection — same as JSON-RPC, applied between down/parse.
        # We can't pre-check down here because _apply_fault wants the method
        # label; we evaluate down separately to mirror JSONRPCHandler's order.
        # In practice REST has no body-parse barrier so down/latency/post-
        # parse-fault simplify into a single _apply_fault call with method=
        # f"{verb} {path}" pre-route, refined to template after routing.
        if snap["mode"] == "down":
            fault = _apply_fault(state, snap, "*", None, lava_headers, t_start)
            self._emit_rest_fault(fault)
            return

        if snap["latency_ms"] > 0:
            time.sleep(snap["latency_ms"] / 1000.0)

        # Read body for verbs that may carry one. GET/HEAD/DELETE typically
        # don't, but the HTTP spec doesn't forbid it — be permissive.
        body: Any = None
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                # Malformed body — leave as None so handlers_rest can decide
                # whether to 400. Don't crash the dispatcher.
                body = None

        # Route match before fault evaluation (other than down/latency above)
        # so the method label in history records the matched template, not the
        # raw path with placeholders unsubstituted.
        match_result = self._match_route(verb, path)
        if match_result is None:
            method_label = f"{verb} {path}"
            fault = _apply_fault(state, snap, method_label, req_id,
                                 lava_headers, t_start)
            if fault is not None:
                self._emit_rest_fault(fault)
                return
            # Genuine 404 — record so /history shows the miss.
            self._reply(404, {"code": "not_found", "method": verb, "path": path})
            state.push_call_to_buffer(method_label, "not_found",
                                      _elapsed_ms(t_start),
                                      request_id=req_id, lava_headers=lava_headers)
            return

        template, path_params = match_result
        method_label = f"{verb} {template}"

        fault = _apply_fault(state, snap, method_label, req_id,
                             lava_headers, t_start)
        if fault is not None:
            self._emit_rest_fault(fault)
            return

        # Success path — chain-specific dispatch (REST handler).
        status, response_body = handlers_rest.handle(
            state, verb, template, path_params, query, body, snap, lava_headers
        )
        emit_status = "error" if (isinstance(response_body, dict) and "error" in response_body) else "success"
        self._reply(status, response_body,
                    corruption_mode=snap.get("corruption_mode"),
                    missing_field=snap.get("missing_field"))
        state.push_call_to_buffer(method_label, emit_status, _elapsed_ms(t_start),
                                  request_id=req_id, lava_headers=lava_headers)

    # ── Routing ───────────────────────────────────────────────────────────────

    def _match_route(self, verb: str, path: str
                     ) -> Optional[Tuple[str, Dict[str, str]]]:
        """Match ``(verb, path)`` against ``_REST_ROUTES``.

        Returns ``(template_str, path_params)`` on first match, else None.
        Path params come straight from the regex's named groups. The match
        is exact (compiled with ``^...$``) so trailing slashes and extra
        segments don't accidentally pass.
        """
        for route_verb, regex, template in _REST_ROUTES:
            if route_verb != verb:
                continue
            m = regex.match(path)
            if m is not None:
                return template, m.groupdict()
        return None

    # ── Wire emission ─────────────────────────────────────────────────────────

    def _emit_rest_fault(self, fault: Optional[Dict[str, Any]]) -> None:
        """Translate a fault dict from ``_apply_fault`` into a REST wire reply.

        REST bodies are bare JSON objects (no JSON-RPC envelope), so rate_limit
        and error compose a small ``{"code": ..., "message": ...}`` body
        instead of the ``{"jsonrpc": "2.0", "id": ..., "error": ...}`` shape.
        Wire-level kinds (``down`` / ``hang`` / ``drop``) are identical to the
        JSON-RPC equivalents — fault kind, not chain, drives the wire action.
        """
        if fault is None:
            return

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
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                elif drop_at == "mid_body":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    self.wfile.write(b'{"block":')
                    self.wfile.flush()
                # before_headers — fall through, no headers sent.
            except Exception:
                pass
            try:
                self.connection.close()
            except Exception:
                pass
            return

        # rate_limit / error — REST shape: {"code": ..., "message": ...}.
        # No id echo, no envelope. Caller-configured corruption hooks still
        # apply because the body is a plain JSON object the corruption layer
        # already knows how to mutate.
        snap = self.server.state.snapshot()
        body = {"code": fault["error_code"], "message": fault["error_message"]}
        self._reply(fault["status"], body,
                    corruption_mode=snap.get("corruption_mode"),
                    missing_field=snap.get("missing_field"))

    def _reply(self, status: int, data: Any,
               corruption_mode: Optional[str] = None,
               missing_field: Optional[str] = None) -> None:
        """Serialise ``data`` as JSON and write a complete HTTP response.

        Mirrors JSONRPCHandler._reply: applies corruption_mode hooks (empty
        body / missing field / wrong type) before serialisation and
        byte-level corruption (truncated / invalid_json) after. The only
        REST-specific tweak is that ``missing_field`` can be a dotted path
        (``"block.header.height"``) — the helper walks the path and removes
        the leaf when the surrounding dicts exist.

        HEAD requests use the same code path but skip the body write — the
        caller sets ``self._head_mode = True`` for the duration of the
        request.
        """
        if not isinstance(data, dict):
            # REST occasionally returns non-dict (lists, scalars). Wrap so the
            # corruption hooks below have somewhere consistent to operate on.
            body_data: Any = data
            structural_only = False
        else:
            body_data = data
            structural_only = True

        # Structural corruption (dict mutations) — only meaningful when body is dict.
        if structural_only:
            if corruption_mode == "missing_field" and missing_field:
                body_data = _remove_dotted_path(body_data, missing_field)
            elif corruption_mode == "empty_response":
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            elif corruption_mode == "wrong_type":
                target = missing_field or next(iter(body_data.keys()), None)
                if target and target in body_data:
                    current = body_data[target]
                    if isinstance(current, bool):
                        body_data[target] = 1 if current else 0
                    elif isinstance(current, str):
                        body_data[target] = 12345
                    elif isinstance(current, (int, float)):
                        body_data[target] = "wrong_type_value"
                    else:
                        body_data[target] = "wrong_type_value"

        raw = json.dumps(body_data).encode()

        # Byte-level corruption.
        if corruption_mode == "truncated" and len(raw) > 10:
            raw = raw[:-10]
        elif corruption_mode == "invalid_json":
            raw = b"}{ {{ not valid json"

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not getattr(self, "_head_mode", False):
            self.wfile.write(raw)

    def log_message(self, *_):
        """Suppress the default per-request stdout logging from BaseHTTPRequestHandler."""
        pass


def _remove_dotted_path(data: Dict[str, Any], path: str) -> Dict[str, Any]:
    """Return ``data`` with the dotted-path key removed.

    ``path`` is a dot-separated string of object keys. Nested dicts are
    cloned along the descent so the caller's original dict isn't mutated.
    Missing intermediate keys cause the helper to return ``data`` unchanged.
    """
    if not path:
        return data
    segments = path.split(".")
    if len(segments) == 1:
        return {k: v for k, v in data.items() if k != segments[0]}
    head, rest = segments[0], ".".join(segments[1:])
    if not isinstance(data.get(head), dict):
        return data
    return {**data, head: _remove_dotted_path(data[head], rest)}


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

        elif self.path == "/ws/emit":
            sub_id = body.get("subscription_id")
            event = body.get("event")
            if not sub_id:
                self._reply(400, {"error": "missing field: subscription_id"})
                return
            if event is None:
                event = {}

            handle = _lookup_ws_subscription(sub_id)
            if handle is None or handle.closed.is_set():
                self._reply(404, {"error": "unknown subscription"})
                return

            import stubs_ws
            wrapped = stubs_ws.build_event_frame(handle.envelope, sub_id, event)
            import handlers_ws as _hws
            frame_bytes = _hws._text_frame(wrapped)

            try:
                handle.out_queue.put_nowait(frame_bytes)
            except queue.Full:
                self._reply(503, {"error": "queue full"})
                return

            # Record the push in history so /history reflects pushed events.
            state = self.server.provider_states.get(handle.provider_id)
            if state is not None:
                state.push_call_to_buffer(
                    f"{handle.envelope} push", "success", 0,
                    request_id=sub_id, lava_headers={},
                )

            self._reply(200, {"status": "emitted", "subscription_id": sub_id})

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

        elif self.path == "/ws/subscriptions":
            self._reply(200, {"subscriptions": _all_ws_subscriptions()})

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

    Spins up ten HTTPServer/gRPC instances in daemon threads:
      - Three JSONRPCHandler servers (ports 18545 / 18546 / 18547) — ETH/BTC chains.
      - Three RestHandler servers (ports 18551 / 18552 / 18553), one per provider,
        sharing the same ProviderState objects as the JSON-RPC servers (MAG-1777).
      - One ControlHandler server (port 19000) for scenario config and history queries.
      - Three gRPC servers (ports 18548 / 18549 / 18550) — Cosmos chain (MAG-1780),
        sharing the per-provider ProviderState with the matching JSON-RPC port.

    All servers share the same dict of ProviderState objects so control API
    changes are immediately visible to every transport handler.
    Mixed-chain scenarios (a /scenario payload setting chain_family="eth" on
    provider 1 and chain_family="rest" on provider 2) work because each
    provider has its own ProviderState; the JSON-RPC server ignores REST
    config fields and vice versa.

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

    # REST servers (MAG-1777). Share the same ProviderState objects keyed by
    # the same provider id ("1" / "2" / "3"), so a /scenario update on
    # provider 1 changes how both the JSON-RPC port (18545) and the REST
    # port (18551) reply. Each server gets its own RestHandler instance
    # because BaseHTTPRequestHandler is per-request.
    for pid, port in REST_PORTS.items():
        rest_srv = ThreadingHTTPServer(("0.0.0.0", port), RestHandler)
        rest_srv.daemon_threads = True
        rest_srv.state       = states[pid]
        rest_srv.provider_id = pid
        servers.append(rest_srv)

    # WS servers (MAG-1801). Share the same ProviderState objects keyed by
    # the same provider id ("1" / "2" / "3"), so a /scenario update on
    # provider 1 changes how all four transports (JSON-RPC, REST, gRPC,
    # WS) reply. Each server gets its own WsHandler instance because
    # BaseHTTPRequestHandler is per-request.
    for pid, port in WS_PORTS.items():
        ws_srv = ThreadingHTTPServer(("0.0.0.0", port), handlers_ws.WsHandler)
        ws_srv.daemon_threads = True
        ws_srv.state       = states[pid]
        ws_srv.provider_id = pid
        servers.append(ws_srv)

    ctrl = HTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    ctrl.provider_states = states
    servers.append(ctrl)

    print("Provider simulator started")
    for pid, port in PROVIDER_PORTS.items():
        print(f"  provider {pid} (jsonrpc) → :{port}")
    for pid, port in GRPC_PROVIDER_PORTS.items():
        print(f"  provider {pid} (grpc)    → :{port}")
    for pid, port in REST_PORTS.items():
        print(f"  provider {pid} (rest)    → :{port}")
    for pid, port in WS_PORTS.items():
        print(f"  provider {pid} (ws)      → :{port}")
    print(f"  control API  → :{CONTROL_PORT}")
    print(f"  GET /stats   → call counts per provider")
    print(f"  GET /history → ordered call log (who was tried first)")

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]

    # gRPC servers (MAG-1780). Each runs its own asyncio loop on a daemon
    # thread, sharing the same ProviderState instance with the matching
    # JSON-RPC port so /scenario applies to both transports at once.
    # Import locally so a missing grpcio dep doesn't break the JSON-RPC-only
    # path (e.g. in tests that don't install gRPC extras).
    try:
        import grpc_server  # local import keeps gRPC dep optional
        for pid, port in GRPC_PROVIDER_PORTS.items():
            threads.append(threading.Thread(
                target=grpc_server.run_grpc_in_thread,
                args=(port, states[pid]),
                daemon=True,
                name=f"grpc-provider-{pid}",
            ))
    except ImportError as exc:
        print(f"  gRPC servers DISABLED — grpcio import failed: {exc}")

    for t in threads:
        t.start()

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        for s in servers:
            s.shutdown()


if __name__ == "__main__":
    # When server.py is run as a script, Python loads it as the "__main__"
    # module. Any later `from server import ...` (e.g. inside handlers_ws
    # for the lazy WS-subscription registry helpers) would re-load server.py
    # as a SECOND module named "server", duplicating every module-level
    # piece of state including _WS_SUBSCRIPTIONS. The WS handler would
    # register subscriptions into server._WS_SUBSCRIPTIONS while the
    # ControlHandler (running in __main__) would look them up in
    # __main__._WS_SUBSCRIPTIONS — different dicts, never matching.
    # Aliasing the module name here makes both paths resolve to the same
    # module instance so module-level state is shared. The fix has no
    # effect in tests (which import `server` first and never run __main__).
    import sys
    sys.modules["server"] = sys.modules["__main__"]
    main()

