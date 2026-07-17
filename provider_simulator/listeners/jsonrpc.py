"""JSON-RPC listener — parsing + wire-shaping for the JSON-RPC transport.

The request flow lives in the base Listener; this class supplies only the
JSON-RPC specifics: parse the JSON body, and shape faults / successes as
JSON-RPC envelopes. ``down`` / ``hang`` / ``drop`` are transport actions the
base handles or that carry no body; ``rate_limit`` and ``error`` become a
JSON-RPC error envelope.
"""

import json

from provider_simulator import fault_policy
from provider_simulator.listeners.base import Listener, ServeResult


class JsonRpcListener(Listener):
    def parse_request(self, raw: bytes) -> dict:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def build_fault(self, verdict: fault_policy.Verdict, request: dict) -> ServeResult:
        if verdict.kind == "hang":
            return ServeResult(action="hang")
        if verdict.kind == "drop":
            return ServeResult(action="drop", drop_at=verdict.drop_at)
        # rate_limit / error → JSON-RPC error envelope
        rid = self.request_id(request)
        rid = 1 if rid is None else rid
        body = {
            "jsonrpc": "2.0",
            "id": rid,
            "error": {"code": verdict.error_code, "message": verdict.error_message},
        }
        return ServeResult(action="respond", status=verdict.status, body=body)

    def build_success(self, status: int, body: object) -> ServeResult:
        return ServeResult(action="respond", status=status, body=body)
