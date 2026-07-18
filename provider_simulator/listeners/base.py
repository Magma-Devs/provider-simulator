"""Listener base — the request-flow template shared by every transport.

serve() runs one fixed sequence for all transports:

    record arrival → snapshot scenario → (down? emit before parsing) → parse
    request → fault policy verdict → emit fault OR build the chain's success
    response → finalize the log entry.

The fault ladder (via ``fault_policy.decide``) and the success dispatch (via the
provider's chain) live here, once. Transports differ only in parsing the raw
request and shaping the wire output — the ``parse_request`` / ``build_fault`` /
``build_success`` hooks.

``down`` is evaluated and emitted BEFORE the body is parsed (a dead node never
reads the request), so a down call's history carries method ``"*"`` and
``request_id`` None — matching the long-standing contract other code relies on.

serve() returns a ServeResult describing WHAT to put on the wire — including the
latency to wait first and any corruption to apply when serializing a success
body. The socket adapter that performs the write (and the corruption, via
``listeners.wire.serialize``) is wired in at the server cut-over. Returning a
plan keeps the whole flow unit-testable without a socket.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from provider_simulator import fault_policy
from provider_simulator.chains import chain_for
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Provider

# Verdict kind -> the status label recorded in call history.
_STATUS_LABEL = {
    "hang": "hang",
    "drop": "drop_connection",
    "rate_limit": "rate_limit",
    "error": "error",
}


@dataclass
class RawRequest:
    """The transport-agnostic request the socket adapter hands to serve().

    JSON-RPC and gRPC read ``body``; REST and Tendermint's GET form read
    ``verb`` / ``path`` / ``query``. Every field defaults empty, so a
    POST-body transport can pass just ``body`` (and ``headers``).
    """

    body: bytes = b""
    headers: dict = field(default_factory=dict)
    verb: str = ""
    path: str = ""
    query: dict = field(default_factory=dict)


@dataclass
class ServeResult:
    """What the transport should put on the wire.

    ``latency_ms`` is the delay the adapter waits before emitting (0 = none).
    ``corruption_mode`` / ``missing_field`` tell the adapter how to break the
    serialized body on a success response (None = clean).
    """

    action: str  # respond | no_body | hang | drop
    status: int = 200
    body: object = None
    drop_at: str = "before_headers"
    latency_ms: int = 0
    corruption_mode: str | None = None
    missing_field: str | None = None


class Listener(ABC):
    def __init__(self, provider: Provider, endpoint: Endpoint) -> None:
        self.provider = provider
        self.endpoint = endpoint

    def serve(self, request: RawRequest) -> ServeResult:
        lava = {k: v for k, v in (request.headers or {}).items() if k.lower().startswith("lava-")}
        entry = self.provider.log.record_arrival(
            self.endpoint.interface,
            self.endpoint.transport,
            self.endpoint.port,
            lava_headers=lava,
        )
        scenario = self.provider.scenario.snapshot()
        verdict = fault_policy.decide(scenario, self.endpoint, self.provider)
        latency = scenario.get("latency_ms", 0)

        # down is pre-parse: no body is read, so method="*" / request_id=None.
        if verdict.kind == "down":
            self.provider.log.finalize(entry, method="*", status="down", latency_ms=latency)
            return ServeResult(action="no_body", status=503)

        parsed = self.parse_request(request)

        if verdict.kind != "none":
            result = self.build_fault(verdict, parsed)
            if result.action in ("respond", "drop"):
                result.latency_ms = latency
            self.provider.log.finalize(
                entry,
                method=self.request_method(parsed),
                status=_STATUS_LABEL[verdict.kind],
                latency_ms=latency,
                request_id=self.request_id(parsed),
            )
            return result

        chain = chain_for(self.provider.pool.chain)
        status, body = chain.build_success(
            parsed, scenario, self.provider.quirks.snapshot(), self.endpoint.interface
        )
        result = self.build_success(status, body)
        result.latency_ms = latency
        # Corruption composes with a successful response, scoped by the same
        # transports filter the fault ladder uses.
        if self._targeted(scenario):
            result.corruption_mode = scenario.get("corruption_mode")
            result.missing_field = scenario.get("missing_field")
        label = "error" if isinstance(body, dict) and "error" in body else "success"
        self.provider.log.finalize(
            entry,
            method=self.request_method(parsed),
            status=label,
            latency_ms=latency,
            request_id=self.response_id(body) or self.request_id(parsed),
        )
        return result

    def _targeted(self, scenario: dict) -> bool:
        transports = scenario.get("transports")
        return transports is None or self.endpoint.transport in transports

    @abstractmethod
    def parse_request(self, request: RawRequest) -> dict: ...

    @abstractmethod
    def build_fault(self, verdict: fault_policy.Verdict, request: dict) -> ServeResult: ...

    @abstractmethod
    def build_success(self, status: int, body: object) -> ServeResult: ...

    # Default request/response accessors work for the JSON-RPC dict shape;
    # transports with a different shape override these.
    def request_method(self, request: dict) -> str:
        return request.get("method", "*") if isinstance(request, dict) else "*"

    def request_id(self, request: dict):
        return request.get("id") if isinstance(request, dict) else None

    def response_id(self, body: object):
        return body.get("id") if isinstance(body, dict) else None
