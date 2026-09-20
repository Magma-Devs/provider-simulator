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
