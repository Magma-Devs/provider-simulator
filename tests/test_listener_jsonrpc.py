import json

from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners import JsonRpcListener, RawRequest

HTTP = Endpoint("jsonrpc", "http", 18545)


def _listener():
    provider = Pool(name="eth-sim", chain="eth").add_provider("1", [HTTP])
    return JsonRpcListener(provider, HTTP), provider


def _raw(method, params=None, req_id=1):
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    return json.dumps(payload).encode()


def _serve(listener, method, params=None, req_id=1, headers=None):
    return listener.serve(RawRequest(body=_raw(method, params, req_id), headers=headers or {}))


def test_success_builds_chain_response_and_logs():
    listener, provider = _listener()
    res = _serve(listener, "eth_blockNumber")
    assert res.action == "respond"
    assert res.status == 200
    assert res.body["result"] == "0x1312D00"
    hist = provider.log.get_history()
    assert len(hist) == 1
    assert hist[0]["status"] == "success"
    assert hist[0]["method"] == "eth_blockNumber"
    assert hist[0]["request_id"] == 1
    assert provider.log.stats()["calls_by_status"] == {"success": 1}


def test_down_is_pre_parse_no_body_star_method():
    listener, provider = _listener()
    provider.scenario.update({"mode": "down"})
    res = _serve(listener, "eth_blockNumber")
    assert res.action == "no_body"
    assert res.status == 503
    hist = provider.log.get_history()
    assert hist[0]["method"] == "*"
    assert hist[0]["request_id"] is None
    assert hist[0]["status"] == "down"


def test_error_becomes_jsonrpc_error_envelope():
    listener, provider = _listener()
    provider.scenario.update({"mode": "error", "error_code": -32050, "error_message": "boom"})
    res = _serve(listener, "eth_call", req_id=9)
    assert res.action == "respond"
    assert res.body["error"] == {"code": -32050, "message": "boom"}
    assert res.body["id"] == 9
    assert provider.log.get_history()[0]["status"] == "error"


def test_rate_limit_sends_prose_body_not_envelope():
    """Real providers answer a 429 with prose or HTML, never a JSON-RPC
    error envelope — see the module docstring in jsonrpc.py. ``error`` mode
    (test_error_becomes_jsonrpc_error_envelope above) is unchanged."""
    listener, provider = _listener()
    provider.scenario.update({"mode": "rate_limit"})
    res = _serve(listener, "eth_call")
    assert res.status == 429
    assert isinstance(res.body, str), f"rate_limit body must be a plain string, got {res.body!r}"
    assert not res.body.lstrip().startswith("{"), f"rate_limit body must not look like JSON: {res.body!r}"
    assert res.body == ("Rate limit exceeded. Reduce your request rate, or use an API key for a higher limit.")
    assert provider.log.get_history()[0]["status"] == "rate_limit"


def test_rate_limit_body_overridable_per_provider():
    """ScenarioConfig.rate_limit_body lets a test ask for a specific prose
    shape — the same per-provider mechanism error_message/http_status use."""
    listener, provider = _listener()
    provider.scenario.update({"mode": "rate_limit", "rate_limit_body": "Slow down."})
    res = _serve(listener, "eth_call")
    assert res.status == 429
    assert res.body == "Slow down."


def test_hang_and_drop_actions():
    listener, provider = _listener()
    provider.scenario.update({"mode": "hang"})
    assert _serve(listener, "eth_call").action == "hang"
    assert provider.log.get_history()[-1]["status"] == "hang"
    provider.scenario.update({"mode": "drop_connection", "drop_at": "mid_body"})
    res = _serve(listener, "eth_call")
    assert res.action == "drop"
    assert res.drop_at == "mid_body"
    assert provider.log.get_history()[-1]["status"] == "drop_connection"


def test_per_method_error_stub_via_chain():
    listener, provider = _listener()
    provider.scenario.update({"responses": {"eth_call": {"error_stub": "revert"}}})
    res = _serve(listener, "eth_call")
    assert "error" in res.body
    # method-level error still records the winning-path label from the body
    assert provider.log.get_history()[0]["status"] == "error"


def test_lava_headers_captured_on_the_entry():
    listener, provider = _listener()
    _serve(listener, "eth_blockNumber", headers={"Lava-Guid": "GUID_1", "X-Other": "no"})
    entry = provider.log.get_history()[0]
    assert entry["lava_headers"] == {"Lava-Guid": "GUID_1"}
    assert entry["interface"] == "jsonrpc"
    assert entry["transport"] == "http"
    assert entry["port"] == 18545


def test_latency_is_carried_on_the_serve_result():
    listener, provider = _listener()
    provider.scenario.update({"latency_ms": 250})
    res = _serve(listener, "eth_blockNumber")
    assert res.latency_ms == 250


def test_corruption_directive_carried_on_success():
    listener, provider = _listener()
    provider.scenario.update({"corruption_mode": "truncated"})
    res = _serve(listener, "eth_blockNumber")
    assert res.action == "respond"
    assert res.corruption_mode == "truncated"


def test_corruption_scoped_out_when_filter_excludes_transport():
    listener, provider = _listener()
    provider.scenario.update({"corruption_mode": "truncated", "transports": ["ws"]})
    res = _serve(listener, "eth_blockNumber")  # this endpoint is http, not ws
    assert res.corruption_mode is None
