"""WsSubscriptions — the WS subscription registry. Per-frame JSON-RPC over WS is
JsonRpcListener; this covers the subscribe / emit / unsubscribe lifecycle."""

import queue

from provider_simulator.listeners.ws import WsSubscriptions


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
