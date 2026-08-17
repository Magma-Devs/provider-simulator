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
