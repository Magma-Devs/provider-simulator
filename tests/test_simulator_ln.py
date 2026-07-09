"""
Unit tests for the Lightning Network (LND) chain dispatch in the provider
simulator (MAG-1726; revised under MAG-2089 to use dedicated LN listener
ports).

Mirrors the structure of ``tests/test_simulator_btc.py`` (the BTC L1 suite,
MAG-1716) but covers only LN-specific behaviour:

  Happy-path per LN method        — every method in LND_METHOD_DEFAULTS
                                     responds with a JSON-RPC result envelope.
  Fault primitives                  — set_hang / drop / corrupt / status all
                                     apply identically on an LN provider; one
                                     test per primitive (4 minimum).
  Mixed-chain scenario              — one ETH + one LN provider sharing a pid,
                                     each independently configured via
                                     port-derived dispatch.
  block_height shift                — getinfo.block_height tracks blocks_behind
                                     the same way BTC's getblockcount does.
  Invoice / pubkey echo             — decodepayreq / payinvoice / openchannel
                                     echo their request params into the response.
  History tracking                   — LN requests show up in /history exactly
                                     like ETH / BTC ones.

Port layout
-----------
MAG-2089 moved LN dispatch from a per-provider ``chain_family`` flag on the
shared ETH JSON-RPC listener pool (18545-18547) to a dedicated LN listener
pool at 18578-18580. This suite mirrors the move: a dedicated LN test port
range at 23578-23580 (tail digits mirror prod 18578-18580) hosts JSONRPCHandler
listeners with ``handler_chain_family="ln"`` + ``handler_module=
handlers_lnd``. A second ETH listener pool at 23545-23547 hosts default-ETH
listeners, used by the mixed-chain tests. Both pools share ProviderState per
pid so a single ``/scenario`` POST reconfigures both listeners for the same
logical provider — exactly mirroring prod.

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

import handlers_eth
import handlers_lnd
from server import ControlHandler, JSONRPCHandler, ProviderState
from stubs_lnd import LND_METHOD_DEFAULTS

# ── Test ports. Two rules keep binds reliable across the whole suite:
#    1. Stay below 32768. Ports from 32768 up are the kernel's ephemeral
#       client-port range (Linux default 32768-60999, macOS 49152-65535):
#       every outgoing HTTP call an earlier test module makes grabs a random
#       source port there, and a lingering one makes this module's bind fail
#       with "Address already in use" at fixture setup.
#    2. Each test file owns a unique port block (this file: 235xx) so all
#       modules can run in one pytest invocation.
#     The LN pool's tail digits (578-580) mirror prod 18578-18580. ─────

_ETH_PROVIDER_PORTS = {"1": 23545, "2": 23546, "3": 23547}
_LN_PROVIDER_PORTS = {"1": 23578, "2": 23579, "3": 23580}
_CONTROL_PORT = 23500

# 6 LN methods covered by the stub set. Source of truth: stubs_lnd.py.
ALL_LND_METHODS = sorted(LND_METHOD_DEFAULTS.keys())


# ── HTTP helpers (kept independent of test_simulator_btc.py to avoid
#     cross-file fixture coupling — duplication is intentional, the two
#     files run on different ports). ─────────────────────────────────────────


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
    """Start 3 ETH listeners + 3 LN listeners + 1 control server.

    The LN listeners (23578-23580) are the focus of this suite — they run
    JSONRPCHandler with ``handler_chain_family="ln"`` + ``handler_module=
    handlers_lnd`` so the success path always dispatches to LN regardless of
    the snap's ``chain_family``. The ETH listeners (23545-23547) are bound on
    the same ProviderState per pid; they exist for mixed-chain tests that
    drive an ETH-only port on a shared logical provider.

    Yields a dict with base URLs:
      sim["control"]      → http://127.0.0.1:23500
      sim["provider1"]    → http://127.0.0.1:23578    # primary LN URL per pid
      sim["provider2"]    → http://127.0.0.1:23579
      sim["provider3"]    → http://127.0.0.1:23580
      sim["eth_provider1"]→ http://127.0.0.1:23545    # ETH companion per pid
      sim["eth_provider2"]→ http://127.0.0.1:23546
      sim["eth_provider3"]→ http://127.0.0.1:23547
    """
    # One ProviderState per pid, shared between the ETH and LN listeners
    # for that pid — mirrors prod's shared-state model.
    states = {pid: ProviderState() for pid in _LN_PROVIDER_PORTS}

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

    # LN listener pool — port-derived dispatch to handlers_lnd.
    for pid, port in _LN_PROVIDER_PORTS.items():
        srv = ThreadingHTTPServer(("127.0.0.1", port), JSONRPCHandler)
        srv.daemon_threads = True
        srv.state = states[pid]
        srv.provider_id = pid
        srv.handler_chain_family = "ln"
        srv.handler_module = handlers_lnd
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
        "provider1": f"http://127.0.0.1:{_LN_PROVIDER_PORTS['1']}",
        "provider2": f"http://127.0.0.1:{_LN_PROVIDER_PORTS['2']}",
        "provider3": f"http://127.0.0.1:{_LN_PROVIDER_PORTS['3']}",
        "eth_provider1": f"http://127.0.0.1:{_ETH_PROVIDER_PORTS['1']}",
        "eth_provider2": f"http://127.0.0.1:{_ETH_PROVIDER_PORTS['2']}",
        "eth_provider3": f"http://127.0.0.1:{_ETH_PROVIDER_PORTS['3']}",
    }

    for s in servers:
        s.shutdown()


# ── Helper to apply per-provider scenario config ──────────────────────────────


def _set_ln(sim, pid: str = "1", **extra):
    """Convenience: POST /scenario for a single provider.

    MAG-2089: no longer sets ``chain_family="ln"`` — the LN listener pool
    is port-derived. The helper is kept under the LN name so the LN-suite
    call sites stay readable. Use ``_set_ln_with_fault`` when the test
    needs ``chain_family="ln"`` for the listener's fault gate to fire."""
    cfg = dict(extra)
    return _post(_ctrl(sim, "/scenario"), {"providers": {pid: cfg}})


def _set_ln_with_fault(sim, pid: str = "1", **extra):
    """Same as ``_set_ln`` but auto-sets ``chain_family="ln"`` so fault
    primitives gated to the LN listener fire. Used by every fault test."""
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
# Port-derived dispatch (MAG-2089). Pre-MAG-2089 this class verified that
# ``/scenario`` chain_family selected the handler; under the new model, the
# port selects the handler. These tests now verify the PORT-based contract.
# ─────────────────────────────────────────────────────────────────────────────


class TestLNPortDispatch:

    def test_default_chain_family_is_eth(self, sim):
        """Without setting chain_family, /scenario still reports the default
        because the field stays in the snap for fault-primitive gating on
        non-JSON-RPC transports."""
        _, body = _get(_ctrl(sim, "/scenario"))
        for pid in ("1", "2", "3"):
            assert body["providers"][pid]["chain_family"] == "eth"

    def test_ln_port_dispatches_to_handlers_lnd_with_default_chain_family(self, sim):
        """No /scenario call at all — the LN port (23578) must still answer
        LN methods because dispatch is port-derived, not chain_family-derived."""
        status, body = _rpc(sim["provider1"], "getinfo")
        assert status == 200
        assert "error" not in body
        # LN getinfo carries identity_pubkey — proves handlers_lnd handled it.
        assert "identity_pubkey" in body["result"]

    def test_ln_port_ignores_chain_family_eth_override(self, sim):
        """Setting chain_family="eth" must NOT switch the LN port to ETH
        dispatch — port-derived dispatch is the contract MAG-2089 introduced.
        This is the original symptom: a leftover chain_family from a sister
        test must not contaminate the LN port's response."""
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"chain_family": "eth"}}})
        status, body = _rpc(sim["provider1"], "getinfo")
        assert status == 200
        # LN-shaped response, not an ETH stub.
        assert "identity_pubkey" in body["result"]

    def test_reset_clears_chain_family(self, sim):
        _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"chain_family": "ln"}}})
        _post(_ctrl(sim, "/reset"), {})
        _, body = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["1"]["chain_family"] == "eth"

    def test_eth_method_on_ln_port_returns_null_result(self, sim):
        """A method unknown to handlers_lnd returns null result, not error.

        Mirrors the BTC handler's behaviour — the simulator stays in success
        mode, the router sees an unfamiliar but well-formed response.
        """
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200
        assert "error" not in body
        assert body["result"] is None

    def test_btc_method_on_ln_port_returns_null_result(self, sim):
        """The LN port hosts handlers_lnd unconditionally; BTC method names
        aren't known to handlers_lnd, so the simulator stays in success mode
        and returns the null sentinel. This is now port-derived: even if the
        snap's chain_family were "btc", the LN port would still call
        handlers_lnd (the BTC dispatch lives on the dedicated BTC port pool).
        """
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
        # No scenario setup needed — port-derived dispatch (MAG-2089).
        status, body = _rpc(sim["provider1"], method)
        assert status == 200, f"{method} returned HTTP {status}"
        assert body.get("jsonrpc") == "2.0", f"{method} missing jsonrpc=2.0"
        assert "error" not in body, f"{method} returned error: {body.get('error')!r}"
        assert "result" in body, f"{method} missing result"

    def test_getinfo_shape(self, sim):
        """getinfo carries the 8 ticket-required fields."""
        _, body = _rpc(sim["provider1"], "getinfo")
        r = body["result"]
        for key in (
            "identity_pubkey",
            "alias",
            "num_peers",
            "num_active_channels",
            "block_height",
            "synced_to_chain",
            "synced_to_graph",
            "chains",
        ):
            assert key in r, f"getinfo missing required field: {key}"

    def test_listchannels_wraps_in_channels_key(self, sim):
        """LND's wire shape is {"channels": [...]} — preserved by the stub."""
        _, body = _rpc(sim["provider1"], "listchannels")
        assert "channels" in body["result"]
        assert isinstance(body["result"]["channels"], list)
        assert len(body["result"]["channels"]) >= 1

    def test_listpeers_wraps_in_peers_key(self, sim):
        """Same wrapping convention as listchannels."""
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
        _, body = _rpc(sim["provider1"], "getinfo")
        assert body["result"]["synced_to_chain"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Param echo behaviour — decodepayreq / payinvoice / openchannel
# ─────────────────────────────────────────────────────────────────────────────


class TestParamEcho:

    def test_decodepayreq_echoes_invoice(self, sim):
        """The simulator stashes the requested invoice in payment_request so
        tests can round-trip without lifting real bolt11 bytes."""
        invoice = "lnbcrt100u1psim_test_invoice"
        _, body = _rpc(sim["provider1"], "decodepayreq", [invoice])
        assert body["result"]["payment_request"] == invoice

    def test_payinvoice_echoes_invoice(self, sim):
        """payinvoice echoes the invoice into payment_request alongside the
        preimage + hash so /history correlation matches the request."""
        invoice = "lnbcrt500u1psim_pay_invoice"
        _, body = _rpc(sim["provider1"], "payinvoice", [invoice])
        assert body["result"]["payment_request"] == invoice
        # Default payment shape still present.
        assert body["result"]["payment_preimage"]
        assert body["result"]["payment_hash"]

    def test_openchannel_echoes_node_pubkey(self, sim):
        """openchannel echoes the remote pubkey from params[0] into node_pubkey."""
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
    """All fault tests in this class set ``chain_family="ln"`` so the LN
    listener's fault gate (``handler_chain_family="ln"``) matches the snap
    and the fault primitive fires. This is the MAG-2089 contract — faults
    must be gated to the specific listener that owns the chain_family."""

    def test_hang_mode_on_ln_provider(self, sim):
        """mode=hang on an LN-gated scenario hangs the LN listener.

        Covers set_hang on provider 1.
        """
        _set_ln_with_fault(sim, "1", mode="hang")
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
        _set_ln_with_fault(sim, "2", mode="drop_connection", drop_at="before_headers")
        with pytest.raises(
            (urllib.error.URLError, ConnectionResetError, OSError, urllib.error.HTTPError)
        ):
            _rpc(sim["provider2"], "getinfo")

    def test_corrupt_response_on_ln(self, sim):
        """Covers set_corrupt on provider 3.

        corruption_mode=invalid_json on an LN-gated scenario yields un-parseable bytes.
        """
        _set_ln_with_fault(sim, "3", corruption_mode="invalid_json")
        # Build the request manually so we read raw bytes without json.loads.
        req = urllib.request.Request(
            f"{sim['provider3']}/",
            data=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "getinfo", "params": []}
            ).encode(),
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
        _set_ln_with_fault(
            sim,
            "1",
            mode="error",
            http_status=502,
            error_code=-32000,
            error_message="upstream unavailable",
        )
        status, body = _rpc(sim["provider1"], "getinfo")
        assert status == 502
        assert body["error"]["code"] == -32000
        assert "upstream unavailable" in body["error"]["message"]


# ─────────────────────────────────────────────────────────────────────────────
# Per-method error overrides on an LN provider — error_stub catalogue
# ─────────────────────────────────────────────────────────────────────────────


class TestLNErrorStubs:
    """Per-method ``responses`` overrides don't go through the fault ladder —
    they short-circuit before the gate at MAG-1846's body-override branch,
    so these tests don't need ``chain_family="ln"`` to fire."""

    def test_ln_error_stub_named_lookup(self, sim):
        """responses[method] = {"error_stub": <name>} routes to LND_ERROR_STUBS."""
        _set_ln(sim, "1", responses={"payinvoice": {"error_stub": "no_route"}})
        _, body = _rpc(sim["provider1"], "payinvoice", ["lnbcrt_bogus"])
        assert "error" in body
        assert "find a path" in body["error"]["message"]

    def test_ln_error_stub_raw_envelope(self, sim):
        """Escape hatch: responses[method] = {"error": {...}} bypasses the catalogue."""
        _set_ln(
            sim,
            "1",
            responses={"openchannel": {"error": {"code": -99, "message": "Custom LN error"}}},
        )
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
# Mixed-chain scenario — ETH and LN listeners sharing a pid via port-derived
# dispatch. (BTC listener is not in this fixture; the BTC suite covers ETH-vs-
# BTC. The LN suite focuses on ETH-vs-LN isolation.)
# ─────────────────────────────────────────────────────────────────────────────


class TestMixedChainScenario:
    """Each pid has both an ETH listener (23545-7) and an LN listener (23578-80)
    bound on the same ProviderState — mirrors prod's per-pid shared-state
    model. The ETH listener and LN listener for the same pid can serve
    different responses simultaneously because dispatch is port-derived."""

    def test_eth_and_ln_listeners_independent_for_same_pid(self, sim):
        """No scenario at all — the same pid answers ETH on the ETH port and
        LN on the LN port. Pre-MAG-2089 this required setting chain_family
        per-pid; under the new model the ports themselves decide."""
        _, eth_body = _rpc(sim["eth_provider1"], "eth_blockNumber")
        _, ln_body = _rpc(sim["provider1"], "getinfo")

        # ETH: hex string with "0x" prefix.
        assert isinstance(eth_body["result"], str)
        assert eth_body["result"].startswith("0x")
        # LN: dict with identity_pubkey.
        assert isinstance(ln_body["result"], dict)
        assert "identity_pubkey" in ln_body["result"]

    def test_eth_listener_unaffected_by_ln_fault_on_same_pid(self, sim):
        """MAG-2089's core promise for LN: a fault tagged chain_family="ln"
        on a shared ProviderState fires on the LN listener (gate matches)
        but passes through on the ETH listener (gate is "eth")."""
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "1": {"chain_family": "ln", "mode": "rate_limit"},
                }
            },
        )
        eth_status, eth_body = _rpc(sim["eth_provider1"], "eth_blockNumber")
        ln_status, _ = _rpc(sim["provider1"], "getinfo")

        # ETH listener gate is exact-match "eth" — LN fault is ignored.
        assert eth_status == 200, f"ETH listener should ignore LN-tagged fault; got {eth_status}"
        assert "result" in eth_body
        # LN listener gate matches — fault fires.
        assert ln_status == 429, f"LN listener should rate-limit; got {ln_status}"

    def test_eth_and_ln_independently_blocks_behind(self, sim):
        """blocks_behind on an LN-tagged snap shifts the LN block_height
        reported by getinfo. The ETH listener serving the same pid is
        unaffected because its dispatch is to handlers_eth."""
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "1": {"chain_family": "ln", "blocks_behind": 50},
                }
            },
        )
        _, ln_body = _rpc(sim["provider1"], "getinfo")
        assert ln_body["result"]["block_height"] == 850_000 - 50


# ─────────────────────────────────────────────────────────────────────────────
# History tracking — LN requests must show up in /history like ETH/BTC ones
# ─────────────────────────────────────────────────────────────────────────────


class TestLNHistoryTracking:

    def test_ln_request_recorded_in_history(self, sim):
        _rpc(sim["provider1"], "getinfo")
        _, hist = _get(_ctrl(sim, "/history?provider=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "getinfo"
        assert last["status"] == "success"

    def test_ln_history_filter_by_method(self, sim):
        """?method= filters work for LN method names just like ETH/BTC ones."""
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
