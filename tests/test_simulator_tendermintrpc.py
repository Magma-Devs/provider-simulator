"""Unit tests for the Tendermint-RPC chain dispatch in the provider simulator (MAG-1841).

Mirrors ``tests/test_simulator_rest.py`` (REST) but covers Tendermint-RPC-specific
behaviour:

  chain_family on /scenario   — ``"tendermintrpc"`` is accepted and round-trips
                                through GET /scenario.
  Verb dispatch               — GET (URI form) and POST (JSON-RPC body) both
                                hit the right pipeline and return JSON-RPC
                                envelopes.
  Method coverage             — each of the 7 v1 methods (status / health /
                                abci_info / block / validators / abci_query /
                                net_info) returns a non-empty 200 envelope.
  Param normalisation         — GET (quoted strings) and POST (plain values)
                                produce the same handler input.
  Echo / pagination           — block echoes the requested height; validators
                                paginates by page/per_page.
  Fault primitives            — down / error / corruption_mode all apply on
                                a Tendermint provider.
  Mixed-chain                 — one ETH (JSON-RPC) + one Tendermint provider
                                in the same /scenario body, each independently
                                faulted.

Run with:
  pytest tests/test_simulator_tendermintrpc.py -v
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import HTTPServer, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

import pytest

from server import (
    ControlHandler,
    JSONRPCHandler,
    ProviderState,
    TendermintHandler,
)
from stubs_tendermintrpc import TENDERMINT_ERROR_STUBS, TENDERMINT_METHOD_DEFAULTS


# Test ports — distinct from the other suites' ranges so all four can co-exist
# if run in parallel. ETH uses 28xxx, BTC uses 38xxx, REST uses 48xxx; TM gets 58xxx.
_PROVIDER_PORTS = {"1": 58545, "2": 58546, "3": 58547}
_TM_PORTS       = {"1": 58554, "2": 58555, "3": 58556}
_CONTROL_PORT   = 59000


# ── HTTP helpers (independent of the other test files — duplication intentional) ──


def _request(
    method: str,
    url: str,
    body: Optional[dict] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Any, Dict[str, str]]:
    """Send an HTTP request and return (status, parsed_body, response_headers)."""
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            try:
                parsed = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = raw
            return resp.status, parsed, dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
            parsed = json.loads(raw) if raw else {}
        except (ConnectionResetError, OSError, json.JSONDecodeError):
            parsed = {}
        return e.code, parsed, dict(e.headers)


def _ctrl(sim: dict, path: str) -> str:
    return sim["control"] + path


def _tm_post(sim: dict, pid: str, method: str, params: Optional[Any] = None, request_id: int = 1):
    """POST a JSON-RPC body to a TM sim port."""
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return _request("POST", sim[f"tm{pid}"], body=body)


def _tm_get(sim: dict, pid: str, method: str, params: Optional[Dict[str, str]] = None):
    """GET the TM URI form. ``params`` values are URL-encoded as-is (callers
    decide whether to wrap strings in quotes per CometBFT's GET form)."""
    url = sim[f"tm{pid}"] + f"/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _request("GET", url)


# ── Module-scoped fixture: start all servers once ─────────────────────────────


@pytest.fixture(scope="module")
def sim():
    """Start 3 JSON-RPC + 3 Tendermint + 1 control server on dedicated test ports.

    JSON-RPC servers are started so mixed-chain tests can exercise both
    transports against the same ProviderState dict.
    """
    states = {pid: ProviderState() for pid in _PROVIDER_PORTS}

    servers = []
    for pid, port in _PROVIDER_PORTS.items():
        srv = ThreadingHTTPServer(("127.0.0.1", port), JSONRPCHandler)
        srv.daemon_threads = True
        srv.state = states[pid]
        srv.provider_id = pid
        servers.append(srv)

    for pid, port in _TM_PORTS.items():
        srv = ThreadingHTTPServer(("127.0.0.1", port), TendermintHandler)
        srv.daemon_threads = True
        srv.state = states[pid]
        srv.provider_id = pid
        servers.append(srv)

    ctrl = HTTPServer(("127.0.0.1", _CONTROL_PORT), ControlHandler)
    ctrl.provider_states = states
    servers.append(ctrl)

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()

    time.sleep(0.15)

    yield {
        "control":  f"http://127.0.0.1:{_CONTROL_PORT}",
        "tm1":      f"http://127.0.0.1:{_TM_PORTS['1']}",
        "tm2":      f"http://127.0.0.1:{_TM_PORTS['2']}",
        "tm3":      f"http://127.0.0.1:{_TM_PORTS['3']}",
        "jsonrpc1": f"http://127.0.0.1:{_PROVIDER_PORTS['1']}",
        "jsonrpc2": f"http://127.0.0.1:{_PROVIDER_PORTS['2']}",
    }

    for s in servers:
        s.shutdown()


def _set_tm(sim, pid: str = "1", **extra):
    """POST /scenario to put a provider into ``chain_family="tendermintrpc"``."""
    cfg = {"chain_family": "tendermintrpc", **extra}
    return _request("POST", _ctrl(sim, "/scenario"), body={"providers": {pid: cfg}})


@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario config AND clear history before and after every test."""
    _request("POST", _ctrl(sim, "/reset/all"), body={})
    yield
    _request("POST", _ctrl(sim, "/reset/all"), body={})


# ─────────────────────────────────────────────────────────────────────────────
# Chain-family routing
# ─────────────────────────────────────────────────────────────────────────────


class TestTmChainFamily:

    def test_chain_family_tendermintrpc_accepted_on_scenario(self, sim):
        """``chain_family="tendermintrpc"`` round-trips through /scenario."""
        _set_tm(sim, "1")
        _, body, _ = _request("GET", _ctrl(sim, "/scenario"))
        assert body["providers"]["1"]["chain_family"] == "tendermintrpc"

    def test_other_providers_unchanged(self, sim):
        _set_tm(sim, "1")
        _, body, _ = _request("GET", _ctrl(sim, "/scenario"))
        # Default chain_family is "eth" for providers not explicitly set.
        assert body["providers"]["2"]["chain_family"] == "eth"
        assert body["providers"]["3"]["chain_family"] == "eth"

    def test_reset_clears_chain_family(self, sim):
        _set_tm(sim, "1")
        _request("POST", _ctrl(sim, "/reset"), body={})
        _, body, _ = _request("GET", _ctrl(sim, "/scenario"))
        assert body["providers"]["1"]["chain_family"] == "eth"


# ─────────────────────────────────────────────────────────────────────────────
# Verb dispatch — POST + GET both produce JSON-RPC envelopes
# ─────────────────────────────────────────────────────────────────────────────


class TestTmVerbDispatch:

    def test_post_status_returns_envelope(self, sim):
        _set_tm(sim, "1")
        status, body, _ = _tm_post(sim, "1", "status")
        assert status == 200
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 1
        assert "result" in body
        assert body["result"]["node_info"]["network"]  # stub-defined value

    def test_get_status_returns_envelope(self, sim):
        _set_tm(sim, "1")
        status, body, _ = _tm_get(sim, "1", "status")
        assert status == 200
        assert body["jsonrpc"] == "2.0"
        assert "result" in body
        assert body["result"]["node_info"]["network"]

    def test_post_empty_body_returns_parse_error(self, sim):
        _set_tm(sim, "1")
        # POST with no body — should return JSON-RPC -32700.
        status, body, _ = _request("POST", sim["tm1"], body=None)
        # ``urllib.request`` sends GET when body is None; force it via _request
        # with method="POST" + Content-Length: 0.
        # Workaround: hit POST via raw body but empty dict — that's a different
        # branch (missing method). Instead test missing-method separately below.
        # This case effectively becomes a GET on / which has empty path.
        assert status == 400
        assert body.get("error", {}).get("code") == -32700

    def test_post_missing_method_returns_parse_error(self, sim):
        _set_tm(sim, "1")
        # POST a valid JSON body that lacks the ``method`` field.
        status, body, _ = _request("POST", sim["tm1"], body={"jsonrpc": "2.0", "id": 7})
        assert status == 400
        assert body.get("error", {}).get("code") == -32700

    def test_get_root_path_returns_parse_error(self, sim):
        _set_tm(sim, "1")
        # GET / (no method in URI) → -32700 (empty path).
        status, body, _ = _request("GET", sim["tm1"] + "/")
        assert status == 400
        assert body.get("error", {}).get("code") == -32700

    def test_post_unknown_method_returns_method_not_found(self, sim):
        _set_tm(sim, "1")
        status, body, _ = _tm_post(sim, "1", "this_does_not_exist")
        assert status == 200  # JSON-RPC errors come on HTTP 200.
        assert body.get("error", {}).get("code") == -32601


# ─────────────────────────────────────────────────────────────────────────────
# Method coverage — each v1 method returns a non-empty envelope
# ─────────────────────────────────────────────────────────────────────────────


_V1_METHODS = sorted(TENDERMINT_METHOD_DEFAULTS.keys())


class TestTmMethodCoverage:

    @pytest.mark.parametrize("method", _V1_METHODS)
    def test_post_method_returns_200_envelope(self, sim, method):
        """Every v1 method returns HTTP 200 with a JSON-RPC envelope on POST."""
        _set_tm(sim, "1")
        status, body, _ = _tm_post(sim, "1", method)
        assert status == 200, f"{method}: expected HTTP 200, got {status}, body={body!r}"
        assert body["jsonrpc"] == "2.0", f"{method}: jsonrpc envelope missing"
        assert "result" in body, f"{method}: result key missing, body={body!r}"
        # ``health`` returns ``{}``; every other method must return a non-empty dict.
        if method == "health":
            assert body["result"] == {}
        else:
            assert isinstance(body["result"], dict) and body["result"], (
                f"{method}: result should be a non-empty dict, got {body['result']!r}"
            )

    @pytest.mark.parametrize("method", _V1_METHODS)
    def test_get_method_returns_200_envelope(self, sim, method):
        """Every v1 method also works via GET URI form."""
        _set_tm(sim, "1")
        status, body, _ = _tm_get(sim, "1", method)
        assert status == 200, f"{method}: GET status {status}, body={body!r}"
        assert body["jsonrpc"] == "2.0"
        assert "result" in body


# ─────────────────────────────────────────────────────────────────────────────
# Echo / pagination — request-time logic
# ─────────────────────────────────────────────────────────────────────────────


class TestTmRequestTimeLogic:

    def test_block_echoes_post_height(self, sim):
        """POST /  with method=block + params.height echoes the height back."""
        _set_tm(sim, "1")
        _, body, _ = _tm_post(sim, "1", "block", params={"height": "4500000"})
        assert body["result"]["block"]["header"]["height"] == "4500000"

    def test_block_echoes_get_height(self, sim):
        """GET /block?height="4500000" echoes the height back after normalisation."""
        _set_tm(sim, "1")
        # CometBFT GET URI form quotes string values.
        _, body, _ = _tm_get(sim, "1", "block", params={"height": '"4500000"'})
        assert body["result"]["block"]["header"]["height"] == "4500000"

    def test_abci_query_echoes_post_height(self, sim):
        """POST abci_query echoes height in response.height (the MAG-1741 contract)."""
        _set_tm(sim, "1")
        _, body, _ = _tm_post(
            sim, "1", "abci_query",
            params={"path": "/store/auth/key", "data": "", "height": "4500000", "prove": False},
        )
        envelope_keys = set(body["result"]["response"].keys())
        assert envelope_keys == {
            "code", "log", "info", "index", "key", "value", "proofOps", "height", "codespace",
        }
        assert body["result"]["response"]["height"] == "4500000"

    def test_abci_query_echoes_get_height(self, sim):
        """GET abci_query?height="H" echoes height after normalisation."""
        _set_tm(sim, "1")
        _, body, _ = _tm_get(
            sim, "1", "abci_query",
            params={
                "path": '"/store/auth/key"',
                "data": '""',
                "height": '"4500000"',
                "prove": "false",
            },
        )
        assert body["result"]["response"]["height"] == "4500000"

    def test_validators_pagination_post(self, sim):
        """POST validators?page=1&per_page=2 returns count=2, len(validators)=2."""
        _set_tm(sim, "1")
        _, body, _ = _tm_post(
            sim, "1", "validators",
            params={"page": "1", "per_page": "2"},
        )
        result = body["result"]
        assert result["count"] == "2"
        assert len(result["validators"]) == 2
        # ``total`` reflects the pool size (12 stubbed validators).
        assert int(result["total"]) >= 2

    def test_validators_pagination_distinct_pages(self, sim):
        """Pages 1 and 2 with the same per_page return disjoint validator sets."""
        _set_tm(sim, "1")
        _, body1, _ = _tm_post(sim, "1", "validators", params={"page": "1", "per_page": "2"})
        _, body2, _ = _tm_post(sim, "1", "validators", params={"page": "2", "per_page": "2"})
        addrs1 = {v["address"] for v in body1["result"]["validators"]}
        addrs2 = {v["address"] for v in body2["result"]["validators"]}
        assert addrs1.isdisjoint(addrs2), (
            f"page 1 and page 2 share validators — pagination broken. "
            f"page1={addrs1!r}  page2={addrs2!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Fault primitives — down / error / corruption
# ─────────────────────────────────────────────────────────────────────────────


class TestTmFaults:

    def test_down_returns_503(self, sim):
        """mode=down rejects the request with HTTP 503 (no body)."""
        _set_tm(sim, "1", mode="down")
        status, body, _ = _tm_post(sim, "1", "status")
        assert status == 503

    def test_error_mode_returns_jsonrpc_error_envelope(self, sim):
        """mode=error returns an envelope with the configured code + message."""
        _set_tm(
            sim, "1",
            mode="error",
            error_code=-32603,
            error_message="Internal error injected by test",
            http_status=200,
        )
        status, body, _ = _tm_post(sim, "1", "status")
        assert status == 200
        assert body["jsonrpc"] == "2.0"
        assert body["error"]["code"] == -32603
        assert "Internal error" in body["error"]["message"]
        assert "result" not in body, "error envelope must not also carry result"

    def test_rate_limit_returns_429_with_jsonrpc_envelope(self, sim):
        """mode=rate_limit returns HTTP 429 with a JSON-RPC error envelope."""
        _set_tm(sim, "1", mode="rate_limit")
        status, body, _ = _tm_post(sim, "1", "status")
        assert status == 429
        assert body["jsonrpc"] == "2.0"
        assert body["error"]["code"] == 429
        assert "Too many" in body["error"]["message"]

    def test_corruption_missing_field_strips_result(self, sim):
        """corruption_mode=missing_field+missing_field='result' drops the result key."""
        _set_tm(
            sim, "1",
            corruption_mode="missing_field",
            missing_field="result",
        )
        _, body, _ = _tm_post(sim, "1", "status")
        # The envelope is sent but the result field is removed.
        assert body["jsonrpc"] == "2.0"
        assert "result" not in body
        assert "error" not in body, (
            "missing_field on 'result' should produce an envelope with neither "
            "result nor error — clients see no useful payload (classifier signal)"
        )

    def test_corruption_invalid_json_returns_garbage_body(self, sim):
        """corruption_mode=invalid_json overwrites the body with non-JSON bytes."""
        _set_tm(sim, "1", corruption_mode="invalid_json")
        # _request returns raw bytes when JSON parsing fails.
        status, body, _ = _tm_post(sim, "1", "status")
        assert status == 200
        assert isinstance(body, (bytes, str)), f"expected raw garbage, got {body!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Per-method error overrides — named error-stub catalogue + raw envelope
#
# Primary: responses[method] = {"error_stub": "<name>"} — the simulator
# resolves the name against TENDERMINT_ERROR_STUBS and the caller wraps the
# error into the JSON-RPC envelope. Escape hatch: responses[method] =
# {"error": {...}} for ad-hoc shapes. Mirrors TestErrorStubs (ETH,
# test_simulator.py) and TestBTCErrorStubs (test_simulator_btc.py).
# ─────────────────────────────────────────────────────────────────────────────


class TestTmErrorStubs:

    @pytest.mark.parametrize("stub_name", sorted(TENDERMINT_ERROR_STUBS.keys()))
    def test_each_stub_emits_matching_envelope(self, sim, stub_name):
        """Each TENDERMINT_ERROR_STUBS entry round-trips through the wire unchanged."""
        stub = TENDERMINT_ERROR_STUBS[stub_name]
        _set_tm(sim, "1", responses={"status": {"error_stub": stub_name}})
        status, body, _ = _tm_post(sim, "1", "status")
        assert status == 200  # JSON-RPC errors ride on HTTP 200 by default.
        assert body["error"] == stub
        assert "result" not in body, "error envelope must not also carry result"

    def test_error_stub_scopes_to_named_method(self, sim):
        """An error_stub on one method leaves the other methods healthy."""
        _set_tm(sim, "1", responses={"status": {"error_stub": "internal"}})
        _, err_body, _ = _tm_post(sim, "1", "status")
        assert "error" in err_body

        _, ok_body, _ = _tm_post(sim, "1", "health")
        assert "result" in ok_body
        assert "error" not in ok_body

    def test_raw_error_envelope_escape_hatch(self, sim):
        """responses[method] = {"error": {...}} emits ad-hoc shapes not in the catalogue."""
        ad_hoc = {"code": -32099, "message": "synthetic test error"}
        _set_tm(sim, "1", responses={"status": {"error": ad_hoc}})
        status, body, _ = _tm_post(sim, "1", "status")
        assert status == 200
        assert body["error"] == ad_hoc
        assert "result" not in body

    def test_error_stub_records_error_status_in_history(self, sim):
        """The per-method error path records status='error' in /history."""
        _set_tm(sim, "1", responses={"status": {"error_stub": "internal"}})
        _tm_post(sim, "1", "status")
        _, hist, _ = _request("GET", _ctrl(sim, "/history?provider=1"))
        entries = [e for e in hist["history"] if e["method"] == "status"]
        assert len(entries) == 1
        assert entries[0]["status"] == "error"


# ─────────────────────────────────────────────────────────────────────────────
# Mixed-chain — ETH on p1 + Tendermint on p2 in one /scenario
# ─────────────────────────────────────────────────────────────────────────────


class TestTmMixedChain:

    def test_mixed_chain_each_independent(self, sim):
        """One provider can be ETH (mode=error) and another TM (mode=success) at once."""
        _request(
            "POST",
            _ctrl(sim, "/scenario"),
            body={
                "providers": {
                    "1": {"chain_family": "eth", "mode": "error", "error_code": -32000},
                    "2": {"chain_family": "tendermintrpc", "mode": "success"},
                }
            },
        )
        # ETH provider 1 — JSON-RPC port returns error envelope.
        eth_status, eth_body, _ = _request(
            "POST",
            sim["jsonrpc1"],
            body={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber"},
        )
        assert eth_status == 200
        assert eth_body.get("error", {}).get("code") == -32000

        # TM provider 2 — Tendermint port returns success envelope.
        tm_status, tm_body, _ = _tm_post(sim, "2", "status")
        assert tm_status == 200
        assert "result" in tm_body
        assert tm_body["result"]["node_info"]["network"]


# ─────────────────────────────────────────────────────────────────────────────
# History tracking
# ─────────────────────────────────────────────────────────────────────────────


class TestTmHistory:

    def test_post_request_appears_in_history(self, sim):
        """A successful TM POST shows up in /history with status=success."""
        _set_tm(sim, "1")
        _tm_post(sim, "1", "status", request_id=42)
        _, body, _ = _request("GET", _ctrl(sim, "/history") + "?provider=1&method=status")
        entries = body.get("history", [])
        assert len(entries) >= 1, f"expected at least 1 history entry, got body={body!r}"
        latest = entries[-1]
        assert latest["method"] == "status"
        assert latest["status"] == "success"
        assert latest["request_id"] == 42

    def test_get_request_uses_sim_counter_for_request_id(self, sim):
        """GET URI form has no native id; sim assigns a monotonic counter."""
        _set_tm(sim, "1")
        _tm_get(sim, "1", "status")
        _, body, _ = _request("GET", _ctrl(sim, "/history") + "?provider=1&method=status")
        entries = body.get("history", [])
        assert len(entries) >= 1, f"expected at least 1 history entry, got body={body!r}"
        latest = entries[-1]
        # Sim-assigned id is a positive integer.
        assert isinstance(latest["request_id"], int)
        assert latest["request_id"] >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Cross-transport isolation — the Tendermint handler's fault ladder is gated
# on chain_family="tendermintrpc" so a fault authored for another transport
# doesn't leak onto the TM port. Mirrors MAG-1838's JSON-RPC isolation
# (TestJsonRpcCrossTransportFaultIsolation in test_simulator.py) and
# TestRestCrossTransportFaultIsolation in test_simulator_rest.py. Surfaced
# in the 2026-05-18 suite triage as one of the leak paths feeding the ~37
# spurious failures.
# ─────────────────────────────────────────────────────────────────────────────


class TestTmCrossTransportFaultIsolation:
    """TM port must ignore faults authored for any other chain_family."""

    def test_tm_killed_by_eth_down_fault(self, sim):
        """A ``chain_family="eth"`` down fault MUST 503 the TM port.

        MAG-2092: mode="down" is honored on every transport regardless of
        chain_family because reachability is provider-wide. Without the
        universal-down semantic, an ETH provider in mode=down would keep
        serving TM responses, hiding router-side bugs that depend on the
        provider being unreachable across every node-url (e.g. MAG-2061).
        Per-transport isolation still applies to content modes (error /
        corrupt / hang / rate_limit / drop_connection)."""
        _request("POST", _ctrl(sim, "/scenario"), body={
            "providers": {"1": {"chain_family": "eth", "mode": "down"}}
        })
        status, _, _ = _tm_get(sim, "1", "status")
        assert status == 503, f"TM should refuse with 503 under universal-down; got {status}"

    def test_tm_unaffected_by_btc_error_fault(self, sim):
        """A ``chain_family="btc"`` mode=error must not produce an error
        body on the TM port — direct mirror of the leak shape surfaced in
        2026-05-18 triage."""
        _request("POST", _ctrl(sim, "/scenario"), body={"providers": {"1": {
            "chain_family": "btc",
            "mode": "error", "error_code": -32000,
            "error_message": "BTC error stub",
        }}})
        status, body, _ = _tm_get(sim, "1", "status")
        assert status == 200, f"TM should ignore btc-error; got {status}"
        assert "result" in body, f"expected TM success body; got {body!r}"

    def test_tm_unaffected_by_rest_rate_limit_fault(self, sim):
        """REST rate_limit must not 429 the TM port."""
        _request("POST", _ctrl(sim, "/scenario"), body={
            "providers": {"1": {"chain_family": "rest", "mode": "rate_limit"}}
        })
        status, body, _ = _tm_get(sim, "1", "status")
        assert status == 200
        assert "result" in body

    def test_tm_fault_still_fires_when_chain_family_is_tendermintrpc(self, sim):
        """Sanity check: ``chain_family="tendermintrpc"`` + mode=rate_limit
        must still fire on the TM port. The gate must not regress
        TM-authored faults."""
        _request("POST", _ctrl(sim, "/scenario"), body={
            "providers": {"1": {"chain_family": "tendermintrpc", "mode": "rate_limit"}}
        })
        status, _, _ = _tm_get(sim, "1", "status")
        assert status == 429

    def test_tm_killed_by_btc_down_fault(self, sim):
        """MAG-2092 universal-down: a ``chain_family="btc"`` mode=down
        also 503s the TM port."""
        _request("POST", _ctrl(sim, "/scenario"), body={
            "providers": {"1": {"chain_family": "btc", "mode": "down"}}
        })
        status, _, _ = _tm_get(sim, "1", "status")
        assert status == 503, f"TM should refuse with 503 under universal-down; got {status}"

    def test_tm_killed_by_rest_down_fault(self, sim):
        """MAG-2092 universal-down: a ``chain_family="rest"`` mode=down
        also 503s the TM port."""
        _request("POST", _ctrl(sim, "/scenario"), body={
            "providers": {"1": {"chain_family": "rest", "mode": "down"}}
        })
        status, _, _ = _tm_get(sim, "1", "status")
        assert status == 503, f"TM should refuse with 503 under universal-down; got {status}"
