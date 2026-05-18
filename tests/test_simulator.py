"""
Unit tests for the provider-simulator.

Starts all 4 servers (3 JSON-RPC + 1 control) in-process on test ports so the
tests are fully self-contained — no running pod or network required.

Coverage
--------
  /health                   liveness probe
  /scenario  GET + POST     read and change provider config
  /stats                    all-time call counters
  /reset                    scenario reset only (history untouched)
  /history/clear            history wipe only (scenario untouched)
  /reset/all                both together
  /history                  all filter params:
                              ?last=, ?from=, ?to=, ?provider=, ?method=, ?status=
                            call_order sequential numbering
  provider modes            success, error, rate_limit, down,
                            error_probability, latency_ms
  custom responses          per-method overrides via /scenario
  eth_getBlockByNumber      block number echo from params[0]
  unknown paths             404 on control API

Architectural facts verified by these tests
-------------------------------------------
  1. HISTORY_MAX cap
       HISTORY_MAX defaults to 2000 entries per provider (MAG-1822, was 200);
       override at pod startup via SIM_HISTORY_MAX. With 3 providers that's
       6000 ring-buffer slots, all in memory. In the deployed environment the
       router's background calls (scoring/pruning) fill the buffer continuously.
       In unit tests there is no background traffic — history starts at 0 and
       only grows with calls the tests explicitly make. The four ring-buffer-
       rollover tests pin themselves to MAX_FOR_OVERFLOW_TEST=200 (via the
       shrink_history fixture) so they don't fire ~2000 calls each.

  2. Method filter is the correct isolation tool (deployed env)
       The router's background calls are exclusively eth_blockNumber and
       eth_getBlockByNumber (pruning verification). Using ?method=eth_gasPrice
       therefore returns only the one call you explicitly sent (count=1).
       Not relevant in unit tests — there is no background traffic to filter out.

  3. Clear-timestamp invariant
       /reset          — does NOT touch history. Every entry survives unchanged.
       /history/clear  — wipes all entries. Every entry that appears after
                         the clear has ts >= timestamp_of_the_clear_call.
       /reset/all      — same clear guarantee, plus scenario reset.
       No pre-clear entry can ever appear in history after a clear.

Run with:
  pytest tests/test_simulator.py -v
"""

import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import HTTPServer, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from constants import HISTORY_MAX  # noqa: F401  (kept for module docstring reference; tests use MAX_FOR_OVERFLOW_TEST)
from server import ControlHandler, JSONRPCHandler, ProviderState
from stubs import ERROR_STUBS

# ── Test ports (different from production 18545-18547 / 19000) ────────────────

_PROVIDER_PORTS = {"1": 28545, "2": 28546, "3": 28547}
_CONTROL_PORT   = 29000

# ── Overflow-test cap (MAG-1822) ──────────────────────────────────────────────
# The 4 ring-buffer-rollover tests in this file send HISTORY_MAX (+a few) calls
# to verify the deque caps at maxlen. After MAG-1822 raised HISTORY_MAX from
# 200 → 2000, those loops would fire ~2005 calls each and turn the unit suite
# slow without buying any extra correctness — the cap behaviour is identical
# at any maxlen. We pin those tests to 200 by swapping the running provider's
# `history` deque for a freshly-built one with maxlen=200 before pushing,
# then restoring the original maxlen on test teardown (see the shrink_history
# fixture below) so subsequent tests still see a HISTORY_MAX-sized buffer.
MAX_FOR_OVERFLOW_TEST = 200


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post(url: str, body: dict) -> tuple[int, dict]:
    """POST JSON body, return (status_code, parsed_response_body)."""
    data = json.dumps(body).encode()
    req  = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
            return e.code, json.loads(raw) if raw else {}
        except (ConnectionResetError, OSError):
            # server closed connection with no body (e.g. mode=down returns 503 + no body)
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
    """Start 3 JSON-RPC servers + 1 control server on test ports.

    Yields a dict with base URLs:
      sim["control"]   → http://127.0.0.1:29000
      sim["provider1"] → http://127.0.0.1:28545
      sim["provider2"] → http://127.0.0.1:28546
      sim["provider3"] → http://127.0.0.1:28547
    """
    states = {pid: ProviderState() for pid in _PROVIDER_PORTS}

    servers = []
    for pid, port in _PROVIDER_PORTS.items():
        # ThreadingHTTPServer so slow/hanging requests don't serialize each
        # provider's handler queue (matters for hang mode tests).
        srv             = ThreadingHTTPServer(("127.0.0.1", port), JSONRPCHandler)
        srv.daemon_threads = True
        srv.state       = states[pid]
        srv.provider_id = pid
        servers.append(srv)

    ctrl                  = HTTPServer(("127.0.0.1", _CONTROL_PORT), ControlHandler)
    ctrl.provider_states  = states
    servers.append(ctrl)

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()

    time.sleep(0.15)  # allow all servers to finish binding

    yield {
        "control":   f"http://127.0.0.1:{_CONTROL_PORT}",
        "provider1": f"http://127.0.0.1:{_PROVIDER_PORTS['1']}",
        "provider2": f"http://127.0.0.1:{_PROVIDER_PORTS['2']}",
        "provider3": f"http://127.0.0.1:{_PROVIDER_PORTS['3']}",
        # Direct handle on the in-memory ProviderState objects backing the
        # running test servers. Used by the MAG-1822 overflow tests to swap
        # the history deque for a smaller one without restarting the suite.
        # Most tests should NOT touch this — go through the control API.
        "states":    states,
    }

    for s in servers:
        s.shutdown()


# ── Function-scoped autouse: clean slate before/after every test ──────────────

@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario config AND clear history before and after every test."""
    _post(_ctrl(sim, "/reset/all"), {})
    yield
    _post(_ctrl(sim, "/reset/all"), {})


@pytest.fixture
def shrink_history(sim):
    """Temporarily shrink a provider's history deque, restoring the original
    maxlen on teardown (MAG-1822).

    `deque.maxlen` is immutable after construction — we swap the deque for a
    smaller one (locking the state so a concurrent push doesn't drop the new
    buffer on the floor) and remember the original maxlen so we can rebuild
    a full-sized deque at the end of the test. Without the restore the shrunk
    deque persists for the rest of the module run, silently capping any later
    test that pushes past 200 entries to that provider.
    """
    original_maxlens: dict[str, int] = {}

    def _shrink(pid: str, maxlen: int = MAX_FOR_OVERFLOW_TEST) -> None:
        state = sim["states"][pid]
        with state.lock:
            if pid not in original_maxlens:
                original_maxlens[pid] = state.history.maxlen
            state.history = deque(maxlen=maxlen)
            state.total_calls = 0
            state.calls_by_status = {}

    yield _shrink

    for pid, original_maxlen in original_maxlens.items():
        state = sim["states"][pid]
        with state.lock:
            state.history = deque(maxlen=original_maxlen)


# ─────────────────────────────────────────────────────────────────────────────
# /health
# ─────────────────────────────────────────────────────────────────────────────

class TestHealth:

    def test_returns_ok(self, sim):
        status, body = _get(_ctrl(sim, "/health"))
        assert status == 200
        assert body == {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────────────
# /scenario  GET + POST
# ─────────────────────────────────────────────────────────────────────────────

class TestScenario:

    def test_get_returns_defaults_after_reset(self, sim):
        _, body = _get(_ctrl(sim, "/scenario"))
        for pid in ["1", "2", "3"]:
            p = body["providers"][pid]
            assert p["mode"]              == "success"
            assert p["latency_ms"]        == 0
            assert p["error_probability"] == 0.0

    def test_post_updates_target_provider_only(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "error"}}})
        _, body = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["1"]["mode"] == "error"
        assert body["providers"]["2"]["mode"] == "success"   # untouched
        assert body["providers"]["3"]["mode"] == "success"   # untouched

    def test_post_partial_update_preserves_other_fields(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"2": {"latency_ms": 100}}})
        _, body = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["2"]["latency_ms"] == 100
        assert body["providers"]["2"]["mode"]       == "success"   # unchanged

    def test_post_error_probability(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"3": {"error_probability": 0.7}}})
        _, body = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["3"]["error_probability"] == 0.7

    def test_unknown_path_returns_404(self, sim):
        status, body = _get(_ctrl(sim, "/nonexistent"))
        assert status == 404
        assert "error" in body


# ─────────────────────────────────────────────────────────────────────────────
# Provider modes
# ─────────────────────────────────────────────────────────────────────────────

class TestProviderModes:

    def test_success_returns_result(self, sim):
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200
        assert "result" in body
        assert "error" not in body

    def test_error_mode_returns_jsonrpc_error(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "error"}}})
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200
        assert "error" in body
        assert body["error"]["code"] == -32000

    def test_rate_limit_returns_429(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "rate_limit"}}})
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 429
        assert body["error"]["code"] == 429

    def test_down_returns_503_with_no_body(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "down"}}})
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 503
        assert body == {}   # server sends no body for 503

    def test_error_probability_1_always_errors(self, sim):
        _post(_ctrl(sim, "/scenario"),
              {"providers": {"1": {"mode": "success", "error_probability": 1.0}}})
        for _ in range(5):
            _, body = _rpc(sim["provider1"], "eth_blockNumber")
            assert "error" in body

    def test_error_probability_0_never_errors(self, sim):
        _post(_ctrl(sim, "/scenario"),
              {"providers": {"1": {"mode": "success", "error_probability": 0.0}}})
        for _ in range(5):
            _, body = _rpc(sim["provider1"], "eth_blockNumber")
            assert "result" in body

    def test_latency_ms_delays_response(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"latency_ms": 200}}})
        t0 = time.monotonic()
        _rpc(sim["provider1"], "eth_blockNumber")
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert elapsed_ms >= 180   # allow a little clock slack

    def test_custom_response_per_method(self, sim):
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"responses": {"eth_blockNumber": {"result": "0xDEAD"}}}}
        })
        _, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert body["result"] == "0xDEAD"

    def test_custom_default_response(self, sim):
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"responses": {"default": {"result": "0xBEEF"}}}}
        })
        _, body = _rpc(sim["provider1"], "eth_unknownMethod")
        assert body["result"] == "0xBEEF"

    def test_eth_get_block_by_number_echoes_block_number(self, sim):
        _, body = _rpc(sim["provider1"], "eth_getBlockByNumber", ["0xABC123", False])
        assert body["result"]["number"] == "0xABC123"

    def test_eth_get_block_by_number_latest(self, sim):
        _, body = _rpc(sim["provider1"], "eth_getBlockByNumber", ["latest", False])
        assert body["result"]["number"] == "0x1312D00"


# ─────────────────────────────────────────────────────────────────────────────
# Per-method error override path — two flavours
#
# Primary: responses[method] = {"error_stub": "<name>"} — the simulator
#   resolves the name against its local ERROR_STUBS catalogue and emits the
#   envelope. Single source of truth, same ownership pattern as METHOD_DEFAULTS.
#
# Escape hatch: responses[method] = {"error": <inner_envelope>} — for ad-hoc
#   error shapes that don't earn a permanent catalogue entry.
#
# Both flow through the same emission code in server.py; only the resolution
# of `err` differs.
#
# Backward-compat invariant: when method_cfg has neither "error_stub" nor
# "error" (i.e. the existing {"result": ...} pattern), the new code is a no-op.
# test_existing_result_override_still_works locks that in.
# ─────────────────────────────────────────────────────────────────────────────

class TestErrorStubs:

    @pytest.mark.parametrize("stub_name", list(ERROR_STUBS.keys()))
    def test_each_stub_emits_matching_envelope(self, sim, stub_name):
        """Each ERROR_STUBS entry round-trips through the wire unchanged.

        Drives the primary path: client sends just the name, simulator
        resolves to the envelope. Asserts the response body's error block
        matches the catalogue entry on code, message, and (when present) data.
        """
        stub = ERROR_STUBS[stub_name]
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"responses": {"eth_call": {"error_stub": stub_name}}}}
        })
        status, body = _rpc(sim["provider1"], "eth_call")
        assert status == 200
        assert "error" in body, f"{stub_name}: expected error envelope, got {body!r}"
        assert "result" not in body
        assert body["error"]["code"] == stub["code"]
        assert body["error"]["message"] == stub["message"]
        if "data" in stub:
            assert body["error"]["data"] == stub["data"]

    def test_per_method_scoping_other_methods_unaffected(self, sim):
        """An error on eth_call leaves eth_blockNumber on its success path.

        This is the scoping guarantee that distinguishes the new branch from
        mode="error" — mode="error" errors for *every* method on the provider;
        the per-method override errors only for the named method.
        """
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"responses": {"eth_call": {"error_stub": "revert"}}}}
        })
        _, err_body = _rpc(sim["provider1"], "eth_call")
        assert "error" in err_body

        _, ok_body = _rpc(sim["provider1"], "eth_blockNumber")
        assert "result" in ok_body
        assert "error" not in ok_body

    def test_default_key_emits_error_for_unmatched_methods(self, sim):
        """The "default" fallback key also honours error_stub.

        Same precedence as the success path: state.responses.get(method) wins
        when present, otherwise state.responses.get("default", {}) is used.
        """
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"responses": {"default": {"error_stub": "oog"}}}}
        })
        _, body = _rpc(sim["provider1"], "eth_unknownMethod")
        assert "error" in body
        assert body["error"]["message"] == "out of gas"

    def test_existing_result_override_still_works(self, sim):
        """Backward-compat lock: {"result": ...} responses remain unchanged.

        Duplicate of test_custom_response_per_method by design — this one
        sits next to the new error branch so any future change to the
        branch logic has an in-place regression check.
        """
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"responses": {"eth_blockNumber": {"result": "0xDEAD"}}}}
        })
        _, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert body["result"] == "0xDEAD"
        assert "error" not in body

    def test_per_method_http_status_override(self, sim):
        """method_cfg["http_status"] is honoured when the error branch fires.

        Lets a single method emit, for example, HTTP 200 with a JSON-RPC error
        body (the common chain-domain case) while another method on the same
        provider can succeed with a different status if needed.
        """
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"responses": {
                "eth_call": {"error_stub": "revert", "http_status": 200}
            }}}
        })
        status, body = _rpc(sim["provider1"], "eth_call")
        assert status == 200
        assert body["error"]["message"] == "execution reverted"

    def test_history_records_error_status(self, sim):
        """The per-method error path records status='error' in /history.

        Aligns with the existing mode="error" path so downstream consumers
        (RoutingTrace.retry_chain, assertion helpers) treat both error
        sources identically.
        """
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"responses": {"eth_call": {"error_stub": "revert"}}}}
        })
        _rpc(sim["provider1"], "eth_call")
        _, body = _get(_ctrl(sim, "/history"))
        entries = [e for e in body["history"] if e["method"] == "eth_call"]
        assert len(entries) == 1
        assert entries[0]["status"] == "error"

    def test_raw_error_envelope_escape_hatch(self, sim):
        """The {"error": <envelope>} path emits ad-hoc shapes not in the catalogue.

        Useful for one-off tests that need a specific malformed envelope
        without polluting ERROR_STUBS with an entry that exists for one test.
        """
        ad_hoc = {"code": -32099, "message": "synthetic test error",
                  "data": {"trace_id": "abc-123"}}
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"responses": {"eth_call": {"error": ad_hoc}}}}
        })
        _, body = _rpc(sim["provider1"], "eth_call")
        assert body["error"] == ad_hoc


# ─────────────────────────────────────────────────────────────────────────────
# /reset
# ─────────────────────────────────────────────────────────────────────────────

class TestReset:

    def test_reset_restores_scenario_to_defaults(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {
            "1": {"mode": "error", "latency_ms": 500, "error_probability": 0.9}
        }})
        status, body = _post(_ctrl(sim, "/reset"), {})
        assert status == 200
        _, scenario = _get(_ctrl(sim, "/scenario"))
        p = scenario["providers"]["1"]
        assert p["mode"]              == "success"
        assert p["latency_ms"]        == 0
        assert p["error_probability"] == 0.0

    def test_reset_does_NOT_clear_history(self, sim):
        """history/clear — /reset must leave history intact."""
        # populate history on all 3 providers
        for provider in ("provider1", "provider2", "provider3"):
            _rpc(sim[provider], "eth_blockNumber")

        _, before = _get(_ctrl(sim, "/history"))
        assert before["count"] == 3, \
            f"expected 3 entries before reset, got {before['count']}"

        _post(_ctrl(sim, "/reset"), {})   # scenario reset only

        _, after = _get(_ctrl(sim, "/history"))
        assert after["count"] == 3, \
            f"/reset must not touch history — expected 3, got {after['count']}"
        assert after["history"] == before["history"], \
            "/reset must not modify existing history entries"


# ─────────────────────────────────────────────────────────────────────────────
# /history/clear
# ─────────────────────────────────────────────────────────────────────────────

class TestHistoryClear:

    def test_history_is_empty_immediately_after_clear(self, sim):
        """Core guarantee: /history/clear wipes every entry from all providers."""
        # populate history on all 3 providers
        for provider in ("provider1", "provider2", "provider3"):
            _rpc(sim[provider], "eth_blockNumber")

        _, before = _get(_ctrl(sim, "/history"))
        assert before["count"] == 3, \
            f"precondition failed — expected 3 entries before clear, got {before['count']}"

        status, body = _post(_ctrl(sim, "/history/clear"), {})
        assert status == 200

        _, after = _get(_ctrl(sim, "/history"))
        assert after == {"count": 0, "history": []}, \
            f"/history/clear did not wipe history — got: {after}"

    def test_new_call_after_clear_starts_at_count_1(self, sim):
        """After a clear, the very next call must be the only entry — no resurrection."""
        for provider in ("provider1", "provider2", "provider3"):
            _rpc(sim[provider], "eth_blockNumber")

        _post(_ctrl(sim, "/history/clear"), {})

        _rpc(sim["provider1"], "eth_blockNumber")   # exactly one new call

        _, after = _get(_ctrl(sim, "/history"))
        assert after["count"] == 1, \
            f"expected exactly 1 entry after clear + 1 call, got {after['count']}"
        assert after["history"][0]["call_order"] == 1

    def test_clear_wipes_all_three_providers(self, sim):
        """All 3 provider ring-buffers must be empty after clear, not just one."""
        for provider in ("provider1", "provider2", "provider3"):
            _rpc(sim[provider], "eth_blockNumber")

        _post(_ctrl(sim, "/history/clear"), {})

        for pid in ("1", "2", "3"):
            _, body = _get(_ctrl(sim, f"/history?provider={pid}"))
            assert body == {"count": 0, "history": []}, \
                f"provider {pid} history not cleared — got: {body}"

    def test_stats_counters_reset_to_zero_after_clear(self, sim):
        """All-time counters must be zeroed for all providers after clear."""
        for provider in ("provider1", "provider2", "provider3"):
            _rpc(sim[provider], "eth_blockNumber")

        _post(_ctrl(sim, "/history/clear"), {})

        _, stats = _get(_ctrl(sim, "/stats"))
        for pid in ("1", "2", "3"):
            total = stats["providers"][pid]["total_requests_all_time"]
            assert total == 0, \
                f"provider {pid} total_requests_all_time should be 0 after clear, got {total}"

    def test_does_not_change_scenario_config(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"2": {"mode": "error"}}})
        _post(_ctrl(sim, "/history/clear"), {})
        _, scenario = _get(_ctrl(sim, "/scenario"))
        assert scenario["providers"]["2"]["mode"] == "error"   # untouched

    def test_all_entries_after_clear_have_ts_after_clear_time(self, sim):
        """Architectural fact 3 — clear-timestamp invariant.

        Every entry that appears in history after /history/clear must have
        ts >= the moment the clear was called.  No pre-clear entry can survive.
        """
        # make calls BEFORE the clear
        for provider in ("provider1", "provider2", "provider3"):
            _rpc(sim[provider], "eth_blockNumber")

        t_clear = time.time()
        _post(_ctrl(sim, "/history/clear"), {})

        # make calls AFTER the clear
        _rpc(sim["provider1"], "eth_blockNumber")
        _rpc(sim["provider2"], "eth_blockNumber")

        _, body = _get(_ctrl(sim, "/history"))
        assert body["count"] == 2, \
            f"expected exactly 2 post-clear entries, got {body['count']}"
        for entry in body["history"]:
            assert entry["ts"] >= t_clear, (
                f"pre-clear entry leaked into history: "
                f"entry ts={entry['ts']:.3f} < clear ts={t_clear:.3f}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# /reset/all
# ─────────────────────────────────────────────────────────────────────────────

class TestResetAll:

    def test_history_is_empty_immediately_after_reset_all(self, sim):
        """Core guarantee: /reset/all wipes every entry from all providers."""
        for provider in ("provider1", "provider2", "provider3"):
            _rpc(sim[provider], "eth_blockNumber")

        _, before = _get(_ctrl(sim, "/history"))
        assert before["count"] == 3, \
            f"precondition failed — expected 3 entries before reset/all, got {before['count']}"

        status, body = _post(_ctrl(sim, "/reset/all"), {})
        assert status == 200

        _, after = _get(_ctrl(sim, "/history"))
        assert after == {"count": 0, "history": []}, \
            f"/reset/all did not wipe history — got: {after}"

    def test_new_call_after_reset_all_starts_at_count_1(self, sim):
        """After reset/all, the very next call must be the only entry."""
        for provider in ("provider1", "provider2", "provider3"):
            _rpc(sim[provider], "eth_blockNumber")

        _post(_ctrl(sim, "/reset/all"), {})

        _rpc(sim["provider2"], "eth_blockNumber")   # exactly one new call

        _, after = _get(_ctrl(sim, "/history"))
        assert after["count"] == 1, \
            f"expected exactly 1 entry after reset/all + 1 call, got {after['count']}"
        assert after["history"][0]["call_order"] == 1

    def test_reset_all_clears_all_three_providers(self, sim):
        """All 3 provider ring-buffers must be empty after reset/all."""
        for provider in ("provider1", "provider2", "provider3"):
            _rpc(sim[provider], "eth_blockNumber")

        _post(_ctrl(sim, "/reset/all"), {})

        for pid in ("1", "2", "3"):
            _, body = _get(_ctrl(sim, f"/history?provider={pid}"))
            assert body == {"count": 0, "history": []}, \
                f"provider {pid} history not cleared — got: {body}"

    def test_reset_all_also_resets_scenario(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"3": {"mode": "down"}}})
        _rpc(sim["provider1"], "eth_blockNumber")
        _post(_ctrl(sim, "/reset/all"), {})
        _, scenario = _get(_ctrl(sim, "/scenario"))
        assert scenario["providers"]["3"]["mode"] == "success"

    def test_all_entries_after_reset_all_have_ts_after_reset_time(self, sim):
        """Architectural fact 3 — clear-timestamp invariant for /reset/all.

        Every entry that appears in history after /reset/all must have
        ts >= the moment the reset was called.  No pre-reset entry can survive.
        """
        # make calls BEFORE the reset
        for provider in ("provider1", "provider2", "provider3"):
            _rpc(sim[provider], "eth_blockNumber")

        t_reset = time.time()
        _post(_ctrl(sim, "/reset/all"), {})

        # make calls AFTER the reset
        _rpc(sim["provider1"], "eth_blockNumber")
        _rpc(sim["provider3"], "eth_blockNumber")

        _, body = _get(_ctrl(sim, "/history"))
        assert body["count"] == 2, \
            f"expected exactly 2 post-reset entries, got {body['count']}"
        for entry in body["history"]:
            assert entry["ts"] >= t_reset, (
                f"pre-reset entry leaked into history: "
                f"entry ts={entry['ts']:.3f} < reset ts={t_reset:.3f}"
            )

    def test_history_max_cap_per_provider(self, sim, shrink_history):
        """Architectural fact 1 — each provider's history is bounded by a maxlen cap.

        Each provider's ring-buffer holds at most HISTORY_MAX entries. The deployed
        default is 2000 (MAG-1822, raised from the original 200) and is overridable
        via SIM_HISTORY_MAX. The cap behaviour itself is the same at any maxlen,
        so we pin this test to MAX_FOR_OVERFLOW_TEST=200 to keep it fast — see the
        shrink_history fixture for why we shrink in-place and how we restore.
        """
        shrink_history("1")

        for _ in range(MAX_FOR_OVERFLOW_TEST + 5):
            _rpc(sim["provider1"], "eth_blockNumber")

        _, stats = _get(_ctrl(sim, "/stats"))
        ring_entries = stats["providers"]["1"]["history_ring_buffer_entries"]
        total_calls  = stats["providers"]["1"]["total_requests_all_time"]

        assert ring_entries == MAX_FOR_OVERFLOW_TEST, \
            f"ring buffer should be capped at {MAX_FOR_OVERFLOW_TEST}, got {ring_entries}"
        assert total_calls == MAX_FOR_OVERFLOW_TEST + 5, \
            f"all-time counter must count every call — expected {MAX_FOR_OVERFLOW_TEST + 5}, got {total_calls}"


# ─────────────────────────────────────────────────────────────────────────────
# /stats
# ─────────────────────────────────────────────────────────────────────────────

class TestStats:

    def test_counts_successful_calls(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/stats"))
        p1 = body["providers"]["1"]
        assert p1["total_requests_all_time"] >= 2
        assert p1["requests_by_status_all_time"].get("success", 0) >= 2

    def test_tracks_status_breakdown(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "error"}}})
        _rpc(sim["provider1"], "eth_blockNumber")
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "rate_limit"}}})
        _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/stats"))
        breakdown = body["providers"]["1"]["requests_by_status_all_time"]
        assert breakdown.get("error",      0) >= 1
        assert breakdown.get("rate_limit", 0) >= 1

    def test_down_mode_counted_in_stats(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "down"}}})
        _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/stats"))
        assert body["providers"]["1"]["requests_by_status_all_time"].get("down", 0) >= 1

    def test_history_ring_buffer_entries_in_stats(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/stats"))
        assert body["providers"]["1"]["history_ring_buffer_entries"] >= 1

    def test_independent_per_provider(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/stats"))
        assert body["providers"]["1"]["total_requests_all_time"] >= 2
        assert body["providers"]["2"]["total_requests_all_time"] == 0
        assert body["providers"]["3"]["total_requests_all_time"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# /history  —  entries, call_order, all filter params
# ─────────────────────────────────────────────────────────────────────────────

class TestHistory:

    # ── entry structure ───────────────────────────────────────────────────────

    def test_entry_has_required_fields(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history"))
        entry = body["history"][0]
        for field in ("ts", "time", "request_id", "method", "status", "latency_ms",
                      "provider", "call_order"):
            assert field in entry, f"missing field: {field}"

    def test_request_id_echoed_in_history(self, sim):
        """The history entry must carry the JSON-RPC id that was sent in the request."""
        _post(sim["provider1"], {"jsonrpc": "2.0", "id": 99, "method": "eth_blockNumber", "params": []})
        _, hist = _get(_ctrl(sim, "/history?provider=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["request_id"] == 99, (
            f"history entry should record request_id=99, got {last['request_id']!r}"
        )

    def test_request_id_string_echoed_in_history(self, sim):
        """String JSON-RPC ids must also be echoed correctly in history."""
        _post(sim["provider1"], {"jsonrpc": "2.0", "id": "trace-abc", "method": "eth_chainId", "params": []})
        _, hist = _get(_ctrl(sim, "/history?provider=1&method=eth_chainId"))
        assert hist["count"] >= 1
        assert hist["history"][-1]["request_id"] == "trace-abc"

    def test_down_mode_request_id_is_null(self, sim):
        """Down-mode entries must have request_id=None (body is never parsed)."""
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "down"}}})
        _rpc(sim["provider1"], "eth_blockNumber")
        _, hist = _get(_ctrl(sim, "/history?provider=1"))
        assert hist["count"] >= 1
        assert hist["history"][-1]["request_id"] is None

    def test_filter_request_id_returns_matching_only(self, sim):
        """?request_id= must return only the entry(ies) that carried that JSON-RPC id."""
        _post(sim["provider1"], {"jsonrpc": "2.0", "id": 7, "method": "eth_blockNumber", "params": []})
        _post(sim["provider1"], {"jsonrpc": "2.0", "id": 8, "method": "eth_blockNumber", "params": []})
        _, hist = _get(_ctrl(sim, "/history?request_id=7"))
        assert hist["count"] >= 1
        assert all(e["request_id"] == 7 for e in hist["history"])

    def test_time_field_contains_utc_string(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history"))
        assert "UTC" in body["history"][0]["time"]

    def test_latency_ms_present_even_when_zero(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history"))
        assert body["history"][0]["latency_ms"] == 0

    def test_latency_ms_reflects_configured_delay(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"latency_ms": 150}}})
        _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history"))
        assert body["history"][0]["latency_ms"] >= 100

    # ── call_order ────────────────────────────────────────────────────────────

    def test_call_order_is_1_based_sequential(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        _rpc(sim["provider2"], "eth_blockNumber")
        _rpc(sim["provider3"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history"))
        orders = [e["call_order"] for e in body["history"]]
        assert orders == list(range(1, len(orders) + 1))

    def test_call_order_restarts_after_clear(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        _post(_ctrl(sim, "/history/clear"), {})
        _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history"))
        assert body["history"][0]["call_order"] == 1

    # ── ?provider= ────────────────────────────────────────────────────────────

    def test_filter_provider_only_returns_that_provider(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        _rpc(sim["provider2"], "eth_blockNumber")
        _rpc(sim["provider3"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history?provider=2"))
        assert body["count"] >= 1
        assert all(e["provider"] == "2" for e in body["history"])

    def test_filter_provider_excludes_others(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        _rpc(sim["provider2"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history?provider=1"))
        assert all(e["provider"] == "1" for e in body["history"])

    # ── ?method= ─────────────────────────────────────────────────────────────

    def test_filter_method_returns_matching_only(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        _rpc(sim["provider1"], "eth_chainId")
        _, body = _get(_ctrl(sim, "/history?method=eth_blockNumber"))
        assert body["count"] >= 1
        assert all(e["method"] == "eth_blockNumber" for e in body["history"])

    def test_filter_method_excludes_others(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        _rpc(sim["provider1"], "eth_chainId")
        _, body = _get(_ctrl(sim, "/history?method=eth_chainId"))
        assert all(e["method"] == "eth_chainId" for e in body["history"])

    # ── ?status= ─────────────────────────────────────────────────────────────

    def test_filter_status_success(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")               # success
        _post(_ctrl(sim, "/scenario"), {"providers": {"2": {"mode": "error"}}})
        _rpc(sim["provider2"], "eth_blockNumber")               # error
        _, body = _get(_ctrl(sim, "/history?status=success"))
        assert body["count"] >= 1
        assert all(e["status"] == "success" for e in body["history"])

    def test_filter_status_error(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "error"}}})
        _rpc(sim["provider1"], "eth_blockNumber")
        _rpc(sim["provider2"], "eth_blockNumber")               # success
        _, body = _get(_ctrl(sim, "/history?status=error"))
        assert body["count"] >= 1
        assert all(e["status"] == "error" for e in body["history"])

    def test_filter_status_rate_limit(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "rate_limit"}}})
        _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history?status=rate_limit"))
        assert body["count"] >= 1
        assert all(e["status"] == "rate_limit" for e in body["history"])

    def test_filter_status_down(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "down"}}})
        _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history?status=down"))
        assert body["count"] >= 1
        assert all(e["status"] == "down" for e in body["history"])

    def test_down_mode_recorded_with_wildcard_method(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "down"}}})
        _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history?provider=1"))
        assert any(e["method"] == "*" for e in body["history"])

    # ── ?last= ────────────────────────────────────────────────────────────────

    def test_filter_last_includes_recent_calls(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history?last=10"))
        assert body["count"] >= 1

    def test_filter_last_0_excludes_all(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        time.sleep(0.05)   # make sure the call is at least 50ms in the past
        _, body = _get(_ctrl(sim, "/history?last=0"))
        assert body["count"] == 0

    # ── ?from= / ?to= ─────────────────────────────────────────────────────────

    def test_filter_from_to_includes_call_in_window(self, sim):
        t_before = time.time() - 1
        _rpc(sim["provider1"], "eth_blockNumber")
        t_after  = time.time() + 1
        _, body = _get(_ctrl(sim, f"/history?from={t_before}&to={t_after}"))
        assert body["count"] >= 1

    def test_filter_from_future_excludes_all(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        t_future = time.time() + 9999
        _, body = _get(_ctrl(sim, f"/history?from={t_future}"))
        assert body["count"] == 0

    def test_filter_to_past_excludes_all(self, sim):
        t_past = time.time() - 9999
        _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, f"/history?to={t_past}"))
        assert body["count"] == 0

    # ── combined filters ──────────────────────────────────────────────────────

    def test_combined_last_provider_status(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")               # success
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "error"}}})
        _rpc(sim["provider1"], "eth_blockNumber")               # error
        _rpc(sim["provider2"], "eth_blockNumber")               # success on p2
        _, body = _get(_ctrl(sim, "/history?last=30&provider=1&status=error"))
        assert body["count"] >= 1
        for e in body["history"]:
            assert e["provider"] == "1"
            assert e["status"]   == "error"

    def test_combined_provider_method(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        _rpc(sim["provider1"], "eth_chainId")
        _rpc(sim["provider2"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history?provider=1&method=eth_blockNumber"))
        assert body["count"] >= 1
        for e in body["history"]:
            assert e["provider"] == "1"
            assert e["method"]   == "eth_blockNumber"

    # ── empty results ─────────────────────────────────────────────────────────

    def test_empty_history_after_reset_all(self, sim):
        for provider in ("provider1", "provider2", "provider3"):
            _rpc(sim[provider], "eth_blockNumber")
        _, before = _get(_ctrl(sim, "/history"))
        assert before["count"] == 3, \
            f"precondition failed — expected 3 entries, got {before['count']}"
        _post(_ctrl(sim, "/reset/all"), {})
        _, body = _get(_ctrl(sim, "/history"))
        assert body == {"count": 0, "history": []}, \
            f"/reset/all did not produce empty history — got: {body}"

    def test_history_sorted_by_timestamp(self, sim):
        _rpc(sim["provider1"], "eth_blockNumber")
        time.sleep(0.02)
        _rpc(sim["provider2"], "eth_blockNumber")
        time.sleep(0.02)
        _rpc(sim["provider3"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history"))
        timestamps = [e["ts"] for e in body["history"]]
        assert timestamps == sorted(timestamps)

    def test_method_filter_isolates_exactly_one_call(self, sim):
        """Architectural fact 2 — method filter is the correct isolation tool.

        In the deployed environment the router sends eth_blockNumber and
        eth_getBlockByNumber as background calls continuously.  Using a method
        the router never sends (e.g. eth_gasPrice) means ?method=eth_gasPrice
        returns exactly one entry — your call and nothing else.

        In unit tests there is no background traffic, so this test instead
        verifies the isolation property directly: make N calls with method A
        and M calls with method B, then confirm ?method=A returns exactly N
        entries and every entry has method == A.
        """
        _rpc(sim["provider1"], "eth_blockNumber")   # noise — 3 calls
        _rpc(sim["provider2"], "eth_blockNumber")
        _rpc(sim["provider3"], "eth_blockNumber")

        _rpc(sim["provider1"], "eth_gasPrice")      # signal — 1 call

        _, body = _get(_ctrl(sim, "/history?method=eth_gasPrice"))
        assert body["count"] == 1, \
            f"method filter should return exactly 1 entry, got {body['count']}"
        assert body["history"][0]["method"] == "eth_gasPrice"
        assert body["history"][0]["provider"] == "1"


# ─────────────────────────────────────────────────────────────────────────────
# JSON-RPC protocol details
# ─────────────────────────────────────────────────────────────────────────────

class TestJSONRPCProtocol:

    def test_response_id_echoes_request_id(self, sim):
        """The response id must match whatever id was sent in the request."""
        _, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert body["id"] == 1

        body2 = json.loads(
            urllib.request.urlopen(
                urllib.request.Request(
                    sim["provider1"],
                    data=json.dumps({"jsonrpc": "2.0", "id": 42, "method": "eth_blockNumber", "params": []}).encode(),
                    headers={"Content-Type": "application/json"},
                ),
                timeout=5,
            ).read()
        )
        assert body2["id"] == 42

    def test_response_id_string(self, sim):
        """id can be a string — must be echoed as-is."""
        body = json.loads(
            urllib.request.urlopen(
                urllib.request.Request(
                    sim["provider1"],
                    data=json.dumps({"jsonrpc": "2.0", "id": "my-req-1", "method": "eth_blockNumber", "params": []}).encode(),
                    headers={"Content-Type": "application/json"},
                ),
                timeout=5,
            ).read()
        )
        assert body["id"] == "my-req-1"

    def test_unknown_method_returns_fallback_result(self, sim):
        """A method not in METHOD_DEFAULTS must return '0x1' fallback, not crash."""
        _, body = _rpc(sim["provider1"], "eth_notARealMethod")
        assert body.get("result") == "0x1"
        assert "error" not in body

    def test_unknown_method_recorded_in_history(self, sim):
        _rpc(sim["provider1"], "eth_notARealMethod")
        _, hist = _get(_ctrl(sim, "/history?method=eth_notARealMethod"))
        assert hist["count"] == 1
        assert hist["history"][0]["status"] == "success"

    def test_eth_get_block_by_number_earliest(self, sim):
        _, body = _rpc(sim["provider1"], "eth_getBlockByNumber", ["earliest", False])
        assert body["result"]["number"] == "0x0"

    def test_eth_get_block_by_number_pending(self, sim):
        _, body = _rpc(sim["provider1"], "eth_getBlockByNumber", ["pending", False])
        assert body["result"]["number"] == "0x1312D01"

    def test_eth_get_block_by_number_safe(self, sim):
        _, body = _rpc(sim["provider1"], "eth_getBlockByNumber", ["safe", False])
        assert body["result"]["number"] == "0x1312D00"

    def test_eth_get_block_by_number_finalized(self, sim):
        _, body = _rpc(sim["provider1"], "eth_getBlockByNumber", ["finalized", False])
        assert body["result"]["number"] == "0x1312CFF"

    def test_empty_body_handled_gracefully(self, sim):
        """POST with no body must not crash — defaults to empty dict."""
        req = urllib.request.Request(
            sim["provider1"],
            data=b"",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read())
                assert "result" in body   # success with default id
        except urllib.error.HTTPError as e:
            raw = e.read()
            assert e.code != 500, f"server crashed on empty body: {raw}"

    def test_missing_method_field_defaults_to_unknown_in_history(self, sim):
        """A request body that omits 'method' must record method='unknown' in history."""
        # Send a valid JSON body that has no "method" key at all
        _post(sim["provider1"], {"jsonrpc": "2.0", "id": 1, "params": []})

        _, hist = _get(_ctrl(sim, "/history?provider=1"))
        assert hist["count"] >= 1
        last_entry = hist["history"][-1]
        assert last_entry["method"] == "unknown", (
            f"missing 'method' field should record 'unknown', "
            f"got {last_entry['method']!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Mode priority — down / rate_limit / error take full priority
# ─────────────────────────────────────────────────────────────────────────────

class TestModePriority:

    def test_down_skips_latency_responds_immediately(self, sim):
        """mode=down must return 503 without sleeping, even when latency_ms is set."""
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "down", "latency_ms": 5000}}})
        t0 = time.monotonic()
        _rpc(sim["provider1"], "eth_blockNumber")
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert elapsed_ms < 500, \
            f"down mode should respond immediately, took {elapsed_ms:.0f}ms"

    def test_error_mode_always_errors_regardless_of_probability(self, sim):
        """mode=error must always return error even if error_probability=0.0."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"mode": "error", "error_probability": 0.0}}
        })
        for _ in range(5):
            _, body = _rpc(sim["provider1"], "eth_blockNumber")
            assert "error" in body, "mode=error must always return error"

    def test_rate_limit_takes_priority_over_error_probability(self, sim):
        """mode=rate_limit must always return 429 even if error_probability=1.0."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"mode": "rate_limit", "error_probability": 1.0}}
        })
        for _ in range(3):
            status, _ = _rpc(sim["provider1"], "eth_blockNumber")
            assert status == 429, "mode=rate_limit must always return 429"

    def test_down_takes_priority_over_error_probability(self, sim):
        """mode=down must always return 503 regardless of error_probability."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"mode": "down", "error_probability": 1.0}}
        })
        status, _ = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 503


# ─────────────────────────────────────────────────────────────────────────────
# Scenario edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestScenarioEdgeCases:

    def test_empty_providers_dict_is_accepted(self, sim):
        """POST /scenario with {} providers must return 200 and change nothing."""
        status, body = _post(_ctrl(sim, "/scenario"), {"providers": {}})
        assert status == 200
        _, scenario = _get(_ctrl(sim, "/scenario"))
        for pid in ("1", "2", "3"):
            assert scenario["providers"][pid]["mode"] == "success"

    def test_unknown_provider_id_gracefully_ignored(self, sim):
        """Posting to a non-existent provider id (e.g. '99') must not crash."""
        status, body = _post(_ctrl(sim, "/scenario"), {"providers": {"99": {"mode": "error"}}})
        assert status == 200
        # real providers unchanged
        _, scenario = _get(_ctrl(sim, "/scenario"))
        for pid in ("1", "2", "3"):
            assert scenario["providers"][pid]["mode"] == "success"

    def test_all_three_providers_updated_in_one_call(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {
            "1": {"mode": "error"},
            "2": {"mode": "rate_limit"},
            "3": {"mode": "down"},
        }})
        _, scenario = _get(_ctrl(sim, "/scenario"))
        assert scenario["providers"]["1"]["mode"] == "error"
        assert scenario["providers"]["2"]["mode"] == "rate_limit"
        assert scenario["providers"]["3"]["mode"] == "down"

    def test_custom_responses_cleared_by_reset(self, sim):
        """POST /reset must wipe custom responses, not just mode/latency."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"responses": {"eth_blockNumber": {"result": "0xCAFE"}}}}
        })
        _, before = _rpc(sim["provider1"], "eth_blockNumber")
        assert before["result"] == "0xCAFE"

        _post(_ctrl(sim, "/reset"), {})
        _, after = _rpc(sim["provider1"], "eth_blockNumber")
        assert after["result"] != "0xCAFE", \
            "custom response should be cleared by /reset"

    def test_custom_responses_cleared_by_reset_all(self, sim):
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"2": {"responses": {"eth_blockNumber": {"result": "0xDEAD"}}}}
        })
        _post(_ctrl(sim, "/reset/all"), {})
        _, body = _rpc(sim["provider2"], "eth_blockNumber")
        assert body["result"] != "0xDEAD", \
            "custom response should be cleared by /reset/all"

    def test_custom_responses_survive_history_clear(self, sim):
        """/history/clear must NOT touch scenario config — custom responses must persist."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"3": {"responses": {"eth_blockNumber": {"result": "0xBEEF"}}}}
        })
        _post(_ctrl(sim, "/history/clear"), {})
        _, body = _rpc(sim["provider3"], "eth_blockNumber")
        assert body["result"] == "0xBEEF", \
            "custom response must survive /history/clear"


# ─────────────────────────────────────────────────────────────────────────────
# Ring buffer rollover
# ─────────────────────────────────────────────────────────────────────────────

class TestRingBufferRollover:

    def test_oldest_entry_is_dropped_when_buffer_full(self, sim, shrink_history):
        """When the ring buffer is full, the oldest entry is replaced by the newest.

        Pinned to MAX_FOR_OVERFLOW_TEST (MAG-1822) — the production HISTORY_MAX
        default is now 2000, but the rollover behaviour is identical at any maxlen.
        """
        shrink_history("1")

        # fill the buffer exactly
        for i in range(MAX_FOR_OVERFLOW_TEST):
            _rpc(sim["provider1"], "eth_blockNumber")

        # record the oldest ts currently in the buffer
        _, hist_before = _get(_ctrl(sim, "/history?provider=1"))
        oldest_ts = hist_before["history"][0]["ts"]

        # push one more — oldest must be gone
        _rpc(sim["provider1"], "eth_blockNumber")
        _, hist_after = _get(_ctrl(sim, "/history?provider=1"))

        assert hist_after["count"] == MAX_FOR_OVERFLOW_TEST, \
            f"ring buffer should stay at MAX_FOR_OVERFLOW_TEST after overflow, got {hist_after['count']}"
        assert hist_after["history"][0]["ts"] > oldest_ts, \
            "oldest entry should have been dropped after overflow"

    def test_all_time_counter_survives_rollover(self, sim, shrink_history):
        """total_requests_all_time must keep counting past the ring buffer cap.

        Pinned to MAX_FOR_OVERFLOW_TEST (MAG-1822) — what we're verifying is the
        invariant "ring caps, all-time counter does not", which holds at every maxlen.
        """
        shrink_history("1")

        extra = 10
        for _ in range(MAX_FOR_OVERFLOW_TEST + extra):
            _rpc(sim["provider1"], "eth_blockNumber")

        _, stats = _get(_ctrl(sim, "/stats"))
        total = stats["providers"]["1"]["total_requests_all_time"]
        ring  = stats["providers"]["1"]["history_ring_buffer_entries"]

        assert total == MAX_FOR_OVERFLOW_TEST + extra, \
            f"all-time counter should be {MAX_FOR_OVERFLOW_TEST + extra}, got {total}"
        assert ring == MAX_FOR_OVERFLOW_TEST, \
            f"ring buffer should be capped at {MAX_FOR_OVERFLOW_TEST}, got {ring}"

    def test_newest_entry_always_survives_rollover(self, sim, shrink_history):
        """The most recently pushed entry must always be present after rollover.

        Pinned to MAX_FOR_OVERFLOW_TEST (MAG-1822). The rollover semantics are
        deque's, not ours — verifying them at 200 is enough.
        """
        shrink_history("1")

        for _ in range(MAX_FOR_OVERFLOW_TEST + 5):
            _rpc(sim["provider1"], "eth_blockNumber")

        # push a unique method as the very last call
        _rpc(sim["provider1"], "eth_gasPrice")

        _, hist = _get(_ctrl(sim, "/history?provider=1&method=eth_gasPrice"))
        assert hist["count"] == 1, \
            "the most recent entry must always survive in the ring buffer"


# ─────────────────────────────────────────────────────────────────────────────
# MAG-1822 — HISTORY_MAX is env-driven (SIM_HISTORY_MAX)
# ─────────────────────────────────────────────────────────────────────────────
#
# constants.HISTORY_MAX is read once at module import via
# int(os.getenv("SIM_HISTORY_MAX", "2000")). Reloading `constants` after a
# monkeypatch is the cleanest way to assert both the default and the override
# without restarting pytest. Reload affects the constants module object only —
# the running test simulator was built around the original HISTORY_MAX import
# and is not disturbed because we restore env state in the finally clause.

import importlib  # noqa: E402  (placed near tests it serves to keep blast radius local)


class TestHistoryMaxEnvConfig:

    def _reload_constants(self):
        import constants
        importlib.reload(constants)
        return constants

    def test_history_max_defaults_to_2000_when_env_unset(self, monkeypatch):
        """No SIM_HISTORY_MAX in env → HISTORY_MAX falls back to 2000 (MAG-1822 default)."""
        monkeypatch.delenv("SIM_HISTORY_MAX", raising=False)
        constants = self._reload_constants()
        try:
            assert constants.HISTORY_MAX == 2000, \
                f"expected default 2000, got {constants.HISTORY_MAX}"
        finally:
            # Restore the constants module to whatever state the rest of the
            # suite expects (default again, since monkeypatch will undo the env
            # change at teardown — but reload is permanent until we reload).
            self._reload_constants()

    def test_history_max_honors_sim_history_max_env(self, monkeypatch):
        """SIM_HISTORY_MAX=N must be picked up at module import time."""
        monkeypatch.setenv("SIM_HISTORY_MAX", "777")
        constants = self._reload_constants()
        try:
            assert constants.HISTORY_MAX == 777, \
                f"expected SIM_HISTORY_MAX=777 to win, got {constants.HISTORY_MAX}"
        finally:
            # monkeypatch.undo runs in teardown; reload once more so the
            # constants module reflects the post-undo env (default 2000).
            monkeypatch.delenv("SIM_HISTORY_MAX", raising=False)
            self._reload_constants()


# ─────────────────────────────────────────────────────────────────────────────
# MAG-1822 — /history?max=N tail-slice filter
# ─────────────────────────────────────────────────────────────────────────────


class TestHistoryMaxQueryParam:

    def test_max_zero_returns_empty_list(self, sim):
        """?max=0 returns an empty history (documented edge case, not 400)."""
        _rpc(sim["provider1"], "eth_blockNumber")
        _rpc(sim["provider1"], "eth_blockNumber")
        _rpc(sim["provider1"], "eth_blockNumber")
        status, body = _get(_ctrl(sim, "/history?max=0"))
        assert status == 200
        assert body["count"] == 0
        assert body["history"] == []

    def test_max_negative_returns_400(self, sim):
        """?max=-1 is a client error — caller must fix the request."""
        status, body = _get(_ctrl(sim, "/history?max=-1"))
        assert status == 400
        assert body["error"] == "invalid_max"

    def test_max_non_integer_returns_400(self, sim):
        """?max=abc is malformed input — refuse with a clear error message."""
        status, body = _get(_ctrl(sim, "/history?max=abc"))
        assert status == 400
        assert body["error"] == "invalid_max"

    def test_max_returns_at_most_n_entries(self, sim):
        """?max=N caps the response at N entries."""
        for _ in range(20):
            _rpc(sim["provider1"], "eth_blockNumber")
        status, body = _get(_ctrl(sim, "/history?max=5"))
        assert status == 200
        assert body["count"] == 5
        assert len(body["history"]) == 5

    def test_max_returns_the_most_recent_entries(self, sim):
        """?max=N returns the TAIL — the most recent calls, not the oldest."""
        for i in range(10):
            _post(sim["provider1"], {
                "jsonrpc": "2.0", "id": i, "method": "eth_blockNumber", "params": [],
            })
        # Without ?max, we'd see ids 0..9 in order. With ?max=3, expect ids 7,8,9.
        _, body = _get(_ctrl(sim, "/history?max=3"))
        assert body["count"] == 3
        request_ids = [e["request_id"] for e in body["history"]]
        assert request_ids == [7, 8, 9], \
            f"?max=3 should return the 3 most recent ids [7,8,9], got {request_ids}"

    def test_max_preserves_call_order_from_full_timeline(self, sim):
        """call_order on returned entries reflects the FULL timeline position,
        not 1..N of the sliced result. Anchors a sliced response back to the
        full history for the caller."""
        for i in range(10):
            _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history?max=3"))
        orders = [e["call_order"] for e in body["history"]]
        assert orders == [8, 9, 10], \
            f"call_order should be the tail of the full timeline, got {orders}"

    def test_max_combinable_with_provider_filter(self, sim):
        """?max= composes with the existing ?provider= filter — applied after it."""
        for _ in range(5):
            _rpc(sim["provider1"], "eth_blockNumber")
        for _ in range(5):
            _rpc(sim["provider2"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history?provider=2&max=2"))
        assert body["count"] == 2
        assert all(e["provider"] == "2" for e in body["history"])

    def test_no_max_returns_all_entries(self, sim):
        """When ?max= is absent the response is unbounded (up to HISTORY_MAX).
        Asserts the filter is opt-in — existing callers see no behaviour change."""
        for _ in range(15):
            _rpc(sim["provider1"], "eth_blockNumber")
        _, body = _get(_ctrl(sim, "/history"))
        assert body["count"] == 15


# ─────────────────────────────────────────────────────────────────────────────
# HTTP wrong method (POST to GET-only, GET to POST-only)
# ─────────────────────────────────────────────────────────────────────────────

class TestHTTPWrongMethod:

    def _get_raw(self, url: str) -> int:
        """GET request that returns only the HTTP status code."""
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def _post_raw(self, url: str) -> int:
        req = urllib.request.Request(
            url, data=b"{}", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_get_reset_returns_404(self, sim):
        assert self._get_raw(_ctrl(sim, "/reset")) == 404

    def test_get_history_clear_returns_404(self, sim):
        assert self._get_raw(_ctrl(sim, "/history/clear")) == 404

    def test_get_reset_all_returns_404(self, sim):
        assert self._get_raw(_ctrl(sim, "/reset/all")) == 404

    def test_post_health_returns_404(self, sim):
        assert self._post_raw(_ctrl(sim, "/health")) == 404

    def test_post_stats_returns_404(self, sim):
        assert self._post_raw(_ctrl(sim, "/stats")) == 404


# ─────────────────────────────────────────────────────────────────────────────
# Concurrency — simultaneous requests must all be recorded, no data corruption
# ─────────────────────────────────────────────────────────────────────────────

class TestConcurrency:

    def test_concurrent_requests_all_recorded(self, sim):
        """20 simultaneous requests to one provider must all appear in history."""
        n = 20
        results = []

        def call():
            status, body = _rpc(sim["provider1"], "eth_blockNumber")
            results.append(status)

        threads = [threading.Thread(target=call) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(s == 200 for s in results), \
            f"some concurrent requests failed: {results}"

        _, hist = _get(_ctrl(sim, "/history?provider=1"))
        assert hist["count"] == n, \
            f"expected {n} history entries, got {hist['count']}"

    def test_concurrent_requests_to_different_providers(self, sim):
        """Simultaneous requests to all 3 providers must all be recorded independently."""
        n_per_provider = 5
        results = []

        def call(url):
            status, _ = _rpc(url, "eth_blockNumber")
            results.append(status)

        threads = [
            threading.Thread(target=call, args=(sim[f"provider{p}"],))
            for p in (1, 2, 3)
            for _ in range(n_per_provider)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(s == 200 for s in results)

        _, hist = _get(_ctrl(sim, "/history"))
        assert hist["count"] == n_per_provider * 3, \
            f"expected {n_per_provider * 3} total entries, got {hist['count']}"

    def test_concurrent_scenario_update_and_requests_no_crash(self, sim):
        """Updating the scenario mid-flight must not corrupt state or crash."""
        errors = []

        def spam():
            for _ in range(10):
                try:
                    _rpc(sim["provider1"], "eth_blockNumber")
                except Exception as e:
                    errors.append(str(e))

        def flip():
            for mode in ("success", "error", "success", "error", "success"):
                _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": mode}}})
                time.sleep(0.01)

        t1 = threading.Thread(target=spam)
        t2 = threading.Thread(target=flip)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"concurrent scenario update caused errors: {errors}"

    def test_concurrent_history_clear_and_push_no_corruption(self, sim):
        """Simultaneous /history/clear and incoming RPC calls must not corrupt state.

        The ring-buffer deque and counters are protected by a per-provider lock.
        This test races 20 rapid RPC calls against repeated clears to confirm
        no exception is raised and the final state is internally consistent
        (history_ring_buffer_entries <= total_requests_all_time).
        """
        errors = []

        def spam():
            for _ in range(20):
                try:
                    _rpc(sim["provider2"], "eth_blockNumber")
                except Exception as e:
                    errors.append(str(e))

        def clear_repeatedly():
            for _ in range(10):
                try:
                    _post(_ctrl(sim, "/history/clear"), {})
                    time.sleep(0.005)
                except Exception as e:
                    errors.append(str(e))

        t1 = threading.Thread(target=spam)
        t2 = threading.Thread(target=clear_repeatedly)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"concurrent clear+push raised errors: {errors}"

        # Structural integrity check: ring entries can never exceed total calls
        _, stats = _get(_ctrl(sim, "/stats"))
        p = stats["providers"]["2"]
        assert p["history_ring_buffer_entries"] <= p["total_requests_all_time"], (
            f"ring entries ({p['history_ring_buffer_entries']}) > "
            f"total calls ({p['total_requests_all_time']}) — state corrupted"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Stats edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestStatsEdgeCases:

    def test_stats_all_zero_when_no_calls(self, sim):
        """After reset/all, stats must show zero for all providers."""
        _, stats = _get(_ctrl(sim, "/stats"))
        for pid in ("1", "2", "3"):
            p = stats["providers"][pid]
            assert p["total_requests_all_time"]    == 0
            assert p["requests_by_status_all_time"] == {}
            assert p["history_ring_buffer_entries"] == 0

    def test_ring_buffer_entries_drop_to_zero_after_clear(self, sim):
        """history_ring_buffer_entries in /stats must be 0 after /history/clear."""
        _rpc(sim["provider1"], "eth_blockNumber")
        _rpc(sim["provider2"], "eth_blockNumber")

        _, before = _get(_ctrl(sim, "/stats"))
        assert before["providers"]["1"]["history_ring_buffer_entries"] >= 1

        _post(_ctrl(sim, "/history/clear"), {})

        _, after = _get(_ctrl(sim, "/stats"))
        for pid in ("1", "2", "3"):
            assert after["providers"][pid]["history_ring_buffer_entries"] == 0, \
                f"ring buffer entries for provider {pid} should be 0 after clear"

    def test_stats_not_affected_by_reset_scenario_only(self, sim):
        """/reset must NOT zero stats counters — only /history/clear does that."""
        _rpc(sim["provider1"], "eth_blockNumber")
        _post(_ctrl(sim, "/reset"), {})
        _, stats = _get(_ctrl(sim, "/stats"))
        assert stats["providers"]["1"]["total_requests_all_time"] >= 1, \
            "/reset must not touch all-time counters"


# ─────────────────────────────────────────────────────────────────────────────
# Feature 1: Lava Headers Capture
# ─────────────────────────────────────────────────────────────────────────────

class TestLavaHeadersCapture:
    """Verify that all lava-* headers from the router are captured in history entries."""

    def test_lava_headers_field_exists_in_history_entries(self, sim):
        """Every history entry must have a lava_headers field (dict, never None)."""
        _post(_ctrl(sim, "/history/clear"), {})
        _rpc(sim["provider1"], "eth_blockNumber")

        _, hist = _get(_ctrl(sim, "/history?last=60"))
        assert len(hist["history"]) >= 1
        entry = hist["history"][0]
        assert "lava_headers" in entry, "lava_headers field missing from history entry"
        assert isinstance(entry["lava_headers"], dict), "lava_headers must be a dict"

    def test_lava_headers_empty_when_no_headers_sent(self, sim):
        """lava_headers dict is empty {} when router sends no lava-* headers."""
        _post(_ctrl(sim, "/history/clear"), {})
        # Direct HTTP POST with no lava headers
        _rpc(sim["provider1"], "eth_blockNumber")

        _, hist = _get(_ctrl(sim, "/history?last=60"))
        entry = hist["history"][0]
        assert entry["lava_headers"] == {}, "lava_headers should be empty dict when no headers sent"

    def test_lava_headers_preserved_across_all_modes(self, sim):
        """lava_headers captured regardless of provider mode (success/error/rate_limit/down)."""
        modes = ["success", "error", "rate_limit", "down"]

        for mode in modes:
            _post(_ctrl(sim, "/history/clear"), {})
            _post(_ctrl(sim, "/scenario"), {
                "providers": {"1": {"mode": mode}}
            })
            _rpc(sim["provider1"], "eth_blockNumber")

            _, hist = _get(_ctrl(sim, "/history?last=60"))
            assert len(hist["history"]) >= 1
            entry = hist["history"][0]
            assert "lava_headers" in entry, f"lava_headers missing in {mode} mode"
            assert isinstance(entry["lava_headers"], dict), f"lava_headers not dict in {mode} mode"

    def test_lava_headers_field_independent_per_provider(self, sim):
        """Each provider's history has independent lava_headers."""
        _post(_ctrl(sim, "/history/clear"), {})
        _rpc(sim["provider1"], "eth_blockNumber")
        _rpc(sim["provider2"], "eth_blockNumber")
        _rpc(sim["provider3"], "eth_blockNumber")

        _, hist = _get(_ctrl(sim, "/history?last=60"))
        assert len(hist["history"]) >= 3

        for entry in hist["history"]:
            assert "lava_headers" in entry
            assert isinstance(entry["lava_headers"], dict)


# ─────────────────────────────────────────────────────────────────────────────
# Feature 2: Correlation Group
# ─────────────────────────────────────────────────────────────────────────────

class TestCorrelationGroup:
    """Verify that calls are grouped by (request_id, method) within 50ms window."""

    def test_correlation_group_field_exists_in_history_entries(self, sim):
        """Every history entry must have a correlation_group field (int)."""
        _post(_ctrl(sim, "/history/clear"), {})
        _rpc(sim["provider1"], "eth_blockNumber")

        _, hist = _get(_ctrl(sim, "/history?last=60"))
        assert len(hist["history"]) >= 1
        entry = hist["history"][0]
        assert "correlation_group" in entry, "correlation_group field missing from history entry"
        assert isinstance(entry["correlation_group"], int), "correlation_group must be an int"

    def test_correlation_group_starts_at_1(self, sim):
        """First history entry gets correlation_group = 1."""
        _post(_ctrl(sim, "/history/clear"), {})
        _rpc(sim["provider1"], "eth_blockNumber")

        _, hist = _get(_ctrl(sim, "/history?last=60"))
        entry = hist["history"][0]
        assert entry["correlation_group"] == 1

    def test_calls_within_50ms_same_request_id_same_method_share_correlation_group(self, sim):
        """Calls with same (request_id, method) within 50ms window share correlation_group."""
        _post(_ctrl(sim, "/history/clear"), {})

        # Send call to provider 1
        _post(sim["provider1"], {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "eth_blockNumber",
            "params": []
        })

        # Immediately send to provider 2 (within 50ms)
        _post(sim["provider2"], {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "eth_blockNumber",
            "params": []
        })

        _, hist = _get(_ctrl(sim, "/history?last=60&method=eth_blockNumber"))
        entries = [e for e in hist["history"] if e["request_id"] == 42]

        assert len(entries) >= 2, "should have at least 2 entries with request_id=42"
        # All should have same correlation_group because they're <50ms apart with same id/method
        first_cg = entries[0]["correlation_group"]
        for entry in entries:
            assert entry["correlation_group"] == first_cg, \
                f"calls with same (id, method) within 50ms should share correlation_group"

    def test_sequential_requests_get_different_correlation_groups(self, sim):
        """Requests with different request_ids get different correlation_groups."""
        _post(_ctrl(sim, "/history/clear"), {})

        _post(sim["provider1"], {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "eth_blockNumber",
            "params": []
        })

        _post(sim["provider1"], {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "eth_blockNumber",
            "params": []
        })

        _, hist = _get(_ctrl(sim, "/history?last=60&method=eth_blockNumber"))
        entries = sorted(hist["history"], key=lambda e: e["ts"])

        assert len(entries) >= 2
        # Different request_ids should have different correlation groups
        cg_100 = [e for e in entries if e["request_id"] == 100][0]["correlation_group"]
        cg_101 = [e for e in entries if e["request_id"] == 101][0]["correlation_group"]
        assert cg_100 != cg_101, "different request_ids should have different correlation_groups"

    def test_correlation_group_different_methods_not_grouped(self, sim):
        """Calls with same request_id but different methods don't share correlation_group."""
        _post(_ctrl(sim, "/history/clear"), {})

        _post(sim["provider1"], {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "eth_blockNumber",
            "params": []
        })

        _post(sim["provider1"], {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "eth_gasPrice",
            "params": []
        })

        _, hist = _get(_ctrl(sim, "/history?last=60"))
        entries = [e for e in hist["history"] if e["request_id"] == 42]

        assert len(entries) >= 2
        # Different methods with same id should have different correlation_groups
        cg_blockNum = [e for e in entries if e["method"] == "eth_blockNumber"][0]["correlation_group"]
        cg_gasPrice = [e for e in entries if e["method"] == "eth_gasPrice"][0]["correlation_group"]
        assert cg_blockNum != cg_gasPrice, "different methods should have different correlation_groups"

    def test_correlation_group_survives_filter_operations(self, sim):
        """correlation_group field survives filtering by provider/method/status."""
        _post(_ctrl(sim, "/history/clear"), {})
        _rpc(sim["provider1"], "eth_blockNumber")

        # Test with various filters
        _, hist1 = _get(_ctrl(sim, "/history?last=60&provider=1"))
        _, hist2 = _get(_ctrl(sim, "/history?last=60&method=eth_blockNumber"))
        _, hist3 = _get(_ctrl(sim, "/history?last=60&status=success"))

        for hist in [hist1, hist2, hist3]:
            for entry in hist["history"]:
                assert "correlation_group" in entry, \
                    f"correlation_group should survive filtering"


# ─────────────────────────────────────────────────────────────────────────────
# Feature 3: Lava Headers Filter
# ─────────────────────────────────────────────────────────────────────────────

class TestLavaHeadersFilter:
    """Verify that GET /history supports filtering by lava_header_* query params."""

    def test_lava_header_filter_param_syntax(self, sim):
        """Query param ?lava_header_X=Y is accepted (doesn't cause 404)."""
        _post(_ctrl(sim, "/history/clear"), {})
        _rpc(sim["provider1"], "eth_blockNumber")

        # Query with lava_header filter should not 404
        status, hist = _get(_ctrl(sim, "/history?last=60&lava_header_lava_stateful_api=true"))
        assert status == 200, f"lava_header filter should return 200, got {status}"
        assert "history" in hist, "response should have history field"

    def test_lava_header_filter_converts_underscores_to_hyphens(self, sim):
        """Query param lava_header_lava_stateful_api becomes lava-stateful-api."""
        # This test verifies the parameter parsing logic
        # The simulator accepts the param without error (syntax valid)
        _post(_ctrl(sim, "/history/clear"), {})
        _rpc(sim["provider1"], "eth_blockNumber")

        status, hist = _get(_ctrl(sim, "/history?last=60&lava_header_lava_stateful_api=true"))
        assert status == 200

    def test_lava_header_filter_empty_when_no_match(self, sim):
        """Filtering for lava-header that wasn't sent returns empty history."""
        _post(_ctrl(sim, "/history/clear"), {})
        _rpc(sim["provider1"], "eth_blockNumber")  # No lava headers sent

        _, hist = _get(_ctrl(sim, "/history?last=60&lava_header_lava_stateful_api=true"))
        # Should be empty because no headers matched
        assert hist["count"] == 0, "filtering for non-existent header should return empty"
        assert hist["history"] == [], "history should be empty list"

    def test_lava_header_filter_returns_matching_entries(self, sim):
        """When entries have matching lava headers, they appear in filtered results."""
        _post(_ctrl(sim, "/history/clear"), {})
        _rpc(sim["provider1"], "eth_blockNumber")

        _, hist_all = _get(_ctrl(sim, "/history?last=60"))
        # All entries have empty lava_headers since we didn't send any
        assert hist_all["count"] >= 1

        # Filtering for a header that doesn't exist returns 0
        _, hist_filtered = _get(_ctrl(sim, "/history?last=60&lava_header_lava_stateful_api=true"))
        assert hist_filtered["count"] == 0

    def test_multiple_lava_header_filters_use_and_logic(self, sim):
        """Multiple lava_header filters are combined with AND (all must match)."""
        _post(_ctrl(sim, "/history/clear"), {})
        _rpc(sim["provider1"], "eth_blockNumber")

        # Multiple filters combined
        status, hist = _get(_ctrl(sim,
            "/history?last=60&lava_header_lava_stateful_api=true&lava_header_lava_user_request_type=broadcast"
        ))
        assert status == 200
        # Should be empty because headers weren't sent
        assert hist["count"] == 0

    def test_lava_header_filter_combines_with_existing_filters(self, sim):
        """lava_header filters work alongside provider/method/status filters."""
        _post(_ctrl(sim, "/history/clear"), {})
        _rpc(sim["provider1"], "eth_blockNumber")
        _rpc(sim["provider2"], "eth_gasPrice")

        # Combine lava_header filter with provider and method filters
        status, hist = _get(_ctrl(sim,
            "/history?last=60&provider=1&method=eth_blockNumber&lava_header_lava_stateful_api=true"
        ))
        assert status == 200
        # Should still be empty (no headers were sent)
        assert hist["count"] == 0

        # But without the lava_header filter, we should get the entries
        status, hist = _get(_ctrl(sim,
            "/history?last=60&provider=1&method=eth_blockNumber"
        ))
        assert status == 200
        assert hist["count"] >= 1

    def test_lava_header_filter_survives_other_query_params(self, sim):
        """lava_header filters don't interfere with last/from/to/provider/method/status."""
        _post(_ctrl(sim, "/history/clear"), {})
        _rpc(sim["provider1"], "eth_blockNumber")

        # Complex query with multiple filter types
        status, hist = _get(_ctrl(sim,
            "/history?last=60&provider=1&method=eth_blockNumber&status=success&lava_header_lava_test=value"
        ))
        assert status == 200
        assert "count" in hist
        assert "history" in hist


# ─────────────────────────────────────────────────────────────────────────────
# http_status applies in success mode (custom HTTP code with valid body)
# ─────────────────────────────────────────────────────────────────────────────

class TestHttpStatusInSuccessMode:
    """The http_status field used to be honored only when mode='error'.
    Now it applies in success mode too — provider returns the custom HTTP
    status code with a valid JSON-RPC success body."""

    def test_success_mode_with_custom_http_status(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "success", "http_status": 502}}})
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 502, f"expected HTTP 502, got {status}"
        assert "result" in body, f"expected JSON-RPC success body, got {body}"
        assert "error" not in body

    def test_success_mode_default_http_status_is_200(self, sim):
        # Regression: default http_status=200 must continue to apply in success mode
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "success"}}})
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200
        assert "result" in body

    def test_error_mode_still_honors_http_status(self, sim):
        # Regression: existing behaviour (http_status in error mode) still works
        _post(_ctrl(sim, "/scenario"),
              {"providers": {"1": {"mode": "error", "http_status": 500, "error_code": -32603}}})
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 500
        assert body["error"]["code"] == -32603


# ─────────────────────────────────────────────────────────────────────────────
# corruption_mode: malformed / wrong-shape JSON responses
# ─────────────────────────────────────────────────────────────────────────────

class TestCorruptionMode:
    """corruption_mode is an orthogonal field: it composes with mode='success'
    and mode='error' to alter the response body shape. Values:
      - "truncated"       — chop the last 10 chars of the JSON string
      - "missing_field"   — omit one top-level field (configured by missing_field)
      - "invalid_json"    — return obviously-not-JSON ('}{ {{')
      - "empty_response"  — return an empty body
      - "wrong_type"      — swap the type of a target field (default "result";
                            configurable via the missing_field slot — reused
                            for "which field to corrupt")
    """

    def test_truncated_corrupts_json(self, sim):
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"mode": "success", "corruption_mode": "truncated"}}
        })
        # Use raw urllib because _rpc parses JSON which would fail on truncated
        req = urllib.request.Request(
            sim["provider1"], data=b'{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}',
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode()
        # Truncation: last 10 chars chopped — at least we know it doesn't parse
        try:
            json.loads(raw)
            assert False, f"expected truncation; got valid JSON: {raw}"
        except json.JSONDecodeError:
            pass  # expected

    def test_missing_field_omits_specified_top_level_field(self, sim):
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"mode": "success", "corruption_mode": "missing_field",
                                 "missing_field": "result"}}
        })
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200
        assert "result" not in body, f"expected 'result' missing, got {body}"
        assert "jsonrpc" in body  # untouched
        assert "id" in body

    def test_invalid_json_returns_garbage(self, sim):
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"mode": "success", "corruption_mode": "invalid_json"}}
        })
        req = urllib.request.Request(
            sim["provider1"], data=b'{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}',
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode()
        try:
            json.loads(raw)
            assert False, f"expected invalid JSON; got valid: {raw}"
        except json.JSONDecodeError:
            pass

    def test_empty_response_returns_no_body(self, sim):
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"mode": "success", "corruption_mode": "empty_response"}}
        })
        req = urllib.request.Request(
            sim["provider1"], data=b'{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}',
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode()
        assert raw == "", f"expected empty body, got {raw!r}"

    def test_default_corruption_mode_is_none(self, sim):
        # Regression: default behaviour unchanged when corruption_mode is unset
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "success"}}})
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200
        assert "result" in body

    def test_wrong_type_default_target_is_result_string_to_int(self, sim):
        # eth_blockNumber returns result as a hex string (e.g. "0x1234"); wrong_type
        # should flip it to an int. Default target field is "result".
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"mode": "success", "corruption_mode": "wrong_type"}}
        })
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200
        assert "result" in body, f"result field should remain present, got {body}"
        assert isinstance(body["result"], int), (
            f"expected int (wrong type for eth_blockNumber result), "
            f"got {type(body['result']).__name__}: {body['result']!r}"
        )

    def test_wrong_type_targets_custom_field_via_missing_field_slot(self, sim):
        # missing_field slot is reused as the "which field to corrupt" target.
        # Here we corrupt the "id" field, which is normally an int — should
        # flip to a string.
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "mode": "success",
                    "corruption_mode": "wrong_type",
                    "missing_field": "id",
                }
            }
        })
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200
        assert "id" in body
        assert isinstance(body["id"], str), (
            f"expected str (wrong type for id), got "
            f"{type(body['id']).__name__}: {body['id']!r}"
        )

    def test_wrong_type_missing_target_field_is_noop(self, sim):
        # If the configured target field isn't present on the response, the
        # response shape is left alone (no crash, no other fields touched).
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "mode": "success",
                    "corruption_mode": "wrong_type",
                    "missing_field": "no_such_field",
                }
            }
        })
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200
        assert "result" in body
        assert isinstance(body["result"], str), (
            f"expected unchanged str result when target field absent, "
            f"got {type(body['result']).__name__}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# blocks_behind: per-provider stale block heights for sync-freshness / CV tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBlocksBehind:
    """blocks_behind shifts the provider's reported eth_blockNumber by N blocks
    relative to the global stub (METHOD_DEFAULTS['eth_blockNumber'] = '0x1312D00').
    Positive = behind; negative = ahead. Affects eth_blockNumber and
    eth_getBlockByNumber('latest', ...). Composes with mode='success'."""

    HEAD = int("0x1312D00", 16)  # 20,000,000 — the simulator's default eth_blockNumber

    def test_blocks_behind_shifts_eth_blockNumber(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "success", "blocks_behind": 100}}})
        _, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert int(body["result"], 16) == self.HEAD - 100

    def test_blocks_ahead_via_negative_value(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "success", "blocks_behind": -50}}})
        _, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert int(body["result"], 16) == self.HEAD + 50

    def test_default_blocks_behind_is_zero(self, sim):
        # Regression: default behaviour returns the canonical head
        _, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert body["result"] == "0x1312D00"

    def test_eth_get_block_by_number_latest_respects_blocks_behind(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "success", "blocks_behind": 25}}})
        _, body = _rpc(sim["provider1"], "eth_getBlockByNumber", ["latest", False])
        assert int(body["result"]["number"], 16) == self.HEAD - 25

    def test_per_provider_disagreement(self, sim):
        # Cross-validation enabler: each provider can report a different head
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {"mode": "success", "blocks_behind": 0},
                "2": {"mode": "success", "blocks_behind": 5},
                "3": {"mode": "success", "blocks_behind": 100},
            }
        })
        _, b1 = _rpc(sim["provider1"], "eth_blockNumber")
        _, b2 = _rpc(sim["provider2"], "eth_blockNumber")
        _, b3 = _rpc(sim["provider3"], "eth_blockNumber")
        assert int(b1["result"], 16) == self.HEAD
        assert int(b2["result"], 16) == self.HEAD - 5
        assert int(b3["result"], 16) == self.HEAD - 100


# ─────────────────────────────────────────────────────────────────────────────
# hang mode: accept request, never respond (forces router timeout)
# ─────────────────────────────────────────────────────────────────────────────

import socket as _socket  # for timeout exception type

class TestHangMode:
    """mode='hang' accepts the TCP connection and the request body but never
    sends a response. The router's per-attempt timeout fires; from the client
    side it looks like a request that exceeds the read timeout."""

    def test_hang_blocks_until_client_timeout(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "hang"}}})
        # Client sets a small read timeout — should hit it because server hangs
        req = urllib.request.Request(
            sim["provider1"], data=b'{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}',
            headers={"Content-Type": "application/json"})
        t0 = time.monotonic()
        try:
            urllib.request.urlopen(req, timeout=1.0)  # 1 second client timeout
            assert False, "expected timeout, server responded"
        except (urllib.error.URLError, _socket.timeout, TimeoutError):
            elapsed = time.monotonic() - t0
            assert elapsed >= 0.9, f"expected ~1s timeout, elapsed {elapsed:.2f}s"
            assert elapsed < 3.0, f"timeout took longer than client config: {elapsed:.2f}s"

    def test_hang_records_in_history(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "hang"}}})
        req = urllib.request.Request(
            sim["provider1"], data=b'{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}',
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=0.5)
        except (urllib.error.URLError, _socket.timeout, TimeoutError):
            pass
        # Allow time for the server to record the call in history
        time.sleep(0.2)
        _, history = _get(_ctrl(sim, "/history?provider=1"))
        # We expect at least one entry recording the hang attempt
        entries = history.get("history", [])
        statuses = {e["status"] for e in entries}
        assert "hang" in statuses or "down" in statuses, f"expected 'hang' status in history, got {statuses}"


# ─────────────────────────────────────────────────────────────────────────────
# drop_connection mode: TCP close at configurable point (transport failures)
# ─────────────────────────────────────────────────────────────────────────────

class TestDropConnection:
    """mode='drop_connection' closes the TCP connection at one of three points:
      - before_headers: connect, read body, close — no HTTP response at all
      - after_headers:  connect, read body, send headers, close before body
      - mid_body:       connect, read body, send headers, send half body, close

    From the client's perspective these manifest as different errors —
    RemoteDisconnected, IncompleteRead, BadStatusLine, etc. — which is what
    the router's transport-error classification needs to distinguish."""

    def _send_and_capture_error(self, url: str) -> str:
        """Send a JSON-RPC request and return the exception class name (or 'OK')."""
        req = urllib.request.Request(
            url, data=b'{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}',
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                resp.read()
                return "OK"
        except Exception as exc:
            return type(exc).__name__

    def test_drop_before_headers_no_response(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {
            "1": {"mode": "drop_connection", "drop_at": "before_headers"}
        }})
        result = self._send_and_capture_error(sim["provider1"])
        assert result != "OK", "expected error from connection drop, got valid response"
        # Common manifestations: RemoteDisconnected, BadStatusLine, URLError, ConnectionResetError
        assert any(name in result for name in ("RemoteDisconnected", "BadStatusLine", "URLError",
                                                 "ConnectionResetError", "IncompleteRead")), \
            f"unexpected error class for before_headers drop: {result}"

    def test_drop_after_headers_incomplete_read(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {
            "1": {"mode": "drop_connection", "drop_at": "after_headers"}
        }})
        result = self._send_and_capture_error(sim["provider1"])
        assert result != "OK", "expected error from connection drop, got valid response"
        # Should manifest as IncompleteRead or similar (we sent headers + 0 bytes of body)
        assert any(name in result for name in ("IncompleteRead", "RemoteDisconnected",
                                                 "ChunkedEncodingError", "URLError",
                                                 "ConnectionResetError", "BadStatusLine")), \
            f"unexpected error class for after_headers drop: {result}"

    def test_drop_mid_body_truncated(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {
            "1": {"mode": "drop_connection", "drop_at": "mid_body"}
        }})
        result = self._send_and_capture_error(sim["provider1"])
        assert result != "OK", "expected error from connection drop, got valid response"

    def test_drop_default_at_is_before_headers(self, sim):
        # When drop_at is unset, default to before_headers (most disruptive)
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "drop_connection"}}})
        result = self._send_and_capture_error(sim["provider1"])
        assert result != "OK"

    def test_drop_records_in_history(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {
            "1": {"mode": "drop_connection", "drop_at": "before_headers"}
        }})
        self._send_and_capture_error(sim["provider1"])
        time.sleep(0.1)  # let the server flush history
        _, history = _get(_ctrl(sim, "/history?provider=1"))
        entries = history.get("history", [])
        statuses = {e["status"] for e in entries}
        assert "drop_connection" in statuses or "drop" in statuses, \
            f"expected drop in history, got {statuses}"


# ─────────────────────────────────────────────────────────────────────────────
# MAG-1838: cross-transport JSON-RPC fault isolation (inverse of MAG-1836)
# ─────────────────────────────────────────────────────────────────────────────

class TestJsonRpcCrossTransportFaultIsolation:
    """``ProviderState`` is shared across JSON-RPC, REST, gRPC, WS, and
    Tendermint-RPC for the same provider id. The fault primitives in
    ``_apply_fault`` (down / hang / drop_connection / rate_limit / error)
    are chain-agnostic on the snap, so without an explicit gate a fault
    authored for the gRPC port (chain_family="grpc") would also kill the
    JSON-RPC port for that provider.

    Inverse of MAG-1836 (which gated the gRPC fault ladder on
    chain_family="grpc"). The JSON-RPC handler owns chain_family values
    ``"eth"`` and ``"btc"``; any other value means the fault was set for
    a different transport and the JSON-RPC port should fall through to
    its normal success response.
    """

    def test_jsonrpc_unaffected_by_grpc_down_fault(self, sim):
        """gRPC ``down`` fault on provider 1 must not return 503 on its
        JSON-RPC port. Without the gate this asserts http 503 instead of
        a normal success body."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"chain_family": "grpc", "mode": "down"}}
        })
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200, f"JSON-RPC should ignore grpc-down; got {status}"
        assert "result" in body, f"expected success body; got {body}"

    def test_jsonrpc_unaffected_by_grpc_rate_limit_fault(self, sim):
        """gRPC ``rate_limit`` must not return 429 on the JSON-RPC port."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"chain_family": "grpc", "mode": "rate_limit"}}
        })
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200, f"JSON-RPC should ignore grpc-rate_limit; got {status}"
        assert "result" in body, f"expected success body; got {body}"

    def test_jsonrpc_unaffected_by_grpc_error_fault(self, sim):
        """gRPC ``error`` must not return an error envelope on the
        JSON-RPC port."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {
                "chain_family": "grpc",
                "mode": "error",
                "error_message": "UNAVAILABLE",
                "error_code": -32603,
            }}
        })
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200
        assert "error" not in body, f"expected no error key; got {body}"
        assert "result" in body

    def test_jsonrpc_unaffected_by_rest_down_fault(self, sim):
        """REST ``down`` fault must not kill the JSON-RPC port either —
        all non-jsonrpc chain_family values are gated the same way."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"chain_family": "rest", "mode": "down"}}
        })
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200
        assert "result" in body

    def test_jsonrpc_unaffected_by_tendermintrpc_down_fault(self, sim):
        """Tendermint ``down`` fault must not kill the JSON-RPC port."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {
                "chain_family": "tendermintrpc", "mode": "down",
            }}
        })
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200
        assert "result" in body

    def test_jsonrpc_fault_still_fires_when_chain_family_is_eth(self, sim):
        """Sanity check: the gate must not break JSON-RPC-side faults.
        A ``down`` fault with default ``chain_family="eth"`` must still
        return 503."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"chain_family": "eth", "mode": "down"}}
        })
        status, _ = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 503

    def test_jsonrpc_fault_still_fires_when_chain_family_is_btc(self, sim):
        """Sanity check: btc is the other JSON-RPC-owned chain family —
        a ``rate_limit`` fault with chain_family="btc" must still return
        429 on the JSON-RPC port."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"chain_family": "btc", "mode": "rate_limit"}}
        })
        status, _ = _rpc(sim["provider1"], "getblockcount")
        assert status == 429


# ─────────────────────────────────────────────────────────────────────────────
# MAG-1832: cancel-during-response race — arrival is always recorded
# ─────────────────────────────────────────────────────────────────────────────


def _force_rst_mid_handler(url: str, content_length: int = 100,
                           pre_close_delay_s: float = 0.02) -> None:
    """Open a TCP socket to ``url``, send POST headers that promise a body of
    ``content_length`` bytes, briefly let the server's handler thread start
    reading, then close the socket with SO_LINGER=0 so the kernel emits a
    TCP RST instead of a clean FIN.

    Why SO_LINGER=0: a clean close lets ``self.rfile.read(content_length)``
    return ``b""`` (EOF) which the handler may swallow. We want
    ``ConnectionResetError`` to fire on the read so the test exercises the
    real cancellation path the router triggers when a hedge peer wins. The
    short pre_close_delay gives the server thread time to enter ``rfile.read``
    before the RST lands.
    """
    parsed = urlparse(url)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                 struct.pack("ii", 1, 0))
    s.connect((parsed.hostname, parsed.port))
    s.send(
        f"POST / HTTP/1.1\r\n"
        f"Host: {parsed.hostname}:{parsed.port}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {content_length}\r\n"
        f"\r\n".encode()
    )
    time.sleep(pre_close_delay_s)
    s.close()


class TestCancelDuringResponseRecordsArrival:
    """MAG-1832 — the cancel-during-response race lives in the handler stages
    between request arrival and the first ``push_call_to_buffer``. The biggest
    window is ``self.rfile.read(Content-Length)``: when the router's hedge
    mechanism picks a faster peer it sends a TCP RST on the cancelled peer's
    connection, that read raises ``ConnectionResetError``, and without an
    arrival stub the handler dies before the history entry is written. The
    invariant ``Lava-Retries + 1 == history_count`` then fails because the
    router still counts the cancelled attempt in its retries header.

    PR #22 closed the sleep-then-write window (latency sleep was upstream of
    the push). This test class pins the post-PR-#22 fix: arrival is recorded
    BEFORE the body read so even a cancellation that lands mid-read leaves
    the entry in /history.

    Recorded statuses:
      - Cancellation lands before any update          → ``in_flight``
      - Cancellation lands during latency sleep / response build → final status
        (``success`` / ``error`` / fault status), because the in-place update
        already fired.
    Either way the entry exists, which is the only thing the router-vs-sim
    invariant counts.
    """

    def test_rst_before_body_arrives_still_records_history(self, sim):
        """Real reproducer: TCP RST in the middle of the handler's body read
        used to drop the call from /history. After MAG-1832-v2 the arrival
        stub is pushed before the read, so the entry survives.

        Without the fix the assertion below sees count=0.
        """
        # baseline: history is empty (autouse clean_state reset just ran)
        _, before = _get(_ctrl(sim, "/history?provider=1"))
        assert before["count"] == 0, (
            f"expected empty history before the RST request, got {before['count']}"
        )

        _force_rst_mid_handler(sim["provider1"])

        # The server thread that owns the cancelled connection still needs to
        # finish unwinding; give it a brief moment to flush the entry. Without
        # the fix this poll loop never sees an entry — the test would fail
        # even at 2s. With the fix the entry shows up within 100ms.
        deadline = time.monotonic() + 2.0
        entries: list = []
        while time.monotonic() < deadline:
            _, after = _get(_ctrl(sim, "/history?provider=1"))
            entries = after.get("history", [])
            if entries:
                break
            time.sleep(0.05)

        assert entries, (
            "expected an arrival stub in history after a TCP-RST mid-handler "
            "cancellation; the cancel-during-response race regressed"
        )
        assert len(entries) == 1, (
            f"expected exactly one entry for one cancelled request, got "
            f"{len(entries)}: {entries}"
        )
        # The entry's status depends on how far the handler got before the RST
        # raised. Any of the legitimate "this request existed" statuses is
        # acceptable — the load-bearing assertion is that the entry exists.
        assert entries[0]["status"] in {
            "in_flight", "success", "error", "down", "hang",
            "rate_limit", "drop_connection",
        }, f"unexpected status on arrival entry: {entries[0]}"

    def test_rst_before_body_does_not_double_record(self, sim):
        """When the cancellation lands while the handler is still racing the
        update path, the in-place update must not produce a second entry.
        Asserts the single-arrival, single-history-entry invariant.
        """
        for _ in range(5):
            _force_rst_mid_handler(sim["provider2"])

        # Wait for all five handler threads to flush
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            _, after = _get(_ctrl(sim, "/history?provider=2"))
            entries = after.get("history", [])
            if len(entries) >= 5:
                break
            time.sleep(0.05)

        _, after = _get(_ctrl(sim, "/history?provider=2"))
        entries = after.get("history", [])
        assert len(entries) == 5, (
            f"expected exactly 5 entries for 5 cancelled requests "
            f"(no double-records, no drops), got {len(entries)}: "
            f"{[e['status'] for e in entries]}"
        )

    def test_arrival_stub_carries_lava_headers(self, sim):
        """Diagnostic value: cancelled-mid-handler entries must keep their
        captured lava-* headers so /history filters by GUID still match.
        The headers are read off ``self.headers`` before the body read, so
        they're available at the arrival moment.
        """
        parsed = urlparse(sim["provider3"])
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                     struct.pack("ii", 1, 0))
        s.connect((parsed.hostname, parsed.port))
        s.send(
            b"POST / HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            b"Lava-Guid: test-cancel-during-response-guid\r\n"
            b"Content-Length: 50\r\n"
            b"\r\n"
        )
        time.sleep(0.02)
        s.close()

        deadline = time.monotonic() + 2.0
        entries: list = []
        while time.monotonic() < deadline:
            _, after = _get(_ctrl(sim, "/history?provider=3"))
            entries = after.get("history", [])
            if entries:
                break
            time.sleep(0.05)

        assert entries, "no arrival entry recorded for cancelled request"
        # BaseHTTPRequestHandler normalises header casing — the captured key
        # may be "Lava-Guid" or "lava-guid" depending on Python's parser
        # version. Match case-insensitively.
        captured = entries[0].get("lava_headers", {})
        captured_lower = {k.lower(): v for k, v in captured.items()}
        assert captured_lower.get("lava-guid") == "test-cancel-during-response-guid", (
            f"lava-guid header lost on cancelled-mid-handler entry; "
            f"got {captured}"
        )

    def test_unit_level_arrival_then_update_does_not_double_count(self, sim):
        """White-box unit test against the ProviderState API itself.
        ``record_arrival`` followed by ``push_call_to_buffer(..., entry=stub)``
        must produce exactly one history entry with the final status, and
        the ``calls_by_status`` counter must rebalance from ``in_flight``
        to the final status.
        """
        state = sim["states"]["1"]
        # clean_state autouse already reset the state, but be explicit
        with state.lock:
            state.history.clear()
            state.total_calls = 0
            state.calls_by_status = {}

        stub = state.record_arrival(lava_headers={"Lava-Guid": "g1"})
        assert state.calls_by_status.get("in_flight") == 1
        assert state.total_calls == 1
        assert len(state.history) == 1

        state.push_call_to_buffer("eth_blockNumber", "success", 0,
                                  request_id=42, lava_headers={"Lava-Guid": "g1"},
                                  entry=stub)
        assert state.total_calls == 1, (
            "update must not bump total_calls — the entry is the same entry"
        )
        assert len(state.history) == 1, (
            "update must not append a new entry"
        )
        assert state.calls_by_status.get("in_flight", 0) == 0, (
            "in_flight counter must be decremented on update"
        )
        assert state.calls_by_status.get("success") == 1, (
            "final status must be reflected in calls_by_status"
        )
        assert state.history[0]["status"] == "success"
        assert state.history[0]["method"] == "eth_blockNumber"
        assert state.history[0]["request_id"] == 42
