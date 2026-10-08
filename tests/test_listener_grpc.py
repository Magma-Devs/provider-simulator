"""GrpcListener — gRPC in the request flow of Listener.serve.
No running gRPC server is needed: serve() returns a ServeResult, and its body is
a GrpcStatus or a GrpcReply."""

from typing import NamedTuple

import pytest

# Splice cosmos_pb2 onto sys.path so the generated stubs resolve. It must run
# before the ``from cosmos...`` imports below, so isort must not reorder them.
import cosmos_pb2  # noqa: F401  isort: split

from cosmos.bank.v1beta1 import query_pb2 as bank_query_pb2  # isort: skip
from cosmos.base.tendermint.v1beta1 import query_pb2  # isort: skip

from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners import Listener, RawRequest
from provider_simulator.listeners.grpc import GrpcListener, GrpcReply, GrpcStatus, check_servicers, request_id_fields

GRPC = Endpoint("grpc", "http2", 18548)


def _listener():
    provider = Pool(name="lava-sim-grpc", chain="lava").add_provider("1", [GRPC])
    return GrpcListener(provider, GRPC), provider


def _upd(listener, cfg):
    listener.provider.scenario.update(cfg)


def _serve(listener, method="GetLatestBlock", request=None, lava_headers=None):
    """One call through the request flow, as the gRPC adapter makes it."""
    return listener.serve(RawRequest(path=method, headers=lava_headers or {}, message=request))


def test_success_carries_the_data_of_the_latest_block():
    listener, provider = _listener()
    result = _serve(listener, "GetLatestBlock")
    assert result.action == "respond"
    assert isinstance(result.body, GrpcReply)
    assert result.body.method == "GetLatestBlock"
    assert result.body.data["height"] == 25_000_000
    assert result.body.data["chain_id"] == "lava-sim"
    hist = provider.log.get_history()[0]
    assert hist["status"] == "success"
    assert hist["method"] == "GetLatestBlock"


def test_node_info_success():
    listener, _ = _listener()
    result = _serve(listener, "GetNodeInfo")
    assert result.action == "respond"
    assert result.body.data["network"] == "lava-sim"


def test_blocks_behind_shifts_head():
    listener, _ = _listener()
    _upd(listener, {"blocks_behind": 7})
    assert _serve(listener, "GetLatestBlock").body.data["height"] == 25_000_000 - 7


def test_down_is_unavailable_and_its_row_names_no_method():
    listener, provider = _listener()
    _upd(listener, {"mode": "down"})
    result = _serve(listener, "GetLatestBlock")
    assert result.body == GrpcStatus("UNAVAILABLE", "provider down")
    hist = provider.log.get_history()[0]
    assert hist["status"] == "down"
    assert hist["method"] == "*"  # a dead node does not read the request


def test_hang_is_cancelled_with_the_action_hang():
    listener, _ = _listener()
    _upd(listener, {"mode": "hang"})
    result = _serve(listener, "GetNodeInfo")
    assert result.body.code == "CANCELLED"
    assert result.action == "hang"


def test_rate_limit_resource_exhausted():
    listener, _ = _listener()
    _upd(listener, {"mode": "rate_limit"})
    assert _serve(listener, "GetLatestBlock").body.code == "RESOURCE_EXHAUSTED"


def test_error_maps_status_name_from_message():
    listener, _ = _listener()
    _upd(listener, {"mode": "error", "error_message": "NOT_FOUND"})
    assert _serve(listener, "GetLatestBlock").body.code == "NOT_FOUND"


def test_error_defaults_to_unknown():
    listener, _ = _listener()
    _upd(listener, {"mode": "error", "error_message": "not a status", "error_code": -1})
    assert _serve(listener, "GetLatestBlock").body.code == "UNKNOWN"


def test_per_method_error_stub_is_a_status():
    listener, _ = _listener()
    _upd(listener, {"responses": {"GetLatestBlock": {"error_stub": "NOT_FOUND"}}})
    result = _serve(listener, "GetLatestBlock")
    assert isinstance(result.body, GrpcStatus)
    assert result.body.code == "NOT_FOUND"


def test_wrong_type_corruption_is_internal():
    listener, _ = _listener()
    _upd(listener, {"corruption_mode": "wrong_type"})
    assert _serve(listener, "GetLatestBlock").body.code == "INTERNAL"


def test_invalid_proto_corruption_is_unknown():
    listener, _ = _listener()
    _upd(listener, {"corruption_mode": "invalid_proto"})
    assert _serve(listener, "GetLatestBlock").body.code == "UNKNOWN"


def test_null_body_corruption_is_unknown():
    # A whole-body JSON null has no gRPC shape; falling through to a clean
    # success would be a fault that arms with a 200 and does nothing.
    listener, _ = _listener()
    _upd(listener, {"corruption_mode": "null_body"})
    assert _serve(listener, "GetLatestBlock").body.code == "UNKNOWN"


def test_missing_field_corruption_stays_a_reply():
    listener, _ = _listener()
    _upd(listener, {"corruption_mode": "missing_field", "missing_field": "block"})
    result = _serve(listener, "GetLatestBlock")
    assert isinstance(result.body, GrpcReply)
    assert result.corruption_mode == "missing_field"
    assert result.missing_field == "block"


# --- The request id ---------------------------------------------------------
# gRPC has no id of its own. A served method can name one field of its request
# message as the request id. For AllBalances that field is ``address``.


def _all_balances(address=""):
    return bank_query_pb2.QueryAllBalancesRequest(address=address)


def test_all_balances_row_holds_the_address_as_the_request_id():
    listener, provider = _listener()
    result = _serve(listener, "AllBalances", _all_balances("lava1-probe-a"))
    assert result.action == "respond"
    assert result.body.method == "AllBalances"
    assert result.body.data["balances"] == [{"denom": "ulava", "amount": "1000000"}]
    hist = provider.log.get_history()[0]
    assert hist["method"] == "AllBalances"
    assert hist["status"] == "success"
    assert hist["request_id"] == "lava1-probe-a"


def test_get_latest_block_row_holds_no_request_id():
    listener, provider = _listener()
    _serve(listener, "GetLatestBlock", query_pb2.GetLatestBlockRequest())
    assert provider.log.get_history()[0]["request_id"] is None


def test_an_empty_address_is_no_request_id():
    listener, provider = _listener()
    _serve(listener, "AllBalances", _all_balances(""))
    assert provider.log.get_history()[0]["request_id"] is None


def test_a_call_with_no_request_message_has_no_request_id():
    # serve() keeps working for a caller that passes the method name only.
    listener, provider = _listener()
    _serve(listener, "AllBalances")
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
    result = _serve(listener, "AllBalances", _all_balances("lava1-probe-f"))
    assert isinstance(result.body, GrpcStatus)
    hist = provider.log.get_history()[0]
    assert (hist["status"], hist["request_id"]) == (status, "lava1-probe-f")


def test_a_provider_wide_down_row_has_no_request_id():
    # A dead node does not read the request. The row has the method "*" and no
    # request id, as on JSON-RPC, REST and Tendermint RPC.
    listener, provider = _listener()
    _upd(listener, {"mode": "down"})
    _serve(listener, "AllBalances", _all_balances("never-read"))
    hist = provider.log.get_history()[0]
    assert (hist["status"], hist["method"], hist["request_id"]) == ("down", "*", None)


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
# The gRPC listener module writes its servicer classes by hand. The check
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


# --- The grid: what serve() decides -----------------------------------------
# A gRPC call is answered through Listener.serve. The tables below record
# what serve() decides for each mode, each corruption mode and each per-method
# override: the status that the caller gets, the text of that status, and the
# history row of the call. Each expected value is written out by hand.
#
# ``_decide`` is the one function of this block that calls serve() and reads a
# ServeResult. Before gRPC moved into Listener.serve, it called plan() and read
# a GrpcPlan, and the move changed no expected value of the two tables.
#
# The content of a reply message is not in these tables. A caller reads the
# message that the adapter builds from the data of the chain.
# tests/test_simulator_grpc.py holds that content, read by a real client:
# TestGrpcHappy, TestGrpcAllBalances and TestGrpcReplyFields.

# GRPC, the endpoint of ``_listener``, is at port 18548. This port is not its port.
_ANOTHER_PORT = 18549


class _Decision(NamedTuple):
    code: str  # the status that the caller gets; "OK" when the caller gets a reply message
    text: str  # the text of the status; "" with a reply message
    wait_ms: int  # how long the adapter waits before it answers
    hangs: bool  # the adapter waits 30 seconds, and not wait_ms
    drop_at: str | None  # set when the status stands for a dropped connection
    clears: str | None  # the field that the adapter clears in the reply message


def _decide(listener, method="GetLatestBlock", request=None, lava_headers=None):
    result = _serve(listener, method, request, lava_headers)
    if isinstance(result.body, GrpcStatus):
        drop_at = result.drop_at if result.action == "drop" else None
        return _Decision(result.body.code, result.body.text, result.latency_ms, result.action == "hang", drop_at, None)
    assert isinstance(result.body, GrpcReply), result.body
    clears = result.missing_field if result.corruption_mode == "missing_field" else None
    return _Decision("OK", "", result.latency_ms, False, None, clears)


def _status(code, text, *, hangs=False, drop_at=None):
    """The caller gets a status, with no wait."""
    return _Decision(code, text, 0, hangs, drop_at, None)


def _reply(*, clears=None):
    """The caller gets a reply message, with no wait."""
    return _Decision("OK", "", 0, False, None, clears)


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
        pytest.param({}, _reply(), "success", id="success"),
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
        # Each per-method override. An error override is a status. With a
        # result override the caller still gets a reply message.
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
            _reply(),
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
            _reply(),
            "success",
            id="result-override",
        ),
        pytest.param(
            {"responses": {"default": {"result": {"height": 9}}}},
            _reply(),
            "success",
            id="result-override-in-the-default-entry",
        ),
        # A fault key in a per-method override is not read on gRPC.
        pytest.param(_override({"mode": "down"}), _reply(), "success", id="per-method-mode-down-is-not-read"),
        pytest.param(
            _override({"mode": "rate_limit"}),
            _reply(),
            "success",
            id="per-method-mode-rate-limit-is-not-read",
        ),
        pytest.param(_override({"latency_ms": 700}), _reply(), "success", id="per-method-latency-is-not-read"),
        pytest.param(
            _override({"error_probability": 1.0}),
            _reply(),
            "success",
            id="per-method-error-probability-is-not-read",
        ),
        pytest.param(
            {"mode": "down", **_override({"mode": "success"})},
            _status("UNAVAILABLE", "provider down"),
            "down",
            id="per-method-mode-success-does-not-lift-a-down",
        ),
        # A fault of the provider comes before a per-method override.
        pytest.param(
            {"mode": "rate_limit", **_override({"error_stub": "NOT_FOUND"})},
            _status("RESOURCE_EXHAUSTED", "Too many requests"),
            "rate_limit",
            id="rate-limit-comes-before-an-error-stub",
        ),
        pytest.param(
            {"mode": "error", "error_message": "ABORTED", **_override({"error": {"code": "NOT_FOUND"}})},
            _status("ABORTED", "ABORTED"),
            "error",
            id="error-comes-before-an-error-override",
        ),
        pytest.param(
            {"mode": "drop_connection", **_override({"result": {"height": 7}})},
            _status("UNAVAILABLE", "connection dropped", drop_at="before_headers"),
            "drop_connection",
            id="drop-comes-before-a-result-override",
        ),
        # Each corruption mode. Five of them turn the reply message into a
        # status, and the row then says error. One clears a field. One,
        # invalid_json, does nothing on gRPC: the control API refuses it for a
        # provider that has only gRPC endpoints, and this table writes the
        # scenario with no control API.
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
            _reply(clears="block"),
            "success",
            id="missing-field-clears-the-field",
        ),
        pytest.param(
            {"corruption_mode": "missing_field"},
            _reply(),
            "success",
            id="missing-field-with-no-field-clears-nothing",
        ),
        pytest.param(
            {"missing_field": "block"},
            _reply(),
            "success",
            id="a-field-with-no-corruption-mode-clears-nothing",
        ),
        pytest.param({"corruption_mode": "invalid_json"}, _reply(), "success", id="invalid-json-does-nothing"),
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
            _reply(),
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
            _reply(),
            "success",
            id="mode-with-a-ports-filter-that-does-not-name-it",
        ),
        pytest.param(
            {"mode": "rate_limit", "transports": ["http2"], "ports": [_ANOTHER_PORT]},
            _reply(),
            "success",
            id="mode-with-one-filter-that-names-the-endpoint-and-one-that-does-not",
        ),
        pytest.param(
            {"corruption_mode": "wrong_type", "transports": ["http"]},
            _reply(),
            "success",
            id="corruption-with-a-transports-filter-that-does-not-name-it",
        ),
        pytest.param(
            {"corruption_mode": "wrong_type", "ports": [_ANOTHER_PORT]},
            _reply(),
            "success",
            id="corruption-with-a-ports-filter-that-does-not-name-it",
        ),
        pytest.param(
            {"corruption_mode": "missing_field", "missing_field": "block", "transports": ["ws"]},
            _reply(),
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
            _reply(),
            "success",
            id="result-override-with-a-filter-that-does-not-name-the-endpoint",
        ),
    ],
)
def test_what_serve_decides_for_one_call(scenario, want, want_row_status):
    listener, provider = _listener()
    _upd(listener, scenario)
    assert _decide(listener) == want
    # A down provider does not read the request, so its row has the method "*",
    # as on JSON-RPC, REST and Tendermint RPC. Each down row of this table is
    # the row of a provider-wide down.
    want_method = "*" if want_row_status == "down" else "GetLatestBlock"
    assert _row(provider) == (want_method, want_row_status, 0, None)


def test_the_row_of_get_node_info_names_its_method():
    listener, provider = _listener()
    assert _decide(listener, "GetNodeInfo") == _reply()
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


@pytest.mark.parametrize(
    "filters, want_ms",
    [
        pytest.param({}, 250, id="no-filter"),
        pytest.param({"ports": [18548]}, 250, id="a-ports-filter-that-names-the-endpoint"),
        pytest.param({"transports": ["http2"]}, 250, id="a-transports-filter-that-names-the-endpoint"),
        pytest.param({"ports": [_ANOTHER_PORT]}, 0, id="a-ports-filter-that-does-not-name-it"),
        pytest.param({"transports": ["ws"]}, 0, id="a-transports-filter-that-does-not-name-it"),
    ],
)
def test_on_grpc_latency_ms_obeys_the_filters(filters, want_ms):
    # A filter that does not name the endpoint holds the latency back, as on
    # JSON-RPC, REST and Tendermint RPC. Before gRPC moved into Listener.serve
    # it read ``latency_ms`` with no look at the filters, and the two cases
    # with 0 held 250. The design "One request flow for every interface"
    # changed that on purpose (its section 9.2, row 2).
    listener, provider = _listener()
    _upd(listener, {"latency_ms": 250, **filters})
    assert _decide(listener).wait_ms == want_ms
    assert _row(provider) == ("GetLatestBlock", "success", want_ms, None)


def test_the_grpc_listener_has_no_request_flow_of_its_own():
    assert GrpcListener.serve is Listener.serve


def test_the_row_is_complete_when_serve_returns():
    # The adapter waits for the latency after serve() returns. So a reader of the
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
    # One provider with two gRPC endpoints. The filter names the second one only.
    other = Endpoint("grpc", "http2", _ANOTHER_PORT)
    provider = Pool(name="lava-sim-grpc", chain="lava").add_provider("1", [GRPC, other])
    not_named, named = GrpcListener(provider, GRPC), GrpcListener(provider, other)
    provider.scenario.update({"mode": "down", "fail_first_n": 1, "then_mode": "rate_limit", "ports": [_ANOTHER_PORT]})
    # The endpoint that the filter does not name gets no mode and no then_mode.
    assert [_decide(not_named).code for _ in range(3)] == ["OK", "OK", "OK"]
    # Those three calls did not use up the window: the named endpoint still gets
    # the mode for its first call, and the then_mode after it.
    assert [_decide(named).code for _ in range(3)] == ["UNAVAILABLE", "RESOURCE_EXHAUSTED", "RESOURCE_EXHAUSTED"]


# --- A scenario value of a wrong JSON type ------------------------------------
# The control API stores each JSON type for ``error_stub``, ``error_message``,
# ``error_code`` and ``blocks_behind``. These tests record what a call gets
# then. Three of the answers changed when gRPC moved into Listener.serve: the
# row of an error_stub that is a list or an object, the row of an error whose
# message or code is a list or an object, and the answer for a blocks_behind
# that is no number under a corruption.


@pytest.mark.parametrize(
    "override, want_text",
    [
        pytest.param({"error_stub": ["NOT_FOUND"]}, "['NOT_FOUND']", id="a-list"),
        pytest.param({"error_stub": {"code": "NOT_FOUND"}}, "{'code': 'NOT_FOUND'}", id="an-object"),
        pytest.param({"error_stub": ["NOT_FOUND"], "message": "gone"}, "gone", id="a-list-with-the-key-message"),
    ],
)
def test_an_error_stub_that_is_a_list_or_an_object_gives_unknown_and_an_error_row(override, want_text):
    listener, provider = _listener()
    _upd(listener, _override(override))
    assert _decide(listener) == _status("UNKNOWN", want_text)
    assert _row(provider) == ("GetLatestBlock", "error", 0, None)


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param({"mode": "error", "error_message": ["NOT_FOUND"]}, id="error-message-is-a-list"),
        pytest.param({"mode": "error", "error_message": {"name": "NOT_FOUND"}}, id="error-message-is-an-object"),
        pytest.param({"mode": "error", "error_code": [5]}, id="error-code-is-a-list"),
        pytest.param({"error_probability": 1.0, "error_code": {"code": 5}}, id="error-code-is-an-object"),
    ],
)
def test_an_error_whose_message_or_code_is_a_list_or_an_object_leaves_the_row_in_flight(scenario):
    # A fault of today, recorded as it is. The lookup of the status fails
    # before the flow finishes the row. The gRPC library then ends the call
    # with UNKNOWN: tests/test_simulator_grpc.py holds that for the same kind
    # of fault, in test_a_per_method_error_that_is_no_object_...
    listener, provider = _listener()
    _upd(listener, scenario)
    with pytest.raises(TypeError, match="unhashable type"):
        _serve(listener)
    assert _row(provider) == ("*", "in_flight", 0, None)


def test_an_error_code_that_is_a_list_is_not_read_when_error_message_names_a_status():
    listener, provider = _listener()
    _upd(listener, {"mode": "error", "error_message": "NOT_FOUND", "error_code": [5]})
    assert _decide(listener) == _status("NOT_FOUND", "NOT_FOUND")
    assert _row(provider) == ("GetLatestBlock", "error", 0, None)


@pytest.mark.parametrize("corruption", ["wrong_type", "invalid_proto", "empty_response", "truncated", "null_body"])
def test_a_blocks_behind_that_is_no_number_ends_the_call_before_the_corruption(corruption):
    # The flow asks the chain for the content before it corrupts the reply, as
    # on JSON-RPC, REST and Tendermint RPC. So the fault of the chain comes
    # first, and the row stays in_flight.
    listener, provider = _listener()
    _upd(listener, {"blocks_behind": "abc", "corruption_mode": corruption})
    with pytest.raises(TypeError, match="unsupported operand"):
        _serve(listener)
    assert _row(provider) == ("*", "in_flight", 0, None)


def test_get_node_info_does_not_read_blocks_behind():
    listener, provider = _listener()
    _upd(listener, {"blocks_behind": "abc", "corruption_mode": "wrong_type"})
    assert _decide(listener, "GetNodeInfo") == _status("INTERNAL", "wrong_type corruption on response")
    assert _row(provider) == ("GetNodeInfo", "error", 0, None)


@pytest.mark.parametrize(
    "filters, want_ms",
    [
        pytest.param({"transports": ["http2"]}, 250, id="a-filter-that-names-the-endpoint"),
        pytest.param({"transports": ["ws"]}, 0, id="a-filter-that-does-not-name-it"),
    ],
)
def test_on_grpc_a_status_waits_for_latency_ms_only_when_the_filters_name_the_endpoint(filters, want_ms):
    # An error_stub is a status that no filter holds back. The latency of the
    # scenario is held back by a filter that does not name the endpoint.
    listener, provider = _listener()
    _upd(listener, {"latency_ms": 250, **_override({"error_stub": "NOT_FOUND"}), **filters})
    decision = _decide(listener)
    assert (decision.code, decision.wait_ms) == ("NOT_FOUND", want_ms)
    assert _row(provider) == ("GetLatestBlock", "error", want_ms, None)
