"""GraphQL listener — parsing and wire-shaping for the GraphQL interface.

The request flow lives in the base Listener; this class supplies only what is
GraphQL. A request is one POST with a JSON body, and the name of the request is
the names of its root fields, joined with ``&`` (``graphql_document``). That name
is the history method and the key of a per-method override, so
``responses={"checkpoint": {...}}`` reaches a request for the root field
``checkpoint``, and ``responses={"epoch&chainIdentifier": {...}}`` reaches a
request for those two.

Four GraphQL rules, which differ from the other interfaces:

- A fault is reported inside a 200. An ``error`` is ``data`` null beside one error
  whose ``extensions.code`` is the string of the scenario (or the code
  INTERNAL_SERVER_ERROR when the code is the default integer, and no
  ``extensions`` when the string is empty). ``rate_limit_style`` ``body`` makes a
  rate limit the same, with the code RESOURCE_EXHAUSTED. The default rate limit
  is still the HTTP 429 with plain text. ``error_body`` replaces the whole error
  reply, with the ``http_status`` of the scenario.
- A reply that the chain builds, and that holds ``errors``, is labelled ``error`` in
  the history, also when it holds ``data`` beside them (a partial reply). A canned
  ``body`` of a ``responses`` entry is labelled by its status, as on JSON-RPC:
  ``success`` inside 2xx and ``error`` outside it, whatever the body holds.
- A body that cannot be read as a request gets a reply that imitates the one of the
  real Sui server for that kind of body (``graphql_document`` says where its text
  differs), and a history row with the method ``unknown`` and the
  status ``error``. The row holds the length and the hash of the body, because
  ``serve`` records both before it reads the body. ``unreadable_reply`` replaces
  the reply, for an endpoint that the scenario targets.
- The row holds the URL path, because a GraphQL provider answers at any path.

The listener knows no chain. The chain gives the success content, and the one
thing that the listener adds is the memory of executed transactions: it hands the
chain the transactions that the provider ran before, and it remembers the ones
that the chain ran now.
"""

import time

from provider_simulator import fault_policy
from provider_simulator.graphql_document import (
    GraphQLRequestError,
    default_error_reply,
    graphql_error,
    parse_graphql_request,
)
from provider_simulator.listeners.base import DirectResponse, Listener, RawRequest, ServeResult

_INTERNAL_SERVER_ERROR = "INTERNAL_SERVER_ERROR"
_RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"


class GraphQLListener(Listener):
    def parse_request(self, request: RawRequest) -> dict:
        try:
            graphql = parse_graphql_request(request.body)
        except GraphQLRequestError as error:
            raise DirectResponse(
                result=self._reply_to_an_unreadable_body(error), method="unknown", status_label="error"
            ) from None
        return {"graphql": graphql, "method": graphql.method, "request_id": graphql.request_id}

    def _reply_to_an_unreadable_body(self, error: GraphQLRequestError) -> ServeResult:
        scenario = self.provider.scenario.snapshot()
        chosen = scenario.get("unreadable_reply")
        if chosen is not None and fault_policy.targets(scenario, self.endpoint):
            return ServeResult(action="respond", status=chosen["status"], body=chosen.get("body", ""))
        status, body = default_error_reply(error)
        return ServeResult(action="respond", status=status, body=body)

    def build_fault(self, verdict: fault_policy.Verdict, request: dict) -> ServeResult:
        if verdict.kind == "hang":
            return ServeResult(action="hang")
        if verdict.kind == "drop":
            return ServeResult(action="drop", drop_at=verdict.drop_at)
        if verdict.kind == "rate_limit":
            if verdict.rate_limit_style == "body":
                entry = graphql_error(verdict.rate_limit_body, _RESOURCE_EXHAUSTED)
                return ServeResult(action="respond", status=200, body={"data": None, "errors": [entry]})
            # Plain text, as on every other interface: see the module docstring of jsonrpc.py.
            return ServeResult(action="respond", status=verdict.status, body=verdict.rate_limit_body)
        if verdict.error_body is not None:
            return ServeResult(action="respond", status=verdict.status, body=verdict.error_body)
        code = verdict.error_code if isinstance(verdict.error_code, str) else _INTERNAL_SERVER_ERROR
        entry = graphql_error(verdict.error_message, code)
        return ServeResult(action="respond", status=verdict.status, body={"data": None, "errors": [entry]})

    def build_success(self, status: int, body: object) -> ServeResult:
        return ServeResult(action="respond", status=status, body=body)

    def build_content(self, parsed: dict, scenario: dict, request: RawRequest) -> tuple[int, object]:
        # The chain reads the transactions that this provider ran, and it reports
        # the ones that it ran now. The listener keeps the memory, because the
        # chain is one object for every provider.
        now = time.time()
        parsed["executed_at"] = self.provider.executed_snapshot()
        parsed["now"] = now
        status, body = super().build_content(parsed, scenario, request)
        for digest in parsed.get("executed_now", ()):
            self.provider.remember_executed(digest, now)
        return status, body

    def build_body_override(self, method_cfg: dict) -> ServeResult | None:
        if "body" not in method_cfg or method_cfg.get("body") is None:
            return None
        return ServeResult(action="respond", status=method_cfg.get("status", 200), body=method_cfg["body"])

    def success_label(self, status: int, body: object, request: dict | None = None) -> str:
        if not 200 <= status < 300:
            return "error"
        if isinstance(body, dict) and body.get("errors"):
            return "error"
        return "success"

    def body_for_history(self, request: RawRequest) -> bytes:
        # Every body is recorded, an empty one included: a test finds the row of a
        # request that sent nothing by the hash of nothing.
        return request.body or b""

    def path_for_history(self, request: RawRequest) -> str:
        return request.path

    def request_id(self, request: dict):
        return request.get("request_id")
