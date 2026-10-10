import json

import pytest

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


@pytest.mark.parametrize("mode", ["down", "hang"])
def test_latency_ms_of_the_row_of_a_call_that_the_provider_did_not_wait_for(mode):
    # A down provider answers at once, and a hung call waits its own 30
    # seconds: the adapter does not wait for latency_ms. So the row records 0,
    # as on gRPC.
    listener, provider = _listener()
    provider.scenario.update({"mode": mode, "latency_ms": 250})
    assert _serve(listener, "eth_blockNumber").latency_ms == 0
    assert [(row["status"], row["latency_ms"]) for row in provider.log.get_history()] == [(mode, 0)]


def test_a_rate_limit_row_records_the_latency_that_the_provider_waited():
    # The control for the test above: a call that waits records its wait.
    listener, provider = _listener()
    provider.scenario.update({"mode": "rate_limit", "latency_ms": 250})
    assert _serve(listener, "eth_blockNumber").latency_ms == 250
    assert provider.log.get_history()[0]["latency_ms"] == 250


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


def test_block_above_effective_head_serializes_as_a_null_result():
    """The false-gap symptom, through the listener: HTTP 200, result null, no
    error field. Held here and not only on the chain because what reaches the
    caller is the serialized body, and null is the part that has to survive it."""
    listener, provider = _listener()
    head = int(_serve(listener, "eth_blockNumber").body["result"], 16)
    provider.scenario.update({"blocks_behind": 100})

    res = _serve(listener, "eth_getBlockByNumber", [hex(head - 99), False])
    assert res.action == "respond"
    assert res.status == 200
    assert res.body["result"] is None
    assert "error" not in res.body
    assert json.dumps(res.body) == '{"jsonrpc": "2.0", "id": 1, "result": null}'


def test_block_at_effective_head_still_serializes_a_block():
    listener, provider = _listener()
    head = int(_serve(listener, "eth_blockNumber").body["result"], 16)
    provider.scenario.update({"blocks_behind": 100})

    at_head = hex(head - 100)
    res = _serve(listener, "eth_getBlockByNumber", [at_head, False])
    assert res.status == 200
    assert isinstance(res.body["result"], dict), "a block at the head must not be null"
    assert res.body["result"]["number"] == at_head


def _facts(row):
    return (row["method"], row["status"], row["latency_ms"], row["request_id"])


def test_arrive_writes_one_row_in_flight_with_the_lava_headers_of_the_call():
    # An adapter calls arrive before it reads the body of a request. The row
    # names the endpoint, and it keeps the lava headers only.
    listener, provider = _listener()
    listener.arrive({"Lava-Guid": "GUID_1", "X-Other": "no"})
    rows = provider.log.get_history()
    assert [_facts(row) for row in rows] == [("*", "in_flight", 0, None)]
    assert rows[0]["lava_headers"] == {"Lava-Guid": "GUID_1"}
    assert (rows[0]["interface"], rows[0]["transport"], rows[0]["port"]) == ("jsonrpc", "http", 18545)


def test_serve_finishes_the_row_that_arrive_wrote():
    # serve does not write a second row: it completes the row of arrive.
    listener, provider = _listener()
    entry = listener.arrive({"Lava-Guid": "GUID_1"})
    res = listener.serve(RawRequest(body=_raw("eth_blockNumber")), entry=entry)
    assert res.action == "respond"
    rows = provider.log.get_history()
    assert [_facts(row) for row in rows] == [("eth_blockNumber", "success", 0, 1)]
    assert rows[0]["lava_headers"] == {"Lava-Guid": "GUID_1"}
    assert provider.log.stats()["calls_by_status"] == {"success": 1}


class _OwnContentListener(JsonRpcListener):
    """A listener that gives its own success content. It keeps each call of
    build_content."""

    def __init__(self, provider, endpoint):
        super().__init__(provider, endpoint)
        self.asked = []

    def build_content(self, parsed, scenario, request):
        self.asked.append((parsed, request))
        return 200, {"jsonrpc": "2.0", "id": 5, "result": "own content"}


def _own_content_listener():
    provider = Pool(name="eth-sim", chain="eth").add_provider("1", [HTTP])
    return _OwnContentListener(provider, HTTP), provider


def test_the_flow_asks_build_content_for_the_success_content_and_gives_it_the_request():
    # The body of the hook is the reply, and its id is the request id of the
    # row. The hook gets the request of the adapter, with its connection.
    listener, provider = _own_content_listener()
    connection = object()
    request = RawRequest(body=_raw("eth_blockNumber", req_id=1), connection=connection)

    res = listener.serve(request)

    assert res.action == "respond"
    assert res.body == {"jsonrpc": "2.0", "id": 5, "result": "own content"}
    assert [_facts(row) for row in provider.log.get_history()] == [("eth_blockNumber", "success", 0, 5)]
    assert len(listener.asked) == 1
    parsed, got = listener.asked[0]
    assert parsed["method"] == "eth_blockNumber"
    assert got is request
    assert got.connection is connection


@pytest.mark.parametrize(
    "block, row_status",
    [
        pytest.param({"mode": "error"}, "error", id="error"),
        pytest.param({"mode": "rate_limit"}, "rate_limit", id="rate-limit"),
        pytest.param({"mode": "hang"}, "hang", id="hang"),
        pytest.param({"mode": "drop_connection"}, "drop_connection", id="drop-connection"),
        pytest.param({"responses": {"eth_blockNumber": {"mode": "down"}}}, "down", id="a-per-method-down"),
        pytest.param(
            {"responses": {"eth_blockNumber": {"body": {"jsonrpc": "2.0", "id": 1, "result": "0xcanned"}}}},
            "success",
            id="a-canned-body",
        ),
    ],
)
def test_the_flow_asks_build_content_for_a_success_and_not_for_a_fault_or_a_canned_body(block, row_status):
    # A request with no fault asks the hook one time. The request of each case
    # does not ask it again.
    listener, provider = _own_content_listener()
    _serve(listener, "eth_blockNumber")
    assert len(listener.asked) == 1

    provider.scenario.update(block)
    _serve(listener, "eth_blockNumber")

    assert [row["status"] for row in provider.log.get_history()] == ["success", row_status]
    assert len(listener.asked) == 1
