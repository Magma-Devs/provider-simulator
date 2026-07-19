"""Listener base — the request-flow template shared by every transport.

serve() runs one fixed sequence for all transports:

    record arrival → snapshot scenario → resolve the effective mode (transports
    filter + fail_first_n window, consumed once) → (provider-wide down? emit
    before parsing) → parse request (parse error?) → merge per-method overrides
    → body override OR fault ladder OR chain success → finalize the log entry.

The fault ladder (via ``fault_policy``) and the success dispatch (via the
provider's chain) live here, once. Transports differ only in parsing the raw
request and shaping the wire output — the ``parse_request`` / ``build_fault`` /
``build_success`` hooks (plus optional ``build_parse_error`` / ``success_label``
overrides for transports with a malformed-wire path or a non-standard history
label like REST's 404 → ``not_found``).

``down`` is evaluated and emitted BEFORE the body is parsed (a dead node never
reads the request), so a down call's history carries method ``"*"`` and
``request_id`` None — matching the long-standing contract other code relies on.
The exception is a per-method ``responses`` override with ``mode="down"``: the
method had to be parsed to find the override, so that entry carries the real
method, request id, and the configured latency.

Per-method ``responses`` overrides can shadow the fault keys (mode, latency_ms,
error probability/code/message, http_status, drop_at) for one method — the
merged config inherits every provider-wide key the override doesn't set. The
override key is the transport's ``method_key`` (JSON-RPC: the method name;
REST: the (verb, template) route pair; transports that resolve overrides inside
the chain return None). The transports filter scopes per-method overrides the
same way it scopes everything else in the block.

serve() returns a ServeResult describing WHAT to put on the wire — including the
latency to wait first and any corruption to apply when serializing a ``respond``
body (success OR a rate_limit / error fault, matching the flat handlers). The
socket adapter that performs the write (and the corruption, via
``listeners.wire.serialize``) lives in server.py. Returning a plan keeps the
whole flow unit-testable without a socket.

The adapter may record the arrival stub itself — before it reads the request
body off the socket, so a client that cancels mid-body-read still leaves an
in_flight history row — and pass it in via ``serve(request, entry=...)``.
Without one, serve() records the arrival itself.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from provider_simulator import fault_policy
from provider_simulator.chains import chain_for
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Provider

# Verdict kind -> the status label recorded in call history.
_STATUS_LABEL = {
    "down": "down",
    "hang": "hang",
    "drop": "drop_connection",
    "rate_limit": "rate_limit",
    "error": "error",
}

# The fault-decision keys a per-method override may shadow. Kept narrow on
# purpose: silently mirroring every scenario field would let unrelated config
# (blocks_behind, transports, …) leak into the per-method path.
_METHOD_OVERRIDE_KEYS = (
    "mode",
    "latency_ms",
    "error_probability",
    "error_code",
    "error_message",
    "http_status",
    "drop_at",
)


class ParseError(Exception):
    """Raised by ``parse_request`` when the wire is malformed (Tendermint uses
    this to surface a JSON-RPC -32700). Transports that tolerate junk input
    (JSON-RPC, REST) return an empty dict instead of raising."""


class DirectResponse(Exception):
    """Raised by ``parse_request`` when the request must be answered without any
    fault/chain dispatch — e.g. the JSON-RPC batch rejection. Carries the wire
    response plus the history labels to finalize the entry with."""

    def __init__(
        self,
        result: "ServeResult",
        method: str,
        status_label: str,
        request_id: "int | str | None" = None,
    ) -> None:
        super().__init__(method)
        self.result = result
        self.method = method
        self.status_label = status_label
        self.request_id = request_id


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
    serialized body on a ``respond`` response (None = clean).
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

    def serve(self, request: RawRequest, entry: dict | None = None) -> ServeResult:
        if entry is None:
            lava = {
                k: v for k, v in (request.headers or {}).items() if k.lower().startswith("lava-")
            }
            entry = self.provider.log.record_arrival(
                self.endpoint.interface,
                self.endpoint.transport,
                self.endpoint.port,
                lava_headers=lava,
            )
        scenario = self.provider.scenario.snapshot()
        # One stateful policy step per request: the transports filter plus the
        # fail_first_n window (consumed here, exactly once).
        targeted, mode = fault_policy.resolve_mode(scenario, self.endpoint, self.provider)
        latency = scenario.get("latency_ms", 0) if targeted else 0

        # Provider-wide down is pre-parse: no body is read, so method="*" /
        # request_id=None, and no latency is paid (a dead node answers nothing).
        if targeted and mode == "down":
            self.provider.log.finalize(entry, method="*", status="down", latency_ms=latency)
            return ServeResult(action="no_body", status=503)

        try:
            parsed = self.parse_request(request)
        except DirectResponse as direct:
            self.provider.log.finalize(
                entry,
                method=direct.method,
                status=direct.status_label,
                latency_ms=0,
                request_id=direct.request_id,
            )
            return direct.result
        except ParseError as exc:
            self.provider.log.finalize(entry, method="*", status="parse_error", latency_ms=0)
            return self.build_parse_error(exc)

        # Per-method override merge (targeted endpoints only — the transports
        # filter scopes the whole block, overrides included).
        merged = scenario
        method_cfg: dict = {}
        if targeted:
            key = self.method_key(parsed)
            responses = scenario.get("responses") or {}
            cfg = responses.get(key) if key is not None else None
            if isinstance(cfg, dict):
                method_cfg = cfg
        if any(k in method_cfg for k in _METHOD_OVERRIDE_KEYS):
            merged = dict(scenario)
            merged["mode"] = mode  # the windowed mode; the override may shadow it
            for k in _METHOD_OVERRIDE_KEYS:
                if k in method_cfg:
                    merged[k] = method_cfg[k]
            mode = merged["mode"]
            latency = merged.get("latency_ms", 0)

        override = self.build_body_override(method_cfg) if method_cfg else None
        if override is not None:
            result = override
            status_label = "success"
            request_id = self.request_id(parsed)
        else:
            verdict = fault_policy.ladder(mode, merged) if targeted else fault_policy.NONE_VERDICT
            if verdict.kind == "down":
                # Per-method down: the body was parsed to find the override, so
                # the entry carries the real method / id and pays the latency.
                self.provider.log.finalize(
                    entry,
                    method=self.request_method(parsed),
                    status="down",
                    latency_ms=latency,
                    request_id=self.request_id(parsed),
                )
                return ServeResult(action="no_body", status=503, latency_ms=latency)
            if verdict.kind != "none":
                result = self.build_fault(verdict, parsed)
                status_label = _STATUS_LABEL[verdict.kind]
                request_id = self.request_id(parsed)
            else:
                chain = chain_for(self.provider.pool.chain)
                status, body = chain.build_success(
                    parsed, scenario, self.provider.quirks.snapshot(), self.endpoint.interface
                )
                result = self.build_success(status, body)
                status_label = self.success_label(status, body)
                request_id = self.response_id(body) or self.request_id(parsed)

        if result.action in ("respond", "drop"):
            result.latency_ms = latency
        # Corruption composes with any body actually written (a success OR a
        # rate_limit / error fault), scoped by the same transports filter the
        # fault ladder uses.
        if result.action == "respond" and targeted:
            result.corruption_mode = scenario.get("corruption_mode")
            result.missing_field = scenario.get("missing_field")

        self.provider.log.finalize(
            entry,
            method=self.request_method(parsed),
            status=status_label,
            latency_ms=latency,
            request_id=request_id,
        )
        return result

    @abstractmethod
    def parse_request(self, request: RawRequest) -> dict: ...

    @abstractmethod
    def build_fault(self, verdict: fault_policy.Verdict, request: dict) -> ServeResult: ...

    @abstractmethod
    def build_success(self, status: int, body: object) -> ServeResult: ...

    def build_parse_error(self, exc: ParseError) -> ServeResult:
        """Response for malformed input. Default is the JSON-RPC -32700 envelope
        (what Tendermint emits); transports that never raise ParseError don't
        reach this."""
        return ServeResult(
            action="respond",
            status=400,
            body={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            },
        )

    def method_key(self, request: dict) -> object:
        """The ``responses`` key that selects this request's per-method fault
        override. Default: the JSON-RPC method name. REST overrides to its
        (verb, template) pair; transports whose overrides resolve inside the
        chain (Tendermint) return None to skip the merge."""
        return request.get("method") if isinstance(request, dict) else None

    def build_body_override(self, method_cfg: dict) -> "ServeResult | None":
        """Per-method canned {status, body} response, bypassing fault + chain.
        Only JSON-RPC supports it; the default is no override."""
        return None

    def success_label(self, status: int, body: object) -> str:
        """History status for a success-path response. Default: ``error`` if the
        body carries an error envelope, else ``success``. REST overrides to label
        a 404 ``not_found``."""
        return "error" if isinstance(body, dict) and "error" in body else "success"

    # Default request/response accessors work for the JSON-RPC dict shape;
    # transports with a different shape override these.
    def request_method(self, request: dict) -> str:
        return request.get("method", "*") if isinstance(request, dict) else "*"

    def request_id(self, request: dict):
        return request.get("id") if isinstance(request, dict) else None

    def response_id(self, body: object):
        return body.get("id") if isinstance(body, dict) else None
