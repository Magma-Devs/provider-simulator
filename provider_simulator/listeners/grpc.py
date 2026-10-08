"""gRPC listener — Cosmos gRPC over http2.

gRPC does not fit the serve()/ServeResult template: it is an async servicer that
faults by ``await context.abort(status, message)`` and answers with a protobuf
message, not an HTTP body. So this listener exposes
``plan(method, lava_headers, request)`` — a pure decision that reuses the shared
fault policy and LavaChain's success DATA and returns a GrpcPlan the async
servicer glue performs (abort with a status, or build + return the proto). That
glue (protobuf + abort) lands with the server cut-over; keeping the decision here
makes it unit-testable without a running gRPC server.

gRPC-specific rules preserved from the flat handler:
- The RPC method is always known, so even a ``down`` call records that method
  (not ``"*"`` like the pre-body-parse HTTP down).
- A served method can name one field of its request message as the request id
  (``SERVED_METHODS``). For AllBalances that field is ``address``. A method that
  names no field records no request id, and so does a provider-wide ``down``
  row: a dead node does not read the request.
- Faults map to status codes: down -> UNAVAILABLE, hang -> CANCELLED (after a
  30s sleep), drop -> UNAVAILABLE, rate_limit -> RESOURCE_EXHAUSTED, error -> the
  status named in error_message (or error_code as an int), else UNKNOWN.
- down / hang record latency 0 (they don't pay the configured latency); every
  other outcome records the configured latency.
- A per-method error_stub / error override is a status abort, not a body.
- Corruption is proto-level: missing_field clears a field (still a respond);
  wrong_type aborts INTERNAL; invalid_proto / empty_response / truncated /
  null_body abort
  UNKNOWN.

One mode is not planned here at all. ``port_closed`` is not a reply to a call:
the endpoint's server is stopped, so no call arrives and ``plan`` never runs.
``down`` and ``drop`` above are replies: both answer UNAVAILABLE with this
module's own message, over a connection that stays open. The serve loop in
server.py performs ``port_closed`` (see ``provider_simulator/port_gate.py``); a
call that reaches ``plan`` while the mode is set arrived before the port closed
and is planned like a call on an open port.
"""

from dataclasses import dataclass, field

import grpc

from provider_simulator import fault_policy
from provider_simulator.chains import chain_for
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Provider

_STATUS_BY_NAME = {sc.name: sc for sc in grpc.StatusCode}
_STATUS_BY_VALUE = {sc.value[0]: sc for sc in grpc.StatusCode}


@dataclass
class GrpcPlan:
    """What the async servicer glue should do for one gRPC call."""

    action: str  # abort | respond
    status_code: str = "OK"  # grpc.StatusCode name, for an abort
    message: str = ""
    latency_ms: int = 0
    hang: bool = False  # abort after a 30s sleep instead of the latency
    drop_at: str | None = None  # set when the abort models a connection drop
    grpc_method: str = ""  # respond: which proto to build
    data: dict = field(default_factory=dict)  # respond: LavaChain success data
    corruption_mode: str | None = None  # respond: missing_field only
    missing_field: str | None = None


def _status_name(error_message: str, error_code: int) -> str:
    """Resolve the abort status name: the status named in error_message wins,
    then error_code as an integer status, else UNKNOWN."""
    sc = _STATUS_BY_NAME.get(error_message) or _STATUS_BY_VALUE.get(error_code)
    return (sc or grpc.StatusCode.UNKNOWN).name


# The gRPC methods that this simulator serves. Each row holds the full name of
# the service, the bare name of the method, and the field of the request message
# that carries the request id. None means that the method has no such field.
#
# The bare name is the key of a history row and of a ``responses`` override, so
# two served methods must not share one. ``Params`` is a method of three
# compiled services, so a new row can break that rule.
SERVED_METHODS: tuple[tuple[str, str, str | None], ...] = (
    ("cosmos.base.tendermint.v1beta1.Service", "GetLatestBlock", None),
    ("cosmos.base.tendermint.v1beta1.Service", "GetNodeInfo", None),
    ("cosmos.bank.v1beta1.Query", "AllBalances", "address"),
)


def request_id_fields(
    served: tuple[tuple[str, str, str | None], ...] = SERVED_METHODS,
) -> dict[str, str | None]:
    """Map the bare name of each served method to its request-id field.

    Raises ValueError when two served methods have the same bare name. This
    module calls it at import, so the simulator does not start with such a
    table.
    """
    fields: dict[str, str | None] = {}
    services: dict[str, str] = {}
    for service, method, id_field in served:
        if method in fields:
            raise ValueError(
                f"two served gRPC methods have the name {method!r}: {services[method]}/{method} and "
                f"{service}/{method}. The bare method name is the key of a history row and of a "
                "responses override, so it must be unique."
            )
        fields[method] = id_field
        services[method] = service
    return fields


_REQUEST_ID_FIELD = request_id_fields()


def _request_id(method: str, request: object | None) -> str | None:
    """The request id of one call: the value of the field that the method
    names. None when the method names no field, when the caller passed no
    request message, or when the value is empty."""
    id_field = _REQUEST_ID_FIELD.get(method)
    if id_field is None or request is None:
        return None
    value = getattr(request, id_field, None)
    return str(value) if value else None


class GrpcListener:
    def __init__(self, provider: Provider, endpoint: Endpoint) -> None:
        self.provider = provider
        self.endpoint = endpoint

    def plan(self, method: str, lava_headers: dict | None = None, request: object | None = None) -> GrpcPlan:
        entry = self.provider.log.record_arrival(
            self.endpoint.interface,
            self.endpoint.transport,
            self.endpoint.port,
            lava_headers=lava_headers or {},
        )
        scenario = self.provider.scenario.snapshot()
        verdict = fault_policy.decide(scenario, self.endpoint, self.provider)
        latency = scenario.get("latency_ms", 0)
        request_id = _request_id(method, request)

        def _finalize(status: str, latency_ms: int, with_request_id: bool = True) -> None:
            self.provider.log.finalize(
                entry,
                method=method,
                status=status,
                latency_ms=latency_ms,
                request_id=request_id if with_request_id else None,
            )

        # ── Provider-wide fault verdicts → status aborts ──
        if verdict.kind == "down":
            # A dead node does not read the request, so its row has no request
            # id. That is the rule of every interface.
            _finalize("down", 0, with_request_id=False)
            return GrpcPlan(action="abort", status_code="UNAVAILABLE", message="provider down")
        if verdict.kind == "hang":
            _finalize("hang", 0)
            return GrpcPlan(action="abort", status_code="CANCELLED", message="hang timeout", hang=True)
        if verdict.kind == "drop":
            _finalize("drop_connection", latency)
            return GrpcPlan(
                action="abort",
                status_code="UNAVAILABLE",
                message="connection dropped",
                drop_at=verdict.drop_at,
                latency_ms=latency,
            )
        if verdict.kind == "rate_limit":
            _finalize("rate_limit", latency)
            return GrpcPlan(
                action="abort",
                status_code="RESOURCE_EXHAUSTED",
                message="Too many requests",
                latency_ms=latency,
            )
        if verdict.kind == "error":
            _finalize("error", latency)
            return GrpcPlan(
                action="abort",
                status_code=_status_name(verdict.error_message, verdict.error_code),
                message=verdict.error_message,
                latency_ms=latency,
            )

        # ── Per-method error override → status abort ──
        responses = scenario.get("responses") or {}
        method_cfg = responses.get(method) or responses.get("default", {})
        if isinstance(method_cfg, dict):
            if "error_stub" in method_cfg:
                sc = _STATUS_BY_NAME.get(method_cfg["error_stub"], grpc.StatusCode.UNKNOWN)
                _finalize("error", latency)
                return GrpcPlan(
                    action="abort",
                    status_code=sc.name,
                    message=str(method_cfg.get("message", method_cfg["error_stub"])),
                    latency_ms=latency,
                )
            if "error" in method_cfg:
                err = method_cfg["error"]
                code = err.get("code", "")
                sc = (
                    _STATUS_BY_NAME.get(code if isinstance(code, str) else "")
                    or _STATUS_BY_VALUE.get(code if isinstance(code, int) else -1)
                    or grpc.StatusCode.UNKNOWN
                )
                _finalize("error", latency)
                return GrpcPlan(
                    action="abort",
                    status_code=sc.name,
                    message=err.get("message", "override"),
                    latency_ms=latency,
                )

        # ── Corruption → proto-level or abort ──
        corruption = scenario.get("corruption_mode") if self._targeted(scenario) else None
        # wrong_type aborts immediately by design: proto3 fields are typed, so
        # a wrong-typed value cannot be serialized into the response message
        # the way a JSON body can carry one. The INTERNAL abort is the closest
        # wire-visible stand-in for "the provider answered with garbage types".
        if corruption == "wrong_type":
            _finalize("error", latency)
            return GrpcPlan(
                action="abort",
                status_code="INTERNAL",
                message=f"wrong_type corruption on {scenario.get('missing_field') or 'response'}",
                latency_ms=latency,
            )
        if corruption in ("invalid_proto", "empty_response", "truncated", "null_body"):
            _finalize("error", latency)
            return GrpcPlan(
                action="abort",
                status_code="UNKNOWN",
                message=f"corruption: {corruption}",
                latency_ms=latency,
            )

        # ── Success — LavaChain builds the response DATA; the glue serializes it ──
        _status, data = chain_for(self.provider.pool.chain).build_success(
            {"method": method}, scenario, self.provider.quirks.snapshot(), self.endpoint.interface
        )
        _finalize("success", latency)
        return GrpcPlan(
            action="respond",
            grpc_method=method,
            data=data,
            corruption_mode="missing_field" if corruption == "missing_field" else None,
            missing_field=scenario.get("missing_field"),
            latency_ms=latency,
        )

    def _targeted(self, scenario: dict) -> bool:
        """Whether a scenario block applies to this endpoint.

        Delegates to fault_policy rather than repeating the rule. It used to
        repeat it, reading ``transports`` alone, so when ``ports`` was added
        every fault mode on this listener honoured it EXCEPT corruption — five
        one way and one the other, inside a single listener.
        """
        return fault_policy.targets(scenario, self.endpoint)
