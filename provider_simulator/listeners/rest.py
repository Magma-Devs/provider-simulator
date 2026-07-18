"""REST listener — Cosmos REST over HTTP.

REST carries the request in the URL (verb + path), not a JSON-RPC body, so
``parse_request`` matches the path against the compiled route table and peels off
``{var}`` path params. LavaChain builds the bare REST body (no JSON-RPC
envelope). A rate_limit / error fault is a small ``{"code", "message"}`` object,
not an envelope, and an unmatched path is a 404 recorded in history as
``not_found`` — both matching the flat RestHandler.
"""

import json
import re
import threading

from provider_simulator import fault_policy
from provider_simulator.listeners.base import Listener, RawRequest, ServeResult
from stubs_rest import REST_METHOD_DEFAULTS


def _compile_route(template: str) -> "re.Pattern[str]":
    """Compile ``/.../blocks/{height}`` into an anchored regex with named groups."""
    pattern = re.sub(r"\{([^}/]+)\}", lambda m: rf"(?P<{m.group(1)}>[^/]+)", template)
    return re.compile(rf"^{pattern}$")


# Compiled once: (verb_upper, regex, template) for every known REST route.
_REST_ROUTES = [
    (verb.upper(), _compile_route(template), template) for (verb, template) in REST_METHOD_DEFAULTS
]

_ID_LOCK = threading.Lock()
_ID = 0


def _next_request_id() -> int:
    """Sim-side monotonic id used when the caller sends no X-Request-Id, so every
    REST call still gets a stable /history correlation."""
    global _ID
    with _ID_LOCK:
        _ID += 1
        return _ID


class RestListener(Listener):
    def parse_request(self, request: RawRequest) -> dict:
        verb = request.verb.upper()
        path = request.path
        req_id = request.headers.get("X-Request-Id") or _next_request_id()

        template = None
        path_params: dict = {}
        for route_verb, regex, tmpl in _REST_ROUTES:
            if route_verb != verb:
                continue
            match = regex.match(path)
            if match is not None:
                template, path_params = tmpl, match.groupdict()
                break

        body = None
        if request.body:
            try:
                body = json.loads(request.body)
            except (ValueError, TypeError):
                body = None

        return {
            "verb": verb,
            "template": template,
            "path": path,
            "path_params": path_params,
            "query": request.query,
            "body": body,
            "request_id": req_id,
        }

    def build_fault(self, verdict: fault_policy.Verdict, request: dict) -> ServeResult:
        if verdict.kind == "hang":
            return ServeResult(action="hang")
        if verdict.kind == "drop":
            return ServeResult(action="drop", drop_at=verdict.drop_at)
        # rate_limit / error — bare REST error object, no JSON-RPC envelope.
        return ServeResult(
            action="respond",
            status=verdict.status,
            body={"code": verdict.error_code, "message": verdict.error_message},
        )

    def build_success(self, status: int, body: object) -> ServeResult:
        return ServeResult(action="respond", status=status, body=body)

    def success_label(self, status: int, body: object) -> str:
        if status == 404:
            return "not_found"
        return super().success_label(status, body)

    def request_method(self, request: dict) -> str:
        template = request.get("template")
        return f"{request.get('verb', '*')} {template if template else request.get('path', '')}"

    def request_id(self, request: dict):
        return request.get("request_id")

    def response_id(self, body: object):
        return None  # REST bodies carry no id
