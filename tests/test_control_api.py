"""Control API routes over a real Registry (no socket). Covers the pool:pid
scenario apply (staged, old-format 400, quirk routing, responses normalization),
resets, stats/history filters, advance, and ws/emit."""

from provider_simulator.chains import CHAINS
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
    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"chain_family": "eth", "mode": "down"}}})
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
    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"mode": "down"}, "eth-sim:2": {"mode": "bogus"}}})
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
    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"responses": {"eth_call": {"mode": "error"}}}}})
    assert st == 400


# ── resets ────────────────────────────────────────────────────────────────────
def test_reset_clears_scenario():
    api = _api()
    api.apply_scenario({"providers": {"eth-sim:1": {"mode": "down"}}})
    api.reset()
    _, scen = api.get_scenario()
    assert scen["providers"]["eth-sim:1"]["mode"] == "success"


def _arm_two_pools(api):
    """Put one fault in eth-sim and one in btc-sim, and prove both landed."""
    st, _ = api.apply_scenario({"providers": {"eth-sim:1": {"mode": "down"}, "btc-sim:1": {"mode": "down"}}})
    assert st == 200
    _, scen = api.get_scenario()
    assert scen["providers"]["eth-sim:1"]["mode"] == "down"
    assert scen["providers"]["btc-sim:1"]["mode"] == "down"


def test_reset_one_pool_leaves_another_pools_fault_armed():
    api = _api()
    _arm_two_pools(api)
    st, resp = api.reset("btc-sim")
    assert st == 200
    assert resp["pool"] == "btc-sim"
    _, scen = api.get_scenario()
    assert scen["providers"]["btc-sim:1"]["mode"] == "success"
    assert scen["providers"]["eth-sim:1"]["mode"] == "down"


def test_reset_without_a_pool_still_clears_every_pool():
    api = _api()
    _arm_two_pools(api)
    st, resp = api.reset()
    assert st == 200
    assert resp["pool"] is None
    _, scen = api.get_scenario()
    assert scen["providers"]["btc-sim:1"]["mode"] == "success"
    assert scen["providers"]["eth-sim:1"]["mode"] == "success"


def test_reset_unknown_pool_is_400_naming_the_real_pools():
    api = _api()
    _arm_two_pools(api)
    st, resp = api.reset("eth-simm")
    assert st == 400
    assert "eth-simm" in resp["error"]
    for pool in ("eth-sim", "btc-sim", "solana-sim"):
        assert pool in resp["error"]
    # A rejected reset must not half-clear anything.
    _, scen = api.get_scenario()
    assert scen["providers"]["eth-sim:1"]["mode"] == "down"
    assert scen["providers"]["btc-sim:1"]["mode"] == "down"


def test_reset_all_unknown_pool_is_400_and_clear_history_too():
    api = _api()
    for route in (api.reset_all, api.clear_history):
        st, resp = route("no-such-pool")
        assert st == 400
        assert "no-such-pool" in resp["error"]
        assert "eth-sim" in resp["error"]


def test_reset_pool_must_be_a_string():
    api = _api()
    st, resp = api.reset(7)
    assert st == 400
    assert "string" in resp["error"]


def _push(provider, method):
    provider.log.push(method, "success", 0, interface="jsonrpc", transport="http", port=18545, request_id=1)


def test_reset_all_one_pool_leaves_another_pools_history():
    api = _api()
    _push(api.registry.provider("eth-sim", "1"), "eth_blockNumber")
    _push(api.registry.provider("btc-sim", "1"), "getblockcount")
    st, resp = api.reset_all("btc-sim")
    assert st == 200
    assert resp["providers"] == sorted(p.key for p in api.registry.pools["btc-sim"].providers.values())
    _, hist = api.get_history({})
    assert [e["method"] for e in hist["history"]] == ["eth_blockNumber"]


def test_reset_all_without_a_pool_still_clears_every_history():
    api = _api()
    _push(api.registry.provider("eth-sim", "1"), "eth_blockNumber")
    _push(api.registry.provider("btc-sim", "1"), "getblockcount")
    st, _ = api.reset_all()
    assert st == 200
    _, hist = api.get_history({})
    assert hist["count"] == 0


def test_clear_history_one_pool_leaves_another_pools_history():
    api = _api()
    _push(api.registry.provider("eth-sim", "1"), "eth_blockNumber")
    _push(api.registry.provider("btc-sim", "1"), "getblockcount")
    st, _ = api.clear_history("eth-sim")
    assert st == 200
    _, hist = api.get_history({})
    assert [e["method"] for e in hist["history"]] == ["getblockcount"]


def test_reset_of_another_pool_leaves_this_chains_head_where_it_was():
    """The nightly failure this scoping fixes: one test's clean-up rewound a
    chain height a different router was being measured against. eth is the only
    chain with a movable head today, so btc-sim is the unrelated pool here."""
    api = _api()
    try:
        base = CHAINS["eth"].head.current()
        api.advance({"chain": "eth", "blocks": 5})
        assert CHAINS["eth"].head.current() == base + 5
        st, resp = api.reset("btc-sim")
        assert st == 200
        assert resp["chains"] == ["btc"]
        assert CHAINS["eth"].head.current() == base + 5
        st, resp = api.reset("eth-sim")
        assert st == 200
        assert resp["chains"] == ["eth"]
        assert CHAINS["eth"].head.current() == base
    finally:
        api.reset()  # heads are a shared singleton — don't leak into other tests


def test_pool_scoped_reset_names_only_its_own_providers():
    api = _api()
    st, resp = api.reset_all("eth-solo-sim")
    assert st == 200
    assert resp["providers"] == ["eth-solo-sim:1"]
    assert resp["chains"] == ["eth"]


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
    p2.log.push("eth_call", "error", 0, interface="jsonrpc", transport="http", port=18546, request_id=2)
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
        p.log.push("m", "success", 0, interface="jsonrpc", transport="http", port=18545, request_id=i)
    _, hist = api.get_history({"max": "2"})
    assert hist["count"] == 2
    st, resp = api.get_history({"max": "-1"})
    assert st == 400


# ── topology ──────────────────────────────────────────────────────────────────
def test_topology_shape():
    api = _api()
    st, topo = api.get_topology()
    assert st == 200
    assert topo["topology"]["eth-sim"]["chain"] == "eth"
    assert topo["topology"]["eth-sim"]["providers"]["1"] == [
        {"interface": "jsonrpc", "transport": "http", "port": 18545},
        {"interface": "jsonrpc", "transport": "ws", "port": 18557},
    ]


def test_topology_covers_every_pool():
    api = _api()
    _, topo = api.get_topology()
    assert set(topo["topology"]) == set(build_registry().pools)


def test_topology_has_no_side_effects():
    api = _api()
    _, stats_before = api.get_stats()
    _, topo1 = api.get_topology()
    _, topo2 = api.get_topology()
    assert topo1 == topo2
    _, stats_after = api.get_stats()
    assert stats_before == stats_after


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
