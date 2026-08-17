"""REST listener — Cosmos REST over HTTP.

REST carries the request in the URL (verb + path), not a JSON-RPC body, so
``parse_request`` matches the path against the compiled route table and peels off
``{var}`` path params. LavaChain builds the bare REST body (no JSON-RPC
envelope). A rate_limit / error fault is a small ``{"code", "message"}`` object,
not an envelope, and an unmatched path is a 404 recorded in history as
``not_found`` — both matching the flat RestHandler.

HEAD has no route of its own. HTTP defines a HEAD response as the GET response
with the body dropped — same status, same headers, same faults — so a HEAD is
matched against the GET route table and the plan it returns carries
``suppress_body``. History still records the verb the caller actually sent.
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
_REST_ROUTES = [(verb.upper(), _compile_route(template), template) for (verb, template) in REST_METHOD_DEFAULTS]

# Verbs served by another verb's routes. HEAD asks for a GET's status and
# headers without its body, so it is looked up under GET: every GET path gets
# HEAD for free, and a path with no GET route (the POST-only simulate endpoint,
# or anything uncatalogued) still answers 404 to a HEAD.
_ROUTE_VERB = {"HEAD": "GET"}

_ID_LOCK = threading.Lock()
_ID = 0


def _next_request_id() -> int:
    """Sim-side monotonic id used when the caller sends no X-Request-Id, so every
    REST call still gets a stable /history correlation."""
    global _ID
    with _ID_LOCK:
        _ID += 1
        return _ID


def allowed_verbs(path: str) -> list[str]:
    """The HTTP verbs registered for ``path`` (for the OPTIONS response's
    ``Allow`` header). Empty when no route template matches."""
    verbs: list[str] = []
    for route_verb, regex, _template in _REST_ROUTES:
        if regex.match(path) and route_verb not in verbs:
            verbs.append(route_verb)
    return verbs


class RestListener(Listener):
    def serve(self, request: RawRequest, entry: dict | None = None) -> ServeResult:
        result = super().serve(request, entry=entry)
        # A HEAD pays for everything a GET pays for — the fault ladder, the
        # latency, the history row, the Content-Length — and then withholds the
        # bytes. Saying so on the plan keeps the socket adapter free of its own
        # notion of what HEAD means.
        if request.verb.upper() == "HEAD" and result.action == "respond":
            result.suppress_body = True
        return result

    def parse_request(self, request: RawRequest) -> dict:
        verb = request.verb.upper()
        route_verb = _ROUTE_VERB.get(verb, verb)
        path = request.path
        req_id = request.headers.get("X-Request-Id") or _next_request_id()

        template = None
        path_params: dict = {}
        for known_verb, regex, tmpl in _REST_ROUTES:
            if known_verb != route_verb:
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
            # ``verb`` is what the caller sent (history and the 404 body report
            # it); ``route_verb`` is the verb the route table was searched
            # under, which is what selects the stub and any per-route override.
            # They differ only for HEAD.
            "verb": verb,
            "route_verb": route_verb,
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

    def method_key(self, request: dict) -> object:
        # REST per-method fault overrides are keyed by the matched route:
        # the (verb, template) pair. Unmatched paths have no template and
        # therefore no override. A HEAD keys off the GET route it borrowed, so
        # a fault set on a GET path is felt by a HEAD to the same path.
        template = request.get("template")
        route_verb = request.get("route_verb") or request.get("verb")
        return (route_verb, template) if template else None

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
