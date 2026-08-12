"""
Integration tests for the Lightning Network (LND) pool of the provider
simulator.

Runs against the shared in-process simulator (see conftest.py): the ln-sim
pool listens on 18578-18580 and the eth-sim pool on 18545-18547. Under the
pool:pid model those are SEPARATE providers — ln-sim:1 and eth-sim:1 share no
state, so cross-pool isolation is structural, not gated.

Coverage:
  Happy-path per LN method        — every method in LND_METHOD_DEFAULTS
                                     responds with a JSON-RPC result envelope.
  Fault primitives                 — hang / drop / corrupt / status all apply
                                     identically on an LN provider.
  Cross-pool independence          — a fault on ln-sim:1 never touches
                                     eth-sim:1.
  block_height shift               — getinfo.block_height tracks blocks_behind
                                     the same way BTC's getblockcount does.
  Invoice / pubkey echo            — decodepayreq / payinvoice / openchannel
                                     echo their request params into the response.
  History tracking                  — LN requests show up in /history exactly
                                     like ETH / BTC ones.

Run with:
  pytest tests/test_simulator_ln.py -v
"""

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

from constants import ETH_PRIMARY_PORTS, LN_PRIMARY_PORTS
from stubs_lnd import LND_METHOD_DEFAULTS

_LN_URLS = {pid: f"http://127.0.0.1:{port}" for pid, port in LN_PRIMARY_PORTS.items()}
_ETH1 = f"http://127.0.0.1:{ETH_PRIMARY_PORTS['1']}"

# 6 LN methods covered by the stub set. Source of truth: stubs_lnd.py.
ALL_LND_METHODS = sorted(LND_METHOD_DEFAULTS.keys())


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


def _raw_post_bytes(port: int, method: str) -> bytes:
    """POST over a raw socket and return every byte the server sends before EOF.

    Used by the drop-point tests: urllib collapses every drop variant into an
    exception, while the raw bytes show exactly where the connection ended.
    """
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": []}).encode()
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        s.sendall(
            b"POST / HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\n\r\n".encode()
            + payload
        )
        chunks = []
        while True:
            block = s.recv(4096)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    finally:
        s.close()


def _ctrl(sim: dict, path: str) -> str:
    return sim["control"] + path


def _set_ln(sim, pid: str = "1", **extra):
    """Convenience: POST /scenario for one ln-sim provider. Extra kwargs are
    the provider block (mode, latency_ms, blocks_behind, responses, ...)."""
    return _post(_ctrl(sim, "/scenario"), {"providers": {f"ln-sim:{pid}": dict(extra)}})


# ── Function-scoped autouse: clean slate before/after every test ──────────────


@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario config AND clear history before and after every test."""
    _post(_ctrl(sim, "/reset/all"), {})
    yield
    _post(_ctrl(sim, "/reset/all"), {})


# ─────────────────────────────────────────────────────────────────────────────
# Pool-derived dispatch: the port IS the pool; chain_family is gone
# ─────────────────────────────────────────────────────────────────────────────


class TestLNPortDispatch:

    def test_scenario_snapshot_has_no_chain_family_field(self, sim):
        """The pool:pid model carries no chain_family — the pool name says
        which chain a provider fakes."""
        _, body = _get(_ctrl(sim, "/scenario"))
        for pid in ("1", "2", "3"):
            snap = body["providers"][f"ln-sim:{pid}"]
            assert "chain_family" not in snap
            assert snap["mode"] == "success"

    def test_ln_port_dispatches_ln_methods_with_no_scenario(self, sim):
        """No /scenario call at all — the LN port (18578) answers LN methods
        because the endpoint belongs to the ln-sim pool."""
        status, body = _rpc(_LN_URLS["1"], "getinfo")
        assert status == 200
        assert "error" not in body
        # LN getinfo carries identity_pubkey — proves the LN chain handled it.
        assert "identity_pubkey" in body["result"]

    def test_ln_dispatch_unchanged_by_scenario_config(self, sim):
        """Scenario config tunes behaviour, never dispatch: after touching
        ln-sim:1 the port still serves the LN shape."""
        _set_ln(sim, "1", latency_ms=0)
        status, body = _rpc(_LN_URLS["1"], "getinfo")
        assert status == 200
        assert "identity_pubkey" in body["result"]

    def test_reset_restores_ln_defaults(self, sim):
        _set_ln(sim, "1", mode="error")
        _post(_ctrl(sim, "/reset"), {})
        _, body = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["ln-sim:1"]["mode"] == "success"
        status, _ = _rpc(_LN_URLS["1"], "getinfo")
        assert status == 200

    def test_eth_method_on_ln_port_returns_null_result(self, sim):
        """A method unknown to the LN chain returns null result, not error —
        the simulator stays in success mode, the router sees an unfamiliar
        but well-formed response."""
        status, body = _rpc(_LN_URLS["1"], "eth_blockNumber")
        assert status == 200
        assert "error" not in body
        assert body["result"] is None

    def test_btc_method_on_ln_port_returns_null_result(self, sim):
        """The LN port serves the LN chain unconditionally; BTC method names
        aren't known to it, so the simulator stays in success mode and
        returns the null sentinel (BTC dispatch lives in the btc-sim pool)."""
        status, body = _rpc(_LN_URLS["1"], "getblockcount")
        assert status == 200
        assert "error" not in body
        assert body["result"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Happy-path stubs per LN method (covers all 6 stub-scoped methods)
# ─────────────────────────────────────────────────────────────────────────────


class TestLNDMethodDefaults:

    @pytest.mark.parametrize("method", ALL_LND_METHODS)
    def test_method_returns_success_envelope(self, sim, method):
        """Every method in LND_METHOD_DEFAULTS responds with a JSON-RPC result envelope."""
        status, body = _rpc(_LN_URLS["1"], method)
        assert status == 200, f"{method} returned HTTP {status}"
        assert body.get("jsonrpc") == "2.0", f"{method} missing jsonrpc=2.0"
        assert "error" not in body, f"{method} returned error: {body.get('error')!r}"
        assert "result" in body, f"{method} missing result"

    def test_getinfo_shape(self, sim):
        """getinfo carries the 8 required fields."""
        _, body = _rpc(_LN_URLS["1"], "getinfo")
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
        _, body = _rpc(_LN_URLS["1"], "listchannels")
        assert "channels" in body["result"]
        assert isinstance(body["result"]["channels"], list)
        assert len(body["result"]["channels"]) >= 1

    def test_listpeers_wraps_in_peers_key(self, sim):
        """Same wrapping convention as listchannels."""
        _, body = _rpc(_LN_URLS["1"], "listpeers")
        assert "peers" in body["result"]
        assert isinstance(body["result"]["peers"], list)
        assert len(body["result"]["peers"]) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# block_height shift on getinfo — mirrors BTC's getblockcount shift
# ─────────────────────────────────────────────────────────────────────────────


class TestGetInfoBlockHeight:

    def test_getinfo_block_height_default(self, sim):
        """At blocks_behind=0 the LN node reports the canonical BTC head."""
        _, body = _rpc(_LN_URLS["1"], "getinfo")
        assert body["result"]["block_height"] == 850_000

    def test_getinfo_block_height_shifts_with_blocks_behind(self, sim):
        """blocks_behind=10 → LN node reports 850_000 - 10 as its tracked BTC head."""
        _set_ln(sim, "1", blocks_behind=10)
        _, body = _rpc(_LN_URLS["1"], "getinfo")
        assert body["result"]["block_height"] == 850_000 - 10

    def test_getinfo_synced_to_chain_flips_when_lagged(self, sim):
        """Any positive blocks_behind flips synced_to_chain to False."""
        _set_ln(sim, "1", blocks_behind=5)
        _, body = _rpc(_LN_URLS["1"], "getinfo")
        assert body["result"]["synced_to_chain"] is False

    def test_getinfo_synced_to_chain_true_when_caught_up(self, sim):
        """At blocks_behind=0 the node reports synced_to_chain=True."""
        _, body = _rpc(_LN_URLS["1"], "getinfo")
        assert body["result"]["synced_to_chain"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Param echo behaviour — decodepayreq / payinvoice / openchannel
# ─────────────────────────────────────────────────────────────────────────────


class TestParamEcho:

    def test_decodepayreq_echoes_invoice(self, sim):
        """The simulator stashes the requested invoice in payment_request so
        tests can round-trip without lifting real bolt11 bytes."""
        invoice = "lnbcrt100u1psim_test_invoice"
        _, body = _rpc(_LN_URLS["1"], "decodepayreq", [invoice])
        assert body["result"]["payment_request"] == invoice

    def test_payinvoice_echoes_invoice(self, sim):
        """payinvoice echoes the invoice into payment_request alongside the
        preimage + hash so /history correlation matches the request."""
        invoice = "lnbcrt500u1psim_pay_invoice"
        _, body = _rpc(_LN_URLS["1"], "payinvoice", [invoice])
        assert body["result"]["payment_request"] == invoice
        # Default payment shape still present.
        assert body["result"]["payment_preimage"]
        assert body["result"]["payment_hash"]

    def test_openchannel_echoes_node_pubkey(self, sim):
        """openchannel echoes the remote pubkey from params[0] into node_pubkey."""
        peer_pk = "03" + "ff" * 32
        _, body = _rpc(_LN_URLS["1"], "openchannel", [peer_pk, 250_000])
        assert body["result"]["node_pubkey"] == peer_pk
        # funding_txid_str still present from the default stub.
        assert body["result"]["funding_txid_str"]


# ─────────────────────────────────────────────────────────────────────────────
# Fault-injection primitives applied on an LN provider — covers 4 primitives
# (hang, dropped, corrupt, status).
# ─────────────────────────────────────────────────────────────────────────────


class TestLNFaultInjection:
    """Faults are addressed at ln-sim:<pid> directly — the provider owns its
    endpoints, so no gating field is needed for them to fire."""

    def test_hang_mode_on_ln_provider(self, sim):
        """mode=hang on ln-sim:1 hangs the LN listener."""
        _set_ln(sim, "1", mode="hang")
        t0 = time.monotonic()
        try:
            _rpc(_LN_URLS["1"], "getinfo")
        except (urllib.error.URLError, ConnectionResetError, OSError):
            pass  # 30s sleep → client times out at 5s
        elapsed = time.monotonic() - t0
        # We don't wait 30s; just confirm the client timed out (≥4s, well
        # above the latency_ms=0 fast path) — actual cap is urlopen timeout.
        assert elapsed >= 4, f"hang should block at least the client timeout, got {elapsed:.2f}s"

    @pytest.mark.parametrize("drop_at", ["before_headers", "after_headers", "mid_body"])
    def test_drop_connection_at_each_point_on_ln(self, sim, drop_at):
        """All 3 drop points close the connection on an LN provider.

        The failure must be one of the connection-drop manifestations the eth
        reference suite (test_simulator.py::TestDropConnection) pins — an
        unrelated failure class (a helper bug, a parse error) must not pass.
        """
        _set_ln(sim, "2", mode="drop_connection", drop_at=drop_at)
        try:
            _rpc(_LN_URLS["2"], "getinfo")
            observed = "OK"
        except Exception as exc:  # capture the class name; asserted below
            observed = type(exc).__name__
        assert observed != "OK", "expected a connection-drop error, got a valid response"
        assert any(
            name in observed
            for name in (
                "RemoteDisconnected",
                "BadStatusLine",
                "URLError",
                "ConnectionResetError",
                "IncompleteRead",
            )
        ), f"unexpected error class for {drop_at} drop: {observed}"

    @pytest.mark.parametrize(
        "drop_at,expect",
        [
            ("before_headers", "no_bytes"),
            ("after_headers", "headers_only"),
            ("mid_body", "partial_body"),
        ],
    )
    def test_drop_point_visible_on_the_socket_ln(self, sim, drop_at, expect):
        """Each drop point emits its documented byte shape before closing.

        before_headers: EOF with zero bytes — no status line is ever sent.
        after_headers: a complete header block promising a 100-byte body,
        then EOF with no body byte. mid_body: the same header block plus a
        partial body strictly shorter than the promised length, then EOF.
        A raw socket is used because urllib collapses all three into an
        exception, which cannot tell the points apart.
        """
        _set_ln(sim, "2", mode="drop_connection", drop_at=drop_at)
        raw = _raw_post_bytes(LN_PRIMARY_PORTS["2"], "getinfo")
        if expect == "no_bytes":
            assert raw == b"", f"before_headers must emit nothing, got {raw[:80]!r}"
            return
        head, sep, body_part = raw.partition(b"\r\n\r\n")
        assert sep, f"expected a complete header block, got {raw[:80]!r}"
        assert b" 200 " in head.split(b"\r\n", 1)[0], f"no 200 status line in {head[:80]!r}"
        assert b"Content-Length: 100" in head, f"promised length missing from {head!r}"
        if expect == "headers_only":
            assert body_part == b"", f"after_headers must send no body byte, got {body_part!r}"
        else:
            assert 0 < len(body_part) < 100, (
                f"mid_body must send a strict subset of the promised 100 bytes, "
                f"got {len(body_part)} bytes"
            )

    def test_corrupt_response_on_ln(self, sim):
        """corruption_mode=invalid_json on ln-sim:3 yields un-parseable bytes."""
        _set_ln(sim, "3", corruption_mode="invalid_json")
        # Build the request manually so we read raw bytes without json.loads.
        req = urllib.request.Request(
            f"{_LN_URLS['3']}/",
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
        """mode=error + http_status=502 must propagate the custom HTTP status."""
        _set_ln(
            sim,
            "1",
            mode="error",
            http_status=502,
            error_code=-32000,
            error_message="upstream unavailable",
        )
        status, body = _rpc(_LN_URLS["1"], "getinfo")
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
        _, body = _rpc(_LN_URLS["1"], "payinvoice", ["lnbcrt_bogus"])
        assert "error" in body
        assert "find a path" in body["error"]["message"]

    def test_ln_error_stub_raw_envelope(self, sim):
        """Escape hatch: responses[method] = {"error": {...}} bypasses the catalogue."""
        _set_ln(
            sim,
            "1",
            responses={"openchannel": {"error": {"code": -99, "message": "Custom LN error"}}},
        )
        _, body = _rpc(_LN_URLS["1"], "openchannel", ["02deadbeef" + "00" * 28, 100_000])
        assert body["error"]["code"] == -99
        assert body["error"]["message"] == "Custom LN error"

    def test_ln_method_unaffected_by_other_method_error(self, sim):
        """Per-method overrides scope strictly to that method."""
        _set_ln(sim, "1", responses={"payinvoice": {"error_stub": "no_route"}})
        # The unrelated method must still succeed.
        _, body = _rpc(_LN_URLS["1"], "getinfo")
        assert "result" in body
        assert "error" not in body


# ─────────────────────────────────────────────────────────────────────────────
# Cross-pool independence — eth-sim:1 and ln-sim:1 are different providers
# ─────────────────────────────────────────────────────────────────────────────


class TestMixedChainScenario:
    """eth-sim:1 (18545) and ln-sim:1 (18578) are separate providers in
    separate pools; a fault on one can never reach the other."""

    def test_eth_and_ln_listeners_independent_with_no_scenario(self, sim):
        """No scenario at all — the ETH port answers ETH and the LN port LN."""
        _, eth_body = _rpc(_ETH1, "eth_blockNumber")
        _, ln_body = _rpc(_LN_URLS["1"], "getinfo")

        # ETH: hex string with "0x" prefix.
        assert isinstance(eth_body["result"], str)
        assert eth_body["result"].startswith("0x")
        # LN: dict with identity_pubkey.
        assert isinstance(ln_body["result"], dict)
        assert "identity_pubkey" in ln_body["result"]

    def test_eth_listener_unaffected_by_ln_fault(self, sim):
        """A rate_limit on ln-sim:1 fires on the LN port and leaves eth-sim:1
        serving success — different pools share nothing."""
        _post(
            _ctrl(sim, "/scenario"),
            {"providers": {"ln-sim:1": {"mode": "rate_limit"}}},
        )
        eth_status, eth_body = _rpc(_ETH1, "eth_blockNumber")
        ln_status, _ = _rpc(_LN_URLS["1"], "getinfo")

        assert eth_status == 200, f"eth-sim:1 should ignore an ln-sim fault; got {eth_status}"
        assert "result" in eth_body
        assert ln_status == 429, f"ln-sim:1 should rate-limit; got {ln_status}"

    def test_ln_blocks_behind_leaves_eth_untouched(self, sim):
        """blocks_behind on ln-sim:1 shifts the LN block_height reported by
        getinfo; eth-sim:1 keeps its own head."""
        _post(
            _ctrl(sim, "/scenario"),
            {"providers": {"ln-sim:1": {"blocks_behind": 50}}},
        )
        _, ln_body = _rpc(_LN_URLS["1"], "getinfo")
        assert ln_body["result"]["block_height"] == 850_000 - 50
        _, eth_body = _rpc(_ETH1, "eth_blockNumber")
        assert eth_body["result"].lower() == "0x1312d00"


# ─────────────────────────────────────────────────────────────────────────────
# History tracking — LN requests must show up in /history like ETH/BTC ones
# ─────────────────────────────────────────────────────────────────────────────


class TestLNHistoryTracking:

    def test_ln_request_recorded_in_history(self, sim):
        _rpc(_LN_URLS["1"], "getinfo")
        _, hist = _get(_ctrl(sim, "/history?pool=ln-sim&pid=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "getinfo"
        assert last["status"] == "success"
        assert last["pool"] == "ln-sim"
        assert last["port"] == LN_PRIMARY_PORTS["1"]

    def test_ln_history_filter_by_method(self, sim):
        """?method= filters work for LN method names just like ETH/BTC ones."""
        _rpc(_LN_URLS["1"], "getinfo")
        _rpc(_LN_URLS["1"], "listpeers")
        _, hist = _get(_ctrl(sim, "/history?method=getinfo"))
        assert hist["count"] >= 1
        assert all(e["method"] == "getinfo" for e in hist["history"])

    def test_ln_error_status_recorded(self, sim):
        """Per-method error_stub on an LN method produces status=error in history."""
        _set_ln(sim, "1", responses={"payinvoice": {"error_stub": "no_route"}})
        _rpc(_LN_URLS["1"], "payinvoice", ["lnbcrt_bogus"])
        _, hist = _get(_ctrl(sim, "/history?pool=ln-sim&pid=1&status=error"))
        assert hist["count"] >= 1
        assert hist["history"][-1]["method"] == "payinvoice"
