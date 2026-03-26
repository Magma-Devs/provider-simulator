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


# ── Provider state ────────────────────────────────────────────────────────────

HISTORY_MAX = 200   # call log entries kept per provider


@dataclass
class ProviderState:
    mode: str = "success"               # success | error | rate_limit | down
    latency_ms: int = 0
    error_probability: float = 0.0
    responses: Dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # call history — each entry: {ts, method, status, latency_ms}
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAX), repr=False)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "mode":              self.mode,
                "latency_ms":        self.latency_ms,
                "error_probability": self.error_probability,
            }

    def update(self, cfg: dict) -> None:
        with self.lock:
            self.mode              = cfg.get("mode",              self.mode)
            self.latency_ms        = cfg.get("latency_ms",        self.latency_ms)
            self.error_probability = cfg.get("error_probability", self.error_probability)
            if "responses" in cfg:
                self.responses = cfg["responses"]

    def reset(self) -> None:
        with self.lock:
            self.mode              = "success"
            self.latency_ms        = 0
            self.error_probability = 0.0
            self.responses         = {}
            self.history.clear()

    def log_call(self, method: str, status: str, latency_ms: int) -> None:
        with self.lock:
            self.history.append({
                "ts":         time.time(),
                "method":     method,
                "status":     status,
                "latency_ms": latency_ms,
            })

    def stats(self) -> dict:
        with self.lock:
            by_status: Dict[str, int] = {}
            for entry in self.history:
                by_status[entry["status"]] = by_status.get(entry["status"], 0) + 1
            return {"total": len(self.history), "by_status": by_status}

    def get_history(self) -> list:
        with self.lock:
            return list(self.history)


# ── JSON-RPC handler ──────────────────────────────────────────────────────────

class JSONRPCHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        t_start = time.monotonic()
        state: ProviderState = self.server.state
        provider_id: str     = self.server.provider_id
        snap = state.snapshot()

        # Outage — return 503
        if snap["mode"] == "down":
            self.send_response(503)
            self.end_headers()
            state.log_call("*", "down", 0)
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
            state.log_call(method, "rate_limit", self._elapsed_ms(t_start))
            return

        # Probabilistic / forced error
        if snap["mode"] == "error" or random.random() < snap["error_probability"]:
            self._reply(200, {"jsonrpc": "2.0", "id": req_id,
                              "error": {"code": -32000, "message": "Internal error"}})
            state.log_call(method, "error", self._elapsed_ms(t_start))
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
        state.log_call(method, "success", self._elapsed_ms(t_start))

    @staticmethod
    def _elapsed_ms(t_start: float) -> int:
        return int((time.monotonic() - t_start) * 1000)

    def _reply(self, status: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


# ── Control API handler ───────────────────────────────────────────────────────

class ControlHandler(BaseHTTPRequestHandler):

    def do_POST(self):
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
                state.reset()
            self._reply(200, {"status": "reset"})

        else:
            self._reply(404, {"error": "unknown path"})

    def do_GET(self):
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

        elif self.path.startswith("/history"):
            # Supported query params (all optional, combinable):
            #   ?from=<unix_ts>   — include only calls at or after this timestamp
            #   ?to=<unix_ts>     — include only calls at or before this timestamp
            #   ?last=<seconds>   — shorthand: calls in the last N seconds
            #   ?provider=<id>    — filter to a single provider (1, 2, or 3)
            #   ?method=<name>    — filter to a specific RPC method
            #   ?status=<name>    — filter by status (success, error, rate_limit, down)
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
            self._reply(200, {"count": len(all_calls), "history": all_calls})

        else:
            self._reply(404, {"error": "unknown path"})

    def _reply(self, status: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


# ── Server startup ────────────────────────────────────────────────────────────

PROVIDER_PORTS = {"1": 18545, "2": 18546, "3": 18547}
CONTROL_PORT   = 19000


def main():
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

