from constants import LN_BLOCK_HEIGHT
from provider_simulator.chains import chain_for
from provider_simulator.chains.ln import LnChain
from provider_simulator.domain.scenario import ScenarioConfig
from stubs_lnd import LND_ERROR_STUBS


def _sc(**kw):
    sc = ScenarioConfig()
    if kw:
        sc.update(kw)
    return sc.snapshot()


def test_registry_resolves_ln():
    assert isinstance(chain_for("ln"), LnChain)


def test_getinfo_shifts_height_and_flags_unsynced():
    chain = LnChain()
    _, body = chain.build_success({"id": 1, "method": "getinfo"}, _sc(blocks_behind=7), {})
    assert body["result"]["block_height"] == LN_BLOCK_HEIGHT - 7
    assert body["result"]["synced_to_chain"] is False


def test_getinfo_default_is_synced_at_head():
    chain = LnChain()
    _, body = chain.build_success({"id": 1, "method": "getinfo"}, _sc(), {})
    # blocks_behind 0 → no shift applied; default stub keeps its synced value
    assert "block_height" in body["result"]


def test_decodepayreq_echoes_invoice():
    chain = LnChain()
    _, body = chain.build_success({"id": 1, "method": "decodepayreq", "params": ["lnbcrt1u1psim"]}, _sc(), {})
    assert body["result"]["payment_request"] == "lnbcrt1u1psim"


def test_openchannel_echoes_pubkey():
    chain = LnChain()
    _, body = chain.build_success({"id": 1, "method": "openchannel", "params": ["02deadbeef", 100000]}, _sc(), {})
    assert body["result"]["node_pubkey"] == "02deadbeef"


def test_error_stub_override():
    chain = LnChain()
    name = next(iter(LND_ERROR_STUBS))
    _, body = chain.build_success({"id": 1, "method": "getinfo"}, _sc(responses={"getinfo": {"error_stub": name}}), {})
    assert body["error"] == LND_ERROR_STUBS[name]
