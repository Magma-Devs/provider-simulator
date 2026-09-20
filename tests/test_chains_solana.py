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
