"""
HTTP JSON-RPC Provider Simulator

Three independent JSON-RPC servers (ports 18545 / 18546 / 18547)
plus one control API (port 19000).

Each provider's behaviour is changed at runtime via POST /scenario.

Supported modes per provider:
  success           — returns {"jsonrpc":"2.0","result":"..."} with optional latency
  error             — returns {"jsonrpc":"2.0","error":{"code":-32000,"message":"..."}}
  rate_limit        — returns HTTP 429
  down              — returns HTTP 503 (router treats provider as unavailable)
  error_probability — randomly returns error on X% of requests (0.0–1.0)

Control API:
  POST /scenario   {"providers": {"1": {"mode": "rate_limit"}, "2": {"mode": "down"}}}
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
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict
from urllib.parse import urlparse, parse_qs

from stubs import METHOD_DEFAULTS
from constants import HISTORY_MAX, PROVIDER_PORTS, CONTROL_PORT


# ── Provider state ────────────────────────────────────────────────────────────



@dataclass
class ProviderState:
    mode: str = "success"               # success | error | rate_limit | down
    latency_ms: int = 0
    error_probability: float = 0.0
    responses: Dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # call history — each entry: {ts, method, status, latency_ms}
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAX), repr=False)
    # all-time counters — never capped, survives history ring-buffer rollover
    total_calls: int = 0
    calls_by_status: Dict[str, int] = field(default_factory=dict, repr=False)

    def snapshot(self) -> dict:
        """Return a thread-safe copy of the mutable config fields (mode, latency_ms, error_probability).
        Used by JSONRPCHandler at the start of every request so the handler works
        on a stable snapshot even if a test updates the state mid-request."""
        with self.lock:
            return {
                "mode":              self.mode,
                "latency_ms":        self.latency_ms,
                "error_probability": self.error_probability,
            }

    def update(self, cfg: dict) -> None:
        """Apply a partial config dict received from POST /scenario.
        Only keys present in cfg are updated; omitted keys keep their current value.
        Acquires the lock so updates are atomic and safe to call from any thread."""
        with self.lock:
            self.mode              = cfg.get("mode",              self.mode)
            self.latency_ms        = cfg.get("latency_ms",        self.latency_ms)
            self.error_probability = cfg.get("error_probability", self.error_probability)
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
            self.responses         = {}

    def clear_history(self) -> None:
        """Wipe the in-memory call buffer and reset all-time counters to zero.
        Does NOT touch the scenario config (mode, latency, responses).
        Called by POST /history/clear — use before a specific request to isolate its history."""
        with self.lock:
            self.history.clear()
            self.total_calls       = 0
            self.calls_by_status   = {}

    def push_call_to_buffer(self, method: str, status: str, latency_ms: int) -> None:
        """Push one call record into the in-memory ring-buffer and update all-time counters.

        Storage is entirely in RAM — nothing is written to disk or any logging framework.
        The ring-buffer (deque) automatically drops the oldest entry once it reaches
        HISTORY_MAX (200) entries. All-time counters (total_calls, calls_by_status)
        are never capped and survive buffer rollovers.

        Args:
            method:     JSON-RPC method name, e.g. "eth_blockNumber". Use "*" for
                        requests that were rejected before the body was parsed (mode=down).
            status:     Outcome string — "success" | "error" | "rate_limit" | "down".
            latency_ms: Simulated delay that was injected before the response, in ms.
                        0 when no latency was configured or the request was rejected early.
        """
        now = time.time()
        with self.lock:
            self.history.append({
                "ts":         now,
                "time":       datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.") + f"{int(now % 1 * 1000):03d} UTC",
                "method":     method,
                "status":     status,
                "latency_ms": latency_ms,
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
                "requests_by_status_all_time": dict(self.calls_by_status),
                "history_ring_buffer_entries": len(self.history),  # max = HISTORY_MAX
            }

    def get_history(self) -> list:
        """Return a thread-safe copy of the in-memory ring-buffer as a plain list.
        The returned list is a snapshot — mutations to it do not affect the buffer.
        Used by ControlHandler.do_GET() to build the /history response."""
        with self.lock:
            return list(self.history)


# ── JSON-RPC handler ──────────────────────────────────────────────────────────

class JSONRPCHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        """Handle every incoming JSON-RPC POST request for one simulated provider.

        Decision flow (evaluated in order, first match wins):
          1. mode == "down"          → 503, no body parsed.
          2. latency_ms > 0          → sleep before continuing.
          3. mode == "rate_limit"    → 429 JSON-RPC error.
          4. mode == "error" or
             random() < error_prob   → 200 JSON-RPC error body.
          5. custom response defined → return configured result.
          6. default stub            → return METHOD_DEFAULTS value.

        Every branch calls push_call_to_buffer() so the outcome is always recorded
        in the in-memory ring-buffer regardless of which path was taken.
        """
        t_start = time.monotonic()
        state: ProviderState = self.server.state
        provider_id: str     = self.server.provider_id
        snap = state.snapshot()

        # Outage — return 503
        if snap["mode"] == "down":
            self.send_response(503)
            self.end_headers()
            state.push_call_to_buffer("*", "down", 0)
            return

        # Latency injection
        if snap["latency_ms"] > 0:
            time.sleep(snap["latency_ms"] / 1000.0)

        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}
        req_id = body.get("id", 1)
        method = body.get("method", "unknown")

        # Rate limit — return HTTP 429
        if snap["mode"] == "rate_limit":
            self._reply(429, {"jsonrpc": "2.0", "id": req_id,
                              "error": {"code": 429, "message": "Too many requests"}})
            state.push_call_to_buffer(method, "rate_limit", self._elapsed_ms(t_start))
            return

        # Probabilistic / forced error
        if snap["mode"] == "error" or random.random() < snap["error_probability"]:
            self._reply(200, {"jsonrpc": "2.0", "id": req_id,
                              "error": {"code": -32000, "message": "Internal error"}})
            state.push_call_to_buffer(method, "error", self._elapsed_ms(t_start))
            return

        # Success — look up method-specific result
        with state.lock:
            method_cfg = state.responses.get(method) or state.responses.get("default", {})
        result = method_cfg.get("result", METHOD_DEFAULTS.get(method, "0x1"))

        # eth_getBlockByNumber: echo the requested block number so the router's
        # pruning verification sees the correct block number in the response.
        if method == "eth_getBlockByNumber" and isinstance(result, dict):
            params = body.get("params", [])
            if params:
                named = {"latest": "0x1312D00", "earliest": "0x0", "pending": "0x1312D01",
                         "safe": "0x1312D00",   "finalized": "0x1312CFF"}
                result = dict(result)
                result["number"] = named.get(params[0], params[0])

        self._reply(200, {"jsonrpc": "2.0", "id": req_id, "result": result})
        state.push_call_to_buffer(method, "success", self._elapsed_ms(t_start))

    @staticmethod
    def _elapsed_ms(t_start: float) -> int:
        """Return the integer milliseconds elapsed since t_start (from time.monotonic())."""
        return int((time.monotonic() - t_start) * 1000)

    def _reply(self, status: int, data: dict):
        """Serialise data as JSON and write a complete HTTP response with correct headers.

        Args:
            status: HTTP status code (e.g. 200, 429, 503).
            data:   Response payload — will be JSON-encoded and sent as the body.
        """
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
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
                           Supports query params: last, from, to, provider, method, status.
                           Every entry includes a call_order field (1 = first attempted).

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
            #   ?from=<unix_ts>   — include only calls at or after this timestamp
            #   ?to=<unix_ts>     — include only calls at or before this timestamp
            #   ?last=<seconds>   — shorthand: calls in the last N seconds
            #   ?provider=<id>    — filter to a single provider (1, 2, or 3)
            #   ?method=<name>    — filter to a specific RPC method
            #   ?status=<name>    — filter by status (success, error, rate_limit, down)
            #
            # Each entry in the response includes:
            #   call_order        — 1-based position in the merged timeline (sorted by ts).
            #                       call_order=1 is the provider the router tried FIRST,
            #                       call_order=2 is the second attempt, etc.
            #
            # Examples:
            #   /history?last=60
            #   /history?from=1774534600&to=1774534700
            #   /history?last=120&provider=2
            #   /history?last=60&status=error
            qs = parse_qs(urlparse(self.path).query)

            t_from     = float(qs["from"][0])     if "from"     in qs else None
            t_to       = float(qs["to"][0])       if "to"       in qs else None
            last_secs  = float(qs["last"][0])      if "last"     in qs else None
            f_provider = qs["provider"][0]         if "provider" in qs else None
            f_method   = qs["method"][0]           if "method"   in qs else None
            f_status   = qs["status"][0]           if "status"   in qs else None

            if last_secs is not None:
                t_from = time.time() - last_secs

            all_calls = []
            for pid, s in self.server.provider_states.items():
                if f_provider and pid != f_provider:
                    continue
                for entry in s.get_history():
                    if t_from   and entry["ts"] < t_from:   continue
                    if t_to     and entry["ts"] > t_to:     continue
                    if f_method and entry["method"] != f_method: continue
                    if f_status and entry["status"] != f_status: continue
                    all_calls.append({"provider": pid, **entry})

            all_calls.sort(key=lambda x: x["ts"])
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
        srv = HTTPServer(("0.0.0.0", port), JSONRPCHandler)
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

