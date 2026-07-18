"""GrpcListener — the pure decision core (plan()) an async servicer glue performs.
No running gRPC server needed: plan() returns a GrpcPlan we assert on directly."""

from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners.grpc import GrpcListener

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


def test_missing_field_corruption_stays_respond():
    listener, _ = _listener()
    _upd(listener, {"corruption_mode": "missing_field", "missing_field": "block"})
    plan = listener.plan("GetLatestBlock")
    assert plan.action == "respond"
    assert plan.corruption_mode == "missing_field"
    assert plan.missing_field == "block"
