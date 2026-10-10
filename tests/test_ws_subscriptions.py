"""WsSubscriptions — the WS subscription registry. Per-frame JSON-RPC over WS is
JsonRpcListener; this covers the subscribe / emit / unsubscribe lifecycle."""

import queue

import pytest

from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners import ServeResult
from provider_simulator.listeners.ws import JsonRpcWsListener, WsSubscriptions


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
