"""JSON-RPC listener — parsing + wire-shaping for the JSON-RPC transport.

The request flow lives in the base Listener; this class supplies only the
JSON-RPC specifics: parse the JSON body, and shape faults / successes as
JSON-RPC envelopes. ``down`` / ``hang`` / ``drop`` are transport actions the
base handles or that carry no body; ``error`` becomes a JSON-RPC error
envelope. ``rate_limit`` sends a plain-text body instead — real providers
answer a 429 with prose or HTML, never a JSON-RPC error object, and the
router's node-error classification reads the body shape, so a simulated
rate limit that looked like a JSON-RPC error would exercise a path a real
429 never reaches. See ``ScenarioConfig.rate_limit_body`` for the
per-provider override.

Two JSON-RPC-only wire rules:
- A batch body (top-level JSON array) is not supported: it answers a single
  -32600 envelope and is logged as method ``batch`` / status ``error``.
- A per-method ``responses`` entry may pin a canned ``{status, body}`` success
  response (2xx only, validated at /scenario time); it bypasses the fault
  ladder and the chain.
"""

import json

from provider_simulator import fault_policy
from provider_simulator.listeners.base import DirectResponse, Listener, RawRequest, ServeResult


class JsonRpcListener(Listener):
    def parse_request(self, request: RawRequest) -> dict:
        raw = request.body
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        if isinstance(parsed, list):
            # A JSON-RPC batch. Unsupported — but calling dict methods on a
            # list would crash the handler, so answer a single Invalid-Request
            # error and record the attempt under the ``batch`` label.
            raise DirectResponse(
                result=ServeResult(
                    action="respond",
                    status=200,
                    body={
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "batch requests are not supported"},
                    },
                ),
                method="batch",
                status_label="error",
            )
        return parsed if isinstance(parsed, dict) else {}

    def build_fault(self, verdict: fault_policy.Verdict, request: dict) -> ServeResult:
        if verdict.kind == "hang":
            return ServeResult(action="hang")
        if verdict.kind == "drop":
            return ServeResult(action="drop", drop_at=verdict.drop_at)
        if verdict.kind == "rate_limit":
            # Plain text, not a JSON-RPC envelope — see the module docstring.
            return ServeResult(action="respond", status=verdict.status, body=verdict.rate_limit_body)
        # error → JSON-RPC error envelope
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

    def build_body_override(self, method_cfg: dict) -> ServeResult | None:
        if "body" not in method_cfg or method_cfg.get("body") is None:
            return None
        return ServeResult(
            action="respond",
            status=method_cfg.get("status", 200),
            body=method_cfg["body"],
        )

    def request_method(self, request: dict) -> str:
        # A parsed body with no "method" field is logged as "unknown" (it WAS
        # parsed — "*" is reserved for pre-parse outcomes like down).
        if isinstance(request, dict):
            return request.get("method", "unknown")
        return "*"
