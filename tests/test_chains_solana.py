import pytest

import stubs_solana
from provider_simulator.chains import chain_for
from provider_simulator.chains.solana import SOLANA_ERROR_STUBS, SolanaChain
from provider_simulator.domain.quirks import SolanaQuirks
from provider_simulator.domain.scenario import ScenarioConfig

BASE = stubs_solana.SOLANA_BASE_SLOT
GAP = stubs_solana.SOLANA_DEFAULT_SLOT_BLOCK_GAP


def _sc(**kw):
    sc = ScenarioConfig()
    if kw:
        sc.update(kw)
    return sc.snapshot()


def _q(**kw):
    q = SolanaQuirks()
    if kw:
        q.update(kw)
    return q.snapshot()


def test_registry_resolves_solana():
    assert isinstance(chain_for("solana"), SolanaChain)
    assert chain_for("solana").quirks_type is SolanaQuirks


def test_get_slot_uses_base_plus_offset():
    chain = SolanaChain()
    _, body = chain.build_success({"id": 1, "method": "getSlot"}, _sc(), _q())
    assert body["result"] == BASE
    _, body = chain.build_success({"id": 1, "method": "getSlot"}, _sc(), _q(slot_offset=-120))
    assert body["result"] == BASE - 120


def test_latest_blockhash_carries_the_gap():
    chain = SolanaChain()
    _, body = chain.build_success({"id": 1, "method": "getLatestBlockhash"}, _sc(), _q())
    assert body["result"]["context"]["slot"] == BASE
    assert body["result"]["value"]["lastValidBlockHeight"] == BASE - GAP


def test_latest_blockhash_custom_gap():
    chain = SolanaChain()
    _, body = chain.build_success({"id": 1, "method": "getLatestBlockhash"}, _sc(), _q(slot_block_gap=10))
    assert body["result"]["value"]["lastValidBlockHeight"] == BASE - 10


def test_get_health_and_version():
    chain = SolanaChain()
    _, body = chain.build_success({"id": 1, "method": "getHealth"}, _sc(), _q())
    assert body["result"] == "ok"
    _, body = chain.build_success({"id": 1, "method": "getVersion"}, _sc(), _q())
    assert body["result"]["solana-core"] == stubs_solana.SOLANA_CORE_VERSION


def test_unknown_method_null_by_default_error_on_optin():
    chain = SolanaChain()
    _, body = chain.build_success({"id": 1, "method": "getFoo"}, _sc(), _q())
    assert body["result"] is None
    _, body = chain.build_success({"id": 1, "method": "getFoo"}, _sc(), _q(unknown_method_mode="error"))
    assert body["error"] == SOLANA_ERROR_STUBS["method_not_found"]


def test_error_stub_override():
    chain = SolanaChain()
    _, body = chain.build_success(
        {"id": 1, "method": "getSlot"},
        _sc(responses={"getSlot": {"error_stub": "node_behind"}}),
        _q(),
    )
    assert body["error"] == SOLANA_ERROR_STUBS["node_behind"]


def test_the_head_moves_the_slot_every_reply_carries():
    """A moved head has to reach the reply, not only the control route."""
    chain = SolanaChain()
    chain.head.bump(9)
    try:
        _, body = chain.build_success({"id": 1, "method": "getSlot"}, _sc(), _q())
        assert body["result"] == BASE + 9

        _, body = chain.build_success({"id": 1, "method": "getLatestBlockhash"}, _sc(), _q())
        assert body["result"]["context"]["slot"] == BASE + 9
        assert body["result"]["value"]["lastValidBlockHeight"] == BASE + 9 - GAP
    finally:
        chain.head.reset()


def test_a_static_head_answers_the_base_slot_exactly():
    """At rest the reply must be what it was before the head existed."""
    chain = SolanaChain()
    _, body = chain.build_success({"id": 1, "method": "getSlot"}, _sc(), _q())
    assert body["result"] == BASE


def test_the_head_moves_every_provider_and_keeps_their_offsets():
    """``slot_offset`` is ONE provider's distance from the chain's slot.

    Moving the head must carry every provider with it and leave the distances
    between them unchanged — that is what makes it the chain's height rather
    than a second per-provider knob.
    """
    chain = SolanaChain()
    before = [
        chain.build_success({"id": 1, "method": "getSlot"}, _sc(), _q(slot_offset=o))[1]["result"] for o in (0, 5, -5)
    ]
    chain.head.bump(1000)
    try:
        after = [
            chain.build_success({"id": 1, "method": "getSlot"}, _sc(), _q(slot_offset=o))[1]["result"]
            for o in (0, 5, -5)
        ]
        assert after == [b + 1000 for b in before]
        assert [a - after[0] for a in after] == [b - before[0] for b in before]
    finally:
        chain.head.reset()


# ── the slot of each commitment ───────────────────────────────────────────────
#
# A node answers a slot at the commitment that the request names. The router polls the
# finalized slot and harvests the slot of each relay at the commitment of its client, so
# a node that answers one slot for all three can never show the router two slots.
#
# At the default lags every commitment answers the head of the provider. The shape of a
# real network was measured on 2026-11-10: processed is the head, and confirmed and
# finalized are one slot below it (mainnet: confirmed minus finalized was 0 in 30 of 30
# rounds; testnet: 0 to 1). A test that wants that shape sets the lags, as these tests
# do. The numbers are written here and not read from stubs_solana, so a wrong default
# cannot make its own test pass.

PUBKEY = "11111111111111111111111111111111"
COMMITMENTS = ("processed", "confirmed", "finalized")

# The methods of the simulator that report a slot: getSlot as a bare number, the other two
# as ``result.context.slot``.
SLOT_METHODS = ("getSlot", "getLatestBlockhash", "getBalance")

# The lags of a real network, as measured on 2026-11-10.
REAL_NETWORK_LAGS = {"processed_lag_slots": 0, "confirmed_lag_slots": 1, "finalized_lag_slots": 1}


def _request(method, params=None):
    request = {"id": 1, "method": method}
    if params is not None:
        request["params"] = params
    return request


def _params(method, commitment):
    """The params that a client sends for ``method``: the config object is the last one."""
    config = [] if commitment is None else [{"commitment": commitment}]
    return [PUBKEY, *config] if method == "getBalance" else config


def _slot_of(method, commitment=None, *, scenario=None, quirks=None, chain=None):
    """The slot that one reply carries: the bare result of getSlot, the context slot of the rest."""
    chain = chain or SolanaChain()
    _, body = chain.build_success(_request(method, _params(method, commitment)), scenario or _sc(), quirks or _q())
    result = body["result"]
    return result if method == "getSlot" else result["context"]["slot"]


@pytest.mark.parametrize("method", SLOT_METHODS)
def test_at_the_default_lags_every_commitment_answers_the_head(method):
    """No lag by default: a provider answers one slot for all three commitments, as it did before
    commitments existed."""
    assert [_slot_of(method, c) for c in COMMITMENTS] == [BASE, BASE, BASE]
    assert _slot_of(method) == BASE


@pytest.mark.parametrize("method", SLOT_METHODS)
@pytest.mark.parametrize(
    "commitment, expected", [("processed", BASE), ("confirmed", BASE - 1), ("finalized", BASE - 1)]
)
def test_each_commitment_is_answered_at_its_slot_at_the_lags_of_a_real_network(method, commitment, expected):
    assert _slot_of(method, commitment, quirks=_q(**REAL_NETWORK_LAGS)) == expected


@pytest.mark.parametrize("method", SLOT_METHODS)
def test_a_test_sets_the_lag_of_each_commitment_apart(method):
    """Three different lags give three different slots, so each setting reaches its own commitment."""
    quirks = _q(processed_lag_slots=2, confirmed_lag_slots=3, finalized_lag_slots=33)
    assert [_slot_of(method, c, quirks=quirks) for c in COMMITMENTS] == [BASE - 2, BASE - 3, BASE - 33]


@pytest.mark.parametrize("method", SLOT_METHODS)
def test_a_request_with_no_commitment_is_answered_at_the_finalized_slot(method):
    """Solana answers at the finalized commitment when the request names none. The lags are
    set apart so that a default read from the confirmed commitment would show."""
    quirks = _q(confirmed_lag_slots=1, finalized_lag_slots=5)
    assert _slot_of(method, None, quirks=quirks) == BASE - 5
    assert _slot_of(method, "confirmed", quirks=quirks) == BASE - 1


@pytest.mark.parametrize("params", [None, [], [{}], [{"commitment": None}], [{"encoding": "base64"}]])
def test_get_slot_with_no_commitment_in_any_form_is_answered_at_the_finalized_slot(params):
    quirks = _q(confirmed_lag_slots=1, finalized_lag_slots=5)
    _, body = SolanaChain().build_success(_request("getSlot", params), _sc(), quirks)
    assert body["result"] == BASE - 5


# Measured on 2026-11-10 on Solana mainnet, in one JSON-RPC batch to
# api.mainnet-beta.solana.com, so that one node state answered every request: getSlot with no
# commitment answered 467685357, finalized 467685357, confirmed 467685357 and processed
# 467685358, and {"commitment": "bogus"} answered 467685357, with no error. So a commitment
# that is none of the three is no error: it is answered as a request that names none, at the
# finalized slot. Only "bogus" was measured. The other spellings below take the same path in
# this simulator and were not measured on a real node.
@pytest.mark.parametrize("method", SLOT_METHODS)
@pytest.mark.parametrize("unknown", ["bogus", "finalised", "Finalized", ""])
def test_a_commitment_that_is_none_of_the_three_is_answered_like_a_request_with_no_commitment(method, unknown):
    """The lags are set apart, so the answer must be the finalized slot and no other commitment's."""
    quirks = _q(processed_lag_slots=0, confirmed_lag_slots=1, finalized_lag_slots=5)
    _, body = SolanaChain().build_success(_request(method, _params(method, unknown)), _sc(), quirks)
    assert "error" not in body, body
    result = body["result"]
    assert (result if method == "getSlot" else result["context"]["slot"]) == BASE - 5


def test_the_config_object_is_found_after_the_other_params():
    """getBalance names its commitment in its second param, after the address."""
    quirks = _q(confirmed_lag_slots=1, finalized_lag_slots=5)
    _, body = SolanaChain().build_success(_request("getBalance", [PUBKEY, {"commitment": "confirmed"}]), _sc(), quirks)
    assert body["result"]["context"]["slot"] == BASE - 1


def test_get_balance_carries_a_context_and_a_value():
    _, body = SolanaChain().build_success(_request("getBalance", [PUBKEY]), _sc(), _q())
    assert body["result"] == {"context": {"slot": BASE}, "value": stubs_solana.SOLANA_DEFAULT_BALANCE_LAMPORTS}


@pytest.mark.parametrize("commitment", COMMITMENTS)
def test_the_gap_stays_between_the_context_slot_and_last_valid_block_height_at_every_commitment(commitment):
    quirks = _q(processed_lag_slots=2, confirmed_lag_slots=3, finalized_lag_slots=33, slot_block_gap=25)
    _, body = SolanaChain().build_success(
        _request("getLatestBlockhash", _params("getLatestBlockhash", commitment)), _sc(), quirks
    )
    context_slot = body["result"]["context"]["slot"]
    assert context_slot == BASE - {"processed": 2, "confirmed": 3, "finalized": 33}[commitment]
    assert context_slot - body["result"]["value"]["lastValidBlockHeight"] == 25


def test_a_per_method_override_wins_over_the_commitment():
    scenario = _sc(responses={"getSlot": {"result": 123}})
    _, body = SolanaChain().build_success(_request("getSlot", [{"commitment": "confirmed"}]), scenario, _q())
    assert body["result"] == 123


# ── blocks_behind and frozen_head move the head that the commitments sit below ──


@pytest.mark.parametrize("method", SLOT_METHODS)
def test_blocks_behind_moves_the_head_and_the_commitments_apply_below_it(method):
    scenario, quirks = _sc(blocks_behind=10), _q(**REAL_NETWORK_LAGS)
    assert [_slot_of(method, c, scenario=scenario, quirks=quirks) for c in COMMITMENTS] == [
        BASE - 10,
        BASE - 11,
        BASE - 11,
    ]


@pytest.mark.parametrize("method", SLOT_METHODS)
def test_blocks_behind_moves_every_commitment_at_the_default_lags(method):
    assert [_slot_of(method, c, scenario=_sc(blocks_behind=10)) for c in COMMITMENTS] == [BASE - 10] * 3


def test_a_negative_blocks_behind_puts_the_provider_ahead_of_the_chain():
    scenario, quirks = _sc(blocks_behind=-4), _q(**REAL_NETWORK_LAGS)
    assert [_slot_of("getSlot", c, scenario=scenario, quirks=quirks) for c in COMMITMENTS] == [
        BASE + 4,
        BASE + 3,
        BASE + 3,
    ]


def test_blocks_behind_and_slot_offset_add_up():
    scenario, quirks = _sc(blocks_behind=10), _q(slot_offset=-3)
    assert _slot_of("getSlot", "processed", scenario=scenario, quirks=quirks) == BASE - 13


@pytest.mark.parametrize("method", SLOT_METHODS)
def test_a_frozen_head_stands_still_while_the_chain_head_moves(method):
    chain = SolanaChain()
    frozen, quirks = _sc(frozen_head=BASE + 500), _q(**REAL_NETWORK_LAGS)
    expected = [BASE + 500, BASE + 499, BASE + 499]
    assert [_slot_of(method, c, scenario=frozen, quirks=quirks, chain=chain) for c in COMMITMENTS] == expected
    chain.head.bump(1000)
    assert [_slot_of(method, c, scenario=frozen, quirks=quirks, chain=chain) for c in COMMITMENTS] == expected
    assert _slot_of(method, "processed", chain=chain) == BASE + 1000, "an unfrozen provider moves with the head"


def test_a_frozen_head_is_the_head_of_the_provider_whatever_its_slot_offset():
    """frozen_head names the block that the provider reports, so no offset moves it."""
    frozen, quirks = _sc(frozen_head=BASE + 500), _q(slot_offset=7)
    assert _slot_of("getSlot", "processed", scenario=frozen, quirks=quirks) == BASE + 500


def test_a_frozen_node_answers_two_slots_when_its_confirmed_and_finalized_lags_differ():
    """The node that the router's head-stall rule meets: frozen, polled at finalized,
    and relayed at confirmed. The two slots differ, and neither moves."""
    chain = SolanaChain()
    frozen, quirks = _sc(frozen_head=BASE + 500), _q(confirmed_lag_slots=1, finalized_lag_slots=32)
    for _ in range(3):
        assert _slot_of("getLatestBlockhash", "finalized", scenario=frozen, quirks=quirks, chain=chain) == BASE + 468
        assert _slot_of("getBalance", "confirmed", scenario=frozen, quirks=quirks, chain=chain) == BASE + 499
        chain.head.bump(50)


@pytest.mark.parametrize("commitment", COMMITMENTS)
def test_a_commitment_slot_never_goes_below_zero(commitment):
    """A lag of 1 below a head of 0 is slot 0, and not a negative slot that no node answers."""
    assert _slot_of("getSlot", commitment, scenario=_sc(frozen_head=0), quirks=_q(**REAL_NETWORK_LAGS)) == 0
