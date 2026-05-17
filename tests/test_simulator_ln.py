"""
Unit tests for the Lightning Network (LND) chain dispatch in the provider
simulator (MAG-1726).

Mirrors the structure of ``tests/test_simulator_btc.py`` (the BTC L1 suite,
MAG-1716) but covers only LN-specific behaviour:

  Happy-path per LN method        — every method in LND_METHOD_DEFAULTS
                                     responds with a JSON-RPC result envelope.
  Fault primitives                  — set_hang / drop / corrupt / status all
                                     apply identically on an LN provider; one
                                     test per primitive (4 minimum).
  Mixed-chain scenario              — one ETH + one BTC + one LN provider in
                                     the same /scenario body, each independently
                                     configured.
  block_height shift                — getinfo.block_height tracks blocks_behind
                                     the same way BTC's getblockcount does.
  Invoice / pubkey echo             — decodepayreq / payinvoice / openchannel
                                     echo their request params into the response.
  History tracking                   — LN requests show up in /history exactly
                                     like ETH / BTC ones.

Why no LN_PROVIDER_PORTS test dict
----------------------------------
LN reuses the JSON-RPC listener pool (18545-18547 in prod) the same way BTC
does — chain_family is per-provider, not per-port. The test suite picks
distinct test ports (58575-58577 / 59100) so this file can sit anywhere in
pytest collection order without colliding with another module's listeners.

Run with:
  pytest tests/test_simulator_ln.py -v
"""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer, ThreadingHTTPServer

import pytest

from server import ControlHandler, JSONRPCHandler, ProviderState
from stubs_lnd import LND_METHOD_DEFAULTS

# ── Test ports (distinct from every other test module so all suites can run
#     in any order in a single pytest invocation):
#       ETH base suite                 28545-28547 / 29000
#       BTC suite                       38545-38547 / 39000
#       REST / WS / logs_lag            48545-48547 / 49000  (these collide
#                                                            with each other on
#                                                            purpose — each
#                                                            uses module-scoped
#                                                            fixtures that
#                                                            shut down cleanly
#                                                            between modules)
#       gRPC                            49545-49547 / 49000  (control overlap
#                                                            tolerated via
#                                                            separate fixture)
#       per_method / tendermintrpc      58545-58547 / 59000
#       backup_listeners                58545-58547 / 59000  (shares with the
#                                                            above; runs after
#                                                            them in alpha
#                                                            order)
#     LN picks 58575-58577 / 59100 — outside every existing range, so the LN
#     module can sit anywhere in collection order without colliding with a
#     stale listener from a still-shutting-down module above it. ──────────────

_PROVIDER_PORTS = {"1": 58575, "2": 58576, "3": 58577}
_CONTROL_PORT   = 59100

# 6 LN methods covered by the stub set. Source of truth: stubs_lnd.py.
ALL_LND_METHODS = sorted(LND_METHOD_DEFAULTS.keys())


# ── HTTP helpers (kept independent of test_simulator_btc.py to avoid
#     cross-file fixture coupling — duplication is intentional, the two
#     files run on different ports). ─────────────────────────────────────────


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
    """Start 3 JSON-RPC servers + 1 control server on dedicated LN test ports.

    Yields a dict with base URLs:
      sim["control"]   → http://127.0.0.1:59100
      sim["provider1"] → http://127.0.0.1:58575
      sim["provider2"] → http://127.0.0.1:58576
      sim["provider3"] → http://127.0.0.1:58577
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


# ── Helper to put a provider in LN mode ───────────────────────────────────────

def _set_ln(sim, pid: str = "1", **extra):
    """Convenience: POST /scenario for a single provider with chain_family=ln.

    Any extra kwargs are folded into the per-provider config dict (latency_ms,
    mode, blocks_behind, responses, etc.) so callers can write one-liners
    instead of nesting dicts.
    """
    cfg = {"chain_family": "ln", **extra}
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

    def test_set_chain_family_ln(self, sim):
        _set_ln(sim, "1")
        _, body = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["1"]["chain_family"] == "ln"
        # other providers untouched
        assert body["providers"]["2"]["chain_family"] == "eth"
        assert body["providers"]["3"]["chain_family"] == "eth"

    def test_reset_clears_chain_family(self, sim):
        _set_ln(sim, "1")
        _post(_ctrl(sim, "/reset"), {})
        _, body = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["1"]["chain_family"] == "eth"

    def test_eth_method_on_ln_provider_returns_null_result(self, sim):
        """A method unknown to handlers_lnd returns null result, not error.

        Mirrors the BTC handler's behaviour — the simulator stays in success
        mode, the router sees an unfamiliar but well-formed response.
        """
        _set_ln(sim, "1")
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200
        assert "error" not in body
        assert body["result"] is None

    def test_btc_method_on_ln_provider_returns_null_result(self, sim):
        """LN and BTC namespaces don't overlap on the methods in scope here,
        but ``getblockcount`` is BTC-specific — on an LN provider it must
        fall through to the null sentinel, NOT be answered by the BTC stub.
        Ensures handlers_lnd / handlers_btc selection is mutually exclusive.
        """
        _set_ln(sim, "1")
        status, body = _rpc(sim["provider1"], "getblockcount")
        assert status == 200
        assert "error" not in body
        assert body["result"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Happy-path stubs per LN method (covers all 6 ticket-scoped methods)
# ─────────────────────────────────────────────────────────────────────────────

class TestLNDMethodDefaults:

    @pytest.mark.parametrize("method", ALL_LND_METHODS)
    def test_method_returns_success_envelope(self, sim, method):
        """Every method in LND_METHOD_DEFAULTS responds with a JSON-RPC result envelope."""
        _set_ln(sim, "1")
        status, body = _rpc(sim["provider1"], method)
        assert status == 200, f"{method} returned HTTP {status}"
        assert body.get("jsonrpc") == "2.0", f"{method} missing jsonrpc=2.0"
        assert "error" not in body, f"{method} returned error: {body.get('error')!r}"
        assert "result" in body, f"{method} missing result"

    def test_getinfo_shape(self, sim):
        """getinfo carries the 8 ticket-required fields."""
        _set_ln(sim, "1")
        _, body = _rpc(sim["provider1"], "getinfo")
        r = body["result"]
        for key in ("identity_pubkey", "alias", "num_peers",
                     "num_active_channels", "block_height",
                     "synced_to_chain", "synced_to_graph", "chains"):
            assert key in r, f"getinfo missing required field: {key}"

    def test_listchannels_wraps_in_channels_key(self, sim):
        """LND's wire shape is {"channels": [...]} — preserved by the stub."""
        _set_ln(sim, "1")
        _, body = _rpc(sim["provider1"], "listchannels")
        assert "channels" in body["result"]
        assert isinstance(body["result"]["channels"], list)
        assert len(body["result"]["channels"]) >= 1

    def test_listpeers_wraps_in_peers_key(self, sim):
        """Same wrapping convention as listchannels."""
        _set_ln(sim, "1")
        _, body = _rpc(sim["provider1"], "listpeers")
        assert "peers" in body["result"]
        assert isinstance(body["result"]["peers"], list)
        assert len(body["result"]["peers"]) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# block_height shift on getinfo — mirrors BTC's getblockcount shift
# ─────────────────────────────────────────────────────────────────────────────

class TestGetInfoBlockHeight:

    def test_getinfo_block_height_default(self, sim):
        """At blocks_behind=0 the LN node reports the canonical BTC head."""
        _set_ln(sim, "1")
        _, body = _rpc(sim["provider1"], "getinfo")
        assert body["result"]["block_height"] == 850_000

    def test_getinfo_block_height_shifts_with_blocks_behind(self, sim):
        """blocks_behind=10 → LN node reports 850_000 - 10 as its tracked BTC head."""
        _set_ln(sim, "1", blocks_behind=10)
        _, body = _rpc(sim["provider1"], "getinfo")
        assert body["result"]["block_height"] == 850_000 - 10

    def test_getinfo_synced_to_chain_flips_when_lagged(self, sim):
        """Any positive blocks_behind flips synced_to_chain to False."""
        _set_ln(sim, "1", blocks_behind=5)
        _, body = _rpc(sim["provider1"], "getinfo")
        assert body["result"]["synced_to_chain"] is False

    def test_getinfo_synced_to_chain_true_when_caught_up(self, sim):
        """At blocks_behind=0 the node reports synced_to_chain=True."""
        _set_ln(sim, "1")
        _, body = _rpc(sim["provider1"], "getinfo")
        assert body["result"]["synced_to_chain"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Param echo behaviour — decodepayreq / payinvoice / openchannel
# ─────────────────────────────────────────────────────────────────────────────

class TestParamEcho:

    def test_decodepayreq_echoes_invoice(self, sim):
        """The simulator stashes the requested invoice in payment_request so
        tests can round-trip without lifting real bolt11 bytes."""
        _set_ln(sim, "1")
        invoice = "lnbcrt100u1psim_test_invoice"
        _, body = _rpc(sim["provider1"], "decodepayreq", [invoice])
        assert body["result"]["payment_request"] == invoice

    def test_payinvoice_echoes_invoice(self, sim):
        """payinvoice echoes the invoice into payment_request alongside the
        preimage + hash so /history correlation matches the request."""
        _set_ln(sim, "1")
        invoice = "lnbcrt500u1psim_pay_invoice"
        _, body = _rpc(sim["provider1"], "payinvoice", [invoice])
        assert body["result"]["payment_request"] == invoice
        # Default payment shape still present.
        assert body["result"]["payment_preimage"]
        assert body["result"]["payment_hash"]

    def test_openchannel_echoes_node_pubkey(self, sim):
        """openchannel echoes the remote pubkey from params[0] into node_pubkey."""
        _set_ln(sim, "1")
        peer_pk = "03" + "ff" * 32
        _, body = _rpc(sim["provider1"], "openchannel", [peer_pk, 250_000])
        assert body["result"]["node_pubkey"] == peer_pk
        # funding_txid_str still present from the default stub.
        assert body["result"]["funding_txid_str"]


# ─────────────────────────────────────────────────────────────────────────────
# Fault-injection primitives applied on an LN provider — covers 4 primitives
# (set_hang, set_dropped, set_corrupt, set_status) per ticket requirement.
# ─────────────────────────────────────────────────────────────────────────────

class TestLNFaultInjection:

    def test_hang_mode_on_ln_provider(self, sim):
        """mode=hang behaves identically regardless of chain_family.

        Covers set_hang on provider 1.
        """
        _set_ln(sim, "1", mode="hang")
        t0 = time.monotonic()
        try:
            _rpc(sim["provider1"], "getinfo")
        except (urllib.error.URLError, ConnectionResetError, OSError):
            pass  # 30s sleep → client times out at 5s
        elapsed = time.monotonic() - t0
        # We don't wait 30s; just confirm the client timed out (≥4s, well
        # above the latency_ms=0 fast path) — actual cap is urlopen timeout.
        assert elapsed >= 4, f"hang should block at least the client timeout, got {elapsed:.2f}s"

    def test_drop_connection_before_headers_on_ln(self, sim):
        """Covers set_dropped on provider 2.

        drop_at=before_headers closes the socket before any HTTP headers
        are written — the client sees a URLError or ConnectionResetError.
        """
        _set_ln(sim, "2", mode="drop_connection", drop_at="before_headers")
        with pytest.raises((urllib.error.URLError, ConnectionResetError,
                            OSError, urllib.error.HTTPError)):
            _rpc(sim["provider2"], "getinfo")

    def test_corrupt_response_on_ln(self, sim):
        """Covers set_corrupt on provider 3.

        corruption_mode=invalid_json on an LN provider yields un-parseable bytes.
        """
        _set_ln(sim, "3", corruption_mode="invalid_json")
        # Build the request manually so we read raw bytes without json.loads.
        req = urllib.request.Request(
            f"{sim['provider3']}/",
            data=json.dumps({"jsonrpc": "2.0", "id": 1,
                              "method": "getinfo", "params": []}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)

    def test_status_override_on_ln(self, sim):
        """Covers set_status on provider 1.

        mode=error + http_status=502 must propagate the custom HTTP status.
        """
        _set_ln(sim, "1", mode="error", http_status=502, error_code=-32000,
                 error_message="upstream unavailable")
        status, body = _rpc(sim["provider1"], "getinfo")
        assert status == 502
        assert body["error"]["code"] == -32000
        assert "upstream unavailable" in body["error"]["message"]


# ─────────────────────────────────────────────────────────────────────────────
# Per-method error overrides on an LN provider — error_stub catalogue
# ─────────────────────────────────────────────────────────────────────────────

class TestLNErrorStubs:

    def test_ln_error_stub_named_lookup(self, sim):
        """responses[method] = {"error_stub": <name>} routes to LND_ERROR_STUBS."""
        _set_ln(sim, "1", responses={"payinvoice": {"error_stub": "no_route"}})
        _, body = _rpc(sim["provider1"], "payinvoice", ["lnbcrt_bogus"])
        assert "error" in body
        assert "find a path" in body["error"]["message"]

    def test_ln_error_stub_raw_envelope(self, sim):
        """Escape hatch: responses[method] = {"error": {...}} bypasses the catalogue."""
        _set_ln(sim, "1", responses={"openchannel": {
            "error": {"code": -99, "message": "Custom LN error"}
        }})
        _, body = _rpc(sim["provider1"], "openchannel", ["02deadbeef" + "00" * 28, 100_000])
        assert body["error"]["code"] == -99
        assert body["error"]["message"] == "Custom LN error"

    def test_ln_method_unaffected_by_other_method_error(self, sim):
        """Per-method overrides scope strictly to that method."""
        _set_ln(sim, "1", responses={"payinvoice": {"error_stub": "no_route"}})
        # The unrelated method must still succeed.
        _, body = _rpc(sim["provider1"], "getinfo")
        assert "result" in body
        assert "error" not in body


# ─────────────────────────────────────────────────────────────────────────────
# Mixed-chain scenario — ETH + BTC + LN in one /scenario body
# ─────────────────────────────────────────────────────────────────────────────

class TestMixedChainScenario:

    def test_eth_btc_ln_providers_independent(self, sim):
        """One /scenario call sets three different chain_family values.
        Each provider answers in its own chain's convention."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {"chain_family": "eth"},
                "2": {"chain_family": "btc"},
                "3": {"chain_family": "ln"},
            }
        })
        _, eth_body = _rpc(sim["provider1"], "eth_blockNumber")
        _, btc_body = _rpc(sim["provider2"], "getblockcount")
        _, ln_body  = _rpc(sim["provider3"], "getinfo")

        # ETH: hex string with "0x" prefix.
        assert isinstance(eth_body["result"], str)
        assert eth_body["result"].startswith("0x")
        # BTC: decimal integer.
        assert isinstance(btc_body["result"], int)
        # LN: dict with identity_pubkey.
        assert isinstance(ln_body["result"], dict)
        assert "identity_pubkey" in ln_body["result"]

    def test_eth_and_ln_independently_faulted(self, sim):
        """Each provider can run a different fault mode in the same scenario."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {"chain_family": "eth", "mode": "rate_limit"},
                "2": {"chain_family": "ln",  "blocks_behind": 50},
            }
        })
        eth_status, eth_body = _rpc(sim["provider1"], "eth_blockNumber")
        ln_status,  ln_body  = _rpc(sim["provider2"], "getinfo")

        assert eth_status == 429, "ETH provider was rate-limited"
        assert ln_status  == 200, "LN provider should answer normally"
        assert ln_body["result"]["block_height"] == 850_000 - 50


# ─────────────────────────────────────────────────────────────────────────────
# History tracking — LN requests must show up in /history like ETH/BTC ones
# ─────────────────────────────────────────────────────────────────────────────

class TestLNHistoryTracking:

    def test_ln_request_recorded_in_history(self, sim):
        _set_ln(sim, "1")
        _rpc(sim["provider1"], "getinfo")
        _, hist = _get(_ctrl(sim, "/history?provider=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "getinfo"
        assert last["status"] == "success"

    def test_ln_history_filter_by_method(self, sim):
        """?method= filters work for LN method names just like ETH/BTC ones."""
        _set_ln(sim, "1")
        _rpc(sim["provider1"], "getinfo")
        _rpc(sim["provider1"], "listpeers")
        _, hist = _get(_ctrl(sim, "/history?method=getinfo"))
        assert hist["count"] >= 1
        assert all(e["method"] == "getinfo" for e in hist["history"])

    def test_ln_error_status_recorded(self, sim):
        """Per-method error_stub on an LN method produces status=error in history."""
        _set_ln(sim, "1", responses={"payinvoice": {"error_stub": "no_route"}})
        _rpc(sim["provider1"], "payinvoice", ["lnbcrt_bogus"])
        _, hist = _get(_ctrl(sim, "/history?provider=1&status=error"))
        assert hist["count"] >= 1
        assert hist["history"][-1]["method"] == "payinvoice"
