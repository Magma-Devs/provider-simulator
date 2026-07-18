"""
Integration tests for the Solana pool of the provider simulator.

Runs against the shared in-process simulator (see conftest.py): the solana-sim
pool listens on 18582-18584 and the eth-sim pool on 18545-18547. Under the
pool:pid model those are SEPARATE providers, so cross-pool isolation is
structural, not gated.

Coverage:
  getLatestBlockhash gap            — result.context.slot minus
                                      result.value.lastValidBlockHeight equals
                                      the provider's slot_block_gap quirk. The
                                      default gap (21_900_000) and a custom gap
                                      set via /scenario are both honoured.
  getSlot / getHealth / getVersion  — scalar slot, "ok", and the version object.
  slot_offset                       — per-provider slot divergence; the gap
                                      applies on top of the offset.
  Per-method overrides              — responses[method] = {"result": ...} and
                                      {"error": ...} short-circuit the stub.
  History tracking                  — Solana requests show up in /history with
                                      the method name and status preserved.
  Cross-pool isolation              — a down on eth-sim:1 never touches
                                      solana-sim:1.

Run with:
  pytest tests/test_simulator_solana.py -v
"""

import json
import time
import urllib.error
import urllib.request

import pytest

from constants import ETH_PRIMARY_PORTS, SOLANA_PRIMARY_PORTS
from stubs_solana import (
    SOLANA_BASE_SLOT,
    SOLANA_CORE_VERSION,
    SOLANA_DEFAULT_SLOT_BLOCK_GAP,
    SOLANA_FEATURE_SET,
)

_SOL_URLS = {pid: f"http://127.0.0.1:{port}" for pid, port in SOLANA_PRIMARY_PORTS.items()}
_ETH1 = f"http://127.0.0.1:{ETH_PRIMARY_PORTS['1']}"


# ── HTTP helpers (kept independent of the sibling files — duplication is
#     intentional so each file stays self-contained). ─────────────────────────


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


def _set_solana(sim, pid: str = "1", **extra):
    """Convenience: POST /scenario for one solana-sim provider. Extra kwargs
    are the provider block — universal fault fields plus the Solana quirks
    (slot_block_gap, slot_offset, unknown_method_mode)."""
    return _post(_ctrl(sim, "/scenario"), {"providers": {f"solana-sim:{pid}": dict(extra)}})


# ── Function-scoped autouse: clean slate before/after every test ──────────────


@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario config AND clear history before and after every test."""
    _post(_ctrl(sim, "/reset/all"), {})
    yield
    _post(_ctrl(sim, "/reset/all"), {})


# ─────────────────────────────────────────────────────────────────────────────
# Pool-derived dispatch: the port IS the pool
# ─────────────────────────────────────────────────────────────────────────────


class TestSolanaPortDispatch:

    def test_solana_port_dispatches_with_no_scenario(self, sim):
        """No /scenario call at all — the Solana port (18582) answers Solana
        methods because the endpoint belongs to the solana-sim pool."""
        status, body = _rpc(_SOL_URLS["1"], "getSlot")
        assert status == 200
        assert "error" not in body
        # Scalar slot pinned to the chain's base — proves the Solana chain ran.
        assert body["result"] == SOLANA_BASE_SLOT

    def test_solana_dispatch_unchanged_by_scenario_config(self, sim):
        """Scenario config tunes behaviour, never dispatch: after touching
        solana-sim:1 the port still serves the Solana shape."""
        _set_solana(sim, "1", latency_ms=0)
        status, body = _rpc(_SOL_URLS["1"], "getSlot")
        assert status == 200
        assert body["result"] == SOLANA_BASE_SLOT

    def test_eth_method_on_solana_port_returns_null_result(self, sim):
        """A method unknown to the Solana chain returns null result, not error —
        the simulator stays in success mode, the router sees an unfamiliar but
        well-formed response."""
        status, body = _rpc(_SOL_URLS["1"], "eth_blockNumber")
        assert status == 200
        assert "error" not in body
        assert body["result"] is None

    def test_solana_unknown_method_mode_error_returns_minus_32601(self, sim):
        """Opt-in: with unknown_method_mode="error", an unknown method returns
        a real -32601 method-not-found instead of the default null result — so
        the router's Solana error classifier can be exercised on a bad method.
        The default stays "null" (see the test above)."""
        _set_solana(sim, "1", unknown_method_mode="error")
        status, body = _rpc(_SOL_URLS["1"], "not_a_real_solana_method")
        assert status == 200
        assert "error" in body, f"expected a -32601 error envelope, got {body}"
        assert body["error"]["code"] == -32601


# ─────────────────────────────────────────────────────────────────────────────
# getLatestBlockhash — the slot ↔ lastValidBlockHeight gap (the bug carrier)
# ─────────────────────────────────────────────────────────────────────────────


class TestGetLatestBlockhashGap:

    def test_default_gap_between_slot_and_last_valid_block_height(self, sim):
        """Default scenario: context.slot - value.lastValidBlockHeight equals
        the default slot_block_gap (21_900_000). No /scenario call needed —
        the default is set at provider construction."""
        status, body = _rpc(_SOL_URLS["1"], "getLatestBlockhash")
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
        _set_solana(sim, "1", slot_block_gap=25)
        _, body = _rpc(_SOL_URLS["1"], "getLatestBlockhash")
        result = body["result"]
        assert result["context"]["slot"] - result["value"]["lastValidBlockHeight"] == 25

    def test_large_custom_gap_is_respected(self, sim):
        """A large gap set via /scenario (1_000_000) is honoured exactly."""
        _set_solana(sim, "1", slot_block_gap=1_000_000)
        _, body = _rpc(_SOL_URLS["1"], "getLatestBlockhash")
        result = body["result"]
        assert result["context"]["slot"] - result["value"]["lastValidBlockHeight"] == 1_000_000

    def test_slot_is_pinned_to_base(self, sim):
        """context.slot is pinned to SOLANA_BASE_SLOT so the gap is the only
        moving part — lets gap assertions pin against an exact slot value."""
        _, body = _rpc(_SOL_URLS["1"], "getLatestBlockhash")
        assert body["result"]["context"]["slot"] == SOLANA_BASE_SLOT

    def test_blockhash_value_is_present_and_base58_length(self, sim):
        """value.blockhash is a non-empty base58-length string (the router reads
        the numeric fields, but the hash field must be shaped like a real one)."""
        _, body = _rpc(_SOL_URLS["1"], "getLatestBlockhash")
        blockhash = body["result"]["value"]["blockhash"]
        assert isinstance(blockhash, str)
        # base58 of 32 bytes is 43-44 chars.
        assert 43 <= len(blockhash) <= 44

    def test_custom_gap_on_one_provider_does_not_leak_to_another(self, sim):
        """A gap override on solana-sim:1 must not change solana-sim:2's
        default gap — per-provider isolation."""
        _set_solana(sim, "1", slot_block_gap=25)
        _, body1 = _rpc(_SOL_URLS["1"], "getLatestBlockhash")
        _, body2 = _rpc(_SOL_URLS["2"], "getLatestBlockhash")
        r1, r2 = body1["result"], body2["result"]
        assert r1["context"]["slot"] - r1["value"]["lastValidBlockHeight"] == 25
        assert (
            r2["context"]["slot"] - r2["value"]["lastValidBlockHeight"]
            == SOLANA_DEFAULT_SLOT_BLOCK_GAP
        )


# ─────────────────────────────────────────────────────────────────────────────
# slot_offset — per-provider slot divergence. The reported slot is
# SOLANA_BASE_SLOT + offset (default 0). Distinct offsets per provider let a
# blackbox test stand up one current + several stale-behind providers so the
# router's Solana consistency filter can keep the current one and drop the
# rest. lastValidBlockHeight stays slot - gap, so the gap applies on top.
# ─────────────────────────────────────────────────────────────────────────────


class TestSolanaSlotOffset:

    def test_default_offset_zero_reports_base_slot(self, sim):
        """No /scenario call — the default offset is 0, so getSlot and
        getLatestBlockhash.context.slot both report exactly SOLANA_BASE_SLOT."""
        _, slot_body = _rpc(_SOL_URLS["1"], "getSlot")
        _, bh_body = _rpc(_SOL_URLS["1"], "getLatestBlockhash")
        assert slot_body["result"] == SOLANA_BASE_SLOT
        assert bh_body["result"]["context"]["slot"] == SOLANA_BASE_SLOT

    def test_negative_offset_shifts_slot_below_base_and_gap_applies_on_top(self, sim):
        """offset = -10_000_000 ⇒ slot == base - 10_000_000, on BOTH getSlot and
        getLatestBlockhash.context.slot. And value.lastValidBlockHeight stays
        slot - default_gap, proving the gap applies on top of the offset rather
        than replacing it."""
        offset = -10_000_000
        _set_solana(sim, "1", slot_offset=offset)

        _, slot_body = _rpc(_SOL_URLS["1"], "getSlot")
        assert slot_body["result"] == SOLANA_BASE_SLOT + offset

        _, bh_body = _rpc(_SOL_URLS["1"], "getLatestBlockhash")
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
        _set_solana(sim, "1", slot_offset=offset)

        _, slot_body = _rpc(_SOL_URLS["1"], "getSlot")
        assert slot_body["result"] == SOLANA_BASE_SLOT + offset

        _, bh_body = _rpc(_SOL_URLS["1"], "getLatestBlockhash")
        result = bh_body["result"]
        assert result["context"]["slot"] == SOLANA_BASE_SLOT + offset
        assert (
            result["value"]["lastValidBlockHeight"]
            == SOLANA_BASE_SLOT + offset - SOLANA_DEFAULT_SLOT_BLOCK_GAP
        )

    def test_per_provider_offsets_are_independent(self, sim):
        """Distinct offsets on solana-sim:1/2/3 each report their OWN slot —
        the per-provider divergence the multi-slot test needs. Provider 1
        current (0), provider 2 far behind, provider 3 slightly ahead."""
        _set_solana(sim, "1", slot_offset=0)
        _set_solana(sim, "2", slot_offset=-12_000_000)
        _set_solana(sim, "3", slot_offset=3_000_000)

        _, b1 = _rpc(_SOL_URLS["1"], "getSlot")
        _, b2 = _rpc(_SOL_URLS["2"], "getSlot")
        _, b3 = _rpc(_SOL_URLS["3"], "getSlot")

        assert b1["result"] == SOLANA_BASE_SLOT
        assert b2["result"] == SOLANA_BASE_SLOT - 12_000_000
        assert b3["result"] == SOLANA_BASE_SLOT + 3_000_000
        # All three distinct — proves no cross-provider contamination.
        assert len({b1["result"], b2["result"], b3["result"]}) == 3

    def test_offset_on_one_provider_does_not_leak_to_another(self, sim):
        """An offset override on solana-sim:1 must not change solana-sim:2's
        default (offset 0 ⇒ base slot)."""
        _set_solana(sim, "1", slot_offset=-7_000_000)
        _, b1 = _rpc(_SOL_URLS["1"], "getSlot")
        _, b2 = _rpc(_SOL_URLS["2"], "getSlot")
        assert b1["result"] == SOLANA_BASE_SLOT - 7_000_000
        assert b2["result"] == SOLANA_BASE_SLOT

    def test_offset_and_gap_are_independent_knobs(self, sim):
        """Setting offset and gap together: slot shifts by the offset, and the
        slot ↔ lastValidBlockHeight distance equals the gap — the two knobs
        compose without interfering."""
        offset = -4_000_000
        gap = 25
        _set_solana(sim, "1", slot_offset=offset, slot_block_gap=gap)
        _, bh_body = _rpc(_SOL_URLS["1"], "getLatestBlockhash")
        result = bh_body["result"]
        assert result["context"]["slot"] == SOLANA_BASE_SLOT + offset
        assert result["context"]["slot"] - result["value"]["lastValidBlockHeight"] == gap


# ─────────────────────────────────────────────────────────────────────────────
# getSlot / getHealth / getVersion — the supporting spec-verification methods
# ─────────────────────────────────────────────────────────────────────────────


class TestSolanaSupportingMethods:

    def test_get_slot_returns_base_slot(self, sim):
        """getSlot returns the same slot the getLatestBlockhash context carries."""
        _, slot_body = _rpc(_SOL_URLS["1"], "getSlot")
        _, blockhash_body = _rpc(_SOL_URLS["1"], "getLatestBlockhash")
        assert slot_body["result"] == SOLANA_BASE_SLOT
        # getSlot and getLatestBlockhash.context.slot agree.
        assert slot_body["result"] == blockhash_body["result"]["context"]["slot"]

    def test_get_slot_is_an_integer(self, sim):
        """getSlot returns a JSON number, not a hex string — Solana convention."""
        _, body = _rpc(_SOL_URLS["1"], "getSlot")
        assert isinstance(body["result"], int)

    def test_get_health_returns_ok(self, sim):
        """A healthy Solana validator's getHealth returns the literal "ok"."""
        _, body = _rpc(_SOL_URLS["1"], "getHealth")
        assert body["result"] == "ok"

    def test_get_version_returns_expected_object(self, sim):
        """getVersion returns {"solana-core": <semver>, "feature-set": <u32>}."""
        _, body = _rpc(_SOL_URLS["1"], "getVersion")
        result = body["result"]
        assert result == {
            "solana-core": SOLANA_CORE_VERSION,
            "feature-set": SOLANA_FEATURE_SET,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Per-method overrides on a Solana provider
# ─────────────────────────────────────────────────────────────────────────────


class TestSolanaPerMethodOverrides:

    def test_solana_result_override(self, sim):
        """responses[method] = {"result": <value>} replaces the computed stub."""
        _set_solana(sim, "1", responses={"getVersion": {"result": {"solana-core": "9.9.9"}}})
        _, body = _rpc(_SOL_URLS["1"], "getVersion")
        assert body["result"] == {"solana-core": "9.9.9"}

    def test_solana_error_override_raw_envelope(self, sim):
        """responses[method] = {"error": {...}} emits the JSON-RPC error envelope
        directly — the raw escape-hatch, alongside the named error_stub path."""
        _set_solana(
            sim, "1", responses={"getSlot": {"error": {"code": -32007, "message": "Slot skipped"}}}
        )
        _, body = _rpc(_SOL_URLS["1"], "getSlot")
        assert "error" in body
        assert body["error"]["code"] == -32007
        assert body["error"]["message"] == "Slot skipped"

    def test_solana_error_stub_named_catalogue(self, sim):
        """responses[method] = {"error_stub": "<name>"} resolves against the
        named Solana error catalogue, so tests inject a canonical Solana error
        by name instead of hand-typing the envelope."""
        _set_solana(sim, "1", responses={"getSlot": {"error_stub": "min_context_slot_not_reached"}})
        _, body = _rpc(_SOL_URLS["1"], "getSlot")
        assert "error" in body, f"expected an error envelope, got {body}"
        assert body["error"]["code"] == -32016, f"got {body['error']}"
        assert "Minimum context slot" in body["error"]["message"]

    def test_solana_method_unaffected_by_other_method_override(self, sim):
        """Per-method overrides scope strictly to that method — an override on
        getSlot must not change getHealth."""
        _set_solana(
            sim, "1", responses={"getSlot": {"error": {"code": -32007, "message": "Slot skipped"}}}
        )
        _, body = _rpc(_SOL_URLS["1"], "getHealth")
        assert "error" not in body
        assert body["result"] == "ok"


# ─────────────────────────────────────────────────────────────────────────────
# History tracking — Solana requests must show up in /history like ETH/BTC ones
# ─────────────────────────────────────────────────────────────────────────────


class TestSolanaHistoryTracking:

    def test_solana_request_recorded_in_history(self, sim):
        _rpc(_SOL_URLS["1"], "getSlot")
        _, hist = _get(_ctrl(sim, "/history?pool=solana-sim&pid=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "getSlot"
        assert last["status"] == "success"
        assert last["pool"] == "solana-sim"
        assert last["port"] == SOLANA_PRIMARY_PORTS["1"]

    def test_solana_history_filter_by_method(self, sim):
        """?method= filters work for Solana method names just like ETH ones."""
        _rpc(_SOL_URLS["1"], "getSlot")
        _rpc(_SOL_URLS["1"], "getLatestBlockhash")
        _, hist = _get(_ctrl(sim, "/history?method=getSlot"))
        assert hist["count"] >= 1
        assert all(e["method"] == "getSlot" for e in hist["history"])

    def test_solana_error_status_recorded(self, sim):
        """A per-method error override on a Solana method produces status=error
        in history."""
        _set_solana(
            sim, "1", responses={"getSlot": {"error": {"code": -32007, "message": "Slot skipped"}}}
        )
        _rpc(_SOL_URLS["1"], "getSlot")
        _, hist = _get(_ctrl(sim, "/history?pool=solana-sim&pid=1&status=error"))
        assert hist["count"] >= 1
        assert hist["history"][-1]["method"] == "getSlot"


# ─────────────────────────────────────────────────────────────────────────────
# Cross-pool independence — eth-sim:1 and solana-sim:1 are different providers
# ─────────────────────────────────────────────────────────────────────────────


class TestMixedChainScenario:

    def test_eth_and_solana_listeners_independent_with_no_scenario(self, sim):
        """No scenario at all — the ETH port answers ETH and the Solana port
        answers Solana."""
        _, eth_body = _rpc(_ETH1, "eth_blockNumber")
        _, sol_body = _rpc(_SOL_URLS["1"], "getSlot")

        # ETH side: hex string with "0x" prefix.
        assert isinstance(eth_body["result"], str)
        assert eth_body["result"].startswith("0x")
        # Solana side: decimal slot integer.
        assert isinstance(sol_body["result"], int)
        assert sol_body["result"] == SOLANA_BASE_SLOT

    def test_fail_first_n_window_is_private_to_the_pool(self, sim):
        """fail_first_n's counter belongs to solana-sim:1 alone. Requests to
        eth-sim:1 (a different provider in a different pool) can never burn
        the first-N budget — the Solana provider still fails exactly its
        first 2 calls."""
        _set_solana(
            sim,
            "1",
            mode="error",
            error_code=-32077,
            error_message="boom",
            fail_first_n=2,
        )
        # Hit the eth-sim provider — must NOT consume solana-sim:1's budget.
        for _ in range(3):
            _rpc(_ETH1, "eth_blockNumber")
        # The Solana provider still sees the first 2 calls as failures.
        _, b1 = _rpc(_SOL_URLS["1"], "getSlot")
        _, b2 = _rpc(_SOL_URLS["1"], "getSlot")
        _, b3 = _rpc(_SOL_URLS["1"], "getSlot")
        assert b1.get("error", {}).get("code") == -32077, f"solana call 1 should fail: {b1}"
        assert b2.get("error", {}).get("code") == -32077, f"solana call 2 should fail: {b2}"
        assert "error" not in b3, f"solana call 3 should recover: {b3}"


# ─────────────────────────────────────────────────────────────────────────────
# Cross-pool isolation — a down on eth-sim can never reach solana-sim
# ─────────────────────────────────────────────────────────────────────────────


class TestSolanaCrossPoolIsolation:
    """Under the old bare-pid model, eth pid "1" and solana pid "1" were ONE
    state object, so an eth-authored down also killed the Solana port. The
    pool:pid model abolishes that: this test pins the isolation."""

    def test_solana_unaffected_by_eth_down_fault(self, sim):
        """mode=down on eth-sim:1 kills every eth-sim:1 endpoint and nothing
        else — solana-sim:1 keeps serving success."""
        _post(_ctrl(sim, "/scenario"), {"providers": {"eth-sim:1": {"mode": "down"}}})

        eth_status, _ = _rpc(_ETH1, "eth_blockNumber")
        assert eth_status == 503, f"eth-sim:1 must be down; got {eth_status}"

        status, body = _rpc(_SOL_URLS["1"], "getSlot")
        assert status == 200, f"solana-sim:1 must be untouched by an eth-sim down; got {status}"
        assert body["result"] == SOLANA_BASE_SLOT


# ─────────────────────────────────────────────────────────────────────────────
# Fault injection on the Solana provider — six fault primitives
# ─────────────────────────────────────────────────────────────────────────────


class TestSolanaFaultInjection:
    """Fault primitives addressed at solana-sim:<pid> directly — the provider
    owns its endpoints, so no gating field is needed for them to fire."""

    def test_error_mode_returns_json_rpc_error(self, sim):
        """mode=error on solana-sim:1 returns HTTP 200 with a JSON-RPC error
        envelope. The default error_code is -32000."""
        _set_solana(sim, "1", mode="error")
        status, body = _rpc(_SOL_URLS["1"], "getSlot")
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
        """mode=rate_limit on solana-sim:1 returns HTTP 429 with a JSON-RPC
        error envelope whose code is 429."""
        _set_solana(sim, "1", mode="rate_limit")
        status, body = _rpc(_SOL_URLS["1"], "getSlot")
        assert status == 429, f"mode=rate_limit must return HTTP 429; got {status}"
        assert (
            "error" in body
        ), f"expected a JSON-RPC error envelope in the 429 response; got {body}"
        assert (
            body["error"]["code"] == 429
        ), f"rate_limit error_code is 429; got {body['error'].get('code')}"

    def test_hang_mode_times_out_client(self, sim):
        """mode=hang on solana-sim:1 holds the TCP connection open without
        sending a response. A client with a 1s read timeout must hit that
        timeout; elapsed time must be at least 0.9s — ruling out the fast
        success path where the handler returns immediately."""
        _set_solana(sim, "1", mode="hang")
        req = urllib.request.Request(
            _SOL_URLS["1"],
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
        """mode=drop_connection with drop_at=before_headers on solana-sim:1
        closes the TCP connection before sending any response bytes. The
        client must observe a connection error — not a JSON-RPC response."""
        _set_solana(sim, "1", mode="drop_connection", drop_at="before_headers")
        # The connection is dropped before any HTTP response arrives, so any
        # transport-level error is the valid observable.
        with pytest.raises((urllib.error.URLError, OSError)):
            _rpc(_SOL_URLS["1"], "getSlot")

    def test_corrupt_response_invalid_json_on_solana(self, sim):
        """corruption_mode=invalid_json on solana-sim:1 returns bytes that
        cannot be parsed as JSON. The request is built manually so that
        json.loads is not auto-called — _rpc would swallow the parse error."""
        _set_solana(sim, "1", corruption_mode="invalid_json")
        req = urllib.request.Request(
            _SOL_URLS["1"],
            data=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        # The corruption path returns deliberately unparseable bytes, so
        # json.loads must fail; a success response would parse cleanly.
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)

    def test_latency_mode_delays_response_on_solana(self, sim):
        """latency_ms=200 on solana-sim:1 delays the response by at least 180ms
        (allowing clock slack). Under success mode with no latency the same
        getSlot call returns in under 20ms on loopback — the 180ms lower bound
        proves the delay fires."""
        _set_solana(sim, "1", latency_ms=200)
        t0 = time.monotonic()
        _rpc(_SOL_URLS["1"], "getSlot")
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert (
            elapsed_ms >= 180
        ), f"latency_ms=200 must delay the response by at least 180ms; got {elapsed_ms:.1f}ms"
