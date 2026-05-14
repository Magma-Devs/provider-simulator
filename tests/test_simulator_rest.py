"""
Unit tests for the REST chain dispatch in the provider simulator (MAG-1777).

Mirrors the structure of ``tests/test_simulator_btc.py`` (BTC) but covers
REST-specific behaviour:

  Verb dispatch              — each of GET/POST/PUT/DELETE/HEAD/OPTIONS hits
                               the right do_* method and routes through the
                               shared _handle pipeline.
  Path-template matching     — {address} and {height} placeholders parse,
                               query strings preserved, 404 for unknown paths.
  Happy-path per seed path   — every (verb, template) in REST_METHOD_DEFAULTS
                               returns a non-empty 200 body.
  Fault primitives           — set_hang / drop / stale / corrupt / status all
                               apply identically on a REST provider.
  Mixed-chain scenario       — one ETH (JSON-RPC) + one REST provider in the
                               same /scenario body, each independently faulted.
  chain_family on /scenario   — `"rest"` is accepted and round-trips through
                               GET /scenario.
  History tracking            — REST requests show up in /history with the
                               X-Request-Id header correlated when present.

Run with:
  pytest tests/test_simulator_rest.py -v
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

import pytest

from server import ControlHandler, JSONRPCHandler, ProviderState, RestHandler
from stubs_rest import REST_METHOD_DEFAULTS, REST_LATEST_HEIGHT


# ── Test ports (distinct from ETH suite's 28545-28547 / 29000 and BTC suite's
#     38545-38547 / 39000 so the three suites can co-exist if run in parallel).
#     REST sim uses 48545-48547 for JSON-RPC echoes (helpful for mixed-chain
#     tests) plus 48551-48553 for the REST handler itself. ──────────────────

_PROVIDER_PORTS = {"1": 48545, "2": 48546, "3": 48547}
_REST_PORTS     = {"1": 48551, "2": 48552, "3": 48553}
_CONTROL_PORT   = 49000

# All (verb, template) pairs covered by the seed stub set.
ALL_REST_ROUTES = sorted(REST_METHOD_DEFAULTS.keys())


# ── HTTP helpers (kept independent of test_simulator.py / test_simulator_btc.py
#     to avoid cross-file fixture coupling — duplication is intentional). ──


def _request(method: str, url: str, body: Optional[dict] = None,
             headers: Optional[Dict[str, str]] = None,
             read_body: bool = True) -> Tuple[int, Any, Dict[str, str]]:
    """Send an HTTP request and return (status, parsed_body, response_headers).

    ``parsed_body`` is the JSON-decoded response when the body is valid JSON,
    otherwise the raw bytes (so corruption tests can inspect un-parseable
    output). ``read_body=False`` skips reading and returns ``None`` — used
    by HEAD tests.
    """
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read() if read_body else b""
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


def _get(url: str, headers: Optional[Dict[str, str]] = None
         ) -> Tuple[int, Any, Dict[str, str]]:
    return _request("GET", url, body=None, headers=headers)


def _post(url: str, body: Optional[dict] = None,
          headers: Optional[Dict[str, str]] = None
          ) -> Tuple[int, Any, Dict[str, str]]:
    return _request("POST", url, body=body, headers=headers)


def _ctrl(sim: dict, path: str) -> str:
    return sim["control"] + path


# ── Module-scoped fixture: start all servers once ─────────────────────────────

@pytest.fixture(scope="module")
def sim():
    """Start 3 JSON-RPC + 3 REST + 1 control server on dedicated test ports.

    Yields a dict with base URLs:
      sim["control"]  → http://127.0.0.1:49000
      sim["rest1"]    → http://127.0.0.1:48551
      sim["rest2"]    → http://127.0.0.1:48552
      sim["rest3"]    → http://127.0.0.1:48553
      sim["jsonrpc1"] → http://127.0.0.1:48545
    """
    states = {pid: ProviderState() for pid in _PROVIDER_PORTS}

    servers = []
    for pid, port in _PROVIDER_PORTS.items():
        srv = ThreadingHTTPServer(("127.0.0.1", port), JSONRPCHandler)
        srv.daemon_threads = True
        srv.state = states[pid]
        srv.provider_id = pid
        servers.append(srv)

    for pid, port in _REST_PORTS.items():
        srv = ThreadingHTTPServer(("127.0.0.1", port), RestHandler)
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
        "rest1":    f"http://127.0.0.1:{_REST_PORTS['1']}",
        "rest2":    f"http://127.0.0.1:{_REST_PORTS['2']}",
        "rest3":    f"http://127.0.0.1:{_REST_PORTS['3']}",
        "jsonrpc1": f"http://127.0.0.1:{_PROVIDER_PORTS['1']}",
        "jsonrpc2": f"http://127.0.0.1:{_PROVIDER_PORTS['2']}",
    }

    for s in servers:
        s.shutdown()


# ── Helper: put a REST provider into rest mode with optional extras ───────────

def _set_rest(sim, pid: str = "1", **extra):
    """Convenience: POST /scenario for a single provider with chain_family=rest.

    Any extra kwargs are folded into the per-provider config dict so callers
    can write one-liners like ``_set_rest(sim, "1", blocks_behind=100)``.
    """
    cfg = {"chain_family": "rest", **extra}
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

class TestRestChainFamily:

    def test_chain_family_rest_accepted_on_scenario(self, sim):
        """`chain_family="rest"` round-trips through /scenario."""
        _set_rest(sim, "1")
        _, body, _ = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["1"]["chain_family"] == "rest"

    def test_other_providers_unchanged(self, sim):
        _set_rest(sim, "1")
        _, body, _ = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["2"]["chain_family"] == "eth"
        assert body["providers"]["3"]["chain_family"] == "eth"

    def test_reset_clears_chain_family(self, sim):
        _set_rest(sim, "1")
        _post(_ctrl(sim, "/reset"), {})
        _, body, _ = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["1"]["chain_family"] == "eth"


# ─────────────────────────────────────────────────────────────────────────────
# Verb dispatch — each of GET/POST/PUT/DELETE/HEAD/OPTIONS hits the right path
# ─────────────────────────────────────────────────────────────────────────────

class TestRestRouting:

    def test_get_known_path_returns_200(self, sim):
        _set_rest(sim, "1")
        status, body, _ = _get(sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        assert status == 200
        assert "block" in body

    def test_get_unknown_path_returns_404(self, sim):
        _set_rest(sim, "1")
        status, body, _ = _get(sim["rest1"] + "/cosmos/unknown/path")
        assert status == 404
        assert body["code"] == "not_found"
        assert body["method"] == "GET"

    def test_post_unknown_path_returns_404(self, sim):
        """v1 seeds only GET paths, so any POST is a 404 unless overridden."""
        _set_rest(sim, "1")
        status, body, _ = _post(sim["rest1"] + "/cosmos/staking/v1beta1/validators",
                                 body={"name": "test"})
        assert status == 404
        assert body["method"] == "POST"

    def test_put_unknown_path_returns_404(self, sim):
        _set_rest(sim, "1")
        status, body, _ = _request("PUT", sim["rest1"] + "/cosmos/anything",
                                    body={"x": 1})[:3]
        assert status == 404

    def test_delete_unknown_path_returns_404(self, sim):
        _set_rest(sim, "1")
        status, body, _ = _request("DELETE", sim["rest1"] + "/cosmos/anything")[:3]
        assert status == 404

    def test_head_returns_no_body(self, sim):
        """HEAD reuses the GET pipeline but skips writing the body."""
        _set_rest(sim, "1")
        status, body, headers = _request(
            "HEAD",
            sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest",
        )
        assert status == 200
        # HEAD strips the body — urllib treats an empty read as None.
        assert body is None or body == b"" or body == {}
        # Content-Length still announces the would-be body size.
        assert int(headers.get("Content-Length", "0")) > 0

    def test_options_returns_allow_header(self, sim):
        """OPTIONS on a known path lists the registered verbs."""
        _set_rest(sim, "1")
        status, _, headers = _request(
            "OPTIONS",
            sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest",
            read_body=False,
        )
        assert status == 204
        allow = headers.get("Allow", "")
        assert "GET" in allow
        assert "OPTIONS" in allow

    def test_options_unknown_path_returns_404(self, sim):
        _set_rest(sim, "1")
        status, body, _ = _request("OPTIONS", sim["rest1"] + "/unknown")
        assert status == 404


# ─────────────────────────────────────────────────────────────────────────────
# Path-template matching — {address} / {height} placeholders + query strings
# ─────────────────────────────────────────────────────────────────────────────

class TestRestPathTemplates:

    def test_height_placeholder_parses(self, sim):
        """``/blocks/12345`` matches ``/blocks/{height}`` and echoes 12345."""
        _set_rest(sim, "1")
        status, body, _ = _get(
            sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/12345"
        )
        assert status == 200
        assert body["block"]["header"]["height"] == "12345"

    def test_address_placeholder_parses(self, sim):
        """``/balances/{address}`` echoes the address segment in the response."""
        _set_rest(sim, "1")
        status, body, _ = _get(
            sim["rest1"] + "/cosmos/bank/v1beta1/balances/cosmos1abc"
        )
        assert status == 200
        # handlers_rest tucks the requested address into a sibling field so
        # tests can assert the path param round-tripped without poking inside
        # the balance list.
        assert body["address"] == "cosmos1abc"

    def test_query_string_does_not_break_match(self, sim):
        """``?pagination.limit=10`` is preserved as query, doesn't affect routing."""
        _set_rest(sim, "1")
        status, body, _ = _get(
            sim["rest1"] + "/cosmos/staking/v1beta1/validators?pagination.limit=10"
        )
        assert status == 200
        assert "validators" in body

    def test_trailing_slash_doesnt_match(self, sim):
        """Exact-end regex anchor — ``/...validators/`` is not the same path."""
        _set_rest(sim, "1")
        status, _, _ = _get(sim["rest1"] + "/cosmos/staking/v1beta1/validators/")
        assert status == 404

    def test_path_template_keys_are_unique(self):
        """The compiled route table has no duplicate (verb, template) pairs."""
        seen = set()
        for verb, template in REST_METHOD_DEFAULTS.keys():
            key = (verb.upper(), template)
            assert key not in seen, f"duplicate route key: {key}"
            seen.add(key)


# ─────────────────────────────────────────────────────────────────────────────
# Happy-path stubs per (verb, template)
# ─────────────────────────────────────────────────────────────────────────────

class TestRestHappyPath:

    @pytest.mark.parametrize("verb,template", ALL_REST_ROUTES)
    def test_seed_path_returns_200_and_non_empty_body(self, sim, verb, template):
        """Every (verb, template) in REST_METHOD_DEFAULTS responds with 200 + dict body."""
        _set_rest(sim, "1")
        # Substitute placeholders with realistic values so the URL is legal.
        live_path = re.sub(r"\{height\}", "20000000", template)
        live_path = re.sub(r"\{address\}", "cosmos1abc", live_path)
        status, body, _ = _request(verb, sim["rest1"] + live_path)
        assert status == 200, f"{verb} {template} → {status}"
        assert isinstance(body, dict), f"{verb} {template} body was {type(body).__name__}"
        assert len(body) > 0, f"{verb} {template} returned empty dict"

    def test_blocks_latest_carries_chain_id(self, sim):
        _set_rest(sim, "1")
        _, body, _ = _get(sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        assert body["block"]["header"]["chain_id"] == "lava-sim"

    def test_blocks_latest_height_matches_rest_latest_height(self, sim):
        _set_rest(sim, "1")
        _, body, _ = _get(sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        assert body["block"]["header"]["height"] == str(REST_LATEST_HEIGHT)

    def test_node_info_carries_lava_network(self, sim):
        _set_rest(sim, "1")
        _, body, _ = _get(sim["rest1"] + "/cosmos/base/tendermint/v1beta1/node_info")
        assert body["default_node_info"]["network"] == "lava-sim"
        assert body["application_version"]["app_name"] == "lavad"

    def test_balances_default_returns_ulava(self, sim):
        _set_rest(sim, "1")
        _, body, _ = _get(sim["rest1"] + "/cosmos/bank/v1beta1/balances/cosmos1abc")
        assert body["balances"][0]["denom"] == "ulava"

    def test_validators_default_returns_one_bonded(self, sim):
        _set_rest(sim, "1")
        _, body, _ = _get(sim["rest1"] + "/cosmos/staking/v1beta1/validators")
        assert body["validators"][0]["status"] == "BOND_STATUS_BONDED"


# ─────────────────────────────────────────────────────────────────────────────
# Fault primitives — hang / drop / corrupt / stale / status — applied on REST
# ─────────────────────────────────────────────────────────────────────────────

class TestRestFaultHang:

    def test_hang_blocks_until_client_timeout(self, sim):
        """mode=hang on a REST provider blocks the client connection."""
        _set_rest(sim, "1", mode="hang")
        t0 = time.monotonic()
        try:
            _get(sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        except (urllib.error.URLError, ConnectionResetError, OSError):
            pass  # 30s sleep → client times out at 5s
        elapsed = time.monotonic() - t0
        assert elapsed >= 4, f"hang should block at least client timeout, got {elapsed:.2f}s"


class TestRestFaultDropped:

    @pytest.mark.parametrize("drop_at", ["before_headers", "after_headers", "mid_body"])
    def test_drop_connection_at_each_point(self, sim, drop_at):
        """All 3 drop points work on a REST provider."""
        _set_rest(sim, "1", mode="drop_connection", drop_at=drop_at)
        # before_headers raises URLError; after_headers / mid_body raise
        # IncompleteRead via urllib's response object, or ConnectionResetError
        # depending on platform. All variants are valid for this test.
        with pytest.raises((urllib.error.URLError, ConnectionResetError, OSError,
                            http_err(), Exception)):
            _get(sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")


class TestRestFaultStatus:

    def test_status_override_sets_http_code(self, sim):
        """mode=error + http_status=502 propagates to the wire."""
        _set_rest(sim, "1", mode="error", http_status=502,
                  error_code=-1, error_message="upstream down")
        status, body, _ = _get(
            sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        assert status == 502
        # REST error body shape: {"code": ..., "message": ...}, no JSON-RPC envelope.
        assert body["code"] == -1
        assert body["message"] == "upstream down"


class TestRestFaultCorrupt:

    def test_corrupt_truncated(self, sim):
        """corruption_mode=truncated strips trailing bytes."""
        _set_rest(sim, "1", corruption_mode="truncated")
        req = urllib.request.Request(
            sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)

    def test_corrupt_invalid_json(self, sim):
        _set_rest(sim, "1", corruption_mode="invalid_json")
        req = urllib.request.Request(
            sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)

    def test_corrupt_empty_response(self, sim):
        _set_rest(sim, "1", corruption_mode="empty_response")
        req = urllib.request.Request(
            sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            assert raw == b""
            assert resp.headers.get("Content-Length") == "0"

    def test_corrupt_missing_field_dotted_path(self, sim):
        """missing_field='block.header.height' removes a nested leaf via dotted path."""
        _set_rest(sim, "1", corruption_mode="missing_field",
                  missing_field="block.header.height")
        _, body, _ = _get(
            sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        # block.header should still exist; only the height leaf is removed.
        assert "block" in body
        assert "header" in body["block"]
        assert "height" not in body["block"]["header"]

    def test_corrupt_wrong_type(self, sim):
        """corruption_mode=wrong_type swaps the first top-level field's type."""
        _set_rest(sim, "1", corruption_mode="wrong_type",
                  missing_field="block")
        _, body, _ = _get(
            sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        # block was a dict; corruption swaps to a string sentinel.
        assert isinstance(body["block"], str)


class TestRestFaultStale:

    def test_blocks_behind_shifts_blocks_latest_height(self, sim):
        """blocks_behind=100 shifts /blocks/latest by 100."""
        _set_rest(sim, "1", blocks_behind=100)
        _, body, _ = _get(
            sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        assert body["block"]["header"]["height"] == str(REST_LATEST_HEIGHT - 100)

    def test_blocks_by_height_unaffected_by_blocks_behind(self, sim):
        """``/blocks/{height}`` echoes the requested height regardless of blocks_behind."""
        _set_rest(sim, "1", blocks_behind=100)
        _, body, _ = _get(
            sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/19000000"
        )
        assert body["block"]["header"]["height"] == "19000000"


# ─────────────────────────────────────────────────────────────────────────────
# Per-(verb, template) error overrides — Q9-A wire format
# ─────────────────────────────────────────────────────────────────────────────

class TestRestPerPathOverrides:

    def test_status_and_body_override(self, sim):
        """``responses`` wire list-of-pairs round-trips with tuple keys on read."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "chain_family": "rest",
                    "responses": [
                        [
                            ["GET", "/cosmos/staking/v1beta1/validators"],
                            {"status": 503, "body": {"code": "unavailable"}},
                        ]
                    ],
                }
            }
        })
        status, body, _ = _get(sim["rest1"] + "/cosmos/staking/v1beta1/validators")
        assert status == 503
        assert body == {"code": "unavailable"}

    def test_error_envelope_override(self, sim):
        """``responses[...] = {"error": {...}}`` triggers the error path."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "chain_family": "rest",
                    "responses": [
                        [
                            ["GET", "/cosmos/staking/v1beta1/validators"],
                            {"status": 500,
                             "error": {"code": "internal_error",
                                       "message": "boom"}},
                        ]
                    ],
                }
            }
        })
        status, body, _ = _get(sim["rest1"] + "/cosmos/staking/v1beta1/validators")
        assert status == 500
        assert body["error"]["code"] == "internal_error"
        assert body["error"]["message"] == "boom"

    def test_other_paths_unaffected_by_override(self, sim):
        """Per-path overrides scope strictly to that (verb, template)."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "chain_family": "rest",
                    "responses": [
                        [
                            ["GET", "/cosmos/staking/v1beta1/validators"],
                            {"status": 503, "body": {"code": "unavailable"}},
                        ]
                    ],
                }
            }
        })
        # Different path → default stub, status 200.
        status, body, _ = _get(sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        assert status == 200
        assert "block" in body


# ─────────────────────────────────────────────────────────────────────────────
# Mixed-chain scenario — JSON-RPC + REST in the same /scenario body
# ─────────────────────────────────────────────────────────────────────────────

class TestRestMixedChainScenario:

    def test_eth_jsonrpc_and_rest_independent(self, sim):
        """Provider 1 stays ETH (JSON-RPC); provider 2 flips to REST.

        Each port answers in its own chain's convention without contaminating
        the other.
        """
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {"chain_family": "eth"},
                "2": {"chain_family": "rest"},
            }
        })
        # ETH side: JSON-RPC POST to provider 1.
        eth_status, eth_body, _ = _post(
            sim["jsonrpc1"],
            body={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber"},
        )
        # REST side: GET on provider 2's REST port.
        rest_status, rest_body, _ = _get(
            sim["rest2"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        assert eth_status == 200
        assert eth_body["result"].startswith("0x")
        assert rest_status == 200
        assert rest_body["block"]["header"]["chain_id"] == "lava-sim"

    def test_eth_and_rest_independently_faulted(self, sim):
        """Each provider can run a different fault mode in the same scenario."""
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {"chain_family": "eth", "mode": "rate_limit"},
                "2": {"chain_family": "rest", "blocks_behind": 50},
            }
        })
        eth_status, _, _ = _post(
            sim["jsonrpc1"],
            body={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber"},
        )
        rest_status, rest_body, _ = _get(
            sim["rest2"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        assert eth_status == 429
        assert rest_status == 200
        assert rest_body["block"]["header"]["height"] == str(REST_LATEST_HEIGHT - 50)


# ─────────────────────────────────────────────────────────────────────────────
# History — REST calls show up in /history with X-Request-Id correlation
# ─────────────────────────────────────────────────────────────────────────────

class TestRestHistory:

    def test_rest_request_recorded_in_history(self, sim):
        _set_rest(sim, "1")
        _get(sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        _, hist, _ = _get(_ctrl(sim, "/history?provider=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "GET /cosmos/base/tendermint/v1beta1/blocks/latest"
        assert last["status"] == "success"

    def test_rest_history_filter_by_method(self, sim):
        """?method= filters work for the REST method label (``<VERB> <template>``)."""
        _set_rest(sim, "1")
        _get(sim["rest1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        _get(sim["rest1"] + "/cosmos/staking/v1beta1/validators")
        method = "GET /cosmos/staking/v1beta1/validators"
        _, hist, _ = _get(_ctrl(sim, f"/history?method={method.replace(' ', '%20')}"))
        assert hist["count"] >= 1
        assert all(e["method"] == method for e in hist["history"])

    def test_x_request_id_correlates_into_history(self, sim):
        """X-Request-Id from the router is preserved on the history entry."""
        _set_rest(sim, "1")
        _get(sim["rest1"] + "/cosmos/staking/v1beta1/validators",
             headers={"X-Request-Id": "test-trace-42"})
        _, hist, _ = _get(_ctrl(sim, "/history?request_id=test-trace-42"))
        assert hist["count"] >= 1
        assert hist["history"][-1]["request_id"] == "test-trace-42"

    def test_sim_side_request_id_when_header_missing(self, sim):
        """Without X-Request-Id the sim still assigns a numeric counter id."""
        _set_rest(sim, "1")
        _get(sim["rest1"] + "/cosmos/staking/v1beta1/validators")
        _, hist, _ = _get(_ctrl(sim, "/history?provider=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        # request_id is a numeric counter (int or string-of-int) when no header.
        assert last["request_id"] is not None
        try:
            int(last["request_id"])
        except (TypeError, ValueError):
            pytest.fail(f"sim-side request_id should be numeric, got {last['request_id']!r}")

    def test_404_recorded_with_method_label(self, sim):
        """Unknown paths still appear in /history under a method label."""
        _set_rest(sim, "1")
        _get(sim["rest1"] + "/totally/unknown")
        _, hist, _ = _get(_ctrl(sim, "/history?provider=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "GET /totally/unknown"
        assert last["status"] == "not_found"


def http_err():
    """Lazy reference to urllib's HTTPError for parametrize tuples."""
    return urllib.error.HTTPError
