"""gRPC listener — Cosmos gRPC over http2.

A gRPC call goes through the request flow of ``Listener.serve``, as a JSON-RPC,
a REST and a Tendermint RPC request do. ``GrpcListener`` fills the hooks. The
gRPC adapter in server.py performs the ServeResult that the flow returns: it
waits, and then it ends the call with a status or returns the reply message.

A reply is one of two things. A ``GrpcStatus`` is a status code with a text. A
``GrpcReply`` holds the data of the chain, and ``message_of`` builds the
protobuf message from it after the history row is finished.

This module owns every protobuf class of the simulator's gRPC side: the
servicer classes, their registration with the server and with reflection, the
table of the served methods, and the builders of the reply messages.

The rules that are special to gRPC, and the hook that holds each one:
- A served method can name one field of its request message as the request id
  (``SERVED_METHODS``). For AllBalances that field is ``address``. A method that
  names no field records no request id (``request_id``).
- A fault is a status: down -> UNAVAILABLE (``build_down``); hang -> CANCELLED
  after 30 seconds, drop -> UNAVAILABLE, rate_limit -> RESOURCE_EXHAUSTED,
  error -> the status that error_message names, or error_code as a number,
  else UNKNOWN (``build_fault``).
- A per-method error_stub or error override is a status, not a body. The chain
  returns it as data (``build_success``).
- A corruption acts on a reply message only: missing_field clears a field;
  wrong_type gives INTERNAL; invalid_proto, empty_response, truncated and
  null_body give UNKNOWN (``corrupt``). invalid_json has no meaning for a
  protobuf message: the control API refuses it for a provider that has only
  gRPC endpoints, and this listener does nothing with it.

Three rules are the same as on the HTTP interfaces, and the flow holds them:
- The row of a provider-wide ``down`` has the method ``"*"`` and no request id:
  a dead node does not read the request.
- A provider-wide ``down`` row and a ``hang`` row record latency 0, because the
  provider did not wait for the latency.
- The fault keys of a per-method override are merged into the scenario. The key
  of the entry is the bare method name.

One mode is not served here at all. ``port_closed`` is not a reply to a call:
the endpoint's server is stopped, so no call arrives. ``down`` and ``drop``
above are replies: both answer UNAVAILABLE with this module's own text, over a
connection that stays open. The serve loop in server.py performs
``port_closed`` (see ``provider_simulator/port_gate.py``); a call that arrives
while the mode is set came in before the port closed and is served like a call
on an open port.
"""

import datetime
from dataclasses import dataclass

import grpc
from google.protobuf import descriptor_pool

# Importing the package splices cosmos_pb2/ onto sys.path, so that the absolute
# imports of the generated stubs resolve. It must run before the ``from
# cosmos...`` imports below, so isort must not reorder them.
import cosmos_pb2  # noqa: F401  isort: split

from cosmos.bank.v1beta1 import query_pb2 as bank_query_pb2  # isort: skip
from cosmos.bank.v1beta1 import query_pb2_grpc as bank_query_pb2_grpc  # isort: skip
from cosmos.base.tendermint.v1beta1 import query_pb2, query_pb2_grpc  # isort: skip
from tendermint.types import block_pb2, types_pb2  # isort: skip

from provider_simulator import fault_policy
from provider_simulator.chains.lava import GRPC_LATEST_BLOCK, LAVA_SIM_CHAIN_ID
from provider_simulator.listeners.base import Listener, RawRequest, ServeResult

_STATUS_BY_NAME = {sc.name: sc for sc in grpc.StatusCode}
_STATUS_BY_VALUE = {sc.value[0]: sc for sc in grpc.StatusCode}


@dataclass
class GrpcStatus:
    """A reply that is a status: the name of a ``grpc.StatusCode`` and its text."""

    code: str
    text: str


@dataclass
class GrpcReply:
    """A reply that is a message: the served method and the data of the chain.
    The protobuf message is built from it after the history row is finished."""

    method: str
    data: dict


def _status_name(error_message: str, error_code: int) -> str:
    """Resolve the abort status name: the status named in error_message wins,
    then error_code as an integer status, else UNKNOWN."""
    sc = _STATUS_BY_NAME.get(error_message) or _STATUS_BY_VALUE.get(error_code)
    return (sc or grpc.StatusCode.UNKNOWN).name


def _status_of(code: object) -> str:
    """The status that a per-method error names: the name of a status, or the
    number of a status. Each other value gives UNKNOWN."""
    by_name = _STATUS_BY_NAME.get(code if isinstance(code, str) else "")
    by_number = _STATUS_BY_VALUE.get(code if isinstance(code, int) else -1)
    return (by_name or by_number or grpc.StatusCode.UNKNOWN).name


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


def check_servicers(
    servicers: dict[str, type],
    served: tuple[tuple[str, str, str | None], ...] | None = None,
) -> None:
    """Raise ValueError when the servicer classes and the table differ.

    ``servicers`` maps the full name of a service to the servicer class that
    the gRPC adapter registers for it. A class serves the public methods that
    it defines itself: a method that it only inherits from the generated base
    answers UNIMPLEMENTED. The gRPC adapter calls this before it starts a
    server, with the list of servicers that it then registers. So each served
    method has a row in ``SERVED_METHODS``, and the refusal of two methods of
    one name sees each served method.

    Raises ValueError also when a row names a request-id field that the request
    message of its method does not have. Such a row would lose the request id
    of each call, with no error.
    """
    table = SERVED_METHODS if served is None else served
    in_code = {
        (service, name)
        for service, servicer in servicers.items()
        for name, member in vars(servicer).items()
        if not name.startswith("_") and callable(member)
    }
    in_table = {(service, method) for service, method, _ in table}
    if in_code != in_table:
        with_no_row = sorted(f"{service}/{method}" for service, method in in_code - in_table)
        with_no_servicer = sorted(f"{service}/{method}" for service, method in in_table - in_code)
        raise ValueError(
            "the gRPC servicers and the table SERVED_METHODS name different methods. "
            f"Served with no row: {with_no_row}. Row with no servicer method: {with_no_servicer}."
        )
    for service, method, id_field in table:
        if id_field is None:
            continue
        try:
            message = descriptor_pool.Default().FindServiceByName(service).methods_by_name[method].input_type
        except KeyError as exc:
            raise ValueError(
                f"the request message of {service}/{method} is not loaded, so its request-id field "
                f"{id_field!r} cannot be compared with it. Import the stubs of the service first."
            ) from exc
        if id_field not in message.fields_by_name:
            raise ValueError(
                f"the row of {service}/{method} names the request-id field {id_field!r}, and the request "
                f"message {message.full_name} has no such field. Its fields: {sorted(message.fields_by_name)}."
            )


def _request_id(method: str, request: object | None) -> str | None:
    """The request id of one call: the value of the field that the method
    names. None when the method names no field, when the caller passed no
    request message, or when the value is empty."""
    id_field = _REQUEST_ID_FIELD.get(method)
    if id_field is None or request is None:
        return None
    value = getattr(request, id_field, None)
    return str(value) if value else None


class GrpcListener(Listener):
    """gRPC in the request flow of ``Listener.serve``. This class has no order
    of steps of its own. It fills the hooks, and each hook holds one place where
    gRPC differs from the HTTP interfaces."""

    def parse_request(self, request: RawRequest) -> dict:
        # The gRPC library parsed the call before the adapter saw it. The
        # adapter passes the method name in ``path`` and the request message in
        # ``message``.
        return {"method": request.path, "message": request.message}

    def build_down(self) -> ServeResult:
        return ServeResult(action="respond", body=GrpcStatus("UNAVAILABLE", "provider down"))

    def build_fault(self, verdict: fault_policy.Verdict, request: dict) -> ServeResult:
        if verdict.kind == "hang":
            return ServeResult(action="hang", body=GrpcStatus("CANCELLED", "hang timeout"))
        if verdict.kind == "drop":
            status = GrpcStatus("UNAVAILABLE", "connection dropped")
            return ServeResult(action="drop", drop_at=verdict.drop_at, body=status)
        if verdict.kind == "rate_limit":
            # The verdict also carries an HTTP status and a body text. gRPC has
            # no place for them.
            return ServeResult(action="respond", body=GrpcStatus("RESOURCE_EXHAUSTED", "Too many requests"))
        status = GrpcStatus(_status_name(verdict.error_message, verdict.error_code), verdict.error_message)
        return ServeResult(action="respond", body=status)

    def build_success(self, status: int, body: object) -> ServeResult:
        data = body if isinstance(body, dict) else {}
        error = data.get("error")
        if error is not None:
            # A per-method error of the chain is a status.
            return ServeResult(action="respond", body=GrpcStatus(_status_of(error.get("code")), error.get("message")))
        return ServeResult(action="respond", body=GrpcReply(str(data.get("grpc_method", "")), data))

    def corrupt(self, result: ServeResult, status_label: str, scenario: dict) -> str:
        if not isinstance(result.body, GrpcReply):
            # A corruption acts on a reply message only. A status stays as it is.
            return status_label
        corruption = scenario.get("corruption_mode")
        # wrong_type is a status by design: proto3 fields are typed, so a value
        # of a wrong type cannot be put into the reply message the way a JSON
        # body can carry one. INTERNAL is the closest thing that a caller can
        # read for "the provider answered with garbage types".
        if corruption == "wrong_type":
            field_name = scenario.get("missing_field") or "response"
            result.body = GrpcStatus("INTERNAL", f"wrong_type corruption on {field_name}")
            return "error"
        if corruption in ("invalid_proto", "empty_response", "truncated", "null_body"):
            result.body = GrpcStatus("UNKNOWN", f"corruption: {corruption}")
            return "error"
        if corruption == "missing_field":
            result.corruption_mode = "missing_field"
            result.missing_field = scenario.get("missing_field")
        # invalid_json does nothing here. The control API refuses it for a
        # provider that has only gRPC endpoints.
        return status_label

    def request_id(self, request: dict):
        return _request_id(request.get("method", ""), request.get("message"))

    def response_id(self, body: object):
        return None  # the data of the chain carries no request id


# ── The reply messages ────────────────────────────────────────────────────────
# Each builder makes the protobuf reply of one served method from the data of
# the chain. The adapter calls ``message_of`` after Listener.serve finished the
# history row of the call.


def _merged(data: dict) -> dict:
    # A per-method `responses` result override arrives as {"result": {...}};
    # its keys shadow the defaults of the builder.
    merged = dict(data)
    result = merged.pop("result", None)
    if isinstance(result, dict):
        merged.update(result)
    return merged


def build_latest_block(data: dict):
    merged = _merged(data)
    now = datetime.datetime.now(datetime.timezone.utc)
    header = types_pb2.Header(
        chain_id=merged.get("chain_id", LAVA_SIM_CHAIN_ID),
        height=merged.get("height", GRPC_LATEST_BLOCK),
    )
    header.time.seconds = int(now.timestamp())
    header.time.nanos = now.microsecond * 1000
    block = block_pb2.Block(header=header)
    block_id = types_pb2.BlockID(hash=b"\xab" * 32)
    return query_pb2.GetLatestBlockResponse(block_id=block_id, block=block)


def build_node_info(data: dict):
    merged = _merged(data)
    resp = query_pb2.GetNodeInfoResponse()
    resp.default_node_info.network = merged.get("network", LAVA_SIM_CHAIN_ID)
    resp.default_node_info.moniker = merged.get("moniker", "lava-sim-grpc-provider")
    resp.default_node_info.version = merged.get("version", "sim-1.0")
    resp.application_version.name = "lava-sim"
    resp.application_version.app_name = merged.get("app_name", "lava-sim-app")
    resp.application_version.version = merged.get("app_version", "sim-1.0")
    return resp


def build_all_balances(data: dict):
    # A `result` override is free JSON, and the history row of the call is
    # written before this reply is built. So this builder raises for no
    # shape: each item that is an object is a coin, with its denom and its
    # amount as text, and each other item is skipped. An error here would
    # leave a row that says success for a call that got no reply.
    def _text(value) -> str:
        return "" if value is None else str(value)

    merged = _merged(data)
    resp = bank_query_pb2.QueryAllBalancesResponse()
    balances = merged.get("balances", [])
    for coin in balances if isinstance(balances, list) else []:
        if isinstance(coin, dict):
            resp.balances.add(denom=_text(coin.get("denom")), amount=_text(coin.get("amount")))
    return resp


# The builder of the reply of each served method, by the bare method name.
_REPLY_BUILDERS = {
    "GetLatestBlock": build_latest_block,
    "GetNodeInfo": build_node_info,
    "AllBalances": build_all_balances,
}


def message_of(result: ServeResult):
    """Build the reply message of one call. ``result.body`` is a GrpcReply.

    A ``missing_field`` corruption clears one field of the message. A name that
    the message does not have clears nothing."""
    reply = result.body
    assert isinstance(reply, GrpcReply), reply
    response = _REPLY_BUILDERS[reply.method](reply.data)
    if result.corruption_mode == "missing_field" and result.missing_field:
        # proto3 fields are clearable; the receiver sees the field unset.
        if response.DESCRIPTOR.fields_by_name.get(result.missing_field):
            response.ClearField(result.missing_field)
    return response


# ── The servicers ─────────────────────────────────────────────────────────────


def servicers(perform) -> tuple:
    """The servicer classes of this simulator.

    ``perform`` is the coroutine function of the gRPC adapter:
    ``await perform(method, request, context)`` answers one call. Each row of
    the result holds the full name of a service, its servicer class, and the
    generated function that registers it. A class serves the methods that it
    defines itself. Each other method keeps the generated default, which
    answers UNIMPLEMENTED.

    The adapter gives this list to ``check_servicers`` before it starts a
    server, and to ``register`` for each server that it starts. So a servicer
    is not registered with no check."""

    class _Servicer(query_pb2_grpc.ServiceServicer):
        async def GetLatestBlock(self, request, context):
            return await perform("GetLatestBlock", request, context)

        async def GetNodeInfo(self, request, context):
            return await perform("GetNodeInfo", request, context)

    class _BankServicer(bank_query_pb2_grpc.QueryServicer):
        """One method of the bank service: AllBalances, whose ``address`` is
        the request id of the call."""

        async def AllBalances(self, request, context):
            return await perform("AllBalances", request, context)

    return (
        (
            query_pb2.DESCRIPTOR.services_by_name["Service"].full_name,
            _Servicer,
            query_pb2_grpc.add_ServiceServicer_to_server,
        ),
        (
            bank_query_pb2.DESCRIPTOR.services_by_name["Query"].full_name,
            _BankServicer,
            bank_query_pb2_grpc.add_QueryServicer_to_server,
        ),
    )


def register(server, table) -> None:
    """Register the servicers of ``table`` and the reflection service with one
    ``grpc.aio`` server."""
    from grpc_reflection.v1alpha import reflection

    for _, servicer, add_to_server in table:
        add_to_server(servicer(), server)
    # Server reflection lets grpcurl discover services without a proto bundle —
    # a dev/test convenience worth the negligible surface. The smart-router
    # does not read this list: it asks reflection for the symbol of a service,
    # which is found because the stubs are imported at the top of this module.
    # The list is what ``grpcurl list`` prints.
    service_names = (*(service for service, _, _ in table), reflection.SERVICE_NAME)
    reflection.enable_server_reflection(service_names, server)
