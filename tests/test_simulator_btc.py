"""
Integration tests for the Bitcoin pool of the provider simulator.

Runs against the shared in-process simulator (see conftest.py): the btc-sim
pool listens on 18575-18577 and the eth-sim pool on 18545-18547. Under the
pool:pid model those are SEPARATE providers — btc-sim:1 and eth-sim:1 share no
state, so cross-pool isolation is structural, not gated.

Coverage:
  Happy-path per BTC method        — every method in BTC_METHOD_DEFAULTS
                                      responds with a JSON-RPC result envelope.
  Fault primitives                  — hang / drop / stale / corrupt / status
                                      all apply identically on a BTC provider.
  Cross-pool isolation              — a fault on eth-sim:1 never touches
                                      btc-sim:1 (and vice versa).
  Block-hash format                  — bitcoind's 64-lower-hex, no "0x" prefix.
  Decimal-vs-hex height handling     — getblockcount returns int, not hex string.
  History tracking                   — BTC requests show up in /history exactly
                                      like ETH ones, with method name preserved.

Run with:
  pytest tests/test_simulator_btc.py -v
"""

import json
import re
import socket
import time
import urllib.error
import urllib.request

import pytest

from provider_simulator.topology import port_of
from stubs_btc import BTC_METHOD_DEFAULTS

_BTC1 = f"http://127.0.0.1:{port_of('btc-sim', '1')}"
_ETH1 = f"http://127.0.0.1:{port_of('eth-sim', '1')}"

# 29 BTC methods covered by the stub set. Source of truth: stubs_btc.py.
ALL_BTC_METHODS = sorted(BTC_METHOD_DEFAULTS.keys())


# ── HTTP helpers (kept independent of test_simulator.py — duplication is
#     intentional so the files stay self-contained). ──────────────────────────


def _parse_body(raw: bytes) -> dict | str:
    """JSON-decode ``raw``, falling back to the decoded text when it isn't
    JSON — the rate_limit fault's prose body is not, by design (see
    provider_simulator/listeners/jsonrpc.py)."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode()


def _post(url: str, body: dict) -> tuple[int, dict | str]:
    """POST JSON body, return (status_code, parsed_response_body)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, _parse_body(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, _parse_body(e.read())
        except (ConnectionResetError, OSError):
            return e.code, {}


def _get(url: str) -> tuple[int, dict | str]:
    """GET url, return (status_code, parsed_response_body)."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, _parse_body(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse_body(e.read())


def _rpc(url: str, method: str, params: list | None = None) -> tuple[int, dict | str]:
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


# ── Helper to apply per-provider scenario config ──────────────────────────────


def _set_btc(sim, pid: str = "1", **extra):
    """Convenience: POST /scenario for one btc-sim provider. Extra kwargs are
    the provider block (mode, latency_ms, blocks_behind, responses, ...)."""
    return _post(_ctrl(sim, "/scenario"), {"providers": {f"btc-sim:{pid}": dict(extra)}})


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


class TestBTCPortDispatch:

    def test_scenario_snapshot_has_no_chain_family_field(self, sim):
        """The pool:pid model carries no chain_family — the pool name says
        which chain a provider fakes, and GET /scenario must not resurrect
        the old field."""
        _, body = _get(_ctrl(sim, "/scenario"))
        for pid in ("1", "2", "3"):
            snap = body["providers"][f"btc-sim:{pid}"]
            assert "chain_family" not in snap
            assert snap["mode"] == "success"

    def test_btc_port_dispatches_btc_methods_with_no_scenario(self, sim):
        """No /scenario call at all — the BTC port (18575) answers BTC methods
        because the endpoint belongs to the btc-sim pool."""
        status, body = _rpc(_BTC1, "getblockcount")
        assert status == 200
        assert "error" not in body
        # Decimal int — bitcoind convention — proves the BTC chain handled it.
        assert isinstance(body["result"], int)
        assert body["result"] == 850_000

    def test_chain_family_field_is_rejected_with_400(self, sim):
        """The old wire format fails loudly: a block carrying chain_family gets
        a 400 whose message points at the new addressing."""
        status, body = _post(
            _ctrl(sim, "/scenario"),
            {"providers": {"btc-sim:1": {"chain_family": "eth", "mode": "error"}}},
        )
        assert status == 400
        assert "chain_family" in body["error"]
        # And nothing was applied — the provider still serves success.
        rpc_status, rpc_body = _rpc(_BTC1, "getblockcount")
        assert rpc_status == 200
        assert rpc_body["result"] == 850_000

    def test_bare_pid_key_is_rejected_with_400(self, sim):
        """The old bare-pid provider key fails loudly with a 400 naming the
        pool:pid format."""
        status, body = _post(_ctrl(sim, "/scenario"), {"providers": {"1": {"mode": "down"}}})
        assert status == 400
        assert "pool:pid" in body["error"]

    def test_eth_request_on_btc_port_returns_null_result(self, sim):
        """A method unknown to the BTC chain returns null result, not error —
        the simulator stays in success mode, the router sees an unfamiliar
        but well-formed response."""
        status, body = _rpc(_BTC1, "eth_blockNumber")
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
        status, body = _rpc(_BTC1, method)
        assert status == 200, f"{method} returned HTTP {status}"
        assert body.get("jsonrpc") == "2.0", f"{method} missing jsonrpc=2.0"
        assert "error" not in body, f"{method} returned error: {body.get('error')!r}"
        assert "result" in body, f"{method} missing result"


class TestGetBlockCountReturnsDecimal:

    def test_getblockcount_is_int_not_hex(self, sim):
        """bitcoind returns block heights as JSON numbers, not hex strings."""
        _, body = _rpc(_BTC1, "getblockcount")
        assert isinstance(
            body["result"], int
        ), f"getblockcount must return int, got {type(body['result']).__name__}: {body['result']!r}"

    def test_getblockcount_default_is_realistic_mainnet_height(self, sim):
        """The default head is pinned to BTC_LATEST_BLOCK (850_000) so tests can assert."""
        _, body = _rpc(_BTC1, "getblockcount")
        assert body["result"] == 850_000

    def test_getblockcount_shifts_with_blocks_behind(self, sim):
        """blocks_behind=10 → head reported is 850_000 - 10."""
        _set_btc(sim, "1", blocks_behind=10)
        _, body = _rpc(_BTC1, "getblockcount")
        assert body["result"] == 850_000 - 10


class TestBlockHashFormat:

    HEX64 = re.compile(r"^[0-9a-f]{64}$")

    def test_getblockhash_returns_64_lowerhex_no_prefix(self, sim):
        """BTC contract: 64 lowercase hex chars, no 0x prefix."""
        _, body = _rpc(_BTC1, "getblockhash", [850_000])
        h = body["result"]
        assert isinstance(h, str), f"hash must be str, got {type(h).__name__}"
        assert self.HEX64.match(h), f"hash {h!r} doesn't match 64 lower-hex pattern"
        assert not h.startswith("0x"), "BTC hashes do not carry the 0x prefix"

    def test_getblockhash_is_deterministic_per_height(self, sim):
        """Same height → same hash (lets tests pin against synthesised values)."""
        _, body1 = _rpc(_BTC1, "getblockhash", [12345])
        _, body2 = _rpc(_BTC1, "getblockhash", [12345])
        assert body1["result"] == body2["result"]

    def test_getblockhash_different_heights_distinct_hashes(self, sim):
        _, body1 = _rpc(_BTC1, "getblockhash", [100])
        _, body2 = _rpc(_BTC1, "getblockhash", [200])
        assert body1["result"] != body2["result"]

    def test_getbestblockhash_format(self, sim):
        _, body = _rpc(_BTC1, "getbestblockhash")
        h = body["result"]
        assert self.HEX64.match(h)


class TestGetBlockEcho:

    def test_getblock_echoes_requested_hash(self, sim):
        """Like eth_getBlockByNumber on the ETH side — the simulator echoes the
        request param so the router's pruning verification sees a matching
        block identifier in the response."""
        requested = "ab" * 32
        _, body = _rpc(_BTC1, "getblock", [requested])
        assert body["result"]["hash"] == requested

    def test_getblockheader_echoes_requested_hash(self, sim):
        requested = "cd" * 32
        _, body = _rpc(_BTC1, "getblockheader", [requested])
        assert body["result"]["hash"] == requested

    def test_getblockchaininfo_blocks_shifts_with_blocks_behind(self, sim):
        _set_btc(sim, "1", blocks_behind=5)
        _, body = _rpc(_BTC1, "getblockchaininfo")
        assert body["result"]["blocks"] == 850_000 - 5
        assert body["result"]["headers"] == 850_000 - 5


# ─────────────────────────────────────────────────────────────────────────────
# Fault-injection primitives applied on a BTC provider
# ─────────────────────────────────────────────────────────────────────────────


class TestBTCFaultInjection:
    """Faults are addressed at btc-sim:<pid> directly — the provider owns its
    endpoints, so no gating field is needed for them to fire."""

    def test_hang_mode_on_btc_provider(self, sim):
        """mode=hang on btc-sim:1 hangs the BTC listener."""
        _set_btc(sim, "1", mode="hang")
        t0 = time.monotonic()
        try:
            _rpc(_BTC1, "getblockcount")
        except (urllib.error.URLError, ConnectionResetError, OSError):
            pass  # 30s sleep → client times out at 5s
        elapsed = time.monotonic() - t0
        # We don't wait 30s; just confirm the client timed out (≥4s, well above
        # the latency_ms=0 fast path) — actual cap is the urlopen timeout.
        assert elapsed >= 4, f"hang should block at least the client timeout, got {elapsed:.2f}s"

    @pytest.mark.parametrize("drop_at", ["before_headers", "after_headers", "mid_body"])
    def test_drop_connection_at_each_point_on_btc(self, sim, drop_at):
        """All 3 drop points close the connection on a BTC provider.

        The failure must be one of the connection-drop manifestations the eth
        reference suite (test_simulator.py::TestDropConnection) pins — an
        unrelated failure class (a helper bug, a parse error) must not pass.
        """
        _set_btc(sim, "1", mode="drop_connection", drop_at=drop_at)
        try:
            _rpc(_BTC1, "getblockcount")
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
    def test_drop_point_visible_on_the_socket_btc(self, sim, drop_at, expect):
        """Each drop point emits its documented byte shape before closing.

        before_headers: EOF with zero bytes — no status line is ever sent.
        after_headers: a complete header block promising a 100-byte body,
        then EOF with no body byte. mid_body: the same header block plus a
        partial body strictly shorter than the promised length, then EOF.
        A raw socket is used because urllib collapses all three into an
        exception, which cannot tell the points apart.
        """
        _set_btc(sim, "1", mode="drop_connection", drop_at=drop_at)
        raw = _raw_post_bytes(port_of("btc-sim", "1"), "getblockcount")
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
                f"mid_body must send a strict subset of the promised 100 bytes, " f"got {len(body_part)} bytes"
            )

    def test_stale_blocks_behind_on_btc(self, sim):
        """blocks_behind=100 → BTC head reports 100 below the canonical chain head."""
        _set_btc(sim, "1", blocks_behind=100)
        _, body = _rpc(_BTC1, "getblockcount")
        assert body["result"] == 850_000 - 100

    def test_corrupt_response_on_btc(self, sim):
        """corruption_mode=invalid_json on btc-sim:1 yields un-parseable bytes."""
        _set_btc(sim, "1", corruption_mode="invalid_json")
        # Build the request manually so we read raw bytes without json.loads.
        req = urllib.request.Request(
            f"{_BTC1}/",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getblockcount", "params": []}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)

    def test_status_override_on_btc(self, sim):
        """mode=error + http_status=502 must propagate the custom HTTP status."""
        _set_btc(sim, "1", mode="error", http_status=502, error_code=-5, error_message="Block not found")
        status, body = _rpc(_BTC1, "getblockhash", [999_999_999])
        assert status == 502
        assert body["error"]["code"] == -5


# ─────────────────────────────────────────────────────────────────────────────
# Per-method error overrides on a BTC provider
# ─────────────────────────────────────────────────────────────────────────────


class TestBTCErrorStubs:

    def test_btc_error_stub_named_lookup(self, sim):
        """responses[method] = {"error_stub": <name>} routes to BTC_ERROR_STUBS."""
        _set_btc(sim, "1", responses={"getblockhash": {"error_stub": "block_not_found"}})
        _, body = _rpc(_BTC1, "getblockhash", [999_999])
        assert body["error"]["code"] == -5
        assert "Block not found" in body["error"]["message"]

    def test_btc_error_stub_raw_envelope(self, sim):
        """Escape hatch: responses[method] = {"error": {...}} bypasses the catalogue."""
        _set_btc(
            sim,
            "1",
            responses={"sendrawtransaction": {"error": {"code": -99, "message": "Custom"}}},
        )
        _, body = _rpc(_BTC1, "sendrawtransaction", ["deadbeef"])
        assert body["error"]["code"] == -99
        assert body["error"]["message"] == "Custom"

    def test_btc_method_unaffected_by_other_method_error(self, sim):
        """Per-method overrides scope strictly to that method."""
        _set_btc(sim, "1", responses={"getblockhash": {"error_stub": "block_not_found"}})
        # The unrelated method must still succeed.
        _, body = _rpc(_BTC1, "getblockcount")
        assert "result" in body
        assert "error" not in body


# ─────────────────────────────────────────────────────────────────────────────
# Cross-pool independence — eth-sim:1 and btc-sim:1 are different providers
# ─────────────────────────────────────────────────────────────────────────────


class TestMixedChainScenario:
    """eth-sim:1 (18545) and btc-sim:1 (18575) are separate providers in
    separate pools. One /scenario body can configure both, and a fault on one
    can never reach the other — isolation is structural."""

    def test_eth_and_btc_listeners_independent_with_no_scenario(self, sim):
        """No scenario at all — the ETH port answers ETH and the BTC port
        answers BTC."""
        _, eth_body = _rpc(_ETH1, "eth_blockNumber")
        _, btc_body = _rpc(_BTC1, "getblockcount")

        # ETH side: hex string with "0x" prefix.
        assert isinstance(eth_body["result"], str)
        assert eth_body["result"].startswith("0x")
        # BTC side: decimal integer.
        assert isinstance(btc_body["result"], int)
        assert btc_body["result"] == 850_000

    def test_eth_listener_unaffected_by_btc_fault(self, sim):
        """A rate_limit on btc-sim:1 fires on the BTC port and leaves
        eth-sim:1 serving success — different pools share nothing."""
        _post(
            _ctrl(sim, "/scenario"),
            {"providers": {"btc-sim:1": {"mode": "rate_limit"}}},
        )
        eth_status, eth_body = _rpc(_ETH1, "eth_blockNumber")
        btc_status, _ = _rpc(_BTC1, "getblockcount")

        assert eth_status == 200, f"eth-sim:1 should ignore a btc-sim fault; got {eth_status}"
        assert "result" in eth_body
        assert btc_status == 429, f"btc-sim:1 should rate-limit; got {btc_status}"

    def test_one_scenario_body_configures_both_pools(self, sim):
        """A single /scenario POST can address providers in different pools."""
        status, body = _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "btc-sim:1": {"blocks_behind": 50},
                    "eth-sim:1": {"latency_ms": 0},
                }
            },
        )
        assert status == 200
        assert set(body["applied"]) == {"btc-sim:1", "eth-sim:1"}
        _, btc_body = _rpc(_BTC1, "getblockcount")
        assert btc_body["result"] == 850_000 - 50


# ─────────────────────────────────────────────────────────────────────────────
# History tracking — BTC requests must show up in /history like ETH ones
# ─────────────────────────────────────────────────────────────────────────────


class TestBTCHistoryTracking:

    def test_btc_request_recorded_in_history(self, sim):
        _rpc(_BTC1, "getblockcount")
        _, hist = _get(_ctrl(sim, "/history?pool=btc-sim&pid=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "getblockcount"
        assert last["status"] == "success"
        assert last["pool"] == "btc-sim"
        assert last["port"] == port_of("btc-sim", "1")

    def test_btc_history_filter_by_method(self, sim):
        """?method= filters work for BTC method names just like ETH ones."""
        _rpc(_BTC1, "getblockcount")
        _rpc(_BTC1, "getbestblockhash")
        _, hist = _get(_ctrl(sim, "/history?method=getblockcount"))
        assert hist["count"] >= 1
        assert all(e["method"] == "getblockcount" for e in hist["history"])

    def test_btc_error_status_recorded(self, sim):
        """Per-method error_stub on a BTC method produces status=error in history."""
        _set_btc(sim, "1", responses={"getblockhash": {"error_stub": "block_not_found"}})
        _rpc(_BTC1, "getblockhash", [999_999])
        _, hist = _get(_ctrl(sim, "/history?pool=btc-sim&pid=1&status=error"))
        assert hist["count"] >= 1
        assert hist["history"][-1]["method"] == "getblockhash"


# ─────────────────────────────────────────────────────────────────────────────
# Cross-pool isolation — a down on eth-sim can never reach btc-sim
# ─────────────────────────────────────────────────────────────────────────────


class TestBTCCrossPoolIsolation:
    """Under the old bare-pid model, eth pid "1" and btc pid "1" were ONE
    state object, so an eth-authored down also killed the BTC port. The
    pool:pid model abolishes that: these tests pin the isolation."""

    def test_btc_unaffected_by_eth_down_fault(self, sim):
        """mode=down on eth-sim:1 kills every eth-sim:1 endpoint and nothing
        else — btc-sim:1 keeps serving success."""
        _post(_ctrl(sim, "/scenario"), {"providers": {"eth-sim:1": {"mode": "down"}}})

        eth_status, _ = _rpc(_ETH1, "eth_blockNumber")
        assert eth_status == 503, f"eth-sim:1 must be down; got {eth_status}"

        btc_status, btc_body = _rpc(_BTC1, "getblockcount")
        assert btc_status == 200, f"btc-sim:1 must be untouched by an eth-sim down; got {btc_status}"
        assert btc_body["result"] == 850_000

    def test_btc_stays_healthy_through_eth_down_window(self, sim):
        """A sequenced down (fail_first_n) on eth-sim:1 opens and closes its
        window on eth-sim:1 alone. btc-sim:1 serves success before, during,
        and after — it neither observes nor advances another pool's window."""
        _post(
            _ctrl(sim, "/scenario"),
            {"providers": {"eth-sim:1": {"mode": "down", "fail_first_n": 2, "then_mode": "success"}}},
        )

        status, body = _rpc(_BTC1, "getblockcount")
        assert status == 200, f"btc-sim:1 must be healthy while eth's window is open; got {status}"
        assert body["result"] == 850_000

        for i in (1, 2):
            eth_status, _ = _rpc(_ETH1, "eth_blockNumber")
            assert eth_status == 503, f"eth-sim:1 call {i} is inside the down window; got {eth_status}"

        eth_status, _ = _rpc(_ETH1, "eth_blockNumber")
        assert eth_status == 200, f"eth-sim:1 must recover after the window; got {eth_status}"

        status, body = _rpc(_BTC1, "getblockcount")
        assert status == 200, f"btc-sim:1 must still be healthy after eth's window; got {status}"
        assert isinstance(body.get("result"), int)
