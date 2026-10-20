"""Control API routes over a real Registry (no socket). Covers the pool:pid
scenario apply (staged, old-format 400, quirk routing, responses normalization),
resets, stats/history filters, advance, and ws/emit."""

import hashlib
import json
from collections import Counter

import pytest

from provider_simulator.chains import CHAINS
from provider_simulator.control_api import ControlApi
from provider_simulator.domain.call_log import CallLog
from provider_simulator.domain.registry import build_registry
from provider_simulator.fault_policy import targets
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


def test_a_misspelt_per_method_mode_is_rejected():
    """A per-method ``mode`` the simulator does not know was stored, and the
    method answered as if it had no override: the fault never fired. The
    refusal names the value, the method and the modes that are accepted."""
    api = _api()
    block = {"responses": {"eth_call": {"mode": "dwon"}}}
    st, resp = api.apply_scenario({"providers": {"eth-sim:1": block}})
    assert st == 400
    assert "eth-sim:1" in resp["error"]
    assert "invalid mode 'dwon'" in resp["error"]
    assert "'drop_connection'" in resp["error"]
    assert "eth_call" in resp["error"]
    assert api.registry.provider("eth-sim", "1").scenario.snapshot()["responses"] == {}


def test_a_misspelt_per_method_mode_is_rejected_in_the_rest_form_too():
    """REST sends its overrides as ``[[verb, template], cfg]`` pairs, a second
    path through the same rule."""
    api = _api()
    block = {"responses": [[["GET", "/x"], {"mode": "hnag"}]]}
    st, resp = api.apply_scenario({"providers": {"lava-sim-rest:1": block}})
    assert st == 400
    assert "invalid mode 'hnag'" in resp["error"]
    assert "/x" in resp["error"]
    assert api.registry.provider("lava-sim-rest", "1").scenario.snapshot()["responses"] == {}


@pytest.mark.parametrize("mode", ["success", "rate_limit", "down", "hang", "drop_connection"])
def test_each_per_method_mode_the_simulator_applies_is_accepted(mode):
    """The positive control for the two tests above: a rule that refused every
    per-method mode would pass them."""
    api = _api()
    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"responses": {"eth_call": {"mode": mode}}}}})
    assert st == 200, resp
    assert api.registry.provider("eth-sim", "1").scenario.snapshot()["responses"] == {"eth_call": {"mode": mode}}


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


def _provider_that_ran_a_transaction(api):
    provider = api.registry.provider("eth-sim", "1")
    provider.remember_executed("d1", 1.0)
    return provider


def test_the_scenario_reset_and_the_full_reset_empty_the_memory_of_executed_transactions():
    api = _api()
    for route in (api.reset, api.reset_all):
        provider = _provider_that_ran_a_transaction(api)
        status, _ = route()
        assert status == 200
        assert provider.executed_snapshot() == {}


def test_a_pool_scoped_reset_empties_the_memory_of_that_pool_only():
    api = _api()
    one = _provider_that_ran_a_transaction(api)
    other = api.registry.provider("btc-sim", "1")
    other.remember_executed("d2", 2.0)
    status, _ = api.reset("eth-sim")
    assert status == 200
    assert one.executed_snapshot() == {}
    assert other.executed_snapshot() == {"d2": 2.0}


def test_clearing_the_history_and_a_scenario_update_leave_the_memory_of_executed_transactions():
    api = _api()
    provider = _provider_that_ran_a_transaction(api)
    status, _ = api.clear_history()
    assert status == 200
    assert provider.executed_snapshot() == {"d1": 1.0}
    status, _ = api.apply_scenario(
        {"providers": {"eth-sim:1": {"mode": "success", "latency_ms": 5, "fail_first_n": 2}}}
    )
    assert status == 200
    assert provider.executed_snapshot() == {"d1": 1.0}


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


def _row_with_body(provider, body: bytes, request_id: int) -> None:
    """One finished row whose request body is ``body``, as a listener leaves it."""
    entry = provider.log.record_arrival("graphql", "http", 18999)
    provider.log.record_body(entry, body)
    provider.log.finalize(entry, method="m", status="success", latency_ms=0, request_id=request_id)


def test_history_can_be_filtered_by_body_hash_and_length():
    api = _api()
    first, second = b'{"query":"{ a }"}', b'{"query":"{ b }"} '
    assert len(first) != len(second)
    provider, other = api.registry.provider("eth-sim", "1"), api.registry.provider("eth-sim", "2")
    _row_with_body(provider, first, 1)
    _row_with_body(provider, second, 2)
    _row_with_body(other, first, 3)  # the same body on another provider
    digest = hashlib.sha256(first).hexdigest()
    one = {"pool": "eth-sim", "pid": "1"}

    def ids(extra: dict) -> list:
        status, reply = api.get_history({**one, **extra})
        assert status == 200
        return [row["request_id"] for row in reply["history"]]

    assert ids({"body_sha256": digest}) == [1]
    assert ids({"body_sha256": digest.upper()}) == [1]
    assert ids({"body_sha256": "0" * 64}) == []
    assert ids({"body_len": str(len(first))}) == [1]
    assert ids({"body_len": str(len(second))}) == [2]
    assert ids({"body_sha256": digest, "body_len": str(len(first) + 1)}) == []
    assert ids({"body_sha256": digest, "body_len": str(len(first))}) == [1]
    assert ids({}) == [1, 2]


def test_a_body_len_that_is_not_an_integer_is_a_400():
    api = _api()
    status, reply = api.get_history({"body_len": "many"})
    assert status == 400
    assert "body_len" in reply["error"]


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


def test_advance_a_chain_with_no_head_is_400():
    """A chain nothing can advance is refused, and says so.

    This asked about btc until btc grew a head. The behaviour it locks is
    real and unchanged -- it just needed a chain that still has none, and
    ln is the remaining one. Renamed because the old name said "unknown
    head", which is a different refusal with its own test.
    """
    api = _api()
    st, resp = api.advance({"chain": "ln", "blocks": 5})
    assert st == 400, resp
    assert "no advanceable head" in resp["error"], resp["error"]


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


# ── GET /version ──────────────────────────────────────────────────────────────
def test_version_reports_the_stamped_build(monkeypatch):
    monkeypatch.setenv("SIM_GIT_COMMIT", "cd0c74c2ad8e9ff36d24ce19e29e6db2aba6ec3e")
    monkeypatch.setenv("SIM_GIT_VERSION", "v1.4.0")
    monkeypatch.setenv("SIM_GIT_DESCRIBE", "v1.4.0")
    assert _api().version() == (
        200,
        {
            "version": "v1.4.0",
            "commit": "cd0c74c2ad8e9ff36d24ce19e29e6db2aba6ec3e",
            "git_describe": "v1.4.0",
            "state": "release",
        },
    )


def test_version_is_200_when_nothing_was_stamped(monkeypatch):
    """An unidentifiable build is still a working simulator, so the route
    answers 200 with "unknown" rather than failing. A non-2xx here would make
    every probe that watches this route report a broken pod."""
    for name in ("SIM_GIT_COMMIT", "SIM_GIT_VERSION", "SIM_GIT_DESCRIBE"):
        monkeypatch.delenv(name, raising=False)
    assert _api().version() == (
        200,
        {"version": None, "commit": None, "git_describe": None, "state": "unknown"},
    )


# ── a chain that serves several heads ─────────────────────────────────────────
# Lava answers a different height on each of its three protocols, so one head
# cannot describe it. These pin that each is addressable, that reading one
# never moves it, and that a reset clears all three rather than one.


def test_advance_reads_a_head_back_without_moving_it():
    """A body naming no movement is the READ, and it must not move anything.

    This is what a test comparing the router's reported tip against the truth
    calls. If it moved the head, the comparison would chase its own change.
    """
    api = _api()
    try:
        first = api.advance({"chain": "eth"})[1]["head"]
        second = api.advance({"chain": "eth"})[1]["head"]
        assert first == second, f"reading the head moved it: {first} then {second}"
    finally:
        api.reset()


def test_lava_serves_one_head_per_interface():
    api = _api()
    try:
        st, resp = api.advance({"chain": "lava", "head": "rest"})
        assert st == 200
        assert sorted(resp["heads"]) == ["grpc", "rest", "tendermintrpc"]
        # The three differ on purpose. Equal values would mean one head was
        # serving all three, which is the arrangement this replaced.
        assert (
            len(set(resp["heads"].values())) == 3
        ), f"the three interfaces must keep their own heights, got {resp['heads']}"
    finally:
        api.reset()


def test_lava_without_a_head_name_is_refused_and_names_the_heads():
    """Refusing beats guessing: picking one silently would move the wrong one."""
    api = _api()
    st, resp = api.advance({"chain": "lava", "blocks": 5})
    assert st == 400
    for name in ("rest", "grpc", "tendermintrpc"):
        assert name in resp["error"], f"the refusal must name {name}: {resp['error']}"


def test_advancing_one_lava_head_leaves_the_others_alone():
    api = _api()
    try:
        before = api.advance({"chain": "lava", "head": "rest"})[1]["heads"]
        after = api.advance({"chain": "lava", "head": "rest", "blocks": 7})[1]["heads"]
        assert after["rest"] == before["rest"] + 7
        assert after["grpc"] == before["grpc"]
        assert after["tendermintrpc"] == before["tendermintrpc"]
    finally:
        api.reset()


def test_a_reset_clears_every_lava_head_not_only_one():
    """One un-reset head carries a test's advance into the next test."""
    api = _api()
    try:
        base = api.advance({"chain": "lava", "head": "rest"})[1]["heads"]
        for name in ("rest", "grpc", "tendermintrpc"):
            api.advance({"chain": "lava", "head": name, "blocks": 11})
        api.reset()
        after = api.advance({"chain": "lava", "head": "rest"})[1]["heads"]
        assert after == base, f"a reset left a head moved: {base} then {after}"
    finally:
        api.reset()


def test_advance_names_the_chains_when_the_chain_does_not_exist():
    api = _api()
    st, resp = api.advance({"chain": "nosuchchain"})
    assert st == 400
    # Before this, a missing chain and a chain with no head gave the identical
    # message, so a caller could not tell a typo from an unsupported chain.
    assert "no chain" in resp["error"], resp["error"]
    assert "lava" in resp["error"], resp["error"]


def test_reading_a_many_headed_chain_needs_no_head_name():
    """A read has nothing to choose, so it must not demand a choice.

    Requiring the name to READ would make every caller carry a table of which
    chain has which heads. The reply already contains all of them.
    """
    api = _api()
    try:
        st, resp = api.advance({"chain": "lava"})
        assert st == 200
        assert sorted(resp["heads"]) == ["grpc", "rest", "tendermintrpc"]
        assert resp["head_name"] is None
        assert resp["head"] is None, "a read that named no head must not claim one"
    finally:
        api.reset()


def test_moving_a_many_headed_chain_still_needs_a_head_name():
    """A move IS ambiguous, so it stays refused. The read relaxation is not a
    licence to guess which head a caller meant to move."""
    api = _api()
    st, resp = api.advance({"chain": "lava", "blocks": 3})
    assert st == 400
    assert "move" in resp["error"], resp["error"]


def test_reading_a_many_headed_chain_moves_nothing():
    api = _api()
    try:
        first = api.advance({"chain": "lava"})[1]["heads"]
        second = api.advance({"chain": "lava"})[1]["heads"]
        assert first == second, f"the read moved a head: {first} then {second}"
    finally:
        api.reset()


def test_a_single_headed_chain_needs_no_name_either_way():
    """Eth has one head, so neither reading nor moving it forces a choice."""
    api = _api()
    try:
        read = api.advance({"chain": "eth"})[1]
        assert read["head_name"] == "default"
        assert read["head"] == read["heads"]["default"]
        moved = api.advance({"chain": "eth", "blocks": 6})[1]
        assert moved["head"] == read["head"] + 6
    finally:
        api.reset()


def test_a_wrong_head_name_is_refused_rather_than_silently_substituted():
    """A typo must not move a different head.

    Added after a review mutation: replacing the unknown-name guard with a
    silent pick of the first head passed the ENTIRE suite. The tests covered
    an OMITTED name and never a WRONG one, so the guard was free to delete.
    'tendermint' is the realistic typo for 'tendermintrpc'.
    """
    api = _api()
    try:
        before = api.advance({"chain": "lava"})[1]["heads"]
        st, resp = api.advance({"chain": "lava", "head": "tendermint", "blocks": 100})
        assert st == 400, f"a wrong head name must be refused, got {st} {resp}"
        assert "tendermint'" in resp["error"], resp["error"]
        after = api.advance({"chain": "lava"})[1]["heads"]
        assert after == before, f"the refused call moved something: {before} then {after}"
    finally:
        api.reset()


def test_blocks_zero_is_a_read_not_a_move():
    """``blocks: 0`` changes nothing, so it must not demand a head name.

    Callers send it as a read. Testing for the KEY rather than the value
    refused those on a many-headed chain, naming a move they had not asked for.
    """
    api = _api()
    try:
        st, resp = api.advance({"chain": "lava", "blocks": 0})
        assert st == 200, f"blocks=0 is a read and must be allowed: {resp}"
        assert sorted(resp["heads"]) == ["grpc", "rest", "tendermintrpc"]
    finally:
        api.reset()


def test_setting_a_rate_to_zero_still_counts_as_a_move():
    """Stopping an advancing head IS a change, so it still needs a name."""
    api = _api()
    st, resp = api.advance({"chain": "lava", "per_second": 0})
    assert st == 400, f"per_second is a move at any value, got {st} {resp}"


def test_each_chain_owns_its_own_heads():
    """A mutable default on the base class would be ONE dict for every chain.

    Proven by a review: writing a head onto eth gave btc and solana the same
    one, so a bogus head name would have been accepted on every chain.
    """
    from provider_simulator.chains import CHAINS
    from provider_simulator.chains.base import AdvancingHead

    # Detect the sharing directly. Comparing identity is NOT enough: a shared
    # EMPTY dict looks the same as two chains that each own none, which is how
    # the first version of this test passed against the very bug it names. A
    # review mutation restoring the shared default left the whole suite green.
    # Writing into one chain and reading another is what tells them apart.
    eth, btc = CHAINS["eth"], CHAINS["btc"]
    sentinel = "__only_eth_should_see_this__"
    try:
        if not hasattr(eth, "heads"):
            eth.heads = {}
        eth.heads[sentinel] = AdvancingHead(1)
        leaked = [n for n, _ in btc.iter_heads()]
        assert sentinel not in leaked, (
            f"btc sees a head written onto eth: {leaked}. The base class is handing "
            f"every chain ONE dict, so a head added to any chain exists on all of them."
        )
    finally:
        getattr(eth, "heads", {}).pop(sentinel, None)


def test_a_chain_that_is_not_a_string_is_refused_rather_than_crashing():
    """A JSON body can carry a list or an object where a name belongs.

    ``CHAINS.get`` is a dict lookup, so an unhashable key raises TypeError,
    and ``do_POST`` does not catch what ``advance`` raises — the caller gets a
    dropped connection instead of an answer. Every other control route already
    checks the type of a name it is handed; this one did not.
    """
    api = _api()
    for wrong in ([], {"a": 1}, 7, None):
        st, resp = api.advance({"chain": wrong})
        assert st == 400, f"chain={wrong!r} must be refused, got {st} {resp}"
        assert "chain must be a string" in resp["error"], resp["error"]


def test_a_malformed_move_is_refused_rather_than_answered_as_a_read():
    """``blocks: null`` and ``blocks: false`` are falsey, not absent.

    The move/read decision used to read the VALUE's truthiness, so a caller
    who asked to move with a malformed number was quietly given a read, told
    nothing was wrong, and saw an unmoved head. A string got past that and
    reached ``bump``, which raised ValueError out of the handler. Both are
    refused now, and neither may move anything.
    """
    api = _api()
    try:
        before = api.advance({"chain": "lava"})[1]["heads"]
        for field_name, wrong in (
            ("blocks", None),
            ("blocks", False),
            ("blocks", "abc"),
            ("blocks", 1.5),
            ("blocks", -1),
            ("per_second", None),
            ("per_second", "fast"),
            ("per_second", -1),
        ):
            body = {"chain": "lava", "head": "rest", field_name: wrong}
            st, resp = api.advance(body)
            assert st == 400, f"{field_name}={wrong!r} must be refused, got {st} {resp}"
            assert field_name in resp["error"], resp["error"]
            after = api.advance({"chain": "lava"})[1]["heads"]
            assert after == before, f"the refused {body} moved something: {before} then {after}"
    finally:
        api.reset()


def test_advance_moves_every_chains_head_through_the_real_registry():
    """The control route has to reach the chain the LISTENER serves.

    The per-chain tests build their own chain object, so they cannot tell
    whether a head is wired to the ``CHAINS`` singleton that answers real
    traffic. Proven by mutation: taking the heads off btc and solana entirely
    left this whole file green, because only eth was ever driven through here.

    Each chain is moved on its own and the others are read back unmoved, so a
    head shared between chains fails rather than passing on the total.
    """
    api = _api()
    try:
        names = ["eth", "btc", "solana"]
        start = {c: api.advance({"chain": c})[1]["heads"]["default"] for c in names}
        for moving in names:
            st, resp = api.advance({"chain": moving, "blocks": 3})
            assert st == 200, resp
            assert resp["heads"]["default"] == start[moving] + 3, resp
            for other in names:
                if other == moving:
                    continue
                now = api.advance({"chain": other})[1]["heads"]["default"]
                expected = start[other] + (3 if names.index(other) < names.index(moving) else 0)
                assert now == expected, f"moving {moving} disturbed {other}: {now} != {expected}"
    finally:
        api.reset()


def test_a_reset_rewinds_the_new_heads_too():
    """A head nothing rewinds leaks into whatever runs next."""
    api = _api()
    start = {c: api.advance({"chain": c})[1]["heads"]["default"] for c in ("btc", "solana")}
    for chain in ("btc", "solana"):
        api.advance({"chain": chain, "blocks": 250})
    assert {c: api.advance({"chain": c})[1]["heads"]["default"] for c in ("btc", "solana")} != start
    api.reset()
    assert {c: api.advance({"chain": c})[1]["heads"]["default"] for c in ("btc", "solana")} == start


# ── the ports filter, checked against the provider's own endpoints ────────────


def test_a_ports_filter_the_provider_does_not_serve_is_400():
    """The half ScenarioConfig cannot do: it holds no endpoints, so only here can
    a port the provider does not serve be caught.

    Refused rather than ignored because a filter matching no endpoint applies no
    fault. The provider then answers normally, and a test that mistyped a port
    reads a healthy provider and concludes the router recovered."""
    api = _api()
    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"mode": "down", "ports": [19999]}}})
    assert st == 400
    assert "19999" in resp["error"]
    # The message names what the provider DOES serve, because the mistake is
    # nearly always a neighbour's port.
    assert "serves" in resp["error"]
    # Nothing was applied — the whole request is refused, not part of it.
    _, scen = api.get_scenario()
    assert scen["providers"]["eth-sim:1"]["mode"] == "success"


def test_a_ports_filter_the_provider_does_serve_is_accepted():
    """The positive control for the test above. Without it, a bug that refused
    EVERY ports filter would pass that test and break the feature."""
    api = _api()
    _, providers = api.get_providers({})
    served = sorted(ep["port"] for ep in providers["providers"]["eth-sim:1"]["endpoints"])
    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"mode": "down", "ports": [served[0]]}}})
    assert st == 200, resp
    assert resp["applied"]["eth-sim:1"]["ports"] == [served[0]]


def test_one_bad_port_among_good_ones_refuses_the_whole_block():
    api = _api()
    _, providers = api.get_providers({})
    served = sorted(ep["port"] for ep in providers["providers"]["eth-sim:1"]["endpoints"])
    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"mode": "down", "ports": [served[0], 19999]}}})
    assert st == 400
    assert "19999" in resp["error"]
    assert str(served[0]) not in resp["error"].split("are not served")[0]


def test_a_port_belonging_to_another_provider_is_still_refused():
    """The mistake this is most likely to catch: a port that exists in the
    topology, just not on this provider. A check that only asked "is this a real
    port anywhere" would accept it, and the fault would silently apply to
    nothing."""
    api = _api()
    _, providers = api.get_providers({})
    mine = {ep["port"] for ep in providers["providers"]["eth-sim:1"]["endpoints"]}
    neighbour = next(
        ep["port"]
        for key, v in providers["providers"].items()
        if key != "eth-sim:1"
        for ep in v["endpoints"]
        if ep["port"] not in mine
    )
    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"mode": "down", "ports": [neighbour]}}})
    assert st == 400
    assert str(neighbour) in resp["error"]


# ── a mis-shaped ports value must be ANSWERED, never dropped ─────────────────


def test_a_scalar_ports_value_is_400_and_not_a_crash():
    """The regression this guards is not a wrong answer, it is NO answer.

    _ports_the_provider_does_not_serve used to iterate ``ports`` before anything
    had judged its shape. A bare int is not iterable, so the comprehension raised
    TypeError — which is not a ValueError, so the handler's guard never saw it, it
    escaped do_POST and the client's connection was dropped. A dropped connection
    reads as a broken simulator rather than a refused caller, which is exactly the
    failure PR #125 measured and fixed for the transports rule.
    """
    for bad in (18545, True, "18545"):
        api = _api()
        st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"mode": "down", "ports": bad}}})
        assert st == 400, f"ports={bad!r} gave {st}"
        assert "ports must be a list" in resp["error"], f"ports={bad!r} gave {resp['error']!r}"


def test_a_string_ports_value_is_not_read_as_five_ports():
    """A string is iterable, so it was the worst of the three: it did not crash,
    it produced a refusal naming ['1', '8', '5', '4', '5'] as unserved ports."""
    api = _api()
    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"mode": "down", "ports": "18545"}}})
    assert st == 400
    assert "'1'" not in resp["error"], f"the string was iterated: {resp['error']!r}"


# ── a pause that no named address could perform ──────────────────────────────


def test_a_pause_scoped_to_a_non_http_port_is_refused():
    """The same intent had two spellings and opposite answers.

    _validate_pause refuses a pause paired with transports lacking http, because
    only the HTTP write path performs one. It cannot make that check for ``ports``
    — a port's transport is a fact about the provider — so a pause scoped to a ws
    port was accepted, stored, echoed back, and did nothing.

    scenario.py names that as the worst outcome, and gives the reason: a paused
    reply is a CORRECT reply, so a test whose pause never armed reads a fast valid
    answer and concludes the router was fast.
    """
    api = _api()
    _, providers = api.get_providers({})
    eps = providers["providers"]["eth-sim:1"]["endpoints"]
    ws_port = next(e["port"] for e in eps if e["transport"] == "ws")
    st, resp = api.apply_scenario(
        {"providers": {"eth-sim:1": {"pause_at": "mid_body", "pause_ms": 50, "ports": [ws_port]}}}
    )
    assert st == 400, f"a pause on a ws-only ports filter must be refused, got {st}"
    assert "pause_at" in resp["error"] and str(ws_port) in resp["error"]


def test_a_pause_scoped_to_an_http_port_is_accepted():
    """The positive control. Without it, a bug refusing EVERY pause-plus-ports
    combination would pass the test above and break a legitimate use."""
    api = _api()
    _, providers = api.get_providers({})
    eps = providers["providers"]["eth-sim:1"]["endpoints"]
    http_port = next(e["port"] for e in eps if e["transport"] == "http")
    st, resp = api.apply_scenario(
        {"providers": {"eth-sim:1": {"pause_at": "mid_body", "pause_ms": 50, "ports": [http_port]}}}
    )
    assert st == 200, resp
    assert resp["applied"]["eth-sim:1"]["ports"] == [http_port]


def test_a_pause_armed_earlier_still_refuses_a_non_http_ports_filter_later():
    """Read EFFECTIVELY, merged over what is stored, because a POST merges rather
    than replaces. A pause armed in one call and a ws-only ports filter set in the
    next would otherwise slip past a check that looked at one request only."""
    api = _api()
    _, providers = api.get_providers({})
    eps = providers["providers"]["eth-sim:1"]["endpoints"]
    ws_port = next(e["port"] for e in eps if e["transport"] == "ws")
    st, _ = api.apply_scenario({"providers": {"eth-sim:1": {"pause_at": "mid_body", "pause_ms": 50}}})
    assert st == 200, "a pause with no filter at all is fine"
    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"ports": [ws_port]}}})
    assert st == 400, f"the stored pause must still be considered, got {st}"
    assert "pause_at" in resp["error"]


# ── invalid_json on a provider that has only gRPC endpoints ───────────────────


def test_invalid_json_on_a_provider_with_only_grpc_endpoints_is_400():
    """``invalid_json`` breaks the bytes of a JSON body, and a gRPC reply has
    none. Before this rule the value was stored, and it did nothing: each call
    got a clean reply. The message names the mode that a gRPC provider can
    apply."""
    api = _api()
    st, resp = api.apply_scenario({"providers": {"lava-sim-grpc:1": {"corruption_mode": "invalid_json"}}})
    assert st == 400
    assert "lava-sim-grpc:1" in resp["error"]
    assert "'invalid_json' cannot apply" in resp["error"]
    assert "only gRPC endpoints" in resp["error"]
    assert "'invalid_proto'" in resp["error"]
    _, scen = api.get_scenario()
    assert scen["providers"]["lava-sim-grpc:1"]["corruption_mode"] is None


@pytest.mark.parametrize("key", ["eth-sim:1", "lava-sim-rest:1", "lava-sim-tm:1"])
def test_invalid_json_on_a_provider_with_a_json_endpoint_is_accepted(key):
    """The positive control for the test above. Without it, a rule that refused
    ``invalid_json`` for each provider would pass that test."""
    api = _api()
    st, resp = api.apply_scenario({"providers": {key: {"corruption_mode": "invalid_json"}}})
    assert st == 200, resp
    assert resp["applied"][key]["corruption_mode"] == "invalid_json"


@pytest.mark.parametrize(
    "mode", ["truncated", "missing_field", "empty_response", "wrong_type", "null_body", "invalid_proto"]
)
def test_each_other_corruption_mode_is_accepted_on_a_grpc_provider(mode):
    """The gRPC listener applies each of the six: the rule refuses one mode."""
    api = _api()
    st, resp = api.apply_scenario({"providers": {"lava-sim-grpc:1": {"corruption_mode": mode}}})
    assert st == 200, resp
    assert resp["applied"]["lava-sim-grpc:1"]["corruption_mode"] == mode


def test_an_invalid_json_refusal_writes_no_provider_of_the_request():
    api = _api()
    blocks = {
        "eth-sim:1": {"mode": "down"},
        "lava-sim-grpc:1": {"mode": "rate_limit", "corruption_mode": "invalid_json"},
    }
    st, resp = api.apply_scenario({"providers": blocks})
    assert st == 400, resp
    _, scen = api.get_scenario()
    assert scen["providers"]["eth-sim:1"]["mode"] == "success"
    assert scen["providers"]["lava-sim-grpc:1"]["mode"] == "success"


def test_invalid_json_on_a_grpc_provider_is_refused_with_a_filter_too():
    """The rule reads the endpoints of the provider and no filter. A filter can
    only name fewer endpoints, and each endpoint of this provider is gRPC."""
    api = _api()
    block = {"corruption_mode": "invalid_json", "transports": ["http2"]}
    st, resp = api.apply_scenario({"providers": {"lava-sim-grpc:1": block}})
    assert st == 400
    assert "only gRPC endpoints" in resp["error"]


def test_invalid_json_is_accepted_when_one_endpoint_of_the_provider_is_not_grpc():
    """No provider of the topology has a gRPC endpoint and an endpoint of
    another interface. This provider has both, and its REST endpoint can apply
    the corruption."""
    endpoints = (("grpc", "http2", 40001), ("rest", "http", 40002))
    rows = (("lava-mixed-sim", "lava", "1", "LavaMixedProvider1", False, "", endpoints),)
    api = ControlApi(build_registry(rows), WsSubscriptions())
    st, resp = api.apply_scenario({"providers": {"lava-mixed-sim:1": {"corruption_mode": "invalid_json"}}})
    assert st == 200, resp


# ── invalid_proto on a provider that has no gRPC endpoint ─────────────────────


@pytest.mark.parametrize("key", ["eth-sim:1", "lava-sim-rest:1", "lava-sim-tm:1"])
def test_invalid_proto_on_a_provider_with_no_grpc_endpoint_is_400(key):
    """``invalid_proto`` ends a gRPC call with the status UNKNOWN, and only the
    gRPC listener applies it. On a provider with no gRPC endpoint the value was
    stored and did nothing: each call got a clean reply. The message names the
    mode that such a provider can apply."""
    api = _api()
    st, resp = api.apply_scenario({"providers": {key: {"corruption_mode": "invalid_proto"}}})
    assert st == 400
    assert key in resp["error"]
    assert "'invalid_proto' cannot apply" in resp["error"]
    assert "no gRPC endpoint" in resp["error"]
    assert "'invalid_json'" in resp["error"]
    _, scen = api.get_scenario()
    assert scen["providers"][key]["corruption_mode"] is None


def test_invalid_proto_is_accepted_when_one_endpoint_of_the_provider_is_grpc():
    """The positive control for the test above, on the shape the rule must not
    refuse: a gRPC endpoint beside an endpoint of another interface. A provider
    with only gRPC endpoints is covered by
    ``test_each_other_corruption_mode_is_accepted_on_a_grpc_provider``."""
    endpoints = (("grpc", "http2", 40001), ("rest", "http", 40002))
    rows = (("lava-mixed-sim", "lava", "1", "LavaMixedProvider1", False, "", endpoints),)
    api = ControlApi(build_registry(rows), WsSubscriptions())
    st, resp = api.apply_scenario({"providers": {"lava-mixed-sim:1": {"corruption_mode": "invalid_proto"}}})
    assert st == 200, resp
    assert resp["applied"]["lava-mixed-sim:1"]["corruption_mode"] == "invalid_proto"


def test_an_invalid_proto_refusal_writes_no_provider_of_the_request():
    api = _api()
    blocks = {
        "lava-sim-grpc:1": {"mode": "down"},
        "eth-sim:1": {"mode": "rate_limit", "corruption_mode": "invalid_proto"},
    }
    st, resp = api.apply_scenario({"providers": blocks})
    assert st == 400, resp
    _, scen = api.get_scenario()
    assert scen["providers"]["lava-sim-grpc:1"]["mode"] == "success"
    assert scen["providers"]["eth-sim:1"]["mode"] == "success"


# ── the fields only the GraphQL listener reads, on a provider with no GraphQL endpoint ──

_GRAPHQL_ONLY = [
    ("rate_limit_style", "body", "status"),
    ("error_body", {"errors": [{"message": "refused"}]}, None),
    ("unreadable_reply", {"status": 502, "body": "bad gateway"}, None),
]


@pytest.mark.parametrize("field_name, value, default", _GRAPHQL_ONLY, ids=[row[0] for row in _GRAPHQL_ONLY])
@pytest.mark.parametrize("key", ["eth-sim:1", "lava-sim-rest:1", "lava-sim-grpc:1"])
def test_a_graphql_only_field_on_a_provider_with_no_graphql_endpoint_is_400(key, field_name, value, default):
    """Only the GraphQL listener reads ``rate_limit_style``, ``error_body`` and
    ``unreadable_reply``. On a provider of another interface each value was stored
    and did nothing: every call got its normal reply, and a test that had armed
    the fault read a healthy provider as a router that recovered."""
    api = _api()
    st, resp = api.apply_scenario({"providers": {key: {field_name: value}}})
    assert st == 400, resp
    assert key in resp["error"]
    assert f"{field_name} cannot apply" in resp["error"]
    assert "no GraphQL endpoint" in resp["error"]
    _, scen = api.get_scenario()
    assert scen["providers"][key][field_name] == default


@pytest.mark.parametrize("field_name, value, default", _GRAPHQL_ONLY, ids=[row[0] for row in _GRAPHQL_ONLY])
def test_a_graphql_only_field_is_accepted_when_the_provider_has_a_graphql_endpoint(field_name, value, default):
    """The positive control for the test above: the same value on a provider that
    has a GraphQL endpoint is stored, and the reply says so."""
    rows = (("sui-graphql-x-sim", "sui", "1", "SuiGraphqlX1", False, "", (("graphql", "http", 40003),)),)
    api = ControlApi(build_registry(rows), WsSubscriptions())
    st, resp = api.apply_scenario({"providers": {"sui-graphql-x-sim:1": {field_name: value}}})
    assert st == 200, resp
    assert resp["applied"]["sui-graphql-x-sim:1"][field_name] == value


def test_the_default_of_each_graphql_only_field_is_accepted_on_a_provider_with_no_graphql_endpoint():
    """A caller that resets every field of a provider does not know its interface.
    The value that a field has when it is not set changes no reply, so the rule
    leaves it alone."""
    api = _api()
    block = {"rate_limit_style": "status", "error_body": None, "unreadable_reply": None}
    st, resp = api.apply_scenario({"providers": {"eth-sim:1": block}})
    assert st == 200, resp


def test_a_graphql_only_refusal_writes_no_provider_of_the_request():
    api = _api()
    blocks = {
        "lava-sim-grpc:1": {"mode": "down"},
        "eth-sim:1": {"mode": "rate_limit", "rate_limit_style": "body"},
    }
    st, resp = api.apply_scenario({"providers": blocks})
    assert st == 400, resp
    _, scen = api.get_scenario()
    assert scen["providers"]["lava-sim-grpc:1"]["mode"] == "success"
    assert scen["providers"]["eth-sim:1"]["mode"] == "success"


# ── a write with "transports": null clears a stored filter ────────────────────


def test_a_write_with_null_transports_clears_a_stored_filter():
    """A write applies only the fields it names, so a block that leaves out
    ``transports`` keeps the filter of an earlier write. That is why the
    automation client sends ``"transports": null`` with every ``down`` and
    ``port_closed`` write. This pins the simulator's half: null clears the
    filter, and the fault reaches every endpoint of the provider again."""
    api = _api()
    provider = api.registry.provider("eth-sim", "1")
    by_transport = {ep.transport: ep for ep in provider.endpoints}
    assert set(by_transport) == {"http", "ws"}, "the test needs a provider with an http and a ws endpoint"

    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"mode": "down", "transports": ["ws"]}}})
    assert st == 200, resp
    scenario = provider.scenario.snapshot()
    assert scenario["transports"] == ["ws"]
    assert [targets(scenario, by_transport[t]) for t in ("http", "ws")] == [False, True]

    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"mode": "down"}}})
    assert st == 200, resp
    assert provider.scenario.snapshot()["transports"] == ["ws"], "a block without the key keeps the filter"

    st, resp = api.apply_scenario({"providers": {"eth-sim:1": {"mode": "down", "transports": None}}})
    assert st == 200, resp
    scenario = provider.scenario.snapshot()
    assert scenario["transports"] is None
    assert [targets(scenario, by_transport[t]) for t in ("http", "ws")] == [True, True]


# ── GET /history: filter first, copy only the rows that match ────────────────
#
# A read used to copy every row of every provider and filter the copies, so a
# read of one request id cost as much as a read of the whole log. Two things are
# pinned: the answer of each read is the answer of that old algorithm, and a row
# that a read does not need is never copied.

_HISTORY_TOPOLOGY = (
    # (pool, chain, pid, name, is_backup, group_label, endpoints)
    ("alpha-sim", "eth", "1", "AlphaProvider1", False, "", (("jsonrpc", "http", 41001), ("jsonrpc", "ws", 41002))),
    ("alpha-sim", "eth", "2", "AlphaProvider2", False, "", (("jsonrpc", "http", 41003),)),
    ("alpha-sim", "eth", "3", "AlphaProvider3", True, "", (("jsonrpc", "http", 41004),)),
    ("beta-sim", "btc", "1", "BetaProvider1", False, "", (("jsonrpc", "http", 41005),)),
    ("beta-sim", "btc", "2", "BetaProvider2", False, "", (("grpc", "http2", 41006),)),
    ("gamma-sim", "solana", "1", "GammaProvider1", False, "", (("jsonrpc", "http", 41007),)),
    ("gamma-sim", "solana", "2", "GammaProvider2", False, "", (("rest", "http", 41008),)),
)
_HISTORY_METHODS = ("eth_blockNumber", "eth_call", "getblockcount", "getSlot", "eth_getLogs")
_HISTORY_STATUSES = ("success", "error", "rate_limit", "down")
_HISTORY_HEADERS: tuple[dict[str, str], ...] = (
    {},
    {"lava-stateful-api": "true"},
    {"Lava-Stateful-Api": "false"},
    {"LAVA-STATEFUL-API": "true", "Lava-Stickiness": "s1"},
    {"Lava-Stickiness": "s2"},
)
_HISTORY_ROWS = 84


def _history_api():
    return ControlApi(build_registry(_HISTORY_TOPOLOGY), WsSubscriptions())


def _history_request_id(burst):
    """A number, the same digits as text, other text, or no id at all."""
    kind, n = burst % 4, burst // 4
    if kind == 0:
        return n
    if kind == 1:
        return str(n)
    if kind == 2:
        return f"req-{burst}"
    return None if n % 2 == 0 else 100 + burst


def _fill_history(api, monkeypatch):
    """Push 84 rows over the seven providers of three pools, in 28 bursts of three.

    A burst is one request id and one method on three providers, a few
    milliseconds apart: the rows of a relay. Some bursts put two rows on the same
    instant and some spread them past the 50 ms window, so the sort has ties to
    keep in order and the correlation groups have a border to cross. Returns the
    (provider key, request id) of each row as pushed."""
    now = {"ts": 0.0}
    monkeypatch.setattr("provider_simulator.domain.call_log._now_fields", lambda: (now["ts"], f"{now['ts']:.3f} UTC"))
    providers = api.registry.all_providers()
    pushed = []
    for i in range(_HISTORY_ROWS):
        burst, slot = divmod(i, 3)
        if burst % 5 == 0:
            offsets = (0.0, 0.0, 0.01)
        elif burst % 4 == 3:
            offsets = (0.0, 0.08, 0.16)
        else:
            offsets = (0.0, 0.01, 0.02)
        now["ts"] = 1000.0 + burst * 0.5 + offsets[slot]
        provider = providers[i % 7]
        endpoint = provider.endpoints[(i // 7) % len(provider.endpoints)]
        request_id = _history_request_id(burst)
        provider.log.push(
            _HISTORY_METHODS[burst % 5],
            _HISTORY_STATUSES[i % 4],
            i,
            interface=endpoint.interface,
            transport=endpoint.transport,
            port=endpoint.port,
            request_id=request_id,
            lava_headers=_HISTORY_HEADERS[i % 5],
        )
        pushed.append((provider.key, request_id))
    return pushed


def _number(text, default, kind):
    try:
        return kind(str(text))
    except (TypeError, ValueError):
        return default


def _history_the_old_way(api, query):
    """What GET /history answered before it filtered first: copy every row of
    every provider, filter the copies, sort them by time, then number them.
    Written out here on purpose, so the new read is held to the old answer."""
    rows = [row for provider in api.registry.all_providers() for row in provider.log.get_history()]
    for key in ("pool", "pid", "transport", "method", "status", "interface"):
        if key in query:
            rows = [row for row in rows if row.get(key) == query[key]]
    if "request_id" in query:
        rows = [row for row in rows if str(row.get("request_id")) == str(query["request_id"])]
    if "from" in query:
        low = _number(query["from"], float("-inf"), float)
        rows = [row for row in rows if row.get("ts", 0.0) >= low]
    if "to" in query:
        high = _number(query["to"], float("inf"), float)
        rows = [row for row in rows if row.get("ts", 0.0) <= high]
    for key, value in query.items():
        if key.startswith("lava_header_"):
            wanted = key[len("lava_header_") :].lower()
            rows = [
                row
                for row in rows
                if any(name.lower() == wanted and header == value for name, header in row["lava_headers"].items())
            ]
    rows.sort(key=lambda row: row.get("ts", 0.0))
    groups, last_group = {}, 0
    for order, row in enumerate(rows, start=1):
        row["call_order"] = order
        key = (row.get("request_id"), row.get("method"))
        seen = groups.get(key)
        if seen is not None and row.get("ts", 0.0) - seen[1] <= 0.050:
            row["correlation_group"] = seen[0]
        else:
            last_group += 1
            groups[key] = (last_group, row.get("ts", 0.0))
            row["correlation_group"] = last_group
    if "last" in query:
        tail = _number(query["last"], len(rows), int)
        rows = rows[-tail:] if tail > 0 else []
    if "max" in query:
        cap = _number(query["max"], -1, int)
        if cap < 0:
            return 400, {"error": "max must be a non-negative integer"}
        rows = rows[-cap:] if cap > 0 else []
    return 200, {"count": len(rows), "history": rows}


# name -> query. Each filter the read knows is asked alone, then a few together.
_HISTORY_QUERIES = {
    "no filter": {},
    "pool": {"pool": "alpha-sim"},
    "pool and pid": {"pool": "alpha-sim", "pid": "2"},
    "pid alone, in three pools": {"pid": "1"},
    "pool that does not exist": {"pool": "nope-sim"},
    "pid that the pool does not have": {"pool": "beta-sim", "pid": "3"},
    "request_id of a number and of the same digits as text": {"request_id": "1"},
    "request_id of text": {"request_id": "req-2"},
    "request_id of a row without one": {"request_id": "None"},
    "method": {"method": "eth_call"},
    "status": {"status": "rate_limit"},
    "transport": {"transport": "ws"},
    "interface": {"interface": "rest"},
    "from and to": {"from": "1003.0", "to": "1008.0"},
    "from alone": {"from": "1010.0"},
    "to alone": {"to": "1002.0"},
    "from that is not a number": {"from": "abc"},
    "from that is nan": {"from": "nan"},
    "lava header, name in another case": {"lava_header_Lava-Stateful-Api": "true"},
    "lava header, value in another case": {"lava_header_lava-stateful-api": "True"},
    "two lava headers": {"lava_header_lava-stateful-api": "true", "lava_header_lava-stickiness": "s1"},
    "last": {"last": "10"},
    "last 0": {"last": "0"},
    "last that is not a number": {"last": "abc"},
    "max": {"max": "7"},
    "max 0": {"max": "0"},
    "max below zero": {"max": "-1"},
    "max that is not a number": {"max": "abc"},
    "last and max": {"last": "20", "max": "5"},
    "pool, method and status": {"pool": "alpha-sim", "method": "eth_call", "status": "error"},
    "pid, time window, lava header and last": {
        "pid": "1",
        "from": "1001.0",
        "to": "1012.0",
        "lava_header_lava-stateful-api": "true",
        "last": "3",
    },
}
# How many rows the read gives when it is NOT "some, but not all". A query that
# is missing here must give some rows and fewer than the 84 pushed, so that no
# comparison below is between two empty answers or two unfiltered ones.
_HISTORY_COUNT = {
    "no filter": _HISTORY_ROWS,
    "pool that does not exist": 0,
    "pid that the pool does not have": 0,
    "from that is not a number": _HISTORY_ROWS,
    "from that is nan": 0,
    "lava header, value in another case": 0,  # the value is compared as it is written
    "last 0": 0,
    "last that is not a number": _HISTORY_ROWS,
    "max 0": 0,
}
_HISTORY_REFUSED = ("max below zero", "max that is not a number")


@pytest.mark.parametrize("name", list(_HISTORY_QUERIES))
def test_history_read_gives_the_answer_of_the_old_copy_everything_read(name, monkeypatch):
    api = _history_api()
    _fill_history(api, monkeypatch)
    query = _HISTORY_QUERIES[name]
    expected = _history_the_old_way(api, query)
    answer = api.get_history(query)
    assert answer == expected
    # The wire form too: the same keys in the same order, not only equal values.
    assert json.dumps(answer) == json.dumps(expected)
    status, body = answer
    if name in _HISTORY_REFUSED:
        assert status == 400
        return
    assert status == 200
    if name in _HISTORY_COUNT:
        assert body["count"] == _HISTORY_COUNT[name]
    else:
        assert 0 < body["count"] < _HISTORY_ROWS, f"{name!r} must keep some rows and drop some: {body['count']}"


def _api_that_counts_copies():
    """A control API whose stored rows count their own copies.

    Returns the API and two counters keyed by "pool:pid": the logs that were
    read, and the rows that were copied. A copy of a stored row reads its items,
    which is what the counting row notices. It is counted where the copy happens,
    so a read that copied every row and kept the matching ones would still show."""
    reads = Counter()
    copies = Counter()

    class CountedRow(dict):
        def items(self):
            copies[f"{self['pool']}:{self['pid']}"] += 1
            return super().items()

    class CountingLog(CallLog):
        def _new_entry(self, *args, **kwargs):
            return CountedRow(super()._new_entry(*args, **kwargs))

        def get_history(self, *args, **kwargs):
            reads[f"{self._pool}:{self._pid}"] += 1
            return super().get_history(*args, **kwargs)

    api = _history_api()
    for provider in api.registry.all_providers():
        provider.log = CountingLog(pool=provider.pool.name, pid=provider.pid, name=provider.name)
    return api, reads, copies


@pytest.mark.parametrize(
    "query, read",
    [
        ({"pool": "alpha-sim"}, {"alpha-sim:1", "alpha-sim:2", "alpha-sim:3"}),
        ({"pool": "alpha-sim", "pid": "2"}, {"alpha-sim:2"}),
        ({"pid": "1"}, {"alpha-sim:1", "beta-sim:1", "gamma-sim:1"}),
        ({"pool": "beta-sim", "pid": "3"}, set()),
        ({"pool": "alpha-sim", "method": "eth_call"}, {"alpha-sim:1", "alpha-sim:2", "alpha-sim:3"}),
    ],
    ids=["pool", "pool and pid", "pid in three pools", "pid the pool lacks", "pool and method"],
)
def test_history_read_does_not_ask_the_logs_of_providers_the_pool_and_pid_leave_out(query, read, monkeypatch):
    api, reads, copies = _api_that_counts_copies()
    pushed = _fill_history(api, monkeypatch)
    # The control: a read with no filter asks every log and copies every row. If
    # the counters did not see that, a zero below would prove nothing.
    api.get_history({})
    assert set(reads) == {key for key, _ in pushed} and set(reads.values()) == {1}, reads
    assert sum(copies.values()) == _HISTORY_ROWS, copies
    reads.clear()
    copies.clear()

    status, body = api.get_history(query)

    assert status == 200
    assert set(reads) == read, f"logs read: {sorted(reads)}; only {sorted(read)} may be"
    assert set(reads.values()) <= {1}, reads
    assert set(copies) <= read, f"rows copied from logs outside {sorted(read)}: {dict(copies)}"
    assert sum(copies.values()) == body["count"], f"{sum(copies.values())} rows copied for {body['count']} given"


@pytest.mark.parametrize(
    "query",
    [
        {"request_id": "1"},
        {"request_id": "req-2"},
        {"request_id": "None"},
        {"method": "eth_call"},
        {"status": "rate_limit"},
        {"transport": "ws"},
        {"interface": "rest"},
        {"from": "1003.0", "to": "1008.0"},
        {"lava_header_Lava-Stateful-Api": "true"},
        {"pool": "gamma-sim", "request_id": "1"},
    ],
    ids=lambda query: "&".join(f"{k}={v}" for k, v in query.items()),
)
def test_history_read_copies_exactly_the_rows_that_match(query, monkeypatch):
    api, reads, copies = _api_that_counts_copies()
    pushed = _fill_history(api, monkeypatch)
    matching = _history_the_old_way(api, query)[1]["count"]  # copies everything: counted, then cleared
    assert 0 < matching < _HISTORY_ROWS, f"{query} must match some rows and not all: {matching}"
    if query.get("request_id") == "1":
        # Counted from what was pushed, not from the reference above: a number
        # and text of the same digits both match, because the id is read as text.
        assert matching == len([1 for key, rid in pushed if str(rid) == "1" and key.startswith(query.get("pool", ""))])
    reads.clear()
    copies.clear()

    status, body = api.get_history(query)

    assert status == 200
    assert body["count"] == matching
    assert (
        sum(copies.values()) == matching
    ), f"{sum(copies.values())} rows were copied to answer a read that matches {matching}: {dict(copies)}"


def test_history_read_decodes_the_headers_of_the_rows_it_keeps_and_of_no_other_row(monkeypatch):
    # ``last`` and ``max`` cut the list after the sort. The headers of a row are
    # decoded after the cut, so a read that keeps 2 rows of a full log decodes 2
    # texts, and not one text for each row of the log.
    from provider_simulator import control_api as control_api_module
    from provider_simulator.domain import call_log as call_log_module

    api = _api()
    provider = api.registry.provider("eth-sim", "1")
    for n in range(5):
        provider.log.push(
            "eth_call",
            "success",
            0,
            interface="jsonrpc",
            transport="http",
            port=18545,
            request_id=n,
            headers={"X-N": str(n)},
        )
    decoded: list = []
    real_decode = call_log_module.decode_row_headers

    def _counting_decode(row):
        # Count a real decode only: a row whose headers are still a text.
        if isinstance(row.get("headers"), str):
            decoded.append(row["request_id"])
        real_decode(row)

    # The log and the control API each call the function by a name of their
    # own, so the counter replaces both. A decode inside the log counts too.
    monkeypatch.setattr(call_log_module, "decode_row_headers", _counting_decode)
    monkeypatch.setattr(control_api_module, "decode_row_headers", _counting_decode)

    # The control: a read with no cut decodes all five. If the counter did not
    # see that, the counts below would prove nothing.
    status, body = api.get_history({"pool": "eth-sim", "pid": "1"})
    assert (status, body["count"], sorted(decoded)) == (200, 5, [0, 1, 2, 3, 4])
    decoded.clear()

    status, body = api.get_history({"pool": "eth-sim", "pid": "1", "last": "2"})
    assert status == 200
    assert [row["headers"] for row in body["history"]] == [{"X-N": "3"}, {"X-N": "4"}]
    assert decoded == [3, 4], f"the read kept 2 rows, and it decoded the headers of the rows {decoded}"
    decoded.clear()

    status, body = api.get_history({"pool": "eth-sim", "pid": "1", "max": "1"})
    assert status == 200
    assert [row["headers"] for row in body["history"]] == [{"X-N": "4"}]
    assert decoded == [4], f"the read kept 1 row, and it decoded the headers of the rows {decoded}"
