"""GrpcListener — the pure decision core (plan()) an async servicer glue performs.
No running gRPC server needed: plan() returns a GrpcPlan we assert on directly."""

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
