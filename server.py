"""
HTTP JSON-RPC Provider Simulator

Three independent JSON-RPC servers (ports 18545 / 18546 / 18547)
plus one control API (port 19000).

Each provider's behaviour is changed at runtime via POST /scenario.

Supported modes per provider:
  success       — returns {"jsonrpc":"2.0","result":"..."} with optional latency
  error         — returns {"jsonrpc":"2.0","error":{"code":-32000,"message":"..."}}
  rate_limit    — returns HTTP 429
  down          — returns HTTP 503 (router treats provider as unavailable)
  error_probability — randomly returns error on X% of requests (0.0–1.0)

Control API:
  POST /scenario  {"providers": {"1": {"mode": "rate_limit"}, "2": {"mode": "down"}, "3": {"mode": "success"}}}
  POST /reset     {}
  GET  /scenario  → current state of all providers
  GET  /health    → {"status": "ok"}
"""

import json
import random
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Any


# ── Provider state ────────────────────────────────────────────────────────────

@dataclass
class ProviderState:
    mode: str = "success"               # success | error | rate_limit | down
    latency_ms: int = 0
    error_probability: float = 0.0
    responses: Dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

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


# ── JSON-RPC handler ──────────────────────────────────────────────────────────

class JSONRPCHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        state: ProviderState = self.server.state
        snap = state.snapshot()

        # Outage — return 503 (router treats provider as unavailable)
        if snap["mode"] == "down":
            self.send_response(503)
            self.end_headers()
            return

        # Latency injection
        if snap["latency_ms"] > 0:
            time.sleep(snap["latency_ms"] / 1000.0)

        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}
        req_id = body.get("id", 1)
        method = body.get("method", "default")

        # Rate limit — return HTTP 429
        if snap["mode"] == "rate_limit":
            self._reply(429, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": 429, "message": "Too many requests"}
            })
            return

        # Probabilistic error
        if snap["mode"] == "error" or random.random() < snap["error_probability"]:
            self._reply(200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": "Internal error"}
            })
            return

        # Success — look up method-specific result
        with state.lock:
            method_cfg = state.responses.get(method) or state.responses.get("default", {})
        result = method_cfg.get("result", "0x1")

        self._reply(200, {"jsonrpc": "2.0", "id": req_id, "result": result})

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
            return
        if self.path == "/scenario":
            snapshot = {pid: s.snapshot()
                        for pid, s in self.server.provider_states.items()}
            self._reply(200, {"providers": snapshot})
            return
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
        srv.state = states[pid]
        servers.append(srv)

    ctrl = HTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    ctrl.provider_states = states
    servers.append(ctrl)

    print("Provider simulator started")
    for pid, port in PROVIDER_PORTS.items():
        print(f"  provider {pid} → :{port}")
    print(f"  control API  → :{CONTROL_PORT}")

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

