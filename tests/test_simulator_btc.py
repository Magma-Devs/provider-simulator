"""
Unit tests for the Bitcoin chain dispatch in the provider simulator (MAG-1716).

Mirrors the structure of ``tests/test_simulator.py`` (the ETH suite) but
covers only BTC-specific behaviour:

  Happy-path per BTC method        — every method in BTC_METHOD_DEFAULTS
                                      responds with a JSON-RPC result envelope.
  Fault primitives                  — set_hang / drop / stale / corrupt / status
                                      all apply identically on a BTC provider.
  Mixed-chain scenario              — one ETH + one BTC provider in the same
                                      /scenario body, each independently faulted.
  Block-hash format                  — bitcoind's 64-lower-hex, no "0x" prefix.
  Decimal-vs-hex height handling     — getblockcount returns int, not hex string.
  History tracking                   — BTC requests show up in /history exactly
                                      like ETH ones, with method name preserved.

Run with:
  pytest tests/test_simulator_btc.py -v
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer, ThreadingHTTPServer

import pytest

from server import ControlHandler, JSONRPCHandler, ProviderState
from stubs_btc import BTC_METHOD_DEFAULTS

# ── Test ports (distinct from ETH suite's 28545-28547 / 29000 and from the
#     prod ports 18545-18547 / 19000 so the two suites can co-exist if run in
#     parallel later). ─────────────────────────────────────────────────────────

_PROVIDER_PORTS = {"1": 38545, "2": 38546, "3": 38547}
_CONTROL_PORT   = 39000

# 29 BTC methods covered by the stub set. Source of truth: stubs_btc.py.
ALL_BTC_METHODS = sorted(BTC_METHOD_DEFAULTS.keys())


# ── HTTP helpers (kept independent of test_simulator.py to avoid cross-file
#     fixture coupling — duplication is intentional, the two files run on
#     different ports). ──────────────────────────────────────────────────────


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
    """Start 3 JSON-RPC servers + 1 control server on dedicated BTC test ports.

    Yields a dict with base URLs:
      sim["control"]   → http://127.0.0.1:39000
      sim["provider1"] → http://127.0.0.1:38545
      sim["provider2"] → http://127.0.0.1:38546
      sim["provider3"] → http://127.0.0.1:38547
    """
    states = {pid: ProviderState() for pid in _PROVIDER_PORTS}

    servers = []
    for pid, port in _PROVIDER_PORTS.items():
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

    time.sleep(0.15)

    yield {
        "control":   f"http://127.0.0.1:{_CONTROL_PORT}",
        "provider1": f"http://127.0.0.1:{_PROVIDER_PORTS['1']}",
        "provider2": f"http://127.0.0.1:{_PROVIDER_PORTS['2']}",
        "provider3": f"http://127.0.0.1:{_PROVIDER_PORTS['3']}",
    }

    for s in servers:
        s.shutdown()


# ── Helper to put a provider in BTC mode ──────────────────────────────────────

def _set_btc(sim, pid: str = "1", **extra):
    """Convenience: POST /scenario for a single provider with chain_family=btc.

    Any extra kwargs are folded into the per-provider config dict (latency_ms,
    mode, blocks_behind, responses, etc.) so callers can write one-liners
    instead of nesting dicts.
    """
    cfg = {"chain_family": "btc", **extra}
    return _post(_ctrl(sim, "/scenario"), {"providers": {pid: cfg}})


# ── Function-scoped autouse: clean slate before/after every test ──────────────

@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario config AND clear history before and after every test."""
    _post(_ctrl(sim, "/reset/all"), {})
    yield
    _post(_ctrl(sim, "/reset/all"), {})


# ─────────────────────────────────────────────────────────────────────────────
# Chain-family routing
# ─────────────────────────────────────────────────────────────────────────────

class TestChainFamilyDispatch:

    def test_default_chain_family_is_eth(self, sim):
        """Without setting chain_family, /scenario reports the default."""
        _, body = _get(_ctrl(sim, "/scenario"))
        for pid in ("1", "2", "3"):
            assert body["providers"][pid]["chain_family"] == "eth"

    def test_set_chain_family_btc(self, sim):
        _set_btc(sim, "1")
        _, body = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["1"]["chain_family"] == "btc"
        # other providers untouched
        assert body["providers"]["2"]["chain_family"] == "eth"
        assert body["providers"]["3"]["chain_family"] == "eth"

    def test_reset_clears_chain_family(self, sim):
        _set_btc(sim, "1")
        _post(_ctrl(sim, "/reset"), {})
        _, body = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["1"]["chain_family"] == "eth"

    def test_eth_request_on_btc_provider_returns_null_result(self, sim):
        """A method unknown to handlers_btc returns null result, not error.

        Mirrors the ETH handler's METHOD_DEFAULTS.get(method, "0x1") fallback —
        the simulator stays in success mode, the router sees an unfamiliar
        but well-formed response.
        """
        _set_btc(sim, "1")
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200
        assert "error" not in body
        assert body["result"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Happy-path stubs per BTC method
# ─────────────────────────────────────────────────────────────────────────────

class TestBTCMethodDefaults:

    @pytest.mark.parametrize("method", ALL_BTC_METHODS)
    def test_method_returns_success_envelope(self, sim, method):
        """Every method in BTC_METHOD_DEFAULTS responds with a JSON-RPC result envelope."""
        _set_btc(sim, "1")
        status, body = _rpc(sim["provider1"], method)
        assert status == 200, f"{method} returned HTTP {status}"
        assert body.get("jsonrpc") == "2.0", f"{method} missing jsonrpc=2.0"
        assert "error" not in body, f"{method} returned error: {body.get('error')!r}"
        assert "result" in body, f"{method} missing result"


class TestGetBlockCountReturnsDecimal:

    def test_getblockcount_is_int_not_hex(self, sim):
        """Critical Q3 invariant: bitcoind returns block heights as JSON numbers, not hex strings."""
        _set_btc(sim, "1")
        _, body = _rpc(sim["provider1"], "getblockcount")
        assert isinstance(body["result"], int), (
            f"getblockcount must return int, got {type(body['result']).__name__}: {body['result']!r}"
        )

    def test_getblockcount_default_is_realistic_mainnet_height(self, sim):
        """The default head is pinned to BTC_LATEST_BLOCK (850_000) so tests can assert."""
        _set_btc(sim, "1")
        _, body = _rpc(sim["provider1"], "getblockcount")
        assert body["result"] == 850_000

    def test_getblockcount_shifts_with_blocks_behind(self, sim):
        """blocks_behind=10 → head reported is 850_000 - 10."""
        _set_btc(sim, "1", blocks_behind=10)
        _, body = _rpc(sim["provider1"], "getblockcount")
        assert body["result"] == 850_000 - 10


class TestBlockHashFormat:

    HEX64 = re.compile(r"^[0-9a-f]{64}$")

    def test_getblockhash_returns_64_lowerhex_no_prefix(self, sim):
        """Q3 contract: 64 lowercase hex chars, no 0x prefix."""
        _set_btc(sim, "1")
        _, body = _rpc(sim["provider1"], "getblockhash", [850_000])
        h = body["result"]
        assert isinstance(h, str), f"hash must be str, got {type(h).__name__}"
        assert self.HEX64.match(h), f"hash {h!r} doesn't match 64 lower-hex pattern"
        assert not h.startswith("0x"), "BTC hashes do not carry the 0x prefix"

    def test_getblockhash_is_deterministic_per_height(self, sim):
        """Same height → same hash (lets tests pin against synthesised values)."""
        _set_btc(sim, "1")
        _, body1 = _rpc(sim["provider1"], "getblockhash", [12345])
        _, body2 = _rpc(sim["provider1"], "getblockhash", [12345])
        assert body1["result"] == body2["result"]

    def test_getblockhash_different_heights_distinct_hashes(self, sim):
        _set_btc(sim, "1")
        _, body1 = _rpc(sim["provider1"], "getblockhash", [100])
        _, body2 = _rpc(sim["provider1"], "getblockhash", [200])
        assert body1["result"] != body2["result"]

    def test_getbestblockhash_format(self, sim):
        _set_btc(sim, "1")
        _, body = _rpc(sim["provider1"], "getbestblockhash")
        h = body["result"]
        assert self.HEX64.match(h)


class TestGetBlockEcho:

    def test_getblock_echoes_requested_hash(self, sim):
        """Like eth_getBlockByNumber on the ETH side — the simulator echoes the
        request param so the router's pruning verification sees a matching
        block identifier in the response."""
        _set_btc(sim, "1")
        requested = "ab" * 32
        _, body = _rpc(sim["provider1"], "getblock", [requested])
        assert body["result"]["hash"] == requested

    def test_getblockheader_echoes_requested_hash(self, sim):
        _set_btc(sim, "1")
        requested = "cd" * 32
        _, body = _rpc(sim["provider1"], "getblockheader", [requested])
        assert body["result"]["hash"] == requested

    def test_getblockchaininfo_blocks_shifts_with_blocks_behind(self, sim):
        _set_btc(sim, "1", blocks_behind=5)
        _, body = _rpc(sim["provider1"], "getblockchaininfo")
        assert body["result"]["blocks"] == 850_000 - 5
        assert body["result"]["headers"] == 850_000 - 5


# ─────────────────────────────────────────────────────────────────────────────
# Fault-injection primitives applied on a BTC provider
# ─────────────────────────────────────────────────────────────────────────────

class TestBTCFaultInjection:

    def test_hang_mode_on_btc_provider(self, sim):
        """mode=hang behaves identically regardless of chain_family."""
        _set_btc(sim, "1", mode="hang")
        t0 = time.monotonic()
        try:
            _rpc(sim["provider1"], "getblockcount")
        except (urllib.error.URLError, ConnectionResetError, OSError):
            pass  # 30s sleep → client times out at 5s
        elapsed = time.monotonic() - t0
        # We don't wait 30s; just confirm the client timed out (≥4s, well above
        # the latency_ms=0 fast path) — actual cap is the urlopen timeout.
        assert elapsed >= 4, f"hang should block at least the client timeout, got {elapsed:.2f}s"

    def test_drop_connection_before_headers_on_btc(self, sim):
        _set_btc(sim, "1", mode="drop_connection", drop_at="before_headers")
        with pytest.raises((urllib.error.URLError, ConnectionResetError, OSError, http_err())):
            _rpc(sim["provider1"], "getblockcount")

    def test_stale_blocks_behind_on_btc(self, sim):
        """blocks_behind=100 → BTC head reports 100 below the canonical chain head."""
        _set_btc(sim, "1", blocks_behind=100)
        _, body = _rpc(sim["provider1"], "getblockcount")
        assert body["result"] == 850_000 - 100

    def test_corrupt_response_on_btc(self, sim):
        """corruption_mode=invalid_json on a BTC provider yields un-parseable bytes."""
        _set_btc(sim, "1", corruption_mode="invalid_json")
        # Build the request manually so we read raw bytes without json.loads.
        req = urllib.request.Request(
            f"{sim['provider1']}/",
            data=json.dumps({"jsonrpc": "2.0", "id": 1,
                              "method": "getblockcount", "params": []}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)

    def test_status_override_on_btc(self, sim):
        """mode=error + http_status=502 must propagate the custom HTTP status."""
        _set_btc(sim, "1", mode="error", http_status=502, error_code=-5,
                 error_message="Block not found")
        status, body = _rpc(sim["provider1"], "getblockhash", [999_999_999])
        assert status == 502
        assert body["error"]["code"] == -5


def http_err():
    """Lazy reference to urllib's HTTPError so the parametrize tuple stays compact."""
    return urllib.error.HTTPError


# ─────────────────────────────────────────────────────────────────────────────
# Per-method error overrides on a BTC provider
# ─────────────────────────────────────────────────────────────────────────────

class TestBTCErrorStubs:

    def test_btc_error_stub_named_lookup(self, sim):
        """responses[method] = {"error_stub": <name>} routes to BTC_ERROR_STUBS."""
        _set_btc(sim, "1", responses={"getblockhash": {"error_stub": "block_not_found"}})
        _, body = _rpc(sim["provider1"], "getblockhash", [999_999])
        assert body["error"]["code"] == -5
        assert "Block not found" in body["error"]["message"]

    def test_btc_error_stub_raw_envelope(self, sim):
        """Escape hatch: responses[method] = {"error": {...}} bypasses the catalogue."""
        _set_btc(sim, "1", responses={"sendrawtransaction": {
            "error": {"code": -99, "message": "Custom"}
        }})
        _, body = _rpc(sim["provider1"], "sendrawtransaction", ["deadbeef"])
        assert body["error"]["code"] == -99
        assert body["error"]["message"] == "Custom"

    def test_btc_method_unaffected_by_other_method_error(self, sim):
        """Per-method overrides scope strictly to that method."""
        _set_btc(sim, "1", responses={"getblockhash": {"error_stub": "block_not_found"}})
        # The unrelated method must still succeed.
        _, body = _rpc(sim["provider1"], "getblockcount")
        assert "result" in body
        assert "error" not in body


# ─────────────────────────────────────────────────────────────────────────────
# Mixed-chain scenario — one ETH + one BTC provider in the same /scenario body
# ─────────────────────────────────────────────────────────────────────────────

class TestMixedChainScenario:

    def test_eth_and_btc_providers_independent(self, sim):
        """One /scenario call sets provider1=eth (default) and provider2=btc.
        Each provider answers in its own chain's RPC convention."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {"chain_family": "eth"},
                "2": {"chain_family": "btc"},
            }
        })
        _, eth_body = _rpc(sim["provider1"], "eth_blockNumber")
        _, btc_body = _rpc(sim["provider2"], "getblockcount")

        # ETH side: hex string with "0x" prefix.
        assert isinstance(eth_body["result"], str)
        assert eth_body["result"].startswith("0x")
        # BTC side: decimal integer.
        assert isinstance(btc_body["result"], int)

    def test_eth_and_btc_independently_faulted(self, sim):
        """Each provider can run a different fault mode in the same scenario."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {"chain_family": "eth", "mode": "rate_limit"},
                "2": {"chain_family": "btc", "blocks_behind": 50},
            }
        })
        eth_status, eth_body = _rpc(sim["provider1"], "eth_blockNumber")
        btc_status, btc_body = _rpc(sim["provider2"], "getblockcount")

        assert eth_status == 429, "ETH provider was rate-limited"
        assert btc_status == 200, "BTC provider should answer normally"
        assert btc_body["result"] == 850_000 - 50


# ─────────────────────────────────────────────────────────────────────────────
# History tracking — BTC requests must show up in /history like ETH ones
# ─────────────────────────────────────────────────────────────────────────────

class TestBTCHistoryTracking:

    def test_btc_request_recorded_in_history(self, sim):
        _set_btc(sim, "1")
        _rpc(sim["provider1"], "getblockcount")
        _, hist = _get(_ctrl(sim, "/history?provider=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "getblockcount"
        assert last["status"] == "success"

    def test_btc_history_filter_by_method(self, sim):
        """?method= filters work for BTC method names just like ETH ones."""
        _set_btc(sim, "1")
        _rpc(sim["provider1"], "getblockcount")
        _rpc(sim["provider1"], "getbestblockhash")
        _, hist = _get(_ctrl(sim, "/history?method=getblockcount"))
        assert hist["count"] >= 1
        assert all(e["method"] == "getblockcount" for e in hist["history"])

    def test_btc_error_status_recorded(self, sim):
        """Per-method error_stub on a BTC method produces status=error in history."""
        _set_btc(sim, "1", responses={"getblockhash": {"error_stub": "block_not_found"}})
        _rpc(sim["provider1"], "getblockhash", [999_999])
        _, hist = _get(_ctrl(sim, "/history?provider=1&status=error"))
        assert hist["count"] >= 1
        assert hist["history"][-1]["method"] == "getblockhash"
