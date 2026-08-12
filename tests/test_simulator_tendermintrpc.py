"""Integration tests for the Tendermint-RPC pool of the provider simulator.

Runs against the shared in-process simulator (see conftest.py): the
lava-sim-tm pool listens on 18554-18556 and the eth-sim pool on 18545-18547.
Under the pool:pid model those are SEPARATE providers, so cross-pool isolation
is structural, not gated.

Coverage:
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
  Fault primitives            — down / error / rate_limit / hang / drop (all
                                3 points) / corruption_mode / blocks_behind /
                                latency_ms / error_probability all apply on
                                a Tendermint provider.
  Cross-pool isolation        — faults on eth-sim / btc-sim / lava-sim-rest
                                never leak onto the TM pool.

Run with:
  pytest tests/test_simulator_tendermintrpc.py -v
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

import pytest

from constants import BTC_PRIMARY_PORTS, ETH_PRIMARY_PORTS, REST_PRIMARY_PORTS, TM_PRIMARY_PORTS
from stubs_tendermintrpc import TENDERMINT_ERROR_STUBS, TENDERMINT_METHOD_DEFAULTS

_TM_URLS = {pid: f"http://127.0.0.1:{port}" for pid, port in TM_PRIMARY_PORTS.items()}
_ETH1 = f"http://127.0.0.1:{ETH_PRIMARY_PORTS['1']}"
_BTC1 = f"http://127.0.0.1:{BTC_PRIMARY_PORTS['1']}"
_REST1 = f"http://127.0.0.1:{REST_PRIMARY_PORTS['1']}"


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
    return _request("POST", _TM_URLS[pid], body=body)


def _tm_get(sim: dict, pid: str, method: str, params: Optional[Dict[str, str]] = None):
    """GET the TM URI form. ``params`` values are URL-encoded as-is (callers
    decide whether to wrap strings in quotes per CometBFT's GET form)."""
    url = _TM_URLS[pid] + f"/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _request("GET", url)


def _set_tm(sim, pid: str = "1", **extra):
    """POST /scenario for one lava-sim-tm provider."""
    return _request(
        "POST", _ctrl(sim, "/scenario"), body={"providers": {f"lava-sim-tm:{pid}": dict(extra)}}
    )


# ── Function-scoped autouse: clean slate before/after every test ──────────────


@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario config AND clear history before and after every test."""
    _request("POST", _ctrl(sim, "/reset/all"), body={})
    yield
    _request("POST", _ctrl(sim, "/reset/all"), body={})


# ─────────────────────────────────────────────────────────────────────────────
# Addressing — pool:pid scoping on /scenario
# ─────────────────────────────────────────────────────────────────────────────


class TestTmAddressing:

    def test_scenario_on_tm_provider_round_trips(self, sim):
        """A lava-sim-tm:1 block round-trips through GET /scenario."""
        _set_tm(sim, "1", latency_ms=0)
        _, body, _ = _request("GET", _ctrl(sim, "/scenario"))
        assert body["providers"]["lava-sim-tm:1"]["mode"] == "success"
        assert "chain_family" not in body["providers"]["lava-sim-tm:1"]

    def test_other_providers_unchanged(self, sim):
        _set_tm(sim, "1", mode="error")
        _, body, _ = _request("GET", _ctrl(sim, "/scenario"))
        assert body["providers"]["lava-sim-tm:2"]["mode"] == "success"
        assert body["providers"]["lava-sim-tm:3"]["mode"] == "success"

    def test_reset_restores_tm_defaults(self, sim):
        _set_tm(sim, "1", mode="error")
        _request("POST", _ctrl(sim, "/reset"), body={})
        _, body, _ = _request("GET", _ctrl(sim, "/scenario"))
        assert body["providers"]["lava-sim-tm:1"]["mode"] == "success"


# ─────────────────────────────────────────────────────────────────────────────
# Verb dispatch — POST + GET both produce JSON-RPC envelopes
# ─────────────────────────────────────────────────────────────────────────────


class TestTmVerbDispatch:

    def test_post_status_returns_envelope(self, sim):
        status, body, _ = _tm_post(sim, "1", "status")
        assert status == 200
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 1
        assert "result" in body
        assert body["result"]["node_info"]["network"]  # stub-defined value

    def test_get_status_returns_envelope(self, sim):
        status, body, _ = _tm_get(sim, "1", "status")
        assert status == 200
        assert body["jsonrpc"] == "2.0"
        assert "result" in body
        assert body["result"]["node_info"]["network"]

    def test_post_empty_body_returns_parse_error(self, sim):
        # POST with no body — should return JSON-RPC -32700.
        status, body, _ = _request("POST", _TM_URLS["1"], body=None)
        assert status == 400
        assert body.get("error", {}).get("code") == -32700

    def test_post_missing_method_returns_parse_error(self, sim):
        # POST a valid JSON body that lacks the ``method`` field.
        status, body, _ = _request("POST", _TM_URLS["1"], body={"jsonrpc": "2.0", "id": 7})
        assert status == 400
        assert body.get("error", {}).get("code") == -32700

    def test_get_root_path_returns_parse_error(self, sim):
        # GET / (no method in URI) → -32700 (empty path).
        status, body, _ = _request("GET", _TM_URLS["1"] + "/")
        assert status == 400
        assert body.get("error", {}).get("code") == -32700

    def test_post_unknown_method_returns_method_not_found(self, sim):
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
        status, body, _ = _tm_post(sim, "1", method)
        assert status == 200, f"{method}: expected HTTP 200, got {status}, body={body!r}"
        assert body["jsonrpc"] == "2.0", f"{method}: jsonrpc envelope missing"
        assert "result" in body, f"{method}: result key missing, body={body!r}"
        # ``health`` returns ``{}``; every other method must return a non-empty dict.
        if method == "health":
            assert body["result"] == {}
        else:
            assert (
                isinstance(body["result"], dict) and body["result"]
            ), f"{method}: result should be a non-empty dict, got {body['result']!r}"

    @pytest.mark.parametrize("method", _V1_METHODS)
    def test_get_method_returns_200_envelope(self, sim, method):
        """Every v1 method also works via GET URI form."""
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
        _, body, _ = _tm_post(sim, "1", "block", params={"height": "4500000"})
        assert body["result"]["block"]["header"]["height"] == "4500000"

    def test_block_echoes_get_height(self, sim):
        """GET /block?height="4500000" echoes the height back after normalisation."""
        # CometBFT GET URI form quotes string values.
        _, body, _ = _tm_get(sim, "1", "block", params={"height": '"4500000"'})
        assert body["result"]["block"]["header"]["height"] == "4500000"

    def test_abci_query_echoes_post_height(self, sim):
        """POST abci_query echoes height in response.height."""
        _, body, _ = _tm_post(
            sim,
            "1",
            "abci_query",
            params={"path": "/store/auth/key", "data": "", "height": "4500000", "prove": False},
        )
        envelope_keys = set(body["result"]["response"].keys())
        assert envelope_keys == {
            "code",
            "log",
            "info",
            "index",
            "key",
            "value",
            "proofOps",
            "height",
            "codespace",
        }
        assert body["result"]["response"]["height"] == "4500000"

    def test_abci_query_echoes_get_height(self, sim):
        """GET abci_query?height="H" echoes height after normalisation."""
        _, body, _ = _tm_get(
            sim,
            "1",
            "abci_query",
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
        _, body, _ = _tm_post(
            sim,
            "1",
            "validators",
            params={"page": "1", "per_page": "2"},
        )
        result = body["result"]
        assert result["count"] == "2"
        assert len(result["validators"]) == 2
        # ``total`` reflects the pool size (12 stubbed validators).
        assert int(result["total"]) >= 2

    def test_validators_pagination_distinct_pages(self, sim):
        """Pages 1 and 2 with the same per_page return disjoint validator sets."""
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
            sim,
            "1",
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
            sim,
            "1",
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

    def test_hang_blocks_until_client_timeout(self, sim):
        """mode=hang on a TM provider accepts the connection and never replies."""
        _set_tm(sim, "1", mode="hang")
        t0 = time.monotonic()
        timed_out = False
        try:
            _tm_post(sim, "1", "status")
        except (urllib.error.URLError, ConnectionResetError, OSError):
            timed_out = True  # 30s sleep → client times out at 5s
        elapsed = time.monotonic() - t0
        # We don't wait 30s; just confirm the client timed out (≥4s, well above
        # the latency_ms=0 fast path) — actual cap is the urlopen timeout.
        # timed_out distinguishes a hang from a merely slow reply: a provider
        # that answers after 4s would satisfy the elapsed floor alone.
        assert timed_out, "hang mode replied instead of timing out"
        assert elapsed >= 4, f"hang should block at least client timeout, got {elapsed:.2f}s"

    @pytest.mark.parametrize("drop_at", ["before_headers", "after_headers", "mid_body"])
    def test_drop_connection_at_each_point(self, sim, drop_at):
        """All 3 drop points close the connection on a TM provider."""
        _set_tm(sim, "1", mode="drop_connection", drop_at=drop_at)
        # before_headers raises URLError; after_headers / mid_body raise
        # IncompleteRead via urllib's response object, or ConnectionResetError
        # depending on platform. All variants are valid for this test.
        with pytest.raises((urllib.error.URLError, ConnectionResetError, OSError, Exception)):
            _tm_post(sim, "1", "status")

    def test_blocks_behind_shifts_block_height(self, sim):
        """blocks_behind=100 shifts the height of `block` with no height param.

        The TM head constant is 5_000_000 (constants.TM_LATEST_HEIGHT); heights
        serialise as string-ints, so the shifted head reads "4999900".
        """
        _set_tm(sim, "1", blocks_behind=100)
        _, body, _ = _tm_post(sim, "1", "block")
        assert body["result"]["block"]["header"]["height"] == str(5_000_000 - 100)

    def test_blocks_behind_zero_reports_canonical_head(self, sim):
        """At blocks_behind=0 `block` with no height param reports the head."""
        _set_tm(sim, "1", blocks_behind=0)
        _, body, _ = _tm_post(sim, "1", "block")
        assert body["result"]["block"]["header"]["height"] == str(5_000_000)

    def test_explicit_height_unaffected_by_blocks_behind(self, sim):
        """`block` with an explicit height echoes it regardless of blocks_behind."""
        _set_tm(sim, "1", blocks_behind=100)
        _, body, _ = _tm_post(sim, "1", "block", params={"height": "4500000"})
        assert body["result"]["block"]["header"]["height"] == "4500000"

    def test_latency_ms_delays_reply(self, sim):
        """latency_ms=300 inserts at least 300ms between request and reply."""
        _set_tm(sim, "1", latency_ms=300)
        t0 = time.monotonic()
        status, _, _ = _tm_post(sim, "1", "status")
        elapsed = time.monotonic() - t0
        assert status == 200
        assert elapsed >= 0.28, f"latency floor not paid: elapsed={elapsed:.3f}s"

    def test_error_probability_1_always_errors(self, sim):
        """error_probability=1.0 on mode=success errors every one of 5 requests."""
        _set_tm(sim, "1", mode="success", error_probability=1.0)
        errored = 0
        for i in range(5):
            status, body, _ = _tm_post(sim, "1", "status", request_id=100 + i)
            if isinstance(body, dict) and "error" in body:
                errored += 1
        assert errored == 5, f"expected 5/5 errors at probability 1.0, got {errored}/5"

    def test_error_probability_0_never_errors(self, sim):
        """error_probability=0.0 on mode=success succeeds every one of 5 requests."""
        _set_tm(sim, "1", mode="success", error_probability=0.0)
        succeeded = 0
        for i in range(5):
            status, body, _ = _tm_post(sim, "1", "status", request_id=200 + i)
            if isinstance(body, dict) and "result" in body:
                succeeded += 1
        assert succeeded == 5, f"expected 5/5 successes at probability 0.0, got {succeeded}/5"


# ─────────────────────────────────────────────────────────────────────────────
# Per-method error overrides — named error-stub catalogue + raw envelope
#
# Primary: responses[method] = {"error_stub": "<name>"} — the simulator
# resolves the name against TENDERMINT_ERROR_STUBS and wraps the error into
# the JSON-RPC envelope. Escape hatch: responses[method] = {"error": {...}}
# for ad-hoc shapes.
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
        _, hist, _ = _request("GET", _ctrl(sim, "/history?pool=lava-sim-tm&pid=1"))
        entries = [e for e in hist["history"] if e["method"] == "status"]
        assert len(entries) == 1
        assert entries[0]["status"] == "error"


# ─────────────────────────────────────────────────────────────────────────────
# Mixed pools — eth-sim:1 faulted + lava-sim-tm:2 healthy in one /scenario
# ─────────────────────────────────────────────────────────────────────────────


class TestTmMixedChain:

    def test_mixed_pools_each_independent(self, sim):
        """One /scenario body can fault eth-sim:1 and leave lava-sim-tm:2
        healthy — the two pools never share state."""
        _request(
            "POST",
            _ctrl(sim, "/scenario"),
            body={
                "providers": {
                    "eth-sim:1": {"mode": "error", "error_code": -32000},
                    "lava-sim-tm:2": {"mode": "success"},
                }
            },
        )
        # eth-sim:1 — JSON-RPC port returns error envelope.
        eth_status, eth_body, _ = _request(
            "POST",
            _ETH1,
            body={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber"},
        )
        assert eth_status == 200
        assert eth_body.get("error", {}).get("code") == -32000

        # lava-sim-tm:2 — Tendermint port returns success envelope.
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
        _tm_post(sim, "1", "status", request_id=42)
        _, body, _ = _request(
            "GET", _ctrl(sim, "/history") + "?pool=lava-sim-tm&pid=1&method=status"
        )
        entries = body.get("history", [])
        assert len(entries) >= 1, f"expected at least 1 history entry, got body={body!r}"
        latest = entries[-1]
        assert latest["method"] == "status"
        assert latest["status"] == "success"
        assert latest["request_id"] == 42
        assert latest["interface"] == "tendermintrpc"

    def test_get_request_uses_sim_counter_for_request_id(self, sim):
        """GET URI form has no native id; sim assigns a monotonic counter."""
        _tm_get(sim, "1", "status")
        _, body, _ = _request(
            "GET", _ctrl(sim, "/history") + "?pool=lava-sim-tm&pid=1&method=status"
        )
        entries = body.get("history", [])
        assert len(entries) >= 1, f"expected at least 1 history entry, got body={body!r}"
        latest = entries[-1]
        # Sim-assigned id is a positive integer.
        assert isinstance(latest["request_id"], int)
        assert latest["request_id"] >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Cross-pool isolation — faults on other pools never reach lava-sim-tm.
# Under the old bare-pid model every transport shared pid "1"'s state, so an
# eth/btc/rest down also killed the TM port; the pool:pid model abolishes that.
# ─────────────────────────────────────────────────────────────────────────────


class TestTmCrossPoolIsolation:
    """The TM pool must be untouched by any other pool's faults — and its own
    faults must still fire."""

    def test_tm_unaffected_by_eth_down_fault(self, sim):
        """mode=down on eth-sim:1 kills eth-sim:1 and nothing else — the TM
        port keeps serving success."""
        _request(
            "POST",
            _ctrl(sim, "/scenario"),
            body={"providers": {"eth-sim:1": {"mode": "down"}}},
        )
        eth_status, _, _ = _request(
            "POST", _ETH1, body={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber"}
        )
        assert eth_status == 503, f"eth-sim:1 must be down; got {eth_status}"
        status, body, _ = _tm_get(sim, "1", "status")
        assert status == 200, f"lava-sim-tm:1 must ignore an eth-sim down; got {status}"
        assert "result" in body

    def test_tm_unaffected_by_btc_error_fault(self, sim):
        """mode=error on btc-sim:1 must not produce an error body on the TM
        port — different pools share nothing."""
        _request(
            "POST",
            _ctrl(sim, "/scenario"),
            body={
                "providers": {
                    "btc-sim:1": {
                        "mode": "error",
                        "error_code": -32000,
                        "error_message": "BTC error stub",
                    }
                }
            },
        )
        status, body, _ = _tm_get(sim, "1", "status")
        assert status == 200, f"TM should ignore a btc-sim error; got {status}"
        assert "result" in body, f"expected TM success body; got {body!r}"

    def test_tm_unaffected_by_rest_rate_limit_fault(self, sim):
        """A lava-sim-rest rate_limit must not 429 the TM port."""
        _request(
            "POST",
            _ctrl(sim, "/scenario"),
            body={"providers": {"lava-sim-rest:1": {"mode": "rate_limit"}}},
        )
        status, body, _ = _tm_get(sim, "1", "status")
        assert status == 200
        assert "result" in body

    def test_tm_fault_still_fires_on_its_own_pool(self, sim):
        """Sanity check: mode=rate_limit on lava-sim-tm:1 must still fire on
        the TM port — isolation must not swallow the pool's own faults."""
        _request(
            "POST",
            _ctrl(sim, "/scenario"),
            body={"providers": {"lava-sim-tm:1": {"mode": "rate_limit"}}},
        )
        status, _, _ = _tm_get(sim, "1", "status")
        assert status == 429

    def test_tm_unaffected_by_btc_down_fault(self, sim):
        """mode=down on btc-sim:1 downs only btc-sim:1 — the TM port stays up."""
        _request(
            "POST",
            _ctrl(sim, "/scenario"),
            body={"providers": {"btc-sim:1": {"mode": "down"}}},
        )
        btc_status, _, _ = _request(
            "POST", _BTC1, body={"jsonrpc": "2.0", "id": 1, "method": "getblockcount"}
        )
        assert btc_status == 503, f"btc-sim:1 must be down; got {btc_status}"
        status, body, _ = _tm_get(sim, "1", "status")
        assert status == 200, f"lava-sim-tm:1 must ignore a btc-sim down; got {status}"
        assert "result" in body

    def test_tm_unaffected_by_rest_down_fault(self, sim):
        """mode=down on lava-sim-rest:1 downs only that provider — the TM
        port stays up (rest and tm are two separate lava routers)."""
        _request(
            "POST",
            _ctrl(sim, "/scenario"),
            body={"providers": {"lava-sim-rest:1": {"mode": "down"}}},
        )
        rest_status, _, _ = _request(
            "GET", _REST1 + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        assert rest_status == 503, f"lava-sim-rest:1 must be down; got {rest_status}"
        status, body, _ = _tm_get(sim, "1", "status")
        assert status == 200, f"lava-sim-tm:1 must ignore a lava-sim-rest down; got {status}"
        assert "result" in body


# ─────────────────────────────────────────────────────────────────────────────
# Sequenced faults stay inside their pool — another pool's fail_first_n window
# neither downs the TM port nor is advanced by TM traffic
# ─────────────────────────────────────────────────────────────────────────────


class TestTmSequencedFaultIsolation:

    def test_tm_healthy_through_eth_down_window(self, sim):
        """A sequenced down (fail_first_n) on eth-sim:1 opens and closes its
        window on eth-sim:1 alone. The TM port serves success before, during,
        and after — it neither observes nor advances another pool's window."""
        _request(
            "POST",
            _ctrl(sim, "/scenario"),
            body={
                "providers": {
                    "eth-sim:1": {"mode": "down", "fail_first_n": 2, "then_mode": "success"}
                }
            },
        )

        status, body, _ = _tm_get(sim, "1", "status")
        assert status == 200, f"TM must be healthy while eth's window is open; got {status}"
        assert "result" in body

        for i in (1, 2):
            eth_status, _, _ = _request(
                "POST",
                _ETH1,
                body={"jsonrpc": "2.0", "id": i, "method": "eth_blockNumber"},
            )
            assert (
                eth_status == 503
            ), f"eth-sim:1 call {i} is inside the down window; got {eth_status}"

        eth_status, _, _ = _request(
            "POST", _ETH1, body={"jsonrpc": "2.0", "id": 3, "method": "eth_blockNumber"}
        )
        assert eth_status == 200, f"eth-sim:1 must recover after the window; got {eth_status}"

        status, body, _ = _tm_get(sim, "1", "status")
        assert status == 200, f"TM must still be healthy after eth's window; got {status}"
        assert "result" in body
        assert body["result"]["node_info"]["network"]
