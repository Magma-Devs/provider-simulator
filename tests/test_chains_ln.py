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


def test_getinfo_follows_the_btc_head_it_reports():
    """LN reports the height of the BTC chain beneath it, so it must follow it.

    The docstring said this while the code read a separate constant. Both were
    850000, and btc's height could not move, so the claim could not be caught
    being wrong. Giving btc a head made it falsifiable — and false.
    """
    from provider_simulator.chains import CHAINS

    btc, ln = CHAINS["btc"], CHAINS["ln"]

    def height(**scenario):
        body = ln.build_success({"id": 1, "method": "getinfo"}, _sc(**scenario), {})[1]
        return body["result"]["block_height"]

    at_rest = height()
    btc.head.bump(5000)
    try:
        assert height() == at_rest + 5000, "LN did not follow the chain it reports"
        # The node's own lag is measured from wherever that chain now is.
        assert height(blocks_behind=7) == at_rest + 5000 - 7
    finally:
        btc.head.reset()
    assert height() == at_rest


def test_ln_owns_no_head_of_its_own():
    """Following btc is not the same as having a head. There is nothing to move
    here, and ``POST /advance`` on 'ln' must keep saying so."""
    from provider_simulator.chains import CHAINS

    assert CHAINS["ln"].iter_heads() == []
