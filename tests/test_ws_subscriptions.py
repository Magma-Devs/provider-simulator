"""WsSubscriptions — the WS subscription registry. Per-frame JSON-RPC over WS is
JsonRpcListener; this covers the subscribe / emit / unsubscribe lifecycle."""

import json
import queue
import re

import pytest

from provider_simulator.chains import chain_for
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.domain.registry import build_registry
from provider_simulator.listeners import RawRequest, ServeResult
from provider_simulator.listeners.ws import JsonRpcWsListener, WsConnection, WsSubscriptions


def test_register_and_get():
    reg = WsSubscriptions()
    sub = reg.register("0xabc", "eth-sim", "1", "newHeads")
    assert reg.get("0xabc") is sub
    assert sub.pool == "eth-sim"
    assert sub.pid == "1"
    assert sub.method == "newHeads"


def test_emit_pushes_event_to_queue():
    reg = WsSubscriptions()
    reg.register("0xabc", "eth-sim", "1", "newHeads")
    assert reg.emit("0xabc", {"block": 1}) == "emitted"
    assert reg.get("0xabc").out_queue.get_nowait() == {"block": 1}


def test_emit_unknown_subscription():
    reg = WsSubscriptions()
    assert reg.emit("nope", {}) == "unknown"


def test_unregister_closes_and_blocks_further_emit():
    reg = WsSubscriptions()
    reg.register("0xabc", "eth-sim", "1", "newHeads")
    assert reg.unregister("0xabc") is True
    assert reg.emit("0xabc", {}) == "unknown"
    assert reg.unregister("0xabc") is False  # already gone


def test_emit_full_queue():
    reg = WsSubscriptions()
    reg.register("0xabc", "eth-sim", "1", "newHeads", out_queue=queue.Queue(maxsize=1))
    assert reg.emit("0xabc", 1) == "emitted"
    assert reg.emit("0xabc", 2) == "full"


def test_list_and_clear():
    reg = WsSubscriptions()
    reg.register("a", "eth-sim", "1", "newHeads")
    reg.register("b", "eth-sim", "2", "logs")
    assert {row["subscription_id"] for row in reg.list()} == {"a", "b"}
    reg.clear()
    assert reg.list() == []
    assert reg.get("a") is None


# ── The WebSocket listener, with no socket ────────────────────────────────────

_HTTP = Endpoint("jsonrpc", "http", 18545)
_WS = Endpoint("jsonrpc", "ws", 18557)


def _ws_listener():
    """The listener of the ws endpoint of a provider that also has an http
    endpoint. Return the listener and the provider."""
    provider = Pool(name="eth-sim", chain="eth").add_provider("1", [_HTTP, _WS])
    return JsonRpcWsListener(provider, _WS, WsSubscriptions()), provider


def _facts(row):
    return (row["method"], row["status"], row["latency_ms"], row["request_id"])


@pytest.mark.parametrize(
    "block, decision, rows",
    [
        pytest.param({"mode": "success"}, ServeResult(action="respond", status=101), [], id="no-fault"),
        pytest.param(
            {"mode": "down"},
            ServeResult(action="respond", status=503, body={"error": "provider down"}),
            [("*", "down", 0, None)],
            id="down",
        ),
        pytest.param(
            {"mode": "rate_limit"},
            ServeResult(action="respond", status=429, body={"error": "rate limited"}),
            [("ws_upgrade", "rate_limit", 0, None)],
            id="rate-limit",
        ),
        pytest.param(
            {"mode": "error", "error_message": "refused by the test"},
            ServeResult(action="respond", status=400, body={"error": "refused by the test"}),
            [("ws_upgrade", "error", 0, None)],
            id="error",
        ),
        pytest.param({"mode": "hang"}, ServeResult(action="hang"), [("ws_upgrade", "hang", 0, None)], id="hang"),
        pytest.param(
            {"mode": "drop_connection", "drop_at": "mid_body"},
            ServeResult(action="drop", drop_at="mid_body"),
            [("ws_upgrade", "drop_connection", 0, None)],
            id="drop",
        ),
        pytest.param(
            {"mode": "down", "transports": ["http"]},
            ServeResult(action="respond", status=101),
            [],
            id="a-down-for-the-http-endpoint-only",
        ),
        pytest.param(
            {"mode": "success", "error_probability": 1.0},
            ServeResult(action="respond", status=400, body={"error": "Internal error"}),
            [("ws_upgrade", "error", 0, None)],
            id="an-error-probability-of-one",
        ),
        pytest.param(
            {
                "mode": "success",
                "latency_ms": 1500,
                "corruption_mode": "truncated",
                "responses": {"ws_upgrade": {"mode": "down"}},
            },
            ServeResult(action="respond", status=101),
            [],
            id="a-latency-a-corruption-and-a-responses-entry",
        ),
    ],
)
def test_what_the_listener_decides_for_an_upgrade(block, decision, rows):
    # The status 101 says "complete the handshake", and that decision writes no
    # row. Each other decision is a refusal, and it writes one complete row.
    listener, provider = _ws_listener()
    provider.scenario.update(block)

    assert listener.decide_upgrade({}) == decision
    assert [_facts(row) for row in provider.log.get_history()] == rows


def test_the_row_of_a_refused_upgrade_names_the_ws_endpoint_and_carries_the_lava_headers_only():
    listener, provider = _ws_listener()
    provider.scenario.update({"mode": "rate_limit"})

    listener.decide_upgrade({"Lava-Guid": "GUID_1", "Upgrade": "websocket"})

    rows = provider.log.get_history()
    assert [(row["interface"], row["transport"], row["port"]) for row in rows] == [("jsonrpc", "ws", 18557)]
    assert rows[0]["lava_headers"] == {"Lava-Guid": "GUID_1"}


def test_each_upgrade_decision_uses_one_count_of_the_fail_first_n_window():
    listener, provider = _ws_listener()
    provider.scenario.update({"mode": "rate_limit", "fail_first_n": 2, "then_mode": "success"})

    statuses = [listener.decide_upgrade({}).status for _ in range(3)]

    assert statuses == [429, 429, 101]
    assert [_facts(row) for row in provider.log.get_history()] == [("ws_upgrade", "rate_limit", 0, None)] * 2


# ── The row of a pushed event ─────────────────────────────────────────────────


def test_emit_with_a_registry_writes_one_push_row_for_the_ws_endpoint_of_the_provider():
    # The method of the row is the envelope of the subscribe method and the
    # word "push". 18558 is the ws port of the provider eth-sim:2.
    registry = build_registry()
    subscriptions = WsSubscriptions(registry)
    subscriptions.register("0xabc", "eth-sim", "2", "accountSubscribe")

    assert subscriptions.emit("0xabc", {"tag": "A"}) == "emitted"

    rows = registry.provider("eth-sim", "2").log.get_history()
    assert [_facts(row) for row in rows] == [("solana_account push", "success", 0, "0xabc")]
    assert (rows[0]["interface"], rows[0]["transport"], rows[0]["port"]) == ("jsonrpc", "ws", 18558)
    assert rows[0]["lava_headers"] == {}


def test_emit_on_a_full_queue_writes_no_row():
    registry = build_registry()
    subscriptions = WsSubscriptions(registry)
    full_queue = queue.Queue(maxsize=1)
    full_queue.put_nowait("an older event")
    subscriptions.register("0xabc", "eth-sim", "1", "eth_subscribe", out_queue=full_queue)

    assert subscriptions.emit("0xabc", {"tag": "A"}) == "full"

    assert registry.provider("eth-sim", "1").log.get_history() == []


def test_emit_for_a_provider_that_the_registry_does_not_hold_writes_no_row():
    registry = build_registry()
    subscriptions = WsSubscriptions(registry)
    subscriptions.register("0xabc", "no-such-pool", "1", "eth_subscribe")

    assert subscriptions.emit("0xabc", {"tag": "A"}) == "emitted"

    assert [row for provider in registry.all_providers() for row in provider.log.get_history()] == []


class _BytesSubscriptions(WsSubscriptions):
    """A registry whose frame_of gives bytes, as the registry of the socket
    adapter does."""

    def frame_of(self, sub, event):
        return f"{sub.sub_id}:{event}".encode()


def test_frame_of_gives_what_goes_on_the_queue():
    subscriptions = _BytesSubscriptions()
    sub = subscriptions.register("0xabc", "eth-sim", "1", "eth_subscribe")

    assert subscriptions.emit("0xabc", "tag-A") == "emitted"

    assert sub.out_queue.get_nowait() == b"0xabc:tag-A"


# ── A frame through the listener, with no socket ──────────────────────────────


def _serve_frame(listener, connection, method, params, frame_id):
    """Give one JSON-RPC frame of a connection to the listener."""
    frame = {"jsonrpc": "2.0", "method": method, "params": params, "id": frame_id}
    return listener.serve(RawRequest(body=json.dumps(frame).encode(), connection=connection))


def test_a_subscribe_frame_registers_a_subscription_on_the_connection_of_the_request():
    listener, provider = _ws_listener()
    connection = WsConnection(queue.Queue())

    result = _serve_frame(listener, connection, "accountSubscribe", ["an-account"], 7)

    assert result.action == "respond"
    subscription_id = result.body["result"]
    assert re.fullmatch(r"0x[0-9a-f]{32}", subscription_id), subscription_id
    assert result.body == {"jsonrpc": "2.0", "id": 7, "result": subscription_id}
    assert list(result.body) == ["jsonrpc", "id", "result"]
    sub = listener.subscriptions.get(subscription_id)
    assert (sub.pool, sub.pid, sub.method) == ("eth-sim", "1", "accountSubscribe")
    assert sub.out_queue is connection.out_queue
    assert connection.subscription_ids == {subscription_id}
    assert [_facts(row) for row in provider.log.get_history()] == [("accountSubscribe", "success", 0, 7)]


def test_an_unsubscribe_frame_removes_a_subscription_of_its_own_connection_only():
    listener, _ = _ws_listener()
    owner, other = WsConnection(queue.Queue()), WsConnection(queue.Queue())
    subscription_id = _serve_frame(listener, owner, "eth_subscribe", ["newHeads"], 1).body["result"]

    from_the_other = _serve_frame(listener, other, "eth_unsubscribe", [subscription_id], 2)
    after_the_other = listener.subscriptions.get(subscription_id)
    from_the_owner = _serve_frame(listener, owner, "eth_unsubscribe", [subscription_id], 3)

    assert from_the_other.body == {"jsonrpc": "2.0", "id": 2, "result": False}
    assert after_the_other is not None
    assert from_the_owner.body == {"jsonrpc": "2.0", "id": 3, "result": True}
    assert listener.subscriptions.get(subscription_id) is None
    assert owner.subscription_ids == set()


@pytest.mark.parametrize(
    "block, expected_row",
    [
        pytest.param({"mode": "down"}, ("*", "down", 0, None), id="down"),
        pytest.param({"mode": "hang"}, ("eth_subscribe", "hang", 0, 7), id="hang"),
        pytest.param({"mode": "rate_limit"}, ("eth_subscribe", "rate_limit", 0, 7), id="rate-limit"),
        pytest.param({"mode": "error"}, ("eth_subscribe", "error", 0, 7), id="error"),
        pytest.param({"mode": "drop_connection"}, ("eth_subscribe", "drop_connection", 0, 7), id="drop-connection"),
        pytest.param(
            {"responses": {"eth_subscribe": {"mode": "down"}}}, ("eth_subscribe", "down", 0, 7), id="a-per-method-down"
        ),
        pytest.param(
            {"responses": {"eth_subscribe": {"body": {"jsonrpc": "2.0", "id": 1, "result": "0xcanned"}}}},
            ("eth_subscribe", "success", 0, 7),
            id="a-canned-body",
        ),
    ],
)
def test_a_fault_or_a_canned_body_on_a_subscribe_frame_registers_nothing(block, expected_row):
    listener, provider = _ws_listener()
    connection = WsConnection(queue.Queue())
    provider.scenario.update(block)

    _serve_frame(listener, connection, "eth_subscribe", ["newHeads"], 7)

    assert listener.subscriptions.list() == []
    assert connection.subscription_ids == set()
    assert [_facts(row) for row in provider.log.get_history()] == [expected_row]


def test_release_removes_each_subscription_of_one_connection_and_no_other():
    listener, _ = _ws_listener()
    ended, still_open = WsConnection(queue.Queue()), WsConnection(queue.Queue())
    _serve_frame(listener, ended, "eth_subscribe", ["newHeads"], 1)
    _serve_frame(listener, ended, "logsSubscribe", ["all"], 2)
    kept = _serve_frame(listener, still_open, "eth_subscribe", ["newHeads"], 3).body["result"]
    assert len(listener.subscriptions.list()) == 3

    listener.release(ended)

    assert [entry["subscription_id"] for entry in listener.subscriptions.list()] == [kept]


def test_a_frame_of_another_method_gets_the_content_of_the_chain():
    listener, provider = _ws_listener()
    connection = WsConnection(queue.Queue())

    block_number = _serve_frame(listener, connection, "eth_blockNumber", [], 8)
    no_method = listener.serve(RawRequest(body=b"{}", connection=connection))

    assert block_number.body["id"] == 8
    assert int(block_number.body["result"], 16) == chain_for("eth").head.current()
    assert no_method.body == {"jsonrpc": "2.0", "id": 1, "result": "0x1"}
    assert [_facts(row) for row in provider.log.get_history()] == [
        ("eth_blockNumber", "success", 0, 8),
        ("unknown", "success", 0, 1),
    ]
    assert listener.subscriptions.list() == []


def test_a_subscribe_frame_with_no_connection_raises_a_clear_error():
    listener, _ = _ws_listener()

    with pytest.raises(ValueError, match="WsConnection"):
        _serve_frame(listener, None, "eth_subscribe", ["newHeads"], 7)

    assert listener.subscriptions.list() == []
