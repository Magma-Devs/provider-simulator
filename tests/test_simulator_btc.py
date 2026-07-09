"""
Unit tests for the Bitcoin chain dispatch in the provider simulator
(MAG-1716; revised under MAG-2089 to use dedicated BTC listener ports).

Mirrors the structure of ``tests/test_simulator.py`` (the ETH suite) but
covers only BTC-specific behaviour:

  Happy-path per BTC method        — every method in BTC_METHOD_DEFAULTS
                                      responds with a JSON-RPC result envelope.
  Fault primitives                  — set_hang / drop / stale / corrupt / status
                                      all apply identically on a BTC provider.
  Mixed-chain scenario              — one ETH listener + one BTC listener
                                      sharing the same pid, each independently
                                      faulted via port-derived dispatch.
  Block-hash format                  — bitcoind's 64-lower-hex, no "0x" prefix.
  Decimal-vs-hex height handling     — getblockcount returns int, not hex string.
  History tracking                   — BTC requests show up in /history exactly
                                      like ETH ones, with method name preserved.

Port layout
-----------
MAG-2089 moved BTC dispatch from a per-provider ``chain_family`` flag on the
shared ETH JSON-RPC listener pool (18545-18547) to a dedicated BTC listener
pool at 18575-18577. This suite mirrors the move: a dedicated BTC test port
range at 38575-38577 (parallel to prod 18575-18577) hosts JSONRPCHandler
listeners with ``handler_chain_family="btc"`` + ``handler_module=handlers_btc``.
A second ETH listener pool at 38545-38547 (parallel to prod 18545-18547)
hosts default-ETH listeners, used by the mixed-chain tests. Both pools share
ProviderState per pid so a single ``/scenario`` POST reconfigures both
listeners for the same logical provider — exactly mirroring prod.

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

import handlers_btc
import handlers_eth
from server import ControlHandler, JSONRPCHandler, ProviderState
from stubs_btc import BTC_METHOD_DEFAULTS

# ── Test ports (distinct from ETH suite's 28545-28547 / 29000 and from the
#     prod ports 18545-18547 / 19000 so the two suites can co-exist if run in
#     parallel later).
#
#     ETH ports (38545-7) host default ETH listeners — used by the mixed-chain
#     scenario to drive an ETH-only port for the same pid. BTC ports (38575-7)
#     host BTC-configured listeners and are the focus of this suite. ─────────

_ETH_PROVIDER_PORTS = {"1": 38545, "2": 38546, "3": 38547}
_BTC_PROVIDER_PORTS = {"1": 38575, "2": 38576, "3": 38577}
_CONTROL_PORT       = 39000

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
    """Start 3 ETH listeners + 3 BTC listeners + 1 control server.

    The BTC listeners (38575-38577) are the focus of this suite — they run
    JSONRPCHandler with ``handler_chain_family="btc"`` + ``handler_module=
    handlers_btc`` so the success path always dispatches to BTC regardless of
    the snap's ``chain_family``. The ETH listeners (38545-38547) are bound on
    the same ProviderState per pid; they exist for mixed-chain tests that
    drive an ETH-only port on a shared logical provider.

    Yields a dict with base URLs:
      sim["control"]      → http://127.0.0.1:39000
      sim["provider1"]    → http://127.0.0.1:38575    # primary BTC URL per pid
      sim["provider2"]    → http://127.0.0.1:38576
      sim["provider3"]    → http://127.0.0.1:38577
      sim["eth_provider1"]→ http://127.0.0.1:38545    # ETH companion per pid
      sim["eth_provider2"]→ http://127.0.0.1:38546
      sim["eth_provider3"]→ http://127.0.0.1:38547
    """
    # One ProviderState per pid, shared between the ETH and BTC listeners
    # for that pid — mirrors prod's shared-state model.
    states = {pid: ProviderState() for pid in _BTC_PROVIDER_PORTS}

    servers = []
    # ETH listener pool — default handler_chain_family / handler_module.
    for pid, port in _ETH_PROVIDER_PORTS.items():
        srv                  = ThreadingHTTPServer(("127.0.0.1", port), JSONRPCHandler)
        srv.daemon_threads   = True
        srv.state                = states[pid]
        srv.provider_id          = pid
        srv.handler_chain_family = "eth"
        srv.handler_module       = handlers_eth
        servers.append(srv)

    # BTC listener pool — port-derived dispatch to handlers_btc.
    for pid, port in _BTC_PROVIDER_PORTS.items():
        srv                  = ThreadingHTTPServer(("127.0.0.1", port), JSONRPCHandler)
        srv.daemon_threads   = True
        srv.state                = states[pid]
        srv.provider_id          = pid
        srv.handler_chain_family = "btc"
        srv.handler_module       = handlers_btc
        servers.append(srv)

    ctrl                  = HTTPServer(("127.0.0.1", _CONTROL_PORT), ControlHandler)
    ctrl.provider_states  = states
    servers.append(ctrl)

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()

    time.sleep(0.15)

    yield {
        "control":       f"http://127.0.0.1:{_CONTROL_PORT}",
        "provider1":     f"http://127.0.0.1:{_BTC_PROVIDER_PORTS['1']}",
        "provider2":     f"http://127.0.0.1:{_BTC_PROVIDER_PORTS['2']}",
        "provider3":     f"http://127.0.0.1:{_BTC_PROVIDER_PORTS['3']}",
        "eth_provider1": f"http://127.0.0.1:{_ETH_PROVIDER_PORTS['1']}",
        "eth_provider2": f"http://127.0.0.1:{_ETH_PROVIDER_PORTS['2']}",
        "eth_provider3": f"http://127.0.0.1:{_ETH_PROVIDER_PORTS['3']}",
    }

    for s in servers:
        s.shutdown()


# ── Helper to apply per-provider scenario config ──────────────────────────────

def _set_btc(sim, pid: str = "1", **extra):
    """Convenience: POST /scenario for a single provider.

    MAG-2089: no longer sets ``chain_family="btc"`` — the BTC listener pool
    is port-derived. The helper is kept under the BTC name so the BTC-suite
    call sites stay readable. ``chain_family`` becomes load-bearing again
    only when the fault primitive must gate on it (e.g. ``mode="hang"`` on
    the BTC port requires ``chain_family="btc"`` so the listener's fault
    gate fires); callers can pass it via ``chain_family="btc"`` in extras.

    Any extra kwargs are folded into the per-provider config dict (latency_ms,
    mode, blocks_behind, responses, etc.) so callers can write one-liners
    instead of nesting dicts.

    MAG-1783: /scenario now rejects provider blocks without chain_family, so
    this helper fills "eth" — the exact value the simulator used to default
    to when the field was omitted. On the BTC listener "eth" keeps content
    faults un-armed (the gate needs "btc"), which is what plain-helper
    callers rely on. Override via ``chain_family="btc"`` in extras.
    """
    cfg = {"chain_family": "eth", **extra}
    return _post(_ctrl(sim, "/scenario"), {"providers": {pid: cfg}})


def _set_btc_with_fault(sim, pid: str = "1", **extra):
    """Same as ``_set_btc`` but auto-sets ``chain_family="btc"`` so fault
    primitives gated to the BTC listener fire. Used by every fault test —
    ``mode=hang`` / ``mode=drop_connection`` / ``corruption_mode=...`` /
    ``mode=error`` all require the listener's fault gate to match the
    snap's chain_family.
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
# Port-derived dispatch (MAG-2089). Pre-MAG-2089 this class verified that
# ``/scenario`` chain_family selected the handler; under the new model, the
# port selects the handler. These tests now verify the PORT-based contract.
# ─────────────────────────────────────────────────────────────────────────────

class TestBTCPortDispatch:

    def test_default_chain_family_is_eth(self, sim):
        """Without setting chain_family, /scenario still reports the default
        because the field stays in the snap for fault-primitive gating on
        non-JSON-RPC transports."""
        _, body = _get(_ctrl(sim, "/scenario"))
        for pid in ("1", "2", "3"):
            assert body["providers"][pid]["chain_family"] == "eth"

    def test_btc_port_dispatches_to_handlers_btc_with_default_chain_family(self, sim):
        """No /scenario call at all — the BTC port (38575) must still answer
        BTC methods because dispatch is port-derived, not chain_family-derived."""
        status, body = _rpc(sim["provider1"], "getblockcount")
        assert status == 200
        assert "error" not in body
        # Decimal int — bitcoind convention — proves handlers_btc handled it.
        assert isinstance(body["result"], int)
        assert body["result"] == 850_000

    def test_btc_port_ignores_chain_family_eth_override(self, sim):
        """Setting chain_family="eth" must NOT switch the BTC port to ETH
        dispatch — port-derived dispatch is the contract MAG-2089 introduced.
        This is the original symptom: a leftover chain_family from a sister
        test must not contaminate the BTC port's response."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"chain_family": "eth"}}
        })
        status, body = _rpc(sim["provider1"], "getblockcount")
        assert status == 200
        # Still 850_000 (decimal int), not "0x1" — would be "0x1" if ETH dispatch fired.
        assert body["result"] == 850_000

    def test_reset_clears_chain_family(self, sim):
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"chain_family": "btc"}}
        })
        _post(_ctrl(sim, "/reset"), {})
        _, body = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["1"]["chain_family"] == "eth"

    def test_eth_request_on_btc_port_returns_null_result(self, sim):
        """A method unknown to handlers_btc returns null result, not error.
        Mirrors handlers_btc's fallback for unrecognised methods — the
        simulator stays in success mode, the router sees an unfamiliar
        but well-formed response."""
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
        # No scenario setup needed — port-derived dispatch (MAG-2089).
        status, body = _rpc(sim["provider1"], method)
        assert status == 200, f"{method} returned HTTP {status}"
        assert body.get("jsonrpc") == "2.0", f"{method} missing jsonrpc=2.0"
        assert "error" not in body, f"{method} returned error: {body.get('error')!r}"
        assert "result" in body, f"{method} missing result"


class TestGetBlockCountReturnsDecimal:

    def test_getblockcount_is_int_not_hex(self, sim):
        """Critical Q3 invariant: bitcoind returns block heights as JSON numbers, not hex strings."""
        _, body = _rpc(sim["provider1"], "getblockcount")
        assert isinstance(body["result"], int), (
            f"getblockcount must return int, got {type(body['result']).__name__}: {body['result']!r}"
        )

    def test_getblockcount_default_is_realistic_mainnet_height(self, sim):
        """The default head is pinned to BTC_LATEST_BLOCK (850_000) so tests can assert."""
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
        _, body = _rpc(sim["provider1"], "getblockhash", [850_000])
        h = body["result"]
        assert isinstance(h, str), f"hash must be str, got {type(h).__name__}"
        assert self.HEX64.match(h), f"hash {h!r} doesn't match 64 lower-hex pattern"
        assert not h.startswith("0x"), "BTC hashes do not carry the 0x prefix"

    def test_getblockhash_is_deterministic_per_height(self, sim):
        """Same height → same hash (lets tests pin against synthesised values)."""
        _, body1 = _rpc(sim["provider1"], "getblockhash", [12345])
        _, body2 = _rpc(sim["provider1"], "getblockhash", [12345])
        assert body1["result"] == body2["result"]

    def test_getblockhash_different_heights_distinct_hashes(self, sim):
        _, body1 = _rpc(sim["provider1"], "getblockhash", [100])
        _, body2 = _rpc(sim["provider1"], "getblockhash", [200])
        assert body1["result"] != body2["result"]

    def test_getbestblockhash_format(self, sim):
        _, body = _rpc(sim["provider1"], "getbestblockhash")
        h = body["result"]
        assert self.HEX64.match(h)


class TestGetBlockEcho:

    def test_getblock_echoes_requested_hash(self, sim):
        """Like eth_getBlockByNumber on the ETH side — the simulator echoes the
        request param so the router's pruning verification sees a matching
        block identifier in the response."""
        requested = "ab" * 32
        _, body = _rpc(sim["provider1"], "getblock", [requested])
        assert body["result"]["hash"] == requested

    def test_getblockheader_echoes_requested_hash(self, sim):
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
    """All fault tests in this class set ``chain_family="btc"`` so the BTC
    listener's fault gate (``handler_chain_family="btc"``) matches the snap
    and the fault primitive fires. This is the MAG-2089 contract — faults
    must be gated to the specific listener that owns the chain_family."""

    def test_hang_mode_on_btc_provider(self, sim):
        """mode=hang on a BTC-gated scenario hangs the BTC listener."""
        _set_btc_with_fault(sim, "1", mode="hang")
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
        _set_btc_with_fault(sim, "1", mode="drop_connection", drop_at="before_headers")
        with pytest.raises((urllib.error.URLError, ConnectionResetError, OSError, http_err())):
            _rpc(sim["provider1"], "getblockcount")

    def test_stale_blocks_behind_on_btc(self, sim):
        """blocks_behind=100 → BTC head reports 100 below the canonical chain head."""
        _set_btc(sim, "1", blocks_behind=100)
        _, body = _rpc(sim["provider1"], "getblockcount")
        assert body["result"] == 850_000 - 100

    def test_corrupt_response_on_btc(self, sim):
        """corruption_mode=invalid_json on a BTC-gated scenario yields un-parseable bytes."""
        _set_btc_with_fault(sim, "1", corruption_mode="invalid_json")
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
        _set_btc_with_fault(sim, "1", mode="error", http_status=502, error_code=-5,
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
    """Per-method ``responses`` overrides don't go through the fault ladder —
    they short-circuit before the gate at MAG-1846's body-override branch
    (mode-driven errors are dispatched per-method out of handlers_btc), so
    these tests don't need ``chain_family="btc"`` to fire."""

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
    """Each pid has both an ETH listener (38545-7) and a BTC listener (38575-7)
    bound on the same ProviderState — mirrors prod's per-pid shared-state
    model. The ETH listener and BTC listener for the same pid can serve
    different responses simultaneously because dispatch is port-derived."""

    def test_eth_and_btc_listeners_independent_for_same_pid(self, sim):
        """No scenario at all — the same pid answers ETH on the ETH port and
        BTC on the BTC port. Pre-MAG-2089 this required setting chain_family
        per-pid; under the new model the ports themselves decide."""
        _, eth_body = _rpc(sim["eth_provider1"], "eth_blockNumber")
        _, btc_body = _rpc(sim["provider1"],     "getblockcount")

        # ETH side: hex string with "0x" prefix.
        assert isinstance(eth_body["result"], str)
        assert eth_body["result"].startswith("0x")
        # BTC side: decimal integer.
        assert isinstance(btc_body["result"], int)
        assert btc_body["result"] == 850_000

    def test_eth_listener_unaffected_by_btc_fault_on_same_pid(self, sim):
        """MAG-2089's core promise: a fault tagged chain_family="btc" on a
        shared ProviderState fires on the BTC listener (gate matches) but
        passes through on the ETH listener (gate is ``handler_chain_family=
        "eth"``). The pre-MAG-2089 sim would have rate-limited BOTH listeners
        because the ETH listener's ``{"eth","btc","ln"}`` gate matched the
        BTC tag."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {"chain_family": "btc", "mode": "rate_limit"},
            }
        })
        eth_status, eth_body = _rpc(sim["eth_provider1"], "eth_blockNumber")
        btc_status, _        = _rpc(sim["provider1"],     "getblockcount")

        # ETH listener gate is exact-match "eth" — BTC fault is ignored.
        assert eth_status == 200, f"ETH listener should ignore BTC-tagged fault; got {eth_status}"
        assert "result" in eth_body
        # BTC listener gate matches — fault fires.
        assert btc_status == 429, f"BTC listener should rate-limit; got {btc_status}"

    def test_eth_and_btc_independently_blocks_behind(self, sim):
        """blocks_behind on a BTC-tagged snap shifts the BTC head reported by
        getblockcount; the ETH listener serving the same pid is unaffected
        because its dispatch is to handlers_eth (which doesn't use the
        snap's blocks_behind for eth_blockNumber on the ETH suite's default
        defaults)."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {"chain_family": "btc", "blocks_behind": 50},
            }
        })
        _, btc_body = _rpc(sim["provider1"], "getblockcount")
        assert btc_body["result"] == 850_000 - 50


# ─────────────────────────────────────────────────────────────────────────────
# History tracking — BTC requests must show up in /history like ETH ones
# ─────────────────────────────────────────────────────────────────────────────

class TestBTCHistoryTracking:

    def test_btc_request_recorded_in_history(self, sim):
        _rpc(sim["provider1"], "getblockcount")
        _, hist = _get(_ctrl(sim, "/history?provider=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "getblockcount"
        assert last["status"] == "success"

    def test_btc_history_filter_by_method(self, sim):
        """?method= filters work for BTC method names just like ETH ones."""
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


# ─────────────────────────────────────────────────────────────────────────────
# Cross-transport fault isolation — mode=down is universal across chain_family
# ─────────────────────────────────────────────────────────────────────────────


class TestBTCCrossTransportFaultIsolation:
    """BTC port must honor mode=down regardless of chain_family."""

    def test_btc_killed_by_eth_down_fault(self, sim):
        """A ``chain_family="eth"`` down fault MUST 503 the BTC port.

        Universal-down semantics: mode="down" is honored on every transport
        regardless of chain_family because reachability is provider-wide.
        Without it, an ETH provider in mode=down would keep serving BTC
        responses, hiding router-side bugs that depend on the provider being
        unreachable across every node-url. Per-transport isolation still
        applies to content modes (error / corrupt / hang / rate_limit /
        drop_connection) — those gate on chain_family.
        """
        _post(_ctrl(sim, "/scenario"), {
            "providers": {"1": {"chain_family": "eth", "mode": "down"}}
        })
        status, _ = _rpc(sim["provider1"], "getblockcount")
        assert status == 503, (
            f"BTC port should refuse with 503 under universal-down; got {status}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Sequenced faults across listener pools — the fail_first_n window is consumed
# on the owning ETH listener and only OBSERVED (never advanced) by the BTC one
# ─────────────────────────────────────────────────────────────────────────────


class TestBTCSequencedFaultObservation:
    """The sequenced fault (fail_first_n / then_mode) counts requests on the
    OWNING JSON-RPC listener only — here the ETH pool, for an eth-authored
    snap. The BTC listener shares the ProviderState but never advances the
    window; it observes it: a provider-wide down 503s the BTC port while the
    window is open and clears once the ETH listener has consumed it."""

    def test_btc_down_clears_after_owning_eth_listener_consumes_window(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {
            "chain_family": "eth", "mode": "down",
            "fail_first_n": 2, "then_mode": "success",
        }}})

        status, _ = _rpc(sim["provider1"], "getblockcount")
        assert status == 503, (
            f"BTC port must 503 while the down window is open; got {status}"
        )

        for i in (1, 2):
            eth_status, _ = _rpc(sim["eth_provider1"], "eth_blockNumber")
            assert eth_status == 503, (
                f"owning ETH call {i} is inside the down window; got {eth_status}"
            )

        status, body = _rpc(sim["provider1"], "getblockcount")
        assert status == 200, (
            f"BTC port must observe the consumed window and serve "
            f"then_mode=success; got {status}"
        )
        assert isinstance(body.get("result"), int), (
            f"expected the BTC success stub (decimal block count); got {body!r}"
        )
