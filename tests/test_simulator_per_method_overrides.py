"""
Per-method overrides for the JSON-RPC provider simulator.

These tests exercise the per-method override path on the simulator's
``responses`` dict: a test can set ``latency_ms``, ``mode``, or
``rate_limit`` (and the keys those modes need) for one specific JSON-RPC
method on a provider, while every other method on the same provider keeps
the provider-wide config. The ``body`` + ``status`` override keys emit a
custom 2xx response and bypass the chain entirely.

What they cover
---------------
    1. per-method ``mode: down``           — fires only on the named method.
    2. per-method ``latency_ms``           — fires only on the named method.
    3. per-key fallback                    — partial entries inherit
                                              provider-wide fault keys.
    4. ``mode == "error"`` is rejected     — /scenario POST returns 400.
    5. composition order — latency FIRST   — latency paid even on fault.
    6. body+status returns custom shape    — exact wire bytes, no chain
                                              involvement.
    7. status defaults to 200              — omitting ``status`` is fine.
    8. non-2xx status is rejected          — 4xx/5xx via mode=error path.
    9. body+latency composes               — latency first, then body.
   10. body+mode mutually exclusive        — 400 from /scenario.
   11. body bypasses healthy stub          — no stub fields leak through.

Runs against the shared in-process simulator (see conftest.py).

Run with:
  pytest tests/test_simulator_per_method_overrides.py -v
"""

import json
import time
import urllib.error
import urllib.request

import pytest

from provider_simulator.topology import port_of

_P1 = f"http://127.0.0.1:{port_of('eth-sim', '1')}"


# ── HTTP helpers (same shape as tests/test_simulator.py) ──────────────────────


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


def _rpc(url: str, method: str, params: list | None = None) -> tuple[int, dict | str]:
    """Send a JSON-RPC request, return (http_status, response_body)."""
    return _post(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})


def _ctrl(sim: dict, path: str) -> str:
    return sim["control"] + path


# ── Function-scoped autouse: clean slate before/after every test ──────────────


@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario config AND clear history before and after every test."""
    _post(_ctrl(sim, "/reset/all"), {})
    yield
    _post(_ctrl(sim, "/reset/all"), {})


# ─────────────────────────────────────────────────────────────────────────────
# Per-method override behaviour (fault keys)
# ─────────────────────────────────────────────────────────────────────────────


class TestPerMethodOverrides:

    @pytest.mark.timeout(10)
    def test_per_method_mode_down_isolates_to_named_method(self, sim):
        """A per-method ``mode: down`` fires only for the named method.

        Provider eth-sim:1 starts healthy. We pin ``eth_blockNumber`` to
        ``down`` via the per-method override. The expectation is:

          * eth_blockNumber → HTTP 503 (the down primitive — 503 with no body)
          * eth_getBlockByNumber → 200 success (no override, inherits
                                   the provider-wide healthy config)

        This is the isolation guarantee that distinguishes per-method
        overrides from provider-wide ``mode: down`` (which kills every
        method on the provider).
        """
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "responses": {
                            "eth_blockNumber": {"mode": "down"},
                        },
                    }
                }
            },
        )

        status_down, body_down = _rpc(_P1, "eth_blockNumber")
        assert status_down == 503, f"expected 503 on overridden method, got {status_down}"
        assert body_down == {}, f"down emits no body, got {body_down!r}"

        status_ok, body_ok = _rpc(_P1, "eth_getBlockByNumber", ["latest", False])
        assert status_ok == 200, f"non-overridden method should succeed, got {status_ok}"
        assert "result" in body_ok
        assert "error" not in body_ok

    @pytest.mark.timeout(10)
    def test_per_method_latency_ms_isolates_to_named_method(self, sim):
        """A per-method ``latency_ms`` only delays the named method.

        Provider eth-sim:1 is configured with provider-wide ``latency_ms=0``
        and a per-method ``latency_ms=500`` for ``eth_getBlockByNumber``. We
        assert:

          * eth_getBlockByNumber takes at least ~500ms (with a small slack
            for HTTP framing) — the override fires.
          * eth_blockNumber finishes well under that — no override, no
            delay.
        """
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "latency_ms": 0,
                        "responses": {
                            "eth_getBlockByNumber": {"latency_ms": 500},
                        },
                    }
                }
            },
        )

        t0 = time.monotonic()
        _rpc(_P1, "eth_getBlockByNumber", ["latest", False])
        elapsed_overridden_ms = (time.monotonic() - t0) * 1000
        assert (
            elapsed_overridden_ms >= 480
        ), f"overridden method should sleep ~500ms, elapsed={elapsed_overridden_ms:.0f}ms"

        t1 = time.monotonic()
        _rpc(_P1, "eth_blockNumber")
        elapsed_other_ms = (time.monotonic() - t1) * 1000
        assert elapsed_other_ms < 200, f"non-overridden method should not sleep, elapsed={elapsed_other_ms:.0f}ms"

    @pytest.mark.timeout(10)
    def test_per_key_fallback_inherits_provider_wide_keys(self, sim):
        """A partial per-method entry inherits provider-wide fault keys.

        Provider eth-sim:1 has provider-wide ``latency_ms=100, mode=success``
        and a per-method override ``eth_blockNumber: {"mode": "down"}``. The
        per-method entry does NOT set ``latency_ms``, so the merged config
        for ``eth_blockNumber`` should be ``{mode: down, latency_ms: 100,
        ...}``.

        Expected wire behaviour:
          * eth_blockNumber → 503 (mode=down wins) AND elapsed >= 100ms
            (latency_ms inherited from provider-wide config).
        """
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "latency_ms": 100,
                        "responses": {
                            "eth_blockNumber": {"mode": "down"},
                        },
                    }
                }
            },
        )

        t0 = time.monotonic()
        status, body = _rpc(_P1, "eth_blockNumber")
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert status == 503, f"per-method mode=down should win, got {status}"
        assert body == {}, f"down emits no body, got {body!r}"
        assert elapsed_ms >= 80, f"provider-wide latency_ms=100 should still apply, elapsed={elapsed_ms:.0f}ms"

    @pytest.mark.timeout(10)
    def test_per_method_mode_error_is_rejected_with_400(self, sim):
        """Per-method ``mode: "error"`` is rejected at /scenario POST time.

        Why: the per-method error semantic is owned by the existing
        ``error_stub`` / ``error`` keys on the same method-cfg dict (resolved
        by the chain's success builder). Allowing ``mode: "error"`` here
        would silently shadow that path because the merged fault ladder runs
        before the chain ever reads the error_stub. We surface the conflict
        at config-time with a 400.
        """
        status, body = _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "responses": {
                            "eth_blockNumber": {"mode": "error"},
                        },
                    }
                }
            },
        )
        assert status == 400, f"expected 400 on mode=error override, got {status}"
        assert "error" in body, f"expected error payload, got {body!r}"
        assert (
            "error" in body["error"].lower() or "mode" in body["error"].lower()
        ), f"error message should mention the offending key, got {body['error']!r}"

    @pytest.mark.timeout(10)
    def test_per_method_down_records_method_req_id_and_latency_in_history(self, sim):
        """Per-method ``mode: down`` must record the real method, request_id,
        and latency_ms in the sim history — not placeholder values.

        Background
        ----------
        Provider-wide ``mode: down`` is evaluated pre-body-parse, so the
        listener has no method label or req_id available — its history entry
        carries method ``"*"``, ``request_id=None``. That's expected at that
        layer.

        Per-method ``mode: down`` is different: the body had to be parsed to
        find the override, so the real method / request id are known and the
        configured per-method latency applies. The listener must record those
        real values; an earlier flat-handler draft hardcoded ``"*" / None /
        0`` even on the post-parse path, which silently broke
        ``/history?method=X``, ``/history?request_id=Y``, and any
        latency-based assertion on per-method down outcomes. This test pins
        the behaviour so a refactor can't re-introduce the placeholders.

        Scenario
        --------
        eth-sim:1: provider-wide ``mode=success`` with a per-method override
        ``eth_blockNumber: {mode: down, latency_ms: 50}``. The provider-wide
        config is healthy, so the body is parsed normally and the per-method
        down branch fires post-parse.

        Assertions
        ----------
          * HTTP 503 (confirms the per-method down branch fired).
          * /history filtered to eth-sim:1 returns exactly 1 entry.
          * entry["method"] == "eth_blockNumber"  (NOT "*")
          * entry["request_id"] == the request id we sent  (NOT None)
          * entry["latency_ms"] >= 40  (the 50ms per-method latency — NOT 0)
          * entry["status"] == "down"
          * /history?method=eth_blockNumber resolves to the same entry.
        """
        request_id = 4242
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "responses": {
                            "eth_blockNumber": {
                                "mode": "down",
                                "latency_ms": 50,
                            },
                        },
                    }
                }
            },
        )

        status, body = _post(
            _P1,
            {"jsonrpc": "2.0", "id": request_id, "method": "eth_blockNumber", "params": []},
        )
        assert status == 503, f"per-method down should emit 503, got {status}"
        assert body == {}, f"down emits no body, got {body!r}"

        # Read /history filtered to eth-sim:1 — the test scenario only
        # configures that provider, but the filter makes the assertion robust
        # if a future test edit adds traffic elsewhere.
        with urllib.request.urlopen(_ctrl(sim, "/history?pool=eth-sim&pid=1"), timeout=5) as resp:
            hist = json.loads(resp.read())

        # /history returns {"count": N, "history": [...]} — peel out the list.
        entries = hist["history"]
        assert isinstance(entries, list), f"unexpected /history shape: {hist!r}"
        assert len(entries) == 1, f"expected exactly 1 history entry for eth-sim:1, got {len(entries)}: {entries!r}"

        entry = entries[0]
        assert entry["method"] == "eth_blockNumber", (
            f"history method must be the real method, not placeholder. "
            f"Got method={entry['method']!r}. If '*': the per-method down "
            f"branch reverted to the provider-wide pre-parse placeholder."
        )
        assert entry["request_id"] == request_id, (
            f"history request_id must echo the request id, not None. "
            f"Got request_id={entry['request_id']!r} (sent id={request_id}). "
            f"If None: same site as the method regression."
        )
        assert entry["latency_ms"] >= 40, (
            f"history latency_ms must reflect the configured 50ms per-method "
            f"latency, not the placeholder 0. Got latency_ms={entry['latency_ms']}."
        )
        assert entry["status"] == "down", f"status should be 'down', got {entry['status']!r}"

        # Filter sanity: /history?method=eth_blockNumber must resolve to this
        # entry. This only works if method was labelled correctly (above);
        # the redundant check guards against a future regression where the
        # entry stores the right method but the filter path breaks.
        with urllib.request.urlopen(
            _ctrl(sim, "/history?pool=eth-sim&pid=1&method=eth_blockNumber"), timeout=5
        ) as resp:
            filtered = json.loads(resp.read())
        filtered_entries = filtered["history"]
        assert len(filtered_entries) == 1, (
            f"/history?method=eth_blockNumber must return the entry; got "
            f"{filtered_entries!r}. If empty: method label in history is wrong."
        )

    @pytest.mark.timeout(10)
    def test_composition_order_latency_first_then_fault(self, sim):
        """Per-method composition is latency FIRST, then fault.

        Override: ``eth_blockNumber: {latency_ms: 200, mode: rate_limit}``.
        We assert both:

          * the response is the rate_limit shape (HTTP 429, prose body —
            not a JSON-RPC envelope; see
            tests/test_simulator.py::test_rate_limit_returns_429), AND
          * the elapsed time is >= ~200ms — proving the per-method
            latency was paid before the fault response was emitted.

        Mirrors the provider-wide order (latency injected before the fault
        response is written to the wire).
        """
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "responses": {
                            "eth_blockNumber": {
                                "latency_ms": 200,
                                "mode": "rate_limit",
                            },
                        },
                    }
                }
            },
        )

        t0 = time.monotonic()
        status, body = _rpc(_P1, "eth_blockNumber")
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert status == 429, f"expected rate_limit (429), got {status}"
        assert isinstance(body, str), f"rate_limit body should be prose, not JSON, got {body!r}"
        assert not body.lstrip().startswith("{"), f"rate_limit body must not look like JSON; got {body!r}"
        assert elapsed_ms >= 180, f"per-method latency should fire before fault, elapsed={elapsed_ms:.0f}ms"


# ─────────────────────────────────────────────────────────────────────────────
# Per-method body+status override behaviour
# ─────────────────────────────────────────────────────────────────────────────


class TestPerMethodBodyOverride:
    """Per-method body+status override.

    Extends the per-method ``responses`` dict with two optional keys:

      * ``status`` — HTTP status code, must be 2xx (200-299), default 200.
      * ``body``   — response body dict, JSON-encoded onto the wire.

    When ``body`` is set on a method entry, the sim returns ``{status, body}``
    directly and bypasses the chain's healthy-stub lookup. ``body`` is
    mutually exclusive with ``mode`` (validated at /scenario POST time); it
    composes with ``latency_ms`` in the documented order (latency first,
    then body).

    Why: lets a test pin a provider to "200 OK with this exact body" without
    routing through the chain-specific success-shape builder — e.g. a router
    test that needs a 200 carrying an application-level failure body
    (``{"success": false, ...}``).
    """

    @pytest.mark.timeout(10)
    def test_body_override_returns_custom_status_and_body(self, sim):
        """Given a per-method override with ``status: 200`` and a custom
        ``body``, the sim returns exactly that wire shape — HTTP 200 and the
        body verbatim — instead of the chain success stub.
        """
        override_body = {"success": False, "error": "oops"}
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "responses": {
                            "eth_blockNumber": {
                                "status": 200,
                                "body": override_body,
                            },
                        },
                    }
                }
            },
        )

        status, body = _rpc(_P1, "eth_blockNumber")
        assert status == 200, f"expected override status 200, got {status}"
        assert body == override_body, (
            f"expected exact override body {override_body!r}, got {body!r}. "
            f"Extra keys indicate the chain builder ran instead of the bypass."
        )

    @pytest.mark.timeout(10)
    def test_body_override_default_status_is_200(self, sim):
        """When ``status`` is omitted on the body override, the sim defaults
        to HTTP 200."""
        override_body = {"custom": True}
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "responses": {
                            "eth_blockNumber": {"body": override_body},
                        },
                    }
                }
            },
        )

        status, body = _rpc(_P1, "eth_blockNumber")
        assert status == 200, f"expected default status 200, got {status}"
        assert body == override_body, f"expected {override_body!r}, got {body!r}"

    @pytest.mark.timeout(10)
    def test_body_override_rejects_non_2xx_status(self, sim):
        """Setting ``status`` outside the 2xx band is rejected at /scenario
        POST time with HTTP 400.

        The body override is for "this 200 OK response with a custom body"
        scenarios. Non-2xx response shapes (4xx / 5xx) are already covered
        by ``mode: "error"`` + ``http_status`` / ``error_code`` /
        ``error_message`` — that path owns the error-envelope wire shape.
        """
        status, body = _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "responses": {
                            "eth_blockNumber": {
                                "status": 500,
                                "body": {"oops": True},
                            },
                        },
                    }
                }
            },
        )
        assert status == 400, f"expected 400 for non-2xx body override status, got {status}"
        assert "error" in body, f"expected error payload, got {body!r}"
        err_msg = body["error"].lower()
        assert "status" in err_msg or "2xx" in err_msg, f"error should mention status / 2xx, got {body['error']!r}"

    @pytest.mark.timeout(10)
    def test_body_override_with_latency_applies_latency_first(self, sim):
        """A body override composes with ``latency_ms`` in the same order as
        every other per-method primitive: latency FIRST, then the body
        response."""
        override_body = {"ok": True}
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "responses": {
                            "eth_blockNumber": {
                                "latency_ms": 200,
                                "body": override_body,
                            },
                        },
                    }
                }
            },
        )

        t0 = time.monotonic()
        status, body = _rpc(_P1, "eth_blockNumber")
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert status == 200, f"expected status 200, got {status}"
        assert body == override_body, f"expected {override_body!r}, got {body!r}"
        assert elapsed_ms >= 180, (
            f"latency should fire before body override, elapsed={elapsed_ms:.0f}ms. "
            f"If under ~180ms, the latency_ms sleep is being skipped on the body path."
        )

    @pytest.mark.timeout(10)
    def test_body_and_mode_mutually_exclusive(self, sim):
        """Setting both ``body`` and ``mode`` on the same method entry is
        rejected at /scenario POST time with HTTP 400.

        ``body`` describes a custom success response; ``mode`` describes a
        fault primitive. They describe different outcomes, and the wire can
        only emit one — the config is rejected up front instead of silently
        picking.
        """
        status, body = _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "responses": {
                            "eth_blockNumber": {
                                "mode": "down",
                                "body": {"ok": True},
                            },
                        },
                    }
                }
            },
        )
        assert status == 400, f"expected 400 for body+mode combination, got {status}"
        assert "error" in body, f"expected error payload, got {body!r}"
        err_msg = body["error"].lower()
        assert (
            "body" in err_msg and "mode" in err_msg
        ) or "mutually exclusive" in err_msg, f"error should mention body+mode conflict, got {body['error']!r}"

    @pytest.mark.timeout(10)
    def test_body_override_bypasses_healthy_stub(self, sim):
        """The body override returns exactly the configured payload — no
        keys from the healthy stub leak through, even when the chosen body
        doesn't look like a valid JSON-RPC response."""
        override_body = {"completely": "unrelated", "fields": [1, 2, 3]}
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "responses": {
                            "eth_blockNumber": {"body": override_body},
                        },
                    }
                }
            },
        )

        status, body = _rpc(_P1, "eth_blockNumber")
        assert status == 200, f"expected status 200, got {status}"
        assert body == override_body, (
            f"override body must be returned verbatim. "
            f"Expected {override_body!r}, got {body!r}. "
            f"Extra keys would indicate the chain builder merged stub fields."
        )
        # Belt-and-braces: explicitly assert the canonical JSON-RPC envelope
        # keys are absent. If a future refactor adds a "helpful" wrap, this
        # catches it even if the override dict accidentally collides on keys.
        for leaked_key in ("jsonrpc", "result", "id"):
            assert (
                leaked_key not in body
            ), f"healthy-stub key {leaked_key!r} leaked into body override response: {body!r}"

    @pytest.mark.timeout(10)
    def test_body_override_history_status_is_success_regardless_of_body_shape(self, sim):
        """Body-override responses always record status="success" in /history,
        regardless of body content.

        The body override is validated 2xx-only at /scenario time, so by
        HTTP semantics the request always succeeds. The body itself is
        arbitrary test-supplied content — a body of
        {"success": false, "error": "rate limit"} is still an HTTP-200
        success from the simulator's perspective. Inferring history status
        from body content (recording "error" when the body has an "error"
        key) would silently mis-classify that shape, so both shapes are
        pinned to "success" here.
        """
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "mode": "success",
                        "responses": {
                            "eth_blockNumber": {
                                "body": {"success": False, "error": "rate limit"},
                            },
                            "eth_chainId": {
                                "body": {"data": {"error_count": 0}},
                            },
                        },
                    }
                }
            },
        )

        status_err_shape, body_err_shape = _rpc(_P1, "eth_blockNumber")
        assert status_err_shape == 200
        assert body_err_shape == {"success": False, "error": "rate limit"}

        status_ok_shape, body_ok_shape = _rpc(_P1, "eth_chainId")
        assert status_ok_shape == 200
        assert body_ok_shape == {"data": {"error_count": 0}}

        with urllib.request.urlopen(_ctrl(sim, "/history?pool=eth-sim&pid=1"), timeout=5) as resp:
            entries = json.loads(resp.read())["history"]
        assert len(entries) == 2, f"expected 2 entries, got {len(entries)}: {entries!r}"
        for entry in entries:
            assert entry["status"] == "success", (
                f"body-override history status must be 'success' regardless "
                f"of body shape (HTTP-2xx by construction). Got status="
                f"{entry['status']!r} on method={entry['method']!r}. If "
                f"'error': body-content inference has been re-introduced."
            )

        with urllib.request.urlopen(_ctrl(sim, "/history?pool=eth-sim&pid=1&status=success"), timeout=5) as resp:
            success_entries = json.loads(resp.read())["history"]
        assert len(success_entries) == 2, (
            f"/history?status=success must return both body-override entries; "
            f"got {len(success_entries)}: {success_entries!r}"
        )

        with urllib.request.urlopen(_ctrl(sim, "/history?pool=eth-sim&pid=1&status=error"), timeout=5) as resp:
            error_entries = json.loads(resp.read())["history"]
        assert error_entries == [], (
            f"/history?status=error must not match HTTP-200 body overrides; " f"got {error_entries!r}"
        )
