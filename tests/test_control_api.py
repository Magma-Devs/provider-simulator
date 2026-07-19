"""Control API routes over a real Registry (no socket). Covers the pool:pid
scenario apply (staged, old-format 400, quirk routing, responses normalization),
resets, stats/history filters, advance, and ws/emit."""

from provider_simulator.control_api import ControlApi
from provider_simulator.domain.registry import build_registry
from provider_simulator.listeners.ws import WsSubscriptions


def _api():
    return ControlApi(build_registry(), WsSubscriptions())


# ── POST /scenario ────────────────────────────────────────────────────────────
def test_apply_scenario_by_pool_pid():
    api = _api()
    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"mode": "down"}}})
    assert st == 200
    assert resp["applied"]["eth-sim:1"]["mode"] == "down"
    _, scen = api.get_scenario()
    assert scen["providers"]["eth-sim:1"]["mode"] == "down"


def test_old_bare_pid_is_400():
    api = _api()
    st, resp = api.apply_scenario({"providers": {"1": {"mode": "down"}}})
    assert st == 400
    assert "pool:pid" in resp["error"]


def test_chain_family_field_is_400():
    api = _api()
    st, resp = api.apply_scenario(
        {"providers": {"eth-sim:1": {"chain_family": "eth", "mode": "down"}}}
    )
    assert st == 400
    assert "chain_family" in resp["error"]


def test_unknown_pool_is_400():
    api = _api()
    st, resp = api.apply_scenario({"providers": {"nope:1": {"mode": "down"}}})
    assert st == 400


def test_wrong_chain_quirk_is_400():
    api = _api()
    st, resp = api.apply_scenario({"providers": {"lava-sim-rest:1": {"slot_offset": 3}}})
    assert st == 400  # slot_offset is a Solana quirk


def test_solana_quirk_ok_on_solana():
    api = _api()
    st, resp = api.apply_scenario({"providers": {"solana-sim:1": {"slot_offset": -120}}})
    assert st == 200
    assert resp["applied"]["solana-sim:1"]["slot_offset"] == -120
    assert api.registry.provider("solana-sim", "1").quirks.snapshot()["slot_offset"] == -120


def test_invalid_mode_value_is_400():
    api = _api()
    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"mode": "bogus"}}})
    assert st == 400
    assert "invalid mode" in resp["error"]


def test_scenario_is_staged_all_or_nothing():
    api = _api()
    st, resp = api.apply_scenario(
        {"providers": {"eth-sim:1": {"mode": "down"}, "eth-sim:2": {"mode": "bogus"}}}
    )
    assert st == 400
    _, scen = api.get_scenario()
    assert scen["providers"]["eth-sim:1"]["mode"] == "success"  # first block not applied


def test_rest_responses_are_retupled():
    api = _api()
    st, resp = api.apply_scenario(
        {"providers": {"lava-sim-rest:1": {"responses": [[["GET", "/x"], {"body": {"a": 1}}]]}}}
    )
    assert st == 200
    stored = api.registry.provider("lava-sim-rest", "1").scenario.snapshot()["responses"]
    assert ("GET", "/x") in stored


def test_per_method_mode_error_is_rejected():
    api = _api()
    st, resp = api.apply_scenario(
        {"providers": {"eth-sim:1": {"responses": {"eth_call": {"mode": "error"}}}}}
    )
    assert st == 400


# ── resets ────────────────────────────────────────────────────────────────────
def test_reset_clears_scenario():
    api = _api()
    api.apply_scenario({"providers": {"eth-sim:1": {"mode": "down"}}})
    api.reset()
    _, scen = api.get_scenario()
    assert scen["providers"]["eth-sim:1"]["mode"] == "success"


# ── stats / history ───────────────────────────────────────────────────────────
def test_stats_shape():
    api = _api()
    _, stats = api.get_stats()
    assert stats["providers"]["eth-sim:1"]["total_calls"] == 0


def test_history_merges_filters_and_orders():
    api = _api()
    p1 = api.registry.provider("eth-sim", "1")
    p2 = api.registry.provider("eth-sim", "2")
    p1.log.push(
        "eth_blockNumber",
        "success",
        0,
        interface="jsonrpc",
        transport="http",
        port=18545,
        request_id=1,
    )
    p2.log.push(
        "eth_call", "error", 0, interface="jsonrpc", transport="http", port=18546, request_id=2
    )
    _, hist = api.get_history({})
    assert hist["count"] == 2
    assert all("call_order" in e for e in hist["history"])
    _, only1 = api.get_history({"request_id": "1"})
    assert only1["count"] == 1
    assert only1["history"][0]["method"] == "eth_blockNumber"


def test_history_max_caps_and_rejects_negative():
    api = _api()
    p = api.registry.provider("eth-sim", "1")
    for i in range(5):
        p.log.push(
            "m", "success", 0, interface="jsonrpc", transport="http", port=18545, request_id=i
        )
    _, hist = api.get_history({"max": "2"})
    assert hist["count"] == 2
    st, resp = api.get_history({"max": "-1"})
    assert st == 400


# ── advance ───────────────────────────────────────────────────────────────────
def test_advance_eth_head_then_reset():
    api = _api()
    st, resp = api.advance({"blocks": 5})
    assert st == 200
    assert resp["chain"] == "eth"
    api.reset()  # heads are a shared singleton — don't leak into other tests


def test_advance_unknown_head_is_400():
    api = _api()
    st, resp = api.advance({"chain": "btc", "blocks": 5})
    assert st == 400


# ── ws/emit ───────────────────────────────────────────────────────────────────
def test_ws_emit_unknown_is_404():
    api = _api()
    st, resp = api.ws_emit({"subscription_id": "nope", "event": {}})
    assert st == 404


def test_ws_emit_ok():
    api = _api()
    api.subscriptions.register("0xabc", "eth-sim", "1", "newHeads")
    st, resp = api.ws_emit({"subscription_id": "0xabc", "event": {"block": 1}})
    assert st == 200
    assert resp["status"] == "emitted"


def test_ws_emit_missing_id_is_400():
    api = _api()
    st, resp = api.ws_emit({"event": {}})
    assert st == 400


def test_health():
    assert _api().health() == (200, {"status": "ok"})
