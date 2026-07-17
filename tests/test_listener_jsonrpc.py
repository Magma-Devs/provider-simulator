import json

from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners import JsonRpcListener

HTTP = Endpoint("jsonrpc", "http", 18545)


def _listener():
    provider = Pool(name="eth-sim", chain="eth").add_provider("1", [HTTP])
    return JsonRpcListener(provider, HTTP), provider


def _raw(method, params=None, req_id=1):
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    return json.dumps(payload).encode()


def test_success_builds_chain_response_and_logs():
    listener, provider = _listener()
    res = listener.serve(_raw("eth_blockNumber"), headers={})
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
    res = listener.serve(_raw("eth_blockNumber"), headers={})
    assert res.action == "no_body"
    assert res.status == 503
    hist = provider.log.get_history()
    assert hist[0]["method"] == "*"
    assert hist[0]["request_id"] is None
    assert hist[0]["status"] == "down"


def test_error_becomes_jsonrpc_error_envelope():
    listener, provider = _listener()
    provider.scenario.update({"mode": "error", "error_code": -32050, "error_message": "boom"})
    res = listener.serve(_raw("eth_call", req_id=9), headers={})
    assert res.action == "respond"
    assert res.body["error"] == {"code": -32050, "message": "boom"}
    assert res.body["id"] == 9
    assert provider.log.get_history()[0]["status"] == "error"


def test_rate_limit_envelope_429():
    listener, provider = _listener()
    provider.scenario.update({"mode": "rate_limit"})
    res = listener.serve(_raw("eth_call"), headers={})
    assert res.status == 429
    assert res.body["error"]["code"] == 429
    assert provider.log.get_history()[0]["status"] == "rate_limit"


def test_hang_and_drop_actions():
    listener, provider = _listener()
    provider.scenario.update({"mode": "hang"})
    assert listener.serve(_raw("eth_call"), headers={}).action == "hang"
    assert provider.log.get_history()[-1]["status"] == "hang"
    provider.scenario.update({"mode": "drop_connection", "drop_at": "mid_body"})
    res = listener.serve(_raw("eth_call"), headers={})
    assert res.action == "drop"
    assert res.drop_at == "mid_body"
    assert provider.log.get_history()[-1]["status"] == "drop_connection"


def test_per_method_error_stub_via_chain():
    listener, provider = _listener()
    provider.scenario.update({"responses": {"eth_call": {"error_stub": "revert"}}})
    res = listener.serve(_raw("eth_call"), headers={})
    assert "error" in res.body
    # method-level error still records the winning-path label from the body
    assert provider.log.get_history()[0]["status"] == "error"


def test_lava_headers_captured_on_the_entry():
    listener, provider = _listener()
    listener.serve(_raw("eth_blockNumber"), headers={"Lava-Guid": "GUID_1", "X-Other": "no"})
    entry = provider.log.get_history()[0]
    assert entry["lava_headers"] == {"Lava-Guid": "GUID_1"}
    assert entry["interface"] == "jsonrpc"
    assert entry["transport"] == "http"
    assert entry["port"] == 18545
