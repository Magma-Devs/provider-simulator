"""GrpcListener — the pure decision core (plan()) an async servicer glue performs.
No running gRPC server needed: plan() returns a GrpcPlan we assert on directly."""

from typing import NamedTuple

import pytest

# Splice cosmos_pb2 onto sys.path so the generated stubs resolve. It must run
# before the ``from cosmos...`` imports below, so isort must not reorder them.
import cosmos_pb2  # noqa: F401  isort: split

from cosmos.bank.v1beta1 import query_pb2 as bank_query_pb2  # isort: skip
from cosmos.base.tendermint.v1beta1 import query_pb2  # isort: skip

from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners.grpc import GrpcListener, check_servicers, request_id_fields

GRPC = Endpoint("grpc", "http2", 18548)


def _listener():
    provider = Pool(name="lava-sim-grpc", chain="lava").add_provider("1", [GRPC])
    return GrpcListener(provider, GRPC), provider


def _upd(listener, cfg):
    listener.provider.scenario.update(cfg)


def test_success_plan_carries_latest_block_data():
    listener, provider = _listener()
    plan = listener.plan("GetLatestBlock")
    assert plan.action == "respond"
    assert plan.grpc_method == "GetLatestBlock"
    assert plan.data["height"] == 25_000_000
    assert plan.data["chain_id"] == "lava-sim"
    hist = provider.log.get_history()[0]
    assert hist["status"] == "success"
    assert hist["method"] == "GetLatestBlock"


def test_node_info_success():
    listener, _ = _listener()
    plan = listener.plan("GetNodeInfo")
    assert plan.action == "respond"
    assert plan.data["network"] == "lava-sim"


def test_blocks_behind_shifts_head():
    listener, _ = _listener()
    _upd(listener, {"blocks_behind": 7})
    plan = listener.plan("GetLatestBlock")
    assert plan.data["height"] == 25_000_000 - 7


def test_down_aborts_unavailable_and_records_method_not_star():
    listener, provider = _listener()
    _upd(listener, {"mode": "down"})
    plan = listener.plan("GetLatestBlock")
    assert plan.action == "abort"
    assert plan.status_code == "UNAVAILABLE"
    hist = provider.log.get_history()[0]
    assert hist["status"] == "down"
    assert hist["method"] == "GetLatestBlock"  # gRPC always knows the method


def test_hang_aborts_cancelled_with_hang_flag():
    listener, _ = _listener()
    _upd(listener, {"mode": "hang"})
    plan = listener.plan("GetNodeInfo")
    assert plan.status_code == "CANCELLED"
    assert plan.hang is True


def test_rate_limit_resource_exhausted():
    listener, _ = _listener()
    _upd(listener, {"mode": "rate_limit"})
    assert listener.plan("GetLatestBlock").status_code == "RESOURCE_EXHAUSTED"


def test_error_maps_status_name_from_message():
    listener, _ = _listener()
    _upd(listener, {"mode": "error", "error_message": "NOT_FOUND"})
    assert listener.plan("GetLatestBlock").status_code == "NOT_FOUND"


def test_error_defaults_to_unknown():
    listener, _ = _listener()
    _upd(listener, {"mode": "error", "error_message": "not a status", "error_code": -1})
    assert listener.plan("GetLatestBlock").status_code == "UNKNOWN"


def test_per_method_error_stub_aborts():
    listener, _ = _listener()
    _upd(listener, {"responses": {"GetLatestBlock": {"error_stub": "NOT_FOUND"}}})
    plan = listener.plan("GetLatestBlock")
    assert plan.action == "abort"
    assert plan.status_code == "NOT_FOUND"


def test_wrong_type_corruption_internal_abort():
    listener, _ = _listener()
    _upd(listener, {"corruption_mode": "wrong_type"})
    assert listener.plan("GetLatestBlock").status_code == "INTERNAL"


def test_invalid_proto_corruption_unknown_abort():
    listener, _ = _listener()
    _upd(listener, {"corruption_mode": "invalid_proto"})
    assert listener.plan("GetLatestBlock").status_code == "UNKNOWN"


def test_null_body_corruption_unknown_abort():
    # A whole-body JSON null has no gRPC shape; falling through to a clean
    # success would be a fault that arms with a 200 and does nothing.
    listener, _ = _listener()
    _upd(listener, {"corruption_mode": "null_body"})
    assert listener.plan("GetLatestBlock").status_code == "UNKNOWN"


def test_missing_field_corruption_stays_respond():
    listener, _ = _listener()
    _upd(listener, {"corruption_mode": "missing_field", "missing_field": "block"})
    plan = listener.plan("GetLatestBlock")
    assert plan.action == "respond"
    assert plan.corruption_mode == "missing_field"
    assert plan.missing_field == "block"


# --- The request id ---------------------------------------------------------
# gRPC has no id of its own. A served method can name one field of its request
# message as the request id. For AllBalances that field is ``address``.


def _all_balances(address=""):
    return bank_query_pb2.QueryAllBalancesRequest(address=address)


def test_all_balances_row_holds_the_address_as_the_request_id():
    listener, provider = _listener()
    plan = listener.plan("AllBalances", request=_all_balances("lava1-probe-a"))
    assert plan.action == "respond"
    assert plan.grpc_method == "AllBalances"
    assert plan.data["balances"] == [{"denom": "ulava", "amount": "1000000"}]
    hist = provider.log.get_history()[0]
    assert hist["method"] == "AllBalances"
    assert hist["status"] == "success"
    assert hist["request_id"] == "lava1-probe-a"


def test_get_latest_block_row_holds_no_request_id():
    listener, provider = _listener()
    listener.plan("GetLatestBlock", request=query_pb2.GetLatestBlockRequest())
    assert provider.log.get_history()[0]["request_id"] is None


def test_an_empty_address_is_no_request_id():
    listener, provider = _listener()
    listener.plan("AllBalances", request=_all_balances(""))
    assert provider.log.get_history()[0]["request_id"] is None


def test_a_call_with_no_request_message_has_no_request_id():
    # plan() keeps working for a caller that passes the method name only.
    listener, provider = _listener()
    listener.plan("AllBalances")
    assert provider.log.get_history()[0]["request_id"] is None


@pytest.mark.parametrize(
    "scenario, status",
    [
        ({"mode": "error", "error_message": "NOT_FOUND"}, "error"),
        ({"mode": "rate_limit"}, "rate_limit"),
        ({"mode": "hang"}, "hang"),
        ({"mode": "drop_connection"}, "drop_connection"),
        ({"responses": {"AllBalances": {"error_stub": "NOT_FOUND"}}}, "error"),
        ({"responses": {"AllBalances": {"error": {"code": "NOT_FOUND", "message": "gone"}}}}, "error"),
        ({"corruption_mode": "wrong_type"}, "error"),
        ({"corruption_mode": "invalid_proto"}, "error"),
    ],
)
def test_a_fault_row_of_all_balances_keeps_the_request_id(scenario, status):
    listener, provider = _listener()
    _upd(listener, scenario)
    plan = listener.plan("AllBalances", request=_all_balances("lava1-probe-f"))
    assert plan.action == "abort"
    hist = provider.log.get_history()[0]
    assert (hist["status"], hist["request_id"]) == (status, "lava1-probe-f")


def test_a_provider_wide_down_row_has_no_request_id():
    # A dead node does not read the request. The row keeps the method, as every
    # gRPC row does, and it has no request id, as on JSON-RPC, REST and
    # Tendermint RPC.
    listener, provider = _listener()
    _upd(listener, {"mode": "down"})
    listener.plan("AllBalances", request=_all_balances("never-read"))
    hist = provider.log.get_history()[0]
    assert (hist["status"], hist["method"], hist["request_id"]) == ("down", "AllBalances", None)


def test_the_served_methods_and_their_request_id_fields():
    assert request_id_fields() == {"GetLatestBlock": None, "GetNodeInfo": None, "AllBalances": "address"}


def test_two_served_methods_of_one_name_are_refused():
    # ``Params`` is a method of three compiled services. The bare name is the
    # key of a history row and of a ``responses`` override, so it must be unique.
    served = (
        ("cosmos.auth.v1beta1.Query", "Params", None),
        ("cosmos.bank.v1beta1.Query", "Params", None),
    )
    with pytest.raises(ValueError, match="cosmos.auth.v1beta1.Query/Params.*cosmos.bank.v1beta1.Query/Params"):
        request_id_fields(served)


# --- The table and the servicers --------------------------------------------
# The gRPC adapter in server.py writes its servicer classes by hand. The check
# below ties them to the table of served methods: a method that a servicer
# serves must have a row, and a row must have a servicer method. Without the
# check, a served method with no row records no request id, and the refusal of
# two methods of one name does not see it.

_BANK = "cosmos.bank.v1beta1.Query"


class _GeneratedBankBase:
    """Stands for the generated servicer base: each method answers UNIMPLEMENTED."""

    def AllBalances(self, request, context):
        raise NotImplementedError

    def Params(self, request, context):
        raise NotImplementedError


def test_servicers_that_match_the_table_are_accepted():
    class Bank(_GeneratedBankBase):
        def _perform(self):  # a private helper is not a served method
            return None

        async def AllBalances(self, request, context):
            return None

    check_servicers({_BANK: Bank}, served=((_BANK, "AllBalances", "address"),))


def test_a_servicer_method_with_no_row_is_refused():
    class Bank(_GeneratedBankBase):
        async def AllBalances(self, request, context):
            return None

        async def Params(self, request, context):
            return None

    with pytest.raises(ValueError, match=r"Served with no row: \['cosmos.bank.v1beta1.Query/Params'\]"):
        check_servicers({_BANK: Bank}, served=((_BANK, "AllBalances", "address"),))


def test_a_row_with_no_servicer_method_is_refused():
    class Bank(_GeneratedBankBase):
        pass

    with pytest.raises(ValueError, match=r"Row with no servicer method: \['cosmos.bank.v1beta1.Query/AllBalances'\]"):
        check_servicers({_BANK: Bank}, served=((_BANK, "AllBalances", "address"),))


def test_a_row_whose_request_id_field_is_not_in_the_request_message_is_refused():
    # ``adress`` is a slip for ``address``. Without this check the endpoint
    # starts, and each AllBalances row has no request id, with no error.
    class Bank(_GeneratedBankBase):
        async def AllBalances(self, request, context):
            return None

    with pytest.raises(ValueError, match=r"'adress'.*cosmos\.bank\.v1beta1\.QueryAllBalancesRequest"):
        check_servicers({_BANK: Bank}, served=((_BANK, "AllBalances", "adress"),))


def test_a_request_id_row_of_a_service_with_no_loaded_stubs_is_refused():
    # The field cannot be compared with a request message that is not loaded.
    class Other:
        async def Ping(self, request, context):
            return None

    with pytest.raises(ValueError, match=r"no\.such\.Service/Ping"):
        check_servicers({"no.such.Service": Other}, served=(("no.such.Service", "Ping", "id"),))


# --- The grid: what plan() decides today ------------------------------------
# A gRPC call is answered through GrpcListener.plan. The tables below record
# what plan() decides for each mode, each corruption mode and each per-method
# override: the status that the caller gets, the text of that status, and the
# history row of the call. Each expected value is written out by hand.
#
# ``_decide`` is the one function of this block that calls plan() and reads a
# GrpcPlan. A change of the listener that keeps its behaviour changes
# ``_decide``, and it changes no expected value.

_LATEST_BLOCK = {"grpc_method": "GetLatestBlock", "height": 25_000_000, "chain_id": "lava-sim"}
_NODE_INFO = {
    "grpc_method": "GetNodeInfo",
    "network": "lava-sim",
    "moniker": "lava-sim-grpc-provider",
    "version": "sim-1.0",
    "app_name": "lava-sim-app",
    "app_version": "sim-1.0",
}
# GRPC, the endpoint of ``_listener``, is at port 18548. This port is not its port.
_ANOTHER_PORT = 18549


class _Decision(NamedTuple):
    code: str  # the status that the caller gets; "OK" when the caller gets a reply message
    text: str  # the text of the status; "" with a reply message
    wait_ms: int  # how long the adapter waits before it answers
    hangs: bool  # the adapter waits 30 seconds, and not wait_ms
    drop_at: str | None  # set when the status stands for a dropped connection
    reply: dict | None  # the data of the reply message; None with a status
    clears: str | None  # the field that the adapter clears in the reply message


def _decide(listener, method="GetLatestBlock", request=None, lava_headers=None):
    plan = listener.plan(method, lava_headers, request)
    if plan.action == "abort":
        return _Decision(plan.status_code, plan.message, plan.latency_ms, plan.hang, plan.drop_at, None, None)
    assert plan.action == "respond", plan.action
    clears = plan.missing_field if plan.corruption_mode == "missing_field" else None
    return _Decision("OK", "", plan.latency_ms, False, None, plan.data, clears)


def _status(code, text, *, hangs=False, drop_at=None):
    """The caller gets a status, with no wait."""
    return _Decision(code, text, 0, hangs, drop_at, None, None)


def _reply(data, *, clears=None):
    """The caller gets a reply message, with no wait."""
    return _Decision("OK", "", 0, False, None, data, clears)


def _override(cfg, method="GetLatestBlock"):
    """A scenario with one per-method override."""
    return {"responses": {method: cfg}}


def _row(provider):
    rows = provider.log.get_history()
    assert len(rows) == 1, f"expected one history row, got {len(rows)}"
    return rows[0]["method"], rows[0]["status"], rows[0]["latency_ms"], rows[0]["request_id"]


@pytest.mark.parametrize(
    "scenario, want, want_row_status",
    [
        # Each mode. A fault is a status, and each status has its own text.
        pytest.param({}, _reply(_LATEST_BLOCK), "success", id="success"),
        pytest.param({"mode": "down"}, _status("UNAVAILABLE", "provider down"), "down", id="down"),
        pytest.param({"mode": "hang"}, _status("CANCELLED", "hang timeout", hangs=True), "hang", id="hang"),
        pytest.param(
            {"mode": "drop_connection"},
            _status("UNAVAILABLE", "connection dropped", drop_at="before_headers"),
            "drop_connection",
            id="drop-before-headers-is-the-default",
        ),
        pytest.param(
            {"mode": "drop_connection", "drop_at": "after_headers"},
            _status("UNAVAILABLE", "connection dropped", drop_at="after_headers"),
            "drop_connection",
            id="drop-after-headers",
        ),
        pytest.param(
            {"mode": "drop_connection", "drop_at": "mid_body"},
            _status("UNAVAILABLE", "connection dropped", drop_at="mid_body"),
            "drop_connection",
            id="drop-mid-body",
        ),
        pytest.param(
            {"mode": "rate_limit"}, _status("RESOURCE_EXHAUSTED", "Too many requests"), "rate_limit", id="rate-limit"
        ),
        pytest.param(
            {"mode": "rate_limit", "rate_limit_body": "slow down"},
            _status("RESOURCE_EXHAUSTED", "Too many requests"),
            "rate_limit",
            id="rate-limit-body-does-not-change-the-text",
        ),
        pytest.param({"mode": "error"}, _status("UNKNOWN", "Internal error"), "error", id="error-with-no-status-named"),
        pytest.param(
            {"mode": "error", "error_message": "NOT_FOUND"},
            _status("NOT_FOUND", "NOT_FOUND"),
            "error",
            id="error-message-names-the-status",
        ),
        pytest.param(
            {"mode": "error", "error_message": "no status has this name", "error_code": 5},
            _status("NOT_FOUND", "no status has this name"),
            "error",
            id="error-code-is-the-number-of-the-status",
        ),
        pytest.param(
            {"mode": "error", "error_message": "ABORTED", "error_code": 5},
            _status("ABORTED", "ABORTED"),
            "error",
            id="error-message-wins-over-error-code",
        ),
        pytest.param(
            {"mode": "error", "error_message": "no status has this name", "error_code": -1},
            _status("UNKNOWN", "no status has this name"),
            "error",
            id="error-with-no-status-in-message-or-code",
        ),
        pytest.param(
            {"mode": "error", "error_message": "NOT_FOUND", "http_status": 503},
            _status("NOT_FOUND", "NOT_FOUND"),
            "error",
            id="http-status-does-not-change-the-status",
        ),
        pytest.param({"error_probability": 1.0}, _status("UNKNOWN", "Internal error"), "error", id="error-probability"),
        # Each per-method override. An error override is a status, and a result
        # override is the data of the reply message.
        pytest.param(
            _override({"error_stub": "NOT_FOUND"}), _status("NOT_FOUND", "NOT_FOUND"), "error", id="error-stub"
        ),
        pytest.param(
            _override({"error_stub": "NOT_FOUND", "message": "the block is gone"}),
            _status("NOT_FOUND", "the block is gone"),
            "error",
            id="error-stub-with-a-message",
        ),
        pytest.param(
            _override({"error_stub": "revert"}),
            _status("UNKNOWN", "revert"),
            "error",
            id="error-stub-whose-name-is-no-status",
        ),
        pytest.param(
            {"responses": {"default": {"error_stub": "ABORTED"}}},
            _status("ABORTED", "ABORTED"),
            "error",
            id="error-stub-in-the-default-entry",
        ),
        pytest.param(
            {"responses": {"GetLatestBlock": {"error_stub": "NOT_FOUND"}, "default": {"error_stub": "ABORTED"}}},
            _status("NOT_FOUND", "NOT_FOUND"),
            "error",
            id="the-entry-of-the-method-wins-over-the-default-entry",
        ),
        pytest.param(
            _override({"error_stub": "ABORTED"}, method="GetNodeInfo"),
            _reply(_LATEST_BLOCK),
            "success",
            id="an-override-of-another-method-does-not-apply",
        ),
        pytest.param(
            _override({"error": {"code": "ABORTED", "message": "from the override"}}),
            _status("ABORTED", "from the override"),
            "error",
            id="error-override-with-the-name-of-a-status",
        ),
        pytest.param(
            _override({"error": {"code": 7, "message": "by number"}}),
            _status("PERMISSION_DENIED", "by number"),
            "error",
            id="error-override-with-the-number-of-a-status",
        ),
        pytest.param(
            _override({"error": {"code": "no status has this name"}}),
            _status("UNKNOWN", "override"),
            "error",
            id="error-override-whose-code-is-no-status",
        ),
        pytest.param(
            _override({"error": {}}), _status("UNKNOWN", "override"), "error", id="error-override-that-is-empty"
        ),
        pytest.param(
            _override({"error_stub": "NOT_FOUND", "error": {"code": "ABORTED", "message": "from the override"}}),
            _status("NOT_FOUND", "NOT_FOUND"),
            "error",
            id="error-stub-wins-over-an-error-override",
        ),
        pytest.param(
            _override({"error_stub": "NOT_FOUND", "result": {"height": 7}}),
            _status("NOT_FOUND", "NOT_FOUND"),
            "error",
            id="error-stub-wins-over-a-result-override",
        ),
        pytest.param(
            _override({"result": {"height": 7}}),
            _reply({"grpc_method": "GetLatestBlock", "result": {"height": 7}}),
            "success",
            id="result-override",
        ),
        pytest.param(
            {"responses": {"default": {"result": {"height": 9}}}},
            _reply({"grpc_method": "GetLatestBlock", "result": {"height": 9}}),
            "success",
            id="result-override-in-the-default-entry",
        ),
        # A fault key in a per-method override is not read on gRPC.
        pytest.param(
            _override({"mode": "down"}), _reply(_LATEST_BLOCK), "success", id="per-method-mode-down-is-not-read"
        ),
        pytest.param(
            _override({"mode": "rate_limit"}),
            _reply(_LATEST_BLOCK),
            "success",
            id="per-method-mode-rate-limit-is-not-read",
        ),
        pytest.param(
            _override({"latency_ms": 700}), _reply(_LATEST_BLOCK), "success", id="per-method-latency-is-not-read"
        ),
        pytest.param(
            _override({"error_probability": 1.0}),
            _reply(_LATEST_BLOCK),
            "success",
            id="per-method-error-probability-is-not-read",
        ),
        pytest.param(
            {"mode": "down", **_override({"mode": "success"})},
            _status("UNAVAILABLE", "provider down"),
            "down",
            id="per-method-mode-success-does-not-lift-a-down",
        ),
        # Each corruption mode. Five of them turn the reply message into a
        # status, and the row then says error. One clears a field. One does
        # nothing on gRPC.
        pytest.param(
            {"corruption_mode": "wrong_type"},
            _status("INTERNAL", "wrong_type corruption on response"),
            "error",
            id="wrong-type",
        ),
        pytest.param(
            {"corruption_mode": "wrong_type", "missing_field": "block"},
            _status("INTERNAL", "wrong_type corruption on block"),
            "error",
            id="wrong-type-names-the-field",
        ),
        pytest.param(
            {"corruption_mode": "invalid_proto"},
            _status("UNKNOWN", "corruption: invalid_proto"),
            "error",
            id="invalid-proto",
        ),
        pytest.param(
            {"corruption_mode": "empty_response"},
            _status("UNKNOWN", "corruption: empty_response"),
            "error",
            id="empty-response",
        ),
        pytest.param(
            {"corruption_mode": "truncated"}, _status("UNKNOWN", "corruption: truncated"), "error", id="truncated"
        ),
        pytest.param(
            {"corruption_mode": "null_body"}, _status("UNKNOWN", "corruption: null_body"), "error", id="null-body"
        ),
        pytest.param(
            {"corruption_mode": "missing_field", "missing_field": "block"},
            _reply(_LATEST_BLOCK, clears="block"),
            "success",
            id="missing-field-clears-the-field",
        ),
        pytest.param(
            {"corruption_mode": "missing_field"},
            _reply(_LATEST_BLOCK),
            "success",
            id="missing-field-with-no-field-clears-nothing",
        ),
        pytest.param(
            {"missing_field": "block"},
            _reply(_LATEST_BLOCK),
            "success",
            id="a-field-with-no-corruption-mode-clears-nothing",
        ),
        pytest.param(
            {"corruption_mode": "invalid_json"}, _reply(_LATEST_BLOCK), "success", id="invalid-json-does-nothing"
        ),
        # A fault with a corruption. A corruption acts on a reply message only,
        # so a status stays as it is.
        pytest.param(
            {"mode": "rate_limit", "corruption_mode": "wrong_type"},
            _status("RESOURCE_EXHAUSTED", "Too many requests"),
            "rate_limit",
            id="rate-limit-with-a-corruption-stays-rate-limit",
        ),
        pytest.param(
            {"mode": "error", "error_message": "NOT_FOUND", "corruption_mode": "invalid_proto"},
            _status("NOT_FOUND", "NOT_FOUND"),
            "error",
            id="error-with-a-corruption-stays-the-error",
        ),
        pytest.param(
            {"mode": "down", "corruption_mode": "truncated"},
            _status("UNAVAILABLE", "provider down"),
            "down",
            id="down-with-a-corruption-stays-down",
        ),
        pytest.param(
            {**_override({"error_stub": "ABORTED"}), "corruption_mode": "wrong_type"},
            _status("ABORTED", "ABORTED"),
            "error",
            id="error-stub-with-a-corruption-stays-the-stub",
        ),
        pytest.param(
            {**_override({"result": {"height": 7}}), "corruption_mode": "wrong_type"},
            _status("INTERNAL", "wrong_type corruption on response"),
            "error",
            id="result-override-with-a-corruption-is-corrupted",
        ),
        # The transports filter and the ports filter. A mode and a corruption
        # reach the endpoint only when both filters name it.
        pytest.param(
            {"mode": "down", "transports": ["http2"]},
            _status("UNAVAILABLE", "provider down"),
            "down",
            id="mode-with-a-transports-filter-that-names-the-endpoint",
        ),
        pytest.param(
            {"mode": "down", "transports": ["ws"]},
            _reply(_LATEST_BLOCK),
            "success",
            id="mode-with-a-transports-filter-that-does-not-name-it",
        ),
        pytest.param(
            {"mode": "down", "ports": [18548]},
            _status("UNAVAILABLE", "provider down"),
            "down",
            id="mode-with-a-ports-filter-that-names-the-endpoint",
        ),
        pytest.param(
            {"mode": "down", "ports": [_ANOTHER_PORT]},
            _reply(_LATEST_BLOCK),
            "success",
            id="mode-with-a-ports-filter-that-does-not-name-it",
        ),
        pytest.param(
            {"mode": "rate_limit", "transports": ["http2"], "ports": [_ANOTHER_PORT]},
            _reply(_LATEST_BLOCK),
            "success",
            id="mode-with-one-filter-that-names-the-endpoint-and-one-that-does-not",
        ),
        pytest.param(
            {"corruption_mode": "wrong_type", "transports": ["http"]},
            _reply(_LATEST_BLOCK),
            "success",
            id="corruption-with-a-transports-filter-that-does-not-name-it",
        ),
        pytest.param(
            {"corruption_mode": "wrong_type", "ports": [_ANOTHER_PORT]},
            _reply(_LATEST_BLOCK),
            "success",
            id="corruption-with-a-ports-filter-that-does-not-name-it",
        ),
        pytest.param(
            {"corruption_mode": "missing_field", "missing_field": "block", "transports": ["ws"]},
            _reply(_LATEST_BLOCK),
            "success",
            id="missing-field-with-a-filter-that-does-not-name-it-clears-nothing",
        ),
        # A per-method override is not a fault of the endpoint: the filters do
        # not hold it back.
        pytest.param(
            {**_override({"error_stub": "NOT_FOUND"}), "transports": ["ws"]},
            _status("NOT_FOUND", "NOT_FOUND"),
            "error",
            id="error-stub-with-a-filter-that-does-not-name-the-endpoint",
        ),
        pytest.param(
            {**_override({"result": {"height": 7}}), "ports": [_ANOTHER_PORT]},
            _reply({"grpc_method": "GetLatestBlock", "result": {"height": 7}}),
            "success",
            id="result-override-with-a-filter-that-does-not-name-the-endpoint",
        ),
    ],
)
def test_what_plan_decides_for_one_call(scenario, want, want_row_status):
    listener, provider = _listener()
    _upd(listener, scenario)
    assert _decide(listener) == want
    assert _row(provider) == ("GetLatestBlock", want_row_status, 0, None)


def test_get_node_info_has_its_own_reply_and_its_own_row():
    listener, provider = _listener()
    assert _decide(listener, "GetNodeInfo") == _reply(_NODE_INFO)
    assert _row(provider) == ("GetNodeInfo", "success", 0, None)


@pytest.mark.parametrize(
    "scenario, want_wait_ms, want_row_latency_ms",
    [
        pytest.param({}, 250, 250, id="success"),
        # A down provider and a hung provider do not wait for the latency, and
        # their rows say 0.
        pytest.param({"mode": "down"}, 0, 0, id="down"),
        pytest.param({"mode": "hang"}, 0, 0, id="hang"),
        pytest.param({"mode": "drop_connection"}, 250, 250, id="drop-connection"),
        pytest.param({"mode": "rate_limit"}, 250, 250, id="rate-limit"),
        pytest.param({"mode": "error"}, 250, 250, id="error"),
        pytest.param(_override({"error_stub": "NOT_FOUND"}), 250, 250, id="error-stub"),
        pytest.param(_override({"error": {"code": "ABORTED"}}), 250, 250, id="error-override"),
        pytest.param(_override({"result": {"height": 7}}), 250, 250, id="result-override"),
        pytest.param({"corruption_mode": "wrong_type"}, 250, 250, id="wrong-type"),
        pytest.param({"corruption_mode": "invalid_proto"}, 250, 250, id="invalid-proto"),
        pytest.param({"corruption_mode": "missing_field", "missing_field": "block"}, 250, 250, id="missing-field"),
    ],
)
def test_which_calls_wait_for_latency_ms(scenario, want_wait_ms, want_row_latency_ms):
    listener, provider = _listener()
    _upd(listener, {**scenario, "latency_ms": 250})
    assert _decide(listener).wait_ms == want_wait_ms
    assert _row(provider)[2] == want_row_latency_ms


def test_the_row_is_complete_when_plan_returns():
    # The adapter waits for the latency after plan() returns. So a reader of the
    # history finds the method, the status and the latency of a call that the
    # provider still holds.
    listener, provider = _listener()
    _upd(listener, {"latency_ms": 250})
    _decide(listener)
    assert _row(provider) == ("GetLatestBlock", "success", 250, None)
    assert provider.log.stats()["calls_by_status"] == {"success": 1}


def test_the_row_names_the_endpoint_and_keeps_the_lava_headers():
    listener, provider = _listener()
    _decide(listener, lava_headers={"lava-guid": "guid-1"})
    row = provider.log.get_history()[0]
    assert (row["pool"], row["pid"]) == ("lava-sim-grpc", "1")
    assert (row["interface"], row["transport"], row["port"]) == ("grpc", "http2", 18548)
    assert row["lava_headers"] == {"lava-guid": "guid-1"}


def test_fail_first_n_gives_the_mode_to_the_first_calls_only():
    listener, provider = _listener()
    _upd(listener, {"mode": "down", "fail_first_n": 3, "then_mode": "success"})
    assert [_decide(listener).code for _ in range(5)] == ["UNAVAILABLE", "UNAVAILABLE", "UNAVAILABLE", "OK", "OK"]
    assert [row["status"] for row in provider.log.get_history()] == ["down", "down", "down", "success", "success"]


def test_then_mode_is_the_mode_after_the_first_calls():
    listener, provider = _listener()
    _upd(listener, {"mode": "error", "fail_first_n": 1, "then_mode": "rate_limit"})
    assert [_decide(listener).code for _ in range(3)] == ["UNKNOWN", "RESOURCE_EXHAUSTED", "RESOURCE_EXHAUSTED"]
    assert [row["status"] for row in provider.log.get_history()] == ["error", "rate_limit", "rate_limit"]


def test_a_filter_that_does_not_name_the_endpoint_does_not_use_up_fail_first_n():
    listener, provider = _listener()
    _upd(listener, {"mode": "down", "fail_first_n": 1, "then_mode": "rate_limit", "transports": ["ws"]})
    assert [_decide(listener).code for _ in range(3)] == ["OK", "OK", "OK"]
