"""
Integration tests for the Cosmos-REST pool of the provider simulator.

Runs against the shared in-process simulator (see conftest.py): the
lava-sim-rest pool listens on 18551-18553 and the eth-sim pool on 18545-18547.
Under the pool:pid model those are SEPARATE providers, so cross-pool isolation
is structural, not gated.

Coverage:
  Verb dispatch              — each of GET/POST/PUT/DELETE/HEAD/OPTIONS hits
                               the right pipeline.
  Path-template matching     — {address} and {height} placeholders parse,
                               query strings preserved, the pagination cursor
                               echoed back, 404 for unknown paths.
  Happy-path per seed path   — every (verb, template) in REST_METHOD_DEFAULTS
                               returns a non-empty 200 body.
  Fault primitives           — hang / drop / stale / corrupt / status /
                               latency_ms / error_probability all apply
                               identically on a REST provider.
  Per-(verb, template) overrides — body/status/error/error_stub + the fault
                               keys (mode / latency_ms), list-of-pairs wire
                               format.
  Cross-pool isolation       — faults on eth-sim / btc-sim / lava-sim-grpc /
                               lava-sim-tm never leak onto the REST pool.
  History tracking            — REST requests show up in /history with the
                               X-Request-Id header correlated when present.

Run with:
  pytest tests/test_simulator_rest.py -v
"""

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

import pytest

from constants import ETH_PRIMARY_PORTS, REST_PRIMARY_PORTS
from stubs_rest import REST_ERROR_STUBS, REST_LATEST_HEIGHT, REST_METHOD_DEFAULTS

_REST_URLS = {pid: f"http://127.0.0.1:{port}" for pid, port in REST_PRIMARY_PORTS.items()}
_ETH_URLS = {pid: f"http://127.0.0.1:{port}" for pid, port in ETH_PRIMARY_PORTS.items()}

# All (verb, template) pairs covered by the seed stub set.
ALL_REST_ROUTES = sorted(REST_METHOD_DEFAULTS.keys())


# ── HTTP helpers (kept independent of the sibling files — duplication is
#     intentional so each file stays self-contained). ─────────────────────────


def _request(
    method: str,
    url: str,
    body: Optional[dict] = None,
    headers: Optional[Dict[str, str]] = None,
    read_body: bool = True,
) -> Tuple[int, Any, Dict[str, str]]:
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


def _get(url: str, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any, Dict[str, str]]:
    return _request("GET", url, body=None, headers=headers)


def _post(
    url: str, body: Optional[dict] = None, headers: Optional[Dict[str, str]] = None
) -> Tuple[int, Any, Dict[str, str]]:
    return _request("POST", url, body=body, headers=headers)


def _ctrl(sim: dict, path: str) -> str:
    return sim["control"] + path


def _set_rest(sim, pid: str = "1", **extra):
    """Convenience: POST /scenario for one lava-sim-rest provider."""
    return _post(_ctrl(sim, "/scenario"), {"providers": {f"lava-sim-rest:{pid}": dict(extra)}})


# ── Function-scoped autouse: clean slate before/after every test ──────────────


@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario config AND clear history before and after every test."""
    _post(_ctrl(sim, "/reset/all"), {})
    yield
    _post(_ctrl(sim, "/reset/all"), {})


# ─────────────────────────────────────────────────────────────────────────────
# Addressing — pool:pid scoping on /scenario
# ─────────────────────────────────────────────────────────────────────────────


class TestRestAddressing:

    def test_scenario_on_rest_provider_round_trips(self, sim):
        """A lava-sim-rest:1 block round-trips through GET /scenario."""
        _set_rest(sim, "1", latency_ms=0)
        _, body, _ = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["lava-sim-rest:1"]["mode"] == "success"
        assert "chain_family" not in body["providers"]["lava-sim-rest:1"]

    def test_other_providers_unchanged(self, sim):
        _set_rest(sim, "1", mode="error")
        _, body, _ = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["lava-sim-rest:2"]["mode"] == "success"
        assert body["providers"]["lava-sim-rest:3"]["mode"] == "success"

    def test_reset_restores_rest_defaults(self, sim):
        _set_rest(sim, "1", mode="error")
        _post(_ctrl(sim, "/reset"), {})
        _, body, _ = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["lava-sim-rest:1"]["mode"] == "success"


# ─────────────────────────────────────────────────────────────────────────────
# Verb dispatch — each of GET/POST/PUT/DELETE/HEAD/OPTIONS hits the right path
# ─────────────────────────────────────────────────────────────────────────────


class TestRestRouting:

    def test_get_known_path_returns_200(self, sim):
        status, body, _ = _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        assert status == 200
        assert "block" in body

    def test_get_unknown_path_returns_404(self, sim):
        status, body, _ = _get(_REST_URLS["1"] + "/cosmos/unknown/path")
        assert status == 404
        assert body["code"] == "not_found"
        assert body["method"] == "GET"

    def test_post_unknown_path_returns_404(self, sim):
        """v1 seeds only GET paths, so any POST is a 404 unless overridden."""
        status, body, _ = _post(
            _REST_URLS["1"] + "/cosmos/staking/v1beta1/validators", body={"name": "test"}
        )
        assert status == 404
        assert body["method"] == "POST"

    def test_put_unknown_path_returns_404(self, sim):
        status, body, _ = _request("PUT", _REST_URLS["1"] + "/cosmos/anything", body={"x": 1})[:3]
        assert status == 404

    def test_delete_unknown_path_returns_404(self, sim):
        status, body, _ = _request("DELETE", _REST_URLS["1"] + "/cosmos/anything")[:3]
        assert status == 404

    def test_head_returns_no_body(self, sim):
        """HEAD reuses the GET pipeline but skips writing the body."""
        status, body, headers = _request(
            "HEAD",
            _REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest",
        )
        assert status == 200
        # HEAD strips the body — urllib treats an empty read as None.
        assert body is None or body == b"" or body == {}
        # Content-Length still announces the would-be body size.
        assert int(headers.get("Content-Length", "0")) > 0

    def test_head_content_length_matches_the_get_body(self, sim):
        """The HEAD contract: identical status and headers to the GET, no bytes.

        Asserting only ``Content-Length > 0`` would pass on any non-empty
        response; this pins it to the exact size the GET sends, which is the
        promise a caller sizing a download relies on.
        """
        url = _REST_URLS["1"] + "/cosmos/staking/v1beta1/validators"
        get_status, _, get_headers = _get(url)
        head_status, head_body, head_headers = _request("HEAD", url)
        assert head_status == get_status == 200
        assert head_body is None or head_body == b"" or head_body == {}
        assert head_headers["Content-Length"] == get_headers["Content-Length"]
        assert head_headers["Content-Type"] == get_headers["Content-Type"]

    def test_head_unknown_path_returns_404(self, sim):
        """HEAD borrows the GET route table; it does not invent routes."""
        status, _, _ = _request("HEAD", _REST_URLS["1"] + "/cosmos/unknown/path")
        assert status == 404

    def test_head_post_only_path_returns_404(self, sim):
        """/simulate is catalogued for POST, so there is no GET route to borrow."""
        status, _, _ = _request("HEAD", _REST_URLS["1"] + "/cosmos/tx/v1beta1/simulate")
        assert status == 404

    def test_options_returns_allow_header(self, sim):
        """OPTIONS on a known path lists the registered verbs."""
        status, _, headers = _request(
            "OPTIONS",
            _REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest",
            read_body=False,
        )
        assert status == 204
        allow = headers.get("Allow", "")
        assert "GET" in allow
        assert "OPTIONS" in allow

    def test_options_unknown_path_returns_404(self, sim):
        status, body, _ = _request("OPTIONS", _REST_URLS["1"] + "/unknown")
        assert status == 404


# ─────────────────────────────────────────────────────────────────────────────
# Path-template matching — {address} / {height} placeholders + query strings
# ─────────────────────────────────────────────────────────────────────────────


class TestRestPathTemplates:

    def test_height_placeholder_parses(self, sim):
        """``/blocks/12345`` matches ``/blocks/{height}`` and echoes 12345."""
        status, body, _ = _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/12345")
        assert status == 200
        assert body["block"]["header"]["height"] == "12345"

    def test_address_placeholder_parses(self, sim):
        """``/balances/{address}`` echoes the address segment in the response."""
        status, body, _ = _get(_REST_URLS["1"] + "/cosmos/bank/v1beta1/balances/cosmos1abc")
        assert status == 200
        # The chain tucks the requested address into a sibling field so tests
        # can assert the path param round-tripped without poking inside the
        # balance list.
        assert body["address"] == "cosmos1abc"

    def test_query_string_does_not_break_match(self, sim):
        """``?pagination.limit=10`` is preserved as query, doesn't affect routing."""
        status, body, _ = _get(
            _REST_URLS["1"] + "/cosmos/staking/v1beta1/validators?pagination.limit=10"
        )
        assert status == 200
        assert "validators" in body

    def test_page_key_is_echoed_back(self, sim):
        """``?pagination.key=<cursor>`` comes back as ``pagination.inbound_key``.

        Every page of the stub is identical, so this echo is the only way a
        caller can see that the cursor it sent is the cursor that arrived.
        """
        status, body, _ = _get(
            _REST_URLS["1"] + "/cosmos/staking/v1beta1/validators?pagination.key=sentinel-abc"
        )
        assert status == 200
        assert body["pagination"]["inbound_key"] == "sentinel-abc"

    def test_page_key_echo_is_none_without_a_cursor(self, sim):
        """No cursor sent, no cursor echoed — and the rest of the body is intact."""
        status, body, _ = _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        assert status == 200
        assert body["pagination"]["inbound_key"] is None
        assert body["validators"][0]["status"] == "BOND_STATUS_BONDED"

    def test_page_key_echo_is_per_request(self, sim):
        """A cursor from one request must not linger on the next one's body."""
        _, first, _ = _get(
            _REST_URLS["1"] + "/cosmos/staking/v1beta1/validators?pagination.key=cursor-1"
        )
        _, second, _ = _get(
            _REST_URLS["1"] + "/cosmos/staking/v1beta1/validators?pagination.key=cursor-2"
        )
        assert first["pagination"]["inbound_key"] == "cursor-1"
        assert second["pagination"]["inbound_key"] == "cursor-2"

    def test_trailing_slash_doesnt_match(self, sim):
        """Exact-end regex anchor — ``/...validators/`` is not the same path."""
        status, _, _ = _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators/")
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
        # Substitute placeholders with realistic values so the URL is legal.
        live_path = re.sub(r"\{height\}", "20000000", template)
        live_path = re.sub(r"\{address\}", "cosmos1abc", live_path)
        status, body, _ = _request(verb, _REST_URLS["1"] + live_path)
        assert status == 200, f"{verb} {template} → {status}"
        assert isinstance(body, dict), f"{verb} {template} body was {type(body).__name__}"
        assert len(body) > 0, f"{verb} {template} returned empty dict"

    def test_blocks_latest_carries_chain_id(self, sim):
        _, body, _ = _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        assert body["block"]["header"]["chain_id"] == "lava-sim"

    def test_blocks_latest_height_matches_rest_latest_height(self, sim):
        _, body, _ = _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        assert body["block"]["header"]["height"] == str(REST_LATEST_HEIGHT)

    def test_node_info_carries_lava_network(self, sim):
        _, body, _ = _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/node_info")
        assert body["default_node_info"]["network"] == "lava-sim"
        assert body["application_version"]["app_name"] == "lavad"

    def test_balances_default_returns_ulava(self, sim):
        _, body, _ = _get(_REST_URLS["1"] + "/cosmos/bank/v1beta1/balances/cosmos1abc")
        assert body["balances"][0]["denom"] == "ulava"

    def test_validators_default_returns_one_bonded(self, sim):
        _, body, _ = _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
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
            _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
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
        with pytest.raises((urllib.error.URLError, ConnectionResetError, OSError, Exception)):
            _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")


class TestRestFaultStatus:

    def test_status_override_sets_http_code(self, sim):
        """mode=error + http_status=502 propagates to the wire."""
        _set_rest(
            sim, "1", mode="error", http_status=502, error_code=-1, error_message="upstream down"
        )
        status, body, _ = _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        assert status == 502
        # REST error body shape: {"code": ..., "message": ...}, no JSON-RPC envelope.
        assert body["code"] == -1
        assert body["message"] == "upstream down"


class TestRestFaultCorrupt:

    def test_corrupt_truncated(self, sim):
        """corruption_mode=truncated strips trailing bytes."""
        _set_rest(sim, "1", corruption_mode="truncated")
        req = urllib.request.Request(
            _REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)

    def test_corrupt_invalid_json(self, sim):
        _set_rest(sim, "1", corruption_mode="invalid_json")
        req = urllib.request.Request(
            _REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)

    def test_corrupt_empty_response(self, sim):
        _set_rest(sim, "1", corruption_mode="empty_response")
        req = urllib.request.Request(
            _REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            assert raw == b""
            assert resp.headers.get("Content-Length") == "0"

    def test_corrupt_missing_field_dotted_path(self, sim):
        """missing_field='block.header.height' removes a nested leaf via dotted path."""
        _set_rest(sim, "1", corruption_mode="missing_field", missing_field="block.header.height")
        _, body, _ = _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        # block.header should still exist; only the height leaf is removed.
        assert "block" in body
        assert "header" in body["block"]
        assert "height" not in body["block"]["header"]

    def test_corrupt_wrong_type(self, sim):
        """corruption_mode=wrong_type swaps the targeted field's type."""
        _set_rest(sim, "1", corruption_mode="wrong_type", missing_field="block")
        _, body, _ = _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        # block was a dict; corruption swaps to a string sentinel.
        assert isinstance(body["block"], str)


class TestRestFaultStale:

    def test_blocks_behind_shifts_blocks_latest_height(self, sim):
        """blocks_behind=100 shifts /blocks/latest by 100."""
        _set_rest(sim, "1", blocks_behind=100)
        _, body, _ = _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        assert body["block"]["header"]["height"] == str(REST_LATEST_HEIGHT - 100)

    def test_blocks_by_height_unaffected_by_blocks_behind(self, sim):
        """``/blocks/{height}`` echoes the requested height regardless of blocks_behind."""
        _set_rest(sim, "1", blocks_behind=100)
        _, body, _ = _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/19000000")
        assert body["block"]["header"]["height"] == "19000000"


class TestRestFaultLatency:

    def test_provider_wide_latency_ms_delays_reply(self, sim):
        """latency_ms=300 on the provider block delays every route by ≥300ms.

        Distinct from the per-path override in TestRestPerPathFaultOverrides:
        this is the provider-wide field on the scenario block itself.
        """
        _set_rest(sim, "1", latency_ms=300)
        t0 = time.monotonic()
        status, _, _ = _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        elapsed = time.monotonic() - t0
        assert status == 200
        assert elapsed >= 0.28, f"latency floor not paid: elapsed={elapsed:.3f}s"


class TestRestFaultErrorProbability:

    def test_error_probability_1_always_errors(self, sim):
        """error_probability=1.0 on mode=success errors every one of 5 requests.

        REST errors are a bare {code, message} object, no JSON-RPC envelope.
        """
        _set_rest(
            sim,
            "1",
            mode="success",
            error_probability=1.0,
            error_code=-32077,
            error_message="Forced by test",
        )
        errored = 0
        for _ in range(5):
            _, body, _ = _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
            if isinstance(body, dict) and body.get("code") == -32077:
                errored += 1
        assert errored == 5, f"expected 5/5 errors at probability 1.0, got {errored}/5"

    def test_error_probability_0_never_errors(self, sim):
        """error_probability=0.0 on mode=success succeeds every one of 5 requests."""
        _set_rest(sim, "1", mode="success", error_probability=0.0)
        succeeded = 0
        for _ in range(5):
            _, body, _ = _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
            if isinstance(body, dict) and "block" in body:
                succeeded += 1
        assert succeeded == 5, f"expected 5/5 successes at probability 0.0, got {succeeded}/5"


# ─────────────────────────────────────────────────────────────────────────────
# Per-(verb, template) error overrides — list-of-pairs wire format
# ─────────────────────────────────────────────────────────────────────────────


class TestRestPerPathOverrides:

    def test_status_and_body_override(self, sim):
        """``responses`` wire list-of-pairs round-trips with tuple keys on read."""
        _set_rest(
            sim,
            "1",
            responses=[
                [
                    ["GET", "/cosmos/staking/v1beta1/validators"],
                    {"status": 503, "body": {"code": "unavailable"}},
                ]
            ],
        )
        status, body, _ = _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        assert status == 503
        assert body == {"code": "unavailable"}

    def test_error_envelope_override(self, sim):
        """``responses[...] = {"error": {...}}`` triggers the error path."""
        _set_rest(
            sim,
            "1",
            responses=[
                [
                    ["GET", "/cosmos/staking/v1beta1/validators"],
                    {
                        "status": 500,
                        "error": {"code": "internal_error", "message": "boom"},
                    },
                ]
            ],
        )
        status, body, _ = _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        assert status == 500
        assert body["error"]["code"] == "internal_error"
        assert body["error"]["message"] == "boom"

    def test_http_status_wins_over_status_on_body_override(self, sim):
        """http_status is the primary status key; status is the deprecated
        REST-only fallback. When both are present on a body override, the
        wire must carry http_status."""
        _set_rest(
            sim,
            "1",
            responses=[
                [
                    ["GET", "/cosmos/staking/v1beta1/validators"],
                    {
                        "http_status": 503,
                        "status": 418,
                        "body": {"code": "unavailable"},
                    },
                ]
            ],
        )
        status, body, _ = _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        assert status == 503, f"http_status must win over status; got {status}"
        assert body == {"code": "unavailable"}

    def test_http_status_wins_over_status_on_error_override(self, sim):
        """Same http_status-over-status primacy on the error-envelope branch."""
        _set_rest(
            sim,
            "1",
            responses=[
                [
                    ["GET", "/cosmos/staking/v1beta1/validators"],
                    {
                        "http_status": 502,
                        "status": 500,
                        "error": {"code": "internal_error", "message": "boom"},
                    },
                ]
            ],
        )
        status, body, _ = _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        assert status == 502, f"http_status must win over status; got {status}"
        assert body["error"]["code"] == "internal_error"
        assert body["error"]["message"] == "boom"

    def test_other_paths_unaffected_by_override(self, sim):
        """Per-path overrides scope strictly to that (verb, template)."""
        _set_rest(
            sim,
            "1",
            responses=[
                [
                    ["GET", "/cosmos/staking/v1beta1/validators"],
                    {"status": 503, "body": {"code": "unavailable"}},
                ]
            ],
        )
        # Different path → default stub, status 200.
        status, body, _ = _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        assert status == 200
        assert "block" in body


# ─────────────────────────────────────────────────────────────────────────────
# Named error-stub catalogue — REST_ERROR_STUBS
#
# Primary: responses[(verb, template)] = {"error_stub": "<name>"} — the
# simulator resolves the name against REST_ERROR_STUBS and emits the same
# {"error": {...}} body as the raw-envelope path. The raw {"error": {...}}
# escape hatch stays covered by TestRestPerPathOverrides above.
# ─────────────────────────────────────────────────────────────────────────────


class TestRestErrorStubs:

    @pytest.mark.parametrize("stub_name", sorted(REST_ERROR_STUBS.keys()))
    def test_each_stub_emits_matching_envelope(self, sim, stub_name):
        """Each REST_ERROR_STUBS entry round-trips through the wire unchanged.

        Client sends just the name; the simulator resolves it to the
        catalogue entry. Default HTTP status is 500 — the same default the
        raw {"error": {...}} path uses when no "status" is configured.
        """
        stub = REST_ERROR_STUBS[stub_name]
        _set_rest(
            sim,
            "1",
            responses=[
                [["GET", "/cosmos/staking/v1beta1/validators"], {"error_stub": stub_name}],
            ],
        )
        status, body, _ = _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        assert status == 500, f"{stub_name}: default error status should be 500, got {status}"
        assert body["error"] == stub

    def test_error_stub_honours_status_override(self, sim):
        """A "status" key next to "error_stub" sets the HTTP status."""
        _set_rest(
            sim,
            "1",
            responses=[
                [
                    ["GET", "/cosmos/staking/v1beta1/validators"],
                    {"error_stub": "not_found", "status": 404},
                ],
            ],
        )
        status, body, _ = _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        assert status == 404
        assert body["error"] == REST_ERROR_STUBS["not_found"]

    def test_error_stub_scopes_to_named_route(self, sim):
        """An error_stub on one (verb, template) leaves other routes healthy."""
        _set_rest(
            sim,
            "1",
            responses=[
                [["GET", "/cosmos/staking/v1beta1/validators"], {"error_stub": "internal"}],
            ],
        )
        err_status, err_body, _ = _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        assert err_status == 500
        assert "error" in err_body

        ok_status, ok_body, _ = _get(
            _REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        assert ok_status == 200
        assert "block" in ok_body
        assert "error" not in ok_body

    def test_error_stub_records_error_status_in_history(self, sim):
        """The named-stub error path records status='error' in /history."""
        _set_rest(
            sim,
            "1",
            responses=[
                [["GET", "/cosmos/staking/v1beta1/validators"], {"error_stub": "internal"}],
            ],
        )
        _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        _, hist, _ = _get(_ctrl(sim, "/history?pool=lava-sim-rest&pid=1"))
        entries = [
            e for e in hist["history"] if e["method"] == "GET /cosmos/staking/v1beta1/validators"
        ]
        assert len(entries) == 1
        assert entries[0]["status"] == "error"


# ─────────────────────────────────────────────────────────────────────────────
# Per-(verb, template) FAULT overrides on REST
#
# A per-(verb, template) entry in the `responses` list can carry `mode` /
# `latency_ms` keys in addition to the success-path `status` / `body` /
# `error` keys. Eligible modes are the chain-agnostic fault primitives:
# down, hang, drop_connection, rate_limit, success. `mode == "error"` is
# rejected at /scenario time, matching the JSON-RPC validation rule.
#
# Composition order mirrors JSON-RPC: latency FIRST, then fault, so a
# per-path latency_ms is paid even when the per-path mode is rate_limit.
# Per-key fallback also mirrors JSON-RPC: a partial per-path entry inherits
# provider-wide fault keys it doesn't override.
# ─────────────────────────────────────────────────────────────────────────────


class TestRestPerPathFaultOverrides:

    def test_per_path_mode_down_isolates_to_named_route(self, sim):
        """``mode: down`` fires only for the matching (verb, template)."""
        _set_rest(
            sim,
            "1",
            responses=[
                [["GET", "/cosmos/staking/v1beta1/validators"], {"mode": "down"}],
            ],
        )
        status_down, _, _ = _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        assert status_down == 503, f"expected 503 on overridden route, got {status_down}"

        status_ok, body_ok, _ = _get(
            _REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        assert status_ok == 200, f"non-overridden route should succeed, got {status_ok}"
        assert "block" in body_ok

    def test_per_path_mode_rate_limit_returns_429(self, sim):
        """``mode: rate_limit`` returns HTTP 429 + REST error envelope."""
        _set_rest(
            sim,
            "1",
            responses=[
                [["GET", "/cosmos/staking/v1beta1/validators"], {"mode": "rate_limit"}],
            ],
        )
        status, body, _ = _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        assert status == 429
        assert isinstance(body, dict)
        assert body["code"] == 429

    def test_per_path_latency_ms_isolates_to_named_route(self, sim):
        """``latency_ms`` only delays the matching (verb, template)."""
        _set_rest(
            sim,
            "1",
            latency_ms=0,
            responses=[
                [["GET", "/cosmos/staking/v1beta1/validators"], {"latency_ms": 500}],
            ],
        )
        t0 = time.monotonic()
        _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        elapsed_overridden_ms = (time.monotonic() - t0) * 1000
        assert (
            elapsed_overridden_ms >= 480
        ), f"overridden route should sleep ~500ms, elapsed={elapsed_overridden_ms:.0f}ms"

        t1 = time.monotonic()
        _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        elapsed_other_ms = (time.monotonic() - t1) * 1000
        assert (
            elapsed_other_ms < 200
        ), f"non-overridden route should not sleep, elapsed={elapsed_other_ms:.0f}ms"

    def test_per_key_fallback_inherits_provider_wide_latency(self, sim):
        """A partial per-path entry inherits provider-wide latency_ms it doesn't override."""
        _set_rest(
            sim,
            "1",
            latency_ms=100,
            responses=[
                [["GET", "/cosmos/staking/v1beta1/validators"], {"mode": "down"}],
            ],
        )
        t0 = time.monotonic()
        status, _, _ = _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert status == 503
        assert (
            elapsed_ms >= 80
        ), f"provider-wide latency_ms=100 should still apply, elapsed={elapsed_ms:.0f}ms"

    def test_composition_order_latency_first_then_fault(self, sim):
        """Per-path ``{latency_ms: 200, mode: rate_limit}`` → 429 with >=180ms delay."""
        _set_rest(
            sim,
            "1",
            responses=[
                [
                    ["GET", "/cosmos/staking/v1beta1/validators"],
                    {"latency_ms": 200, "mode": "rate_limit"},
                ],
            ],
        )
        t0 = time.monotonic()
        status, body, _ = _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert status == 429
        assert body["code"] == 429
        assert (
            elapsed_ms >= 180
        ), f"per-path latency should fire before fault, elapsed={elapsed_ms:.0f}ms"

    def test_per_path_mode_error_rejected_with_400(self, sim):
        """``mode: error`` is rejected at /scenario POST time."""
        status, body, _ = _set_rest(
            sim,
            "1",
            responses=[
                [["GET", "/cosmos/staking/v1beta1/validators"], {"mode": "error"}],
            ],
        )
        assert status == 400, f"expected 400 on per-path mode=error, got {status}"
        assert "error" in body
        # Message should reference the offending key for diagnosability.
        assert "mode" in body["error"].lower() or "error" in body["error"].lower()

    def test_per_path_rate_limit_records_status_in_history(self, sim):
        """Per-path rate_limit records status='rate_limit' in /history under the matched template."""
        _set_rest(
            sim,
            "1",
            responses=[
                [["GET", "/cosmos/staking/v1beta1/validators"], {"mode": "rate_limit"}],
            ],
        )
        _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        _, hist, _ = _get(_ctrl(sim, "/history?pool=lava-sim-rest&pid=1"))
        entries = [
            e for e in hist["history"] if e["method"] == "GET /cosmos/staking/v1beta1/validators"
        ]
        assert len(entries) == 1
        assert entries[0]["status"] == "rate_limit"

    def test_jsonrpc_overrides_do_not_affect_rest_pool(self, sim):
        """A string-keyed JSON-RPC override on eth-sim:1 (e.g.
        ``eth_blockNumber: {mode: down}``) can never leak into the REST pool
        — the providers are different objects with different responses maps."""
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "responses": {
                            "eth_blockNumber": {"mode": "down"},
                        },
                    }
                }
            },
        )

        # JSON-RPC side fires the override as configured (sanity check).
        rpc_status, _, _ = _post(
            _ETH_URLS["1"],
            body={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber"},
        )
        assert rpc_status == 503

        # REST pool stays healthy — a different provider entirely.
        rest_status, rest_body, _ = _get(
            _REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        assert rest_status == 200
        assert "block" in rest_body

    def test_rest_overrides_do_not_affect_jsonrpc_pool(self, sim):
        """A tuple-keyed REST override on lava-sim-rest:1 can never shadow a
        JSON-RPC method lookup on eth-sim:1 — different providers, different
        responses maps."""
        _set_rest(
            sim,
            "1",
            responses=[
                [["GET", "/cosmos/staking/v1beta1/validators"], {"mode": "down"}],
            ],
        )

        # REST side faulted as configured.
        rest_status, _, _ = _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        assert rest_status == 503

        # JSON-RPC pool — eth_blockNumber stays healthy.
        rpc_status, rpc_body, _ = _post(
            _ETH_URLS["1"],
            body={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber"},
        )
        assert rpc_status == 200
        assert "result" in rpc_body
        assert "error" not in rpc_body


# ─────────────────────────────────────────────────────────────────────────────
# Mixed pools — JSON-RPC + REST in the same /scenario body
# ─────────────────────────────────────────────────────────────────────────────


class TestRestMixedChainScenario:

    def test_eth_jsonrpc_and_rest_independent(self, sim):
        """eth-sim:1 and lava-sim-rest:2 each answer in their own chain's
        convention without contaminating each other."""
        # ETH side: JSON-RPC POST to eth-sim:1.
        eth_status, eth_body, _ = _post(
            _ETH_URLS["1"],
            body={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber"},
        )
        # REST side: GET on lava-sim-rest:2's port.
        rest_status, rest_body, _ = _get(
            _REST_URLS["2"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        assert eth_status == 200
        assert eth_body["result"].startswith("0x")
        assert rest_status == 200
        assert rest_body["block"]["header"]["chain_id"] == "lava-sim"

    def test_eth_and_rest_independently_faulted(self, sim):
        """Each provider can run a different fault mode in the same scenario."""
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {"mode": "rate_limit"},
                    "lava-sim-rest:2": {"blocks_behind": 50},
                }
            },
        )
        eth_status, _, _ = _post(
            _ETH_URLS["1"],
            body={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber"},
        )
        rest_status, rest_body, _ = _get(
            _REST_URLS["2"] + "/cosmos/base/tendermint/v1beta1/blocks/latest"
        )
        assert eth_status == 429
        assert rest_status == 200
        assert rest_body["block"]["header"]["height"] == str(REST_LATEST_HEIGHT - 50)


# ─────────────────────────────────────────────────────────────────────────────
# History — REST calls show up in /history with X-Request-Id correlation
# ─────────────────────────────────────────────────────────────────────────────


class TestRestHistory:

    def test_rest_request_recorded_in_history(self, sim):
        _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        _, hist, _ = _get(_ctrl(sim, "/history?pool=lava-sim-rest&pid=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "GET /cosmos/base/tendermint/v1beta1/blocks/latest"
        assert last["status"] == "success"
        assert last["interface"] == "rest"
        assert last["port"] == REST_PRIMARY_PORTS["1"]

    def test_rest_history_filter_by_method(self, sim):
        """?method= filters work for the REST method label (``<VERB> <template>``)."""
        _get(_REST_URLS["1"] + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        method = "GET /cosmos/staking/v1beta1/validators"
        _, hist, _ = _get(_ctrl(sim, f"/history?method={method.replace(' ', '%20')}"))
        assert hist["count"] >= 1
        assert all(e["method"] == method for e in hist["history"])

    def test_x_request_id_correlates_into_history(self, sim):
        """X-Request-Id from the router is preserved on the history entry."""
        _get(
            _REST_URLS["1"] + "/cosmos/staking/v1beta1/validators",
            headers={"X-Request-Id": "test-trace-42"},
        )
        _, hist, _ = _get(_ctrl(sim, "/history?request_id=test-trace-42"))
        assert hist["count"] >= 1
        assert hist["history"][-1]["request_id"] == "test-trace-42"

    def test_sim_side_request_id_when_header_missing(self, sim):
        """Without X-Request-Id the sim still assigns a numeric counter id."""
        _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        _, hist, _ = _get(_ctrl(sim, "/history?pool=lava-sim-rest&pid=1"))
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
        _get(_REST_URLS["1"] + "/totally/unknown")
        _, hist, _ = _get(_ctrl(sim, "/history?pool=lava-sim-rest&pid=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "GET /totally/unknown"
        assert last["status"] == "not_found"

    def test_head_recorded_under_its_own_verb(self, sim):
        """A HEAD is served by the GET route but logged as the HEAD it was, so
        a caller reading /history can tell the two apart."""
        _request("HEAD", _REST_URLS["1"] + "/cosmos/staking/v1beta1/validators")
        _, hist, _ = _get(_ctrl(sim, "/history?pool=lava-sim-rest&pid=1"))
        assert hist["count"] >= 1
        last = hist["history"][-1]
        assert last["method"] == "HEAD /cosmos/staking/v1beta1/validators"
        assert last["status"] == "success"


# ─────────────────────────────────────────────────────────────────────────────
# Cross-pool isolation — faults on other pools never reach lava-sim-rest.
# Under the old bare-pid model every transport shared pid "1"'s state, so an
# eth/btc/grpc/tm down also killed the REST port; the pool:pid model
# abolishes that.
# ─────────────────────────────────────────────────────────────────────────────


class TestRestCrossPoolIsolation:
    """The REST pool must be untouched by any other pool's faults — and its
    own faults must still fire."""

    _LATEST = "/cosmos/base/tendermint/v1beta1/blocks/latest"

    def test_rest_unaffected_by_eth_down_fault(self, sim):
        """mode=down on eth-sim:1 kills eth-sim:1 and nothing else — the REST
        port keeps serving success."""
        _post(_ctrl(sim, "/scenario"), {"providers": {"eth-sim:1": {"mode": "down"}}})
        eth_status, _, _ = _post(
            _ETH_URLS["1"], body={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber"}
        )
        assert eth_status == 503, f"eth-sim:1 must be down; got {eth_status}"
        status, body, _ = _get(_REST_URLS["1"] + self._LATEST)
        assert status == 200, f"lava-sim-rest:1 must ignore an eth-sim down; got {status}"
        assert "block" in body

    def test_rest_unaffected_by_btc_error_fault(self, sim):
        """mode=error on btc-sim:1 must not produce an error body on the REST
        port — different pools share nothing."""
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "btc-sim:1": {
                        "mode": "error",
                        "error_code": -32000,
                        "error_message": "BTC error stub",
                    }
                }
            },
        )
        status, body, _ = _get(_REST_URLS["1"] + self._LATEST)
        assert status == 200, f"REST should ignore a btc-sim error; got {status}"
        assert "block" in body, f"expected REST success body; got {body!r}"

    def test_rest_unaffected_by_grpc_rate_limit_fault(self, sim):
        """A lava-sim-grpc rate_limit must not 429 the REST port."""
        _post(
            _ctrl(sim, "/scenario"),
            {"providers": {"lava-sim-grpc:1": {"mode": "rate_limit"}}},
        )
        status, body, _ = _get(_REST_URLS["1"] + self._LATEST)
        assert status == 200, f"REST should ignore a lava-sim-grpc rate-limit; got {status}"
        assert "block" in body

    def test_rest_unaffected_by_tm_down_fault(self, sim):
        """mode=down on lava-sim-tm:1 downs only that provider — the REST
        port stays up (tm and rest are two separate lava routers)."""
        _post(
            _ctrl(sim, "/scenario"),
            {"providers": {"lava-sim-tm:1": {"mode": "down"}}},
        )
        status, body, _ = _get(_REST_URLS["1"] + self._LATEST)
        assert status == 200, f"REST must ignore a lava-sim-tm down; got {status}"
        assert "block" in body

    def test_rest_fault_still_fires_on_its_own_pool(self, sim):
        """Sanity check: isolation must not swallow the pool's own faults.
        mode=rate_limit on lava-sim-rest:1 must still 429 the REST port."""
        _set_rest(sim, "1", mode="rate_limit")
        status, _, _ = _get(_REST_URLS["1"] + self._LATEST)
        assert status == 429

    def test_rest_unaffected_by_btc_down_fault(self, sim):
        """mode=down on btc-sim:1 downs only btc-sim:1 — the REST port stays up."""
        _post(_ctrl(sim, "/scenario"), {"providers": {"btc-sim:1": {"mode": "down"}}})
        status, body, _ = _get(_REST_URLS["1"] + self._LATEST)
        assert status == 200, f"REST must ignore a btc-sim down; got {status}"
        assert "block" in body

    def test_rest_unaffected_by_grpc_down_fault(self, sim):
        """mode=down on lava-sim-grpc:1 downs only that provider — the REST
        port stays up."""
        _post(_ctrl(sim, "/scenario"), {"providers": {"lava-sim-grpc:1": {"mode": "down"}}})
        status, body, _ = _get(_REST_URLS["1"] + self._LATEST)
        assert status == 200, f"REST must ignore a lava-sim-grpc down; got {status}"
        assert "block" in body


# ─────────────────────────────────────────────────────────────────────────────
# Sequenced faults — the fail_first_n window belongs to one provider: its own
# endpoints consume it, other pools never see it
# ─────────────────────────────────────────────────────────────────────────────


class TestRestSequencedFaults:

    _LATEST = "/cosmos/base/tendermint/v1beta1/blocks/latest"

    def test_rest_pool_consumes_its_own_down_window(self, sim):
        """fail_first_n on lava-sim-rest:1 is consumed by the REST endpoint
        itself: the first 2 calls 503, the 3rd serves then_mode=success."""
        _set_rest(sim, "1", mode="down", fail_first_n=2, then_mode="success")

        for i in (1, 2):
            status, _, _ = _get(_REST_URLS["1"] + self._LATEST)
            assert status == 503, f"REST call {i} is inside the down window; got {status}"

        status, body, _ = _get(_REST_URLS["1"] + self._LATEST)
        assert status == 200, f"REST must recover after its own window; got {status}"
        assert "block" in body

    def test_rest_never_advances_another_pools_window(self, sim):
        """REST traffic can never burn another provider's fail_first_n
        budget: with a sequenced down on eth-sim:1, any number of REST calls
        leaves the eth window untouched — eth-sim:1 still fails exactly its
        first 2 calls."""
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {"mode": "down", "fail_first_n": 2, "then_mode": "success"}
                }
            },
        )

        for attempt in range(1, 5):
            status, _, _ = _get(_REST_URLS["1"] + self._LATEST)
            assert status == 200, (
                f"REST call {attempt} must stay healthy — another pool's "
                f"window can't touch it; got {status}"
            )

        # eth-sim:1's window is still fully un-consumed.
        for i in (1, 2):
            eth_status, _, _ = _post(
                _ETH_URLS["1"], body={"jsonrpc": "2.0", "id": i, "method": "eth_blockNumber"}
            )
            assert (
                eth_status == 503
            ), f"eth-sim:1 call {i} is inside the down window; got {eth_status}"
        eth_status, _, _ = _post(
            _ETH_URLS["1"], body={"jsonrpc": "2.0", "id": 3, "method": "eth_blockNumber"}
        )
        assert eth_status == 200, f"eth-sim:1 must recover after the window; got {eth_status}"

    def test_rest_sequenced_success_then_down(self, sim):
        """The sequence works in both directions on the pool's own provider:
        mode=success with then_mode=down serves the first call normally, then
        503s once the window is consumed."""
        _set_rest(sim, "1", mode="success", fail_first_n=1, then_mode="down")

        status, body, _ = _get(_REST_URLS["1"] + self._LATEST)
        assert status == 200, f"first call is inside the mode=success window; got {status}"
        assert "block" in body

        status, _, _ = _get(_REST_URLS["1"] + self._LATEST)
        assert status == 503, f"the call after the window must serve then_mode=down; got {status}"
