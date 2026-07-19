"""TendermintListener — GET URI or POST JSON-RPC body. Drives serve() with both
wire forms and checks the envelope + history."""

import json

from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners import RawRequest, TendermintListener

TM = Endpoint("tendermintrpc", "http", 18554)


def _listener():
    provider = Pool(name="lava-sim-tm", chain="lava").add_provider("1", [TM])
    return TendermintListener(provider, TM), provider


def _post(method, params=None, req_id=1):
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    return RawRequest(verb="POST", body=json.dumps(payload).encode())


def _get(path, query=None):
    return RawRequest(verb="GET", path=path, query=query or {})


def test_post_status_wrapped_in_envelope():
    listener, provider = _listener()
    res = listener.serve(_post("status", req_id=5))
    assert res.action == "respond"
    assert res.body["jsonrpc"] == "2.0"
    assert res.body["id"] == 5
    assert res.body["result"]["sync_info"]["latest_block_height"] == str(5_000_000)
    assert provider.log.get_history()[0]["method"] == "status"


def test_post_block_echoes_requested_height():
    listener, _ = _listener()
    res = listener.serve(_post("block", params={"height": "99"}))
    assert res.body["result"]["block"]["header"]["height"] == "99"


def test_get_form_resolves_method_from_path():
    listener, _ = _listener()
    res = listener.serve(_get("/status"))
    assert res.body["result"]["node_info"]["network"]


def test_get_form_decodes_query_params():
    listener, _ = _listener()
    res = listener.serve(_get("/block", query={"height": ["4500000"]}))
    assert res.body["result"]["block"]["header"]["height"] == "4500000"


def test_unknown_method_is_minus_32601():
    listener, _ = _listener()
    res = listener.serve(_post("bogus"))
    assert res.body["error"]["code"] == -32601


def test_post_empty_body_is_parse_error():
    listener, provider = _listener()
    res = listener.serve(RawRequest(verb="POST", body=b""))
    assert res.status == 400
    assert res.body["error"]["code"] == -32700
    assert provider.log.get_history()[0]["status"] == "parse_error"


def test_get_empty_method_is_parse_error():
    listener, _ = _listener()
    res = listener.serve(_get("/"))
    assert res.body["error"]["code"] == -32700


def test_down_is_503():
    listener, provider = _listener()
    provider.scenario.update({"mode": "down"})
    res = listener.serve(_post("status"))
    assert res.action == "no_body"
    assert res.status == 503


def test_error_fault_is_jsonrpc_error_envelope():
    listener, provider = _listener()
    provider.scenario.update({"mode": "error", "error_code": -32001, "error_message": "boom"})
    res = listener.serve(_post("status", req_id=9))
    assert res.body["jsonrpc"] == "2.0"
    assert res.body["id"] == 9
    assert res.body["error"] == {"code": -32001, "message": "boom"}
    assert provider.log.get_history()[0]["status"] == "error"
