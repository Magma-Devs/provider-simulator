"""
Unit tests for the Solana chain dispatch in the provider simulator (MAG-2231).

Mirrors the structure of ``tests/test_simulator_btc.py`` (the BTC suite) but
covers only Solana-specific behaviour:

  getLatestBlockhash gap            — result.context.slot minus
                                      result.value.lastValidBlockHeight equals
                                      the provider's solana_slot_block_gap. The
                                      default gap (21_900_000) and a custom gap
                                      set via /scenario are both honoured.
  getSlot / getHealth / getVersion  — scalar slot, "ok", and the version object.
  Per-method overrides              — responses[method] = {"result": ...} and
                                      {"error": ...} short-circuit the stub,
                                      identical to the BTC/LN handlers.
  History tracking                  — Solana requests show up in /history with
                                      the method name and status preserved.
  Universal mode=down               — the Solana port refuses with 503 under a
                                      provider-wide down fault, regardless of
                                      chain_family (same contract the BTC suite
                                      locks for its dedicated port).

Port layout
-----------
Solana dispatch lives on a dedicated listener pool at prod ports 18582-18584
(SOLANA_PRIMARY_PORTS), selected by handler_module=handlers_solana — exactly
like BTC (18575-77) and LN (18578-80). This suite mirrors the move: a dedicated
Solana test port range at 25582-25584 (tail digits mirror prod 18582-18584)
hosts JSONRPCHandler listeners with ``handler_chain_family="solana"`` +
``handler_module=handlers_solana``. A second ETH listener pool at 25560-25562
(distinct from the BTC suite's 22545-47) hosts default-ETH listeners, used by
the mixed-chain test. Both pools share ProviderState per pid so a single
``/scenario`` POST reconfigures both listeners for the same logical provider —
exactly mirroring prod.

Run with:
  pytest tests/test_simulator_solana.py -v
"""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer, ThreadingHTTPServer

import pytest

import handlers_eth
import handlers_solana
from handlers_solana import (
    SOLANA_BASE_SLOT,
    SOLANA_CORE_VERSION,
    SOLANA_DEFAULT_SLOT_BLOCK_GAP,
    SOLANA_FEATURE_SET,
)
from server import ControlHandler, JSONRPCHandler, ProviderState

# ── Test ports. Two rules keep binds reliable across the whole suite:
#    1. Stay below 32768. Ports from 32768 up are the kernel's ephemeral
#       client-port range (Linux default 32768-60999, macOS 49152-65535):
#       every outgoing HTTP call an earlier test module makes grabs a random
#       source port there, and a lingering one makes this module's bind fail
#       with "Address already in use" at fixture setup.
#    2. Each test file owns a unique port block (this file: 255xx) so all
#       modules can run in one pytest invocation. Also distinct from the
#       prod ports 18582-18584 / 19000 so a locally running simulator
#       doesn't collide.
#
#     ETH ports (25560-2) host default ETH listeners — used by the mixed-chain
#     test to drive an ETH-only port for the same pid. Solana ports (25582-4)
#     host Solana-configured listeners and are the focus of this suite. ───────

_ETH_PROVIDER_PORTS = {"1": 25560, "2": 25561, "3": 25562}
_SOLANA_PROVIDER_PORTS = {"1": 25582, "2": 25583, "3": 25584}
_CONTROL_PORT = 25500


# ── HTTP helpers (kept independent of test_simulator.py to avoid cross-file
#     fixture coupling — duplication is intentional, the suites run on
#     different ports). ─────────────────────────────────────────────────────


def _post(url: str, body: dict) -> tuple[int, dict]:
    """POST JSON body, return (status_code, parsed_response_body)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
            return e.code, json.loads(raw) if raw else {}
        except (ConnectionResetError, OSError):
            return e.code, {}


def _get(url: str) -> tuple[int, dict]:
    """GET url, return (status_code, parsed_response_body)."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else {}


def _rpc(url: str, method: str, params: list | None = None) -> tuple[int, dict]:
    """Send a JSON-RPC request, return (http_status, response_body)."""
    return _post(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})


def _ctrl(sim: dict, path: str) -> str:
    return sim["control"] + path


# ── Module-scoped fixture: start all servers once ─────────────────────────────


@pytest.fixture(scope="module")
def sim():
    """Start 3 ETH listeners + 3 Solana listeners + 1 control server.

    The Solana listeners (25582-25584) are the focus of this suite — they run
    JSONRPCHandler with ``handler_chain_family="solana"`` + ``handler_module=
    handlers_solana`` so the success path always dispatches to Solana regardless
    of the snap's ``chain_family``. The ETH listeners (25560-25562) are bound on
    the same ProviderState per pid; they exist for the mixed-chain test that
    drives an ETH-only port on a shared logical provider.

    Yields a dict with base URLs:
      sim["control"]      → http://127.0.0.1:25500
      sim["provider1"]    → http://127.0.0.1:25582    # primary Solana URL per pid
      sim["provider2"]    → http://127.0.0.1:25583
      sim["provider3"]    → http://127.0.0.1:25584
      sim["eth_provider1"]→ http://127.0.0.1:25560    # ETH companion per pid
      sim["eth_provider2"]→ http://127.0.0.1:25561
      sim["eth_provider3"]→ http://127.0.0.1:25562
    """
    # One ProviderState per pid, shared between the ETH and Solana listeners
    # for that pid — mirrors prod's shared-state model.
    states = {pid: ProviderState() for pid in _SOLANA_PROVIDER_PORTS}

    servers = []
    # ETH listener pool — default handler_chain_family / handler_module.
    for pid, port in _ETH_PROVIDER_PORTS.items():
        srv = ThreadingHTTPServer(("127.0.0.1", port), JSONRPCHandler)
        srv.daemon_threads = True
        srv.state = states[pid]
        srv.provider_id = pid
        srv.handler_chain_family = "eth"
        srv.handler_module = handlers_eth
        servers.append(srv)

    # Solana listener pool — port-derived dispatch to handlers_solana.
    for pid, port in _SOLANA_PROVIDER_PORTS.items():
        srv = ThreadingHTTPServer(("127.0.0.1", port), JSONRPCHandler)
        srv.daemon_threads = True
        srv.state = states[pid]
        srv.provider_id = pid
        srv.handler_chain_family = "solana"
        srv.handler_module = handlers_solana
        servers.append(srv)

    ctrl = HTTPServer(("127.0.0.1", _CONTROL_PORT), ControlHandler)
    ctrl.provider_states = states
    servers.append(ctrl)

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()

    time.sleep(0.15)

    yield {
        "control": f"http://127.0.0.1:{_CONTROL_PORT}",
        "provider1": f"http://127.0.0.1:{_SOLANA_PROVIDER_PORTS['1']}",
        "provider2": f"http://127.0.0.1:{_SOLANA_PROVIDER_PORTS['2']}",
        "provider3": f"http://127.0.0.1:{_SOLANA_PROVIDER_PORTS['3']}",
        "eth_provider1": f"http://127.0.0.1:{_ETH_PROVIDER_PORTS['1']}",
        "eth_provider2": f"http://127.0.0.1:{_ETH_PROVIDER_PORTS['2']}",
        "eth_provider3": f"http://127.0.0.1:{_ETH_PROVIDER_PORTS['3']}",
    }

    for s in servers:
        s.shutdown()


# ── Helper to apply per-provider scenario config ──────────────────────────────


def _set_solana(sim, pid: str = "1", **extra):
    """Convenience: POST /scenario for a single provider.

    Like the BTC suite's ``_set_btc``, dispatch is port-derived so this does
    NOT set ``chain_family="solana"`` — the Solana listener pool answers Solana
    methods regardless. Use ``_set_solana_with_fault`` when a fault primitive
    must gate on the listener's ``handler_chain_family``.

    Any extra kwargs are folded into the per-provider config dict
    (solana_slot_block_gap, mode, responses, etc.) so callers write one-liners
    instead of nesting dicts.
    """
    cfg = dict(extra)
    return _post(_ctrl(sim, "/scenario"), {"providers": {pid: cfg}})


def _set_solana_with_fault(sim, pid: str = "1", **extra):
    """Same as ``_set_solana`` but auto-sets ``chain_family="solana"`` so fault
    primitives gated to the Solana listener fire (mirrors the BTC suite's
    ``_set_btc_with_fault``)."""
    cfg = {"chain_family": "solana", **extra}
    return _post(_ctrl(sim, "/scenario"), {"providers": {pid: cfg}})


# ── Function-scoped autouse: clean slate before/after every test ──────────────


@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario config AND clear history before and after every test."""
    _post(_ctrl(sim, "/reset/all"), {})
    yield
    _post(_ctrl(sim, "/reset/all"), {})


# ─────────────────────────────────────────────────────────────────────────────
# Port-derived dispatch (mirrors TestBTCPortDispatch). The Solana port answers
# Solana methods with no /scenario call at all, because dispatch is keyed on the
# listener port, not on chain_family.
# ─────────────────────────────────────────────────────────────────────────────


class TestSolanaPortDispatch:

    def test_solana_port_dispatches_with_default_chain_family(self, sim):
        """No /scenario call at all — the Solana port (25582) must answer Solana
        methods because dispatch is port-derived, not chain_family-derived."""
        status, body = _rpc(sim["provider1"], "getSlot")
        assert status == 200
        assert "error" not in body
        # Scalar slot pinned to the handler's base — proves handlers_solana ran.
        assert body["result"] == SOLANA_BASE_SLOT

    def test_solana_port_ignores_chain_family_eth_override(self, sim):
        """Setting chain_family="eth" must NOT switch the Solana port to ETH
        dispatch — port-derived dispatch is the contract. A leftover
        chain_family from a sister test must not contaminate the Solana port."""
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"chain_family": "eth"}}})
        status, body = _rpc(sim["provider1"], "getSlot")
        assert status == 200
        # Still the Solana scalar slot, not an ETH hex string.
        assert body["result"] == SOLANA_BASE_SLOT

    def test_eth_method_on_solana_port_returns_null_result(self, sim):
        """A method unknown to handlers_solana returns null result, not error.
        Mirrors the handler's fallback for unrecognised methods — the simulator
        stays in success mode, the router sees an unfamiliar but well-formed
        response."""
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200
        assert "error" not in body
        assert body["result"] is None

    def test_solana_unknown_method_mode_error_returns_minus_32601(self, sim):
        """Opt-in: with solana_unknown_method_mode="error", an unknown method
        returns a real -32601 method-not-found instead of the default null
        result — so the router's Solana error classifier can be exercised on a
        bad method. The default stays "null" (see the test above)."""
        _set_solana(sim, "1", solana_unknown_method_mode="error")
        status, body = _rpc(sim["provider1"], "not_a_real_solana_method")
        assert status == 200
        assert "error" in body, f"expected a -32601 error envelope, got {body}"
        assert body["error"]["code"] == -32601


# ─────────────────────────────────────────────────────────────────────────────
# getLatestBlockhash — the slot ↔ lastValidBlockHeight gap (the bug carrier)
# ─────────────────────────────────────────────────────────────────────────────


class TestGetLatestBlockhashGap:

    def test_default_gap_between_slot_and_last_valid_block_height(self, sim):
        """Default scenario: context.slot - value.lastValidBlockHeight equals the
        default solana_slot_block_gap (21_900_000). No /scenario call needed —
        the default is set at ProviderState construction."""
        status, body = _rpc(sim["provider1"], "getLatestBlockhash")
        assert status == 200
        assert "error" not in body
        result = body["result"]
        slot = result["context"]["slot"]
        last_vbh = result["value"]["lastValidBlockHeight"]
        assert slot - last_vbh == SOLANA_DEFAULT_SLOT_BLOCK_GAP
        assert SOLANA_DEFAULT_SLOT_BLOCK_GAP == 21_900_000

    def test_small_custom_gap_is_respected(self, sim):
        """A small gap set via /scenario (25, below the router's 50-block
        consistency threshold) is honoured exactly."""
        _set_solana(sim, "1", solana_slot_block_gap=25)
        _, body = _rpc(sim["provider1"], "getLatestBlockhash")
        result = body["result"]
        assert result["context"]["slot"] - result["value"]["lastValidBlockHeight"] == 25

    def test_large_custom_gap_is_respected(self, sim):
        """A large gap set via /scenario (1_000_000) is honoured exactly."""
        _set_solana(sim, "1", solana_slot_block_gap=1_000_000)
        _, body = _rpc(sim["provider1"], "getLatestBlockhash")
        result = body["result"]
        assert result["context"]["slot"] - result["value"]["lastValidBlockHeight"] == 1_000_000

    def test_slot_is_pinned_to_base(self, sim):
        """context.slot is pinned to SOLANA_BASE_SLOT so the gap is the only
        moving part — lets gap assertions pin against an exact slot value."""
        _, body = _rpc(sim["provider1"], "getLatestBlockhash")
        assert body["result"]["context"]["slot"] == SOLANA_BASE_SLOT

    def test_blockhash_value_is_present_and_base58_length(self, sim):
        """value.blockhash is a non-empty base58-length string (the router reads
        the numeric fields, but the hash field must be shaped like a real one)."""
        _, body = _rpc(sim["provider1"], "getLatestBlockhash")
        blockhash = body["result"]["value"]["blockhash"]
        assert isinstance(blockhash, str)
        # base58 of 32 bytes is 43-44 chars.
        assert 43 <= len(blockhash) <= 44

    def test_custom_gap_on_one_provider_does_not_leak_to_another(self, sim):
        """A gap override on provider 1 must not change provider 2's default
        gap — per-provider ProviderState isolation."""
        _set_solana(sim, "1", solana_slot_block_gap=25)
        _, body1 = _rpc(sim["provider1"], "getLatestBlockhash")
        _, body2 = _rpc(sim["provider2"], "getLatestBlockhash")
        r1, r2 = body1["result"], body2["result"]
        assert r1["context"]["slot"] - r1["value"]["lastValidBlockHeight"] == 25
        assert (
            r2["context"]["slot"] - r2["value"]["lastValidBlockHeight"]
            == SOLANA_DEFAULT_SLOT_BLOCK_GAP
        )


# ─────────────────────────────────────────────────────────────────────────────
# solana_slot_offset — per-provider slot divergence (MAG-2233 #1). The reported
# slot is SOLANA_BASE_SLOT + offset (default 0). Distinct offsets per provider
# let a blackbox test stand up one current + several stale-behind providers so
# the router's Solana consistency filter can keep the current one and drop the
# rest. lastValidBlockHeight stays slot - gap, so the gap applies on top.
# ─────────────────────────────────────────────────────────────────────────────


class TestSolanaSlotOffset:

    def test_default_offset_zero_reports_base_slot(self, sim):
        """No /scenario call — the default offset is 0, so getSlot and
        getLatestBlockhash.context.slot both report exactly SOLANA_BASE_SLOT
        (no regression from pre-MAG-2233 behaviour)."""
        _, slot_body = _rpc(sim["provider1"], "getSlot")
        _, bh_body = _rpc(sim["provider1"], "getLatestBlockhash")
        assert slot_body["result"] == SOLANA_BASE_SLOT
        assert bh_body["result"]["context"]["slot"] == SOLANA_BASE_SLOT

    def test_negative_offset_shifts_slot_below_base_and_gap_applies_on_top(self, sim):
        """offset = -10_000_000 ⇒ slot == base - 10_000_000, on BOTH getSlot and
        getLatestBlockhash.context.slot. And value.lastValidBlockHeight stays
        slot - default_gap, proving the gap applies on top of the offset rather
        than replacing it."""
        offset = -10_000_000
        _set_solana(sim, "1", solana_slot_offset=offset)

        _, slot_body = _rpc(sim["provider1"], "getSlot")
        assert slot_body["result"] == SOLANA_BASE_SLOT + offset

        _, bh_body = _rpc(sim["provider1"], "getLatestBlockhash")
        result = bh_body["result"]
        slot = result["context"]["slot"]
        last_vbh = result["value"]["lastValidBlockHeight"]
        assert slot == SOLANA_BASE_SLOT + offset
        # Gap stacks on top of the offset: lastValidBlockHeight == slot - gap.
        assert last_vbh == slot - SOLANA_DEFAULT_SLOT_BLOCK_GAP

    def test_positive_offset_puts_provider_ahead_of_base(self, sim):
        """offset = +5_000_000 ⇒ a provider ahead of the base slot. Slot and the
        gap-derived lastValidBlockHeight both shift up by the offset."""
        offset = 5_000_000
        _set_solana(sim, "1", solana_slot_offset=offset)

        _, slot_body = _rpc(sim["provider1"], "getSlot")
        assert slot_body["result"] == SOLANA_BASE_SLOT + offset

        _, bh_body = _rpc(sim["provider1"], "getLatestBlockhash")
        result = bh_body["result"]
        assert result["context"]["slot"] == SOLANA_BASE_SLOT + offset
        assert (
            result["value"]["lastValidBlockHeight"]
            == SOLANA_BASE_SLOT + offset - SOLANA_DEFAULT_SLOT_BLOCK_GAP
        )

    def test_per_provider_offsets_are_independent(self, sim):
        """Distinct offsets on pid 1/2/3 each report their OWN slot — the
        per-provider divergence the multi-slot test needs. pid 1 current (0),
        pid 2 far behind, pid 3 slightly ahead; per-provider ProviderState
        isolation keeps them from leaking into each other."""
        _set_solana(sim, "1", solana_slot_offset=0)
        _set_solana(sim, "2", solana_slot_offset=-12_000_000)
        _set_solana(sim, "3", solana_slot_offset=3_000_000)

        _, b1 = _rpc(sim["provider1"], "getSlot")
        _, b2 = _rpc(sim["provider2"], "getSlot")
        _, b3 = _rpc(sim["provider3"], "getSlot")

        assert b1["result"] == SOLANA_BASE_SLOT
        assert b2["result"] == SOLANA_BASE_SLOT - 12_000_000
        assert b3["result"] == SOLANA_BASE_SLOT + 3_000_000
        # All three distinct — proves no cross-provider contamination.
        assert len({b1["result"], b2["result"], b3["result"]}) == 3

    def test_offset_on_one_provider_does_not_leak_to_another(self, sim):
        """An offset override on provider 1 must not change provider 2's default
        (offset 0 ⇒ base slot) — same per-provider isolation the gap test locks."""
        _set_solana(sim, "1", solana_slot_offset=-7_000_000)
        _, b1 = _rpc(sim["provider1"], "getSlot")
        _, b2 = _rpc(sim["provider2"], "getSlot")
        assert b1["result"] == SOLANA_BASE_SLOT - 7_000_000
        assert b2["result"] == SOLANA_BASE_SLOT

    def test_offset_and_gap_are_independent_knobs(self, sim):
        """Setting offset and gap together: slot shifts by the offset, and the
        slot ↔ lastValidBlockHeight distance equals the gap — the two knobs
        compose without interfering."""
        offset = -4_000_000
        gap = 25
        _set_solana(sim, "1", solana_slot_offset=offset, solana_slot_block_gap=gap)
        _, bh_body = _rpc(sim["provider1"], "getLatestBlockhash")
        result = bh_body["result"]
        assert result["context"]["slot"] == SOLANA_BASE_SLOT + offset
        assert result["context"]["slot"] - result["value"]["lastValidBlockHeight"] == gap


# ─────────────────────────────────────────────────────────────────────────────
# getSlot / getHealth / getVersion — the supporting spec-verification methods
# ─────────────────────────────────────────────────────────────────────────────


class TestSolanaSupportingMethods:

    def test_get_slot_returns_base_slot(self, sim):
        """getSlot returns the same slot the getLatestBlockhash context carries."""
        _, slot_body = _rpc(sim["provider1"], "getSlot")
        _, blockhash_body = _rpc(sim["provider1"], "getLatestBlockhash")
        assert slot_body["result"] == SOLANA_BASE_SLOT
        # getSlot and getLatestBlockhash.context.slot agree.
        assert slot_body["result"] == blockhash_body["result"]["context"]["slot"]

    def test_get_slot_is_an_integer(self, sim):
        """getSlot returns a JSON number, not a hex string — Solana convention."""
        _, body = _rpc(sim["provider1"], "getSlot")
        assert isinstance(body["result"], int)

    def test_get_health_returns_ok(self, sim):
        """A healthy Solana validator's getHealth returns the literal "ok"."""
        _, body = _rpc(sim["provider1"], "getHealth")
        assert body["result"] == "ok"

    def test_get_version_returns_expected_object(self, sim):
        """getVersion returns {"solana-core": <semver>, "feature-set": <u32>}."""
        _, body = _rpc(sim["provider1"], "getVersion")
        result = body["result"]
        assert result == {
            "solana-core": SOLANA_CORE_VERSION,
            "feature-set": SOLANA_FEATURE_SET,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Per-method overrides on a Solana provider (mirrors TestBTCErrorStubs). These
# short-circuit before the fault gate, so they don't need chain_family="solana".
# ─────────────────────────────────────────────────────────────────────────────


class TestSolanaPerMethodOverrides:

    def test_solana_result_override(self, sim):
        """responses[method] = {"result": <value>} replaces the computed stub."""
        _set_solana(sim, "1", responses={"getVersion": {"result": {"solana-core": "9.9.9"}}})
        _, body = _rpc(sim["provider1"], "getVersion")
        assert body["result"] == {"solana-core": "9.9.9"}

    def test_solana_error_override_raw_envelope(self, sim):
        """responses[method] = {"error": {...}} emits the JSON-RPC error envelope
        directly — the raw escape-hatch, alongside the named error_stub path."""
        _set_solana(
            sim, "1", responses={"getSlot": {"error": {"code": -32007, "message": "Slot skipped"}}}
        )
        _, body = _rpc(sim["provider1"], "getSlot")
        assert "error" in body
        assert body["error"]["code"] == -32007
        assert body["error"]["message"] == "Slot skipped"

    def test_solana_error_stub_named_catalogue(self, sim):
        """responses[method] = {"error_stub": "<name>"} resolves against the
        named SOLANA_ERROR_STUBS catalogue (mirrors the ETH error_stub path), so
        tests inject a canonical Solana error by name instead of hand-typing the
        envelope."""
        _set_solana(sim, "1", responses={"getSlot": {"error_stub": "min_context_slot_not_reached"}})
        _, body = _rpc(sim["provider1"], "getSlot")
        assert "error" in body, f"expected an error envelope, got {body}"
        assert body["error"]["code"] == -32016, f"got {body['error']}"
        assert "Minimum context slot" in body["error"]["message"]

    def test_solana_method_unaffected_by_other_method_override(self, sim):
        """Per-method overrides scope strictly to that method — an override on
        getSlot must not change getHealth."""
        _set_solana(
            sim, "1", responses={"getSlot": {"error": {"code": -32007, "message": "Slot skipped"}}}
        )
        _, body = _rpc(sim["provider1"], "getHealth")
        assert "error" not in body
        assert body["result"] == "ok"


# ─────────────────────────────────────────────────────────────────────────────
# History tracking — Solana requests must show up in /history like ETH/BTC ones
# ─────────────────────────────────────────────────────────────────────────────


class TestSolanaHistoryTracking:

    def test_solana_request_recorded_in_history(self, sim):
        _rpc(sim["provider1"], "getSlot")
        _, hist = _get(_ctrl(sim, "/history?provider=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "getSlot"
        assert last["status"] == "success"

    def test_solana_history_filter_by_method(self, sim):
        """?method= filters work for Solana method names just like ETH ones."""
        _rpc(sim["provider1"], "getSlot")
        _rpc(sim["provider1"], "getLatestBlockhash")
        _, hist = _get(_ctrl(sim, "/history?method=getSlot"))
        assert hist["count"] >= 1
        assert all(e["method"] == "getSlot" for e in hist["history"])

    def test_solana_error_status_recorded(self, sim):
        """A per-method error override on a Solana method produces status=error
        in history."""
        _set_solana(
            sim, "1", responses={"getSlot": {"error": {"code": -32007, "message": "Slot skipped"}}}
        )
        _rpc(sim["provider1"], "getSlot")
        _, hist = _get(_ctrl(sim, "/history?provider=1&status=error"))
        assert hist["count"] >= 1
        assert hist["history"][-1]["method"] == "getSlot"


# ─────────────────────────────────────────────────────────────────────────────
# Mixed-chain scenario — one ETH + one Solana listener on the same pid
# ─────────────────────────────────────────────────────────────────────────────


class TestMixedChainScenario:
    """Each pid has both an ETH listener (25560-2) and a Solana listener
    (25582-4) bound on the same ProviderState — mirrors prod's per-pid
    shared-state model. The ETH and Solana listeners for the same pid serve
    different responses simultaneously because dispatch is port-derived."""

    def test_eth_and_solana_listeners_independent_for_same_pid(self, sim):
        """No scenario at all — the same pid answers ETH on the ETH port and
        Solana on the Solana port, decided by the listener ports themselves."""
        _, eth_body = _rpc(sim["eth_provider1"], "eth_blockNumber")
        _, sol_body = _rpc(sim["provider1"], "getSlot")

        # ETH side: hex string with "0x" prefix.
        assert isinstance(eth_body["result"], str)
        assert eth_body["result"].startswith("0x")
        # Solana side: decimal slot integer.
        assert isinstance(sol_body["result"], int)
        assert sol_body["result"] == SOLANA_BASE_SLOT

    def test_fail_first_n_counts_only_owning_transport(self, sim):
        """fail_first_n's counter is consumed ONLY on the listener that owns the
        provider's chain_family. Requests to a different transport's listener
        (gated out) must not burn the first-N budget — so the owning listener
        still sees the first N calls as failures."""
        _set_solana(
            sim,
            "1",
            mode="error",
            error_code=-32077,
            error_message="boom",
            chain_family="solana",
            fail_first_n=2,
        )
        # Hit the ETH listener (chain_family mismatch) — must NOT consume the budget.
        for _ in range(3):
            _rpc(sim["eth_provider1"], "eth_blockNumber")
        # The Solana listener (owning) still sees the first 2 calls as failures.
        _, b1 = _rpc(sim["provider1"], "getSlot")
        _, b2 = _rpc(sim["provider1"], "getSlot")
        _, b3 = _rpc(sim["provider1"], "getSlot")
        assert b1.get("error", {}).get("code") == -32077, f"solana call 1 should fail: {b1}"
        assert b2.get("error", {}).get("code") == -32077, f"solana call 2 should fail: {b2}"
        assert "error" not in b3, f"solana call 3 should recover: {b3}"


# ─────────────────────────────────────────────────────────────────────────────
# Cross-transport fault isolation — mode=down is universal across chain_family
# ─────────────────────────────────────────────────────────────────────────────


class TestSolanaCrossTransportFaultIsolation:
    """Solana port must honor mode=down regardless of chain_family (the same
    universal-down contract the BTC suite locks for its dedicated port)."""

    def test_solana_killed_by_eth_down_fault(self, sim):
        """A ``chain_family="eth"`` down fault MUST 503 the Solana port.

        Universal-down semantics: mode="down" is honored on every transport
        regardless of chain_family because reachability is provider-wide.
        Without it, an ETH provider in mode=down would keep serving Solana
        responses, hiding router-side bugs that depend on the provider being
        unreachable across every node-url.
        """
        _post(
            _ctrl(sim, "/scenario"), {"providers": {"1": {"chain_family": "eth", "mode": "down"}}}
        )
        status, _ = _rpc(sim["provider1"], "getSlot")
        assert (
            status == 503
        ), f"Solana port should refuse with 503 under universal-down; got {status}"


# ─────────────────────────────────────────────────────────────────────────────
# Fault injection on the Solana listener — six fault primitives
# ─────────────────────────────────────────────────────────────────────────────


class TestSolanaFaultInjection:
    """Fault primitives gated to the Solana listener via chain_family="solana".

    Every test uses ``_set_solana_with_fault`` (not ``_set_solana``) so the
    snap's chain_family matches the listener's handler_chain_family and the
    fault gate fires. A plain ``_set_solana`` call without chain_family leaves
    jsonrpc_owns_snap=False, which silently bypasses fault evaluation for all
    content-mode faults (error / rate_limit / hang / drop_connection /
    corruption) — those tests would pass even if the fault never fired,
    producing false-greens identical to the class of bug described in the
    mode=down cross-transport isolation tests above. The latency test also uses
    ``_set_solana_with_fault`` for consistency, but ``latency_ms`` fires on the
    success path regardless of chain_family — its test is non-vacuous because it
    fails when latency_ms=0, not because of the chain_family gate."""

    def test_error_mode_returns_json_rpc_error(self, sim):
        """mode=error on a Solana-gated scenario returns HTTP 200 with a
        JSON-RPC error envelope. The default error_code is -32000 (the
        ProviderState default when no error_code override is provided)."""
        _set_solana_with_fault(sim, "1", mode="error")
        status, body = _rpc(sim["provider1"], "getSlot")
        assert (
            status == 200
        ), f"mode=error returns HTTP 200 with a JSON-RPC error body; got {status}"
        assert (
            "error" in body
        ), f"expected a JSON-RPC error envelope in the response body; got {body}"
        assert (
            body["error"]["code"] == -32000
        ), f"default error_code is -32000; got {body['error']['code']}"

    def test_rate_limit_mode_returns_429(self, sim):
        """mode=rate_limit on a Solana-gated scenario returns HTTP 429 with a
        JSON-RPC error envelope whose code is 429."""
        _set_solana_with_fault(sim, "1", mode="rate_limit")
        status, body = _rpc(sim["provider1"], "getSlot")
        assert status == 429, f"mode=rate_limit must return HTTP 429; got {status}"
        assert (
            "error" in body
        ), f"expected a JSON-RPC error envelope in the 429 response; got {body}"
        assert (
            body["error"]["code"] == 429
        ), f"rate_limit error_code is 429; got {body['error'].get('code')}"

    def test_hang_mode_times_out_client(self, sim):
        """mode=hang on a Solana-gated scenario holds the TCP connection open
        without sending a response. A client with a 1s read timeout must hit
        that timeout; elapsed time must be at least 0.9s — ruling out the
        fast success path where the handler returns immediately."""
        _set_solana_with_fault(sim, "1", mode="hang")
        req = urllib.request.Request(
            sim["provider1"],
            data=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        t0 = time.monotonic()
        timed_out = False
        try:
            urllib.request.urlopen(req, timeout=1.0)
        except (urllib.error.URLError, TimeoutError, OSError):
            timed_out = True
        elapsed = time.monotonic() - t0
        assert (
            timed_out
        ), "mode=hang should cause a client timeout rather than a successful response"
        assert (
            elapsed >= 0.9
        ), f"hang must block for at least the client timeout (~1s); got {elapsed:.2f}s"
        assert (
            elapsed < 3.0
        ), f"hang must exit at the ~1s client timeout, not a delayed success; got {elapsed:.2f}s"

    def test_drop_connection_before_headers_on_solana(self, sim):
        """mode=drop_connection with drop_at=before_headers on a Solana-gated
        scenario closes the TCP connection before sending any response bytes.
        The client must observe a connection error — not a JSON-RPC response."""
        _set_solana_with_fault(sim, "1", mode="drop_connection", drop_at="before_headers")
        # The connection is dropped before any HTTP response arrives, so any
        # transport-level error is the valid observable (no specific exception value to match).
        with pytest.raises((urllib.error.URLError, OSError)):
            _rpc(sim["provider1"], "getSlot")

    def test_corrupt_response_invalid_json_on_solana(self, sim):
        """corruption_mode=invalid_json on a Solana-gated scenario returns
        bytes that cannot be parsed as JSON. The request is built manually
        so that json.loads is not auto-called — _rpc would swallow the parse
        error. Under success mode without corruption the same path returns a
        valid JSON-RPC result envelope."""
        _set_solana_with_fault(sim, "1", corruption_mode="invalid_json")
        req = urllib.request.Request(
            sim["provider1"],
            data=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        # The corruption path returns deliberately unparseable bytes, so json.loads must
        # fail; a success response would parse cleanly.
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)

    def test_latency_mode_delays_response_on_solana(self, sim):
        """latency_ms=200 on a Solana-gated scenario delays the response by at
        least 180ms (allowing clock slack). Under success mode with no latency
        the same getSlot call returns in under 20ms on loopback — the 180ms
        lower bound proves the delay fires."""
        _set_solana_with_fault(sim, "1", latency_ms=200)
        t0 = time.monotonic()
        _rpc(sim["provider1"], "getSlot")
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert (
            elapsed_ms >= 180
        ), f"latency_ms=200 must delay the response by at least 180ms; got {elapsed_ms:.1f}ms"
