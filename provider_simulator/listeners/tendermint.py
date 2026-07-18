"""Tendermint-RPC listener — the same method two ways.

A client can ask via GET (``/<method>?<params>``) or POST (a JSON-RPC body).
``parse_request`` normalizes either form into ``{method, params, id}``; malformed
input raises ParseError, which the base turns into a JSON-RPC -32700. LavaChain
builds the JSON-RPC ``result`` envelope; a rate_limit / error fault becomes a
JSON-RPC ``error`` envelope — both matching the flat TendermintHandler.
"""

import json
import threading

from provider_simulator import fault_policy
from provider_simulator.listeners.base import Listener, ParseError, RawRequest, ServeResult

_ID_LOCK = threading.Lock()
_ID = 0


def _next_request_id() -> int:
    """Sim-side monotonic id for GET requests (CometBFT has no id on GET), so
    /history correlation stays stable."""
    global _ID
    with _ID_LOCK:
        _ID += 1
        return _ID


def _normalize_params(raw_params) -> dict:
    """Flatten either wire form into a plain dict of Python-native values.

    GET's ``parse_qs`` gives ``{key: [json-encoded-string]}``; POST gives a
    JSON-typed dict already. Single-element lists unwrap to scalars, and string
    values that look like JSON literals (``"5"``, ``"true"``, ``"\\"x\\""``)
    decode; a plain unquoted string stays as-is. Non-dict input (positional
    params) flattens to an empty dict.
    """
    if not isinstance(raw_params, dict):
        return {}
    out: dict = {}
    for key, value in raw_params.items():
        if isinstance(value, list):
            value = value[0] if value else ""
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                pass
        out[key] = value
    return out


class TendermintListener(Listener):
    def parse_request(self, request: RawRequest) -> dict:
        verb = request.verb.upper()
        method: str
        raw_params: object
        request_id: object
        if verb == "GET":
            method = request.path.strip("/")
            if not method:
                raise ParseError("GET URI has no method (empty path)")
            raw_params = request.query
            request_id = _next_request_id()
        else:  # POST JSON-RPC body
            if not request.body:
                raise ParseError("POST body is empty")
            try:
                body = json.loads(request.body)
            except (ValueError, TypeError) as exc:
                raise ParseError(f"POST body not valid JSON: {exc}") from exc
            if not isinstance(body, dict):
                raise ParseError(f"POST body not a JSON object: {type(body).__name__}")
            raw_method = body.get("method")
            if not isinstance(raw_method, str) or not raw_method:
                raise ParseError("POST body missing 'method' field")
            method = raw_method
            raw_params = body.get("params")
            request_id = body.get("id")

        return {"method": method, "params": _normalize_params(raw_params), "id": request_id}

    def build_fault(self, verdict: fault_policy.Verdict, request: dict) -> ServeResult:
        if verdict.kind == "hang":
            return ServeResult(action="hang")
        if verdict.kind == "drop":
            return ServeResult(action="drop", drop_at=verdict.drop_at)
        # rate_limit / error — Tendermint wraps the error in a JSON-RPC envelope.
        return ServeResult(
            action="respond",
            status=verdict.status,
            body={
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": verdict.error_code, "message": verdict.error_message},
            },
        )

    def build_success(self, status: int, body: object) -> ServeResult:
        return ServeResult(action="respond", status=status, body=body)
