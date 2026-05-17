"""
MAG-1821 — per-method overrides for the JSON-RPC provider simulator.

These tests exercise the per-method override path on the simulator's
``responses`` dict: a test can set ``latency_ms``, ``mode``, or
``rate_limit`` (and the keys those modes need) for one specific JSON-RPC
method on a provider, while every other method on the same provider keeps
the provider-wide config.

What they cover
---------------
  1. per-method ``mode: down``           — the override fires only on the
                                            named method; other methods on
                                            the same provider stay healthy.
  2. per-method ``latency_ms``           — the override fires only on the
                                            named method; non-overridden
                                            methods see no delay.
  3. per-key fallback                    — a partial per-method entry
                                            inherits provider-wide fault
                                            keys it doesn't override
                                            (Q4 of MAG-1821).
  4. ``mode == "error"`` is rejected     — the validation pass in
                                            ``_normalise_responses`` raises;
                                            the /scenario POST returns 400.
  5. composition order — latency FIRST   — per-method latency is paid even
                                            when the fault outcome is
                                            rate_limit (Q2 of MAG-1821).

Reuses the in-process fixture pattern from ``tests/test_simulator.py``
(separate test ports so a parallel run doesn't collide with the existing
test module).

Run with:
  pytest tests/test_simulator_per_method_overrides.py -v
"""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer, ThreadingHTTPServer

import pytest

from server import ControlHandler, JSONRPCHandler, ProviderState

# ── Test ports (58xxx range — distinct from every other test module's port
#     selection: 28xxx/29000 for test_simulator, 38xxx/39000 for the BTC
#     suite, 48xxx/49000 for rest / ws / logs_lag, 49545+ for grpc). The
#     other modules occasionally reuse port ranges across each other and
#     rely on serial execution; this one picks an isolated band so a
#     parallel pytest run (e.g. -p xdist) doesn't collide with any of
#     them. The module-scoped sim fixture only binds the ports once, so
#     even a serial run can't double-bind here either.) ────────────────────

_PROVIDER_PORTS = {"1": 58545, "2": 58546, "3": 58547}
_CONTROL_PORT   = 59000


# ── HTTP helpers (same shape as tests/test_simulator.py) ──────────────────────

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


def _rpc(url: str, method: str, params: list | None = None) -> tuple[int, dict]:
    """Send a JSON-RPC request, return (http_status, response_body)."""
    return _post(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})


def _ctrl(sim: dict, path: str) -> str:
    return sim["control"] + path


# ── Module-scoped fixture: start all servers once ─────────────────────────────

@pytest.fixture(scope="module")
def sim():
    """Start 3 JSON-RPC servers + 1 control server on test ports.

    Yields a dict with base URLs:
      sim["control"]   → http://127.0.0.1:59000
      sim["provider1"] → http://127.0.0.1:58545
      sim["provider2"] → http://127.0.0.1:58546
      sim["provider3"] → http://127.0.0.1:58547
    """
    states = {pid: ProviderState() for pid in _PROVIDER_PORTS}

    servers = []
    for pid, port in _PROVIDER_PORTS.items():
        srv = ThreadingHTTPServer(("127.0.0.1", port), JSONRPCHandler)
        srv.daemon_threads = True
        srv.state       = states[pid]
        srv.provider_id = pid
        servers.append(srv)

    ctrl                 = HTTPServer(("127.0.0.1", _CONTROL_PORT), ControlHandler)
    ctrl.provider_states = states
    servers.append(ctrl)

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()

    time.sleep(0.15)  # allow servers to finish binding

    yield {
        "control":   f"http://127.0.0.1:{_CONTROL_PORT}",
        "provider1": f"http://127.0.0.1:{_PROVIDER_PORTS['1']}",
        "provider2": f"http://127.0.0.1:{_PROVIDER_PORTS['2']}",
        "provider3": f"http://127.0.0.1:{_PROVIDER_PORTS['3']}",
    }

    for s in servers:
        s.shutdown()


# ── Function-scoped autouse: clean slate before/after every test ──────────────

@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario config AND clear history before and after every test."""
    _post(_ctrl(sim, "/reset/all"), {})
    yield
    _post(_ctrl(sim, "/reset/all"), {})


# ─────────────────────────────────────────────────────────────────────────────
# MAG-1821 — per-method override behaviour
# ─────────────────────────────────────────────────────────────────────────────

class TestPerMethodOverrides:

    @pytest.mark.timeout(10)
    def test_per_method_mode_down_isolates_to_named_method(self, sim):
        """A per-method ``mode: down`` fires only for the named method.

        Provider 1 starts healthy. We pin ``eth_blockNumber`` to ``down``
        via the per-method override. The expectation is:

          * eth_blockNumber → HTTP 503 (the down primitive — 503 with no
                              body, set by the provider-wide down branch
                              equivalent at the per-method layer)
          * eth_getBlockByNumber → 200 success (no override, inherits
                                   the provider-wide healthy config)

        This is the isolation guarantee that distinguishes per-method
        overrides from provider-wide ``mode: down`` (which kills every
        method on the provider).
        """
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "mode": "success",
                    "responses": {
                        "eth_blockNumber": {"mode": "down"},
                    },
                }
            }
        })

        status_down, body_down = _rpc(sim["provider1"], "eth_blockNumber")
        assert status_down == 503, f"expected 503 on overridden method, got {status_down}"
        assert body_down == {}, f"down emits no body, got {body_down!r}"

        status_ok, body_ok = _rpc(sim["provider1"], "eth_getBlockByNumber", ["latest", False])
        assert status_ok == 200, f"non-overridden method should succeed, got {status_ok}"
        assert "result" in body_ok
        assert "error" not in body_ok

    @pytest.mark.timeout(10)
    def test_per_method_latency_ms_isolates_to_named_method(self, sim):
        """A per-method ``latency_ms`` only delays the named method.

        Provider 1 is configured with provider-wide ``latency_ms=0`` and
        a per-method ``latency_ms=500`` for ``eth_getBlockByNumber``. We
        assert:

          * eth_getBlockByNumber takes at least ~500ms (with a small slack
            for HTTP framing) — the override fires.
          * eth_blockNumber finishes well under that — no override, no
            delay.
        """
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "mode": "success",
                    "latency_ms": 0,
                    "responses": {
                        "eth_getBlockByNumber": {"latency_ms": 500},
                    },
                }
            }
        })

        t0 = time.monotonic()
        _rpc(sim["provider1"], "eth_getBlockByNumber", ["latest", False])
        elapsed_overridden_ms = (time.monotonic() - t0) * 1000
        assert elapsed_overridden_ms >= 480, (
            f"overridden method should sleep ~500ms, elapsed={elapsed_overridden_ms:.0f}ms"
        )

        t1 = time.monotonic()
        _rpc(sim["provider1"], "eth_blockNumber")
        elapsed_other_ms = (time.monotonic() - t1) * 1000
        assert elapsed_other_ms < 200, (
            f"non-overridden method should not sleep, elapsed={elapsed_other_ms:.0f}ms"
        )

    @pytest.mark.timeout(10)
    def test_per_key_fallback_inherits_provider_wide_keys(self, sim):
        """Q4: a partial per-method entry inherits provider-wide fault keys.

        Provider 1 has provider-wide ``latency_ms=100, mode=success`` and a
        per-method override ``eth_blockNumber: {"mode": "down"}``. The
        per-method entry does NOT set ``latency_ms``, so the merged config
        for ``eth_blockNumber`` should be ``{mode: down, latency_ms: 100,
        ...}``.

        Expected wire behaviour:
          * eth_blockNumber → 503 (mode=down wins) AND elapsed >= 100ms
            (latency_ms inherited from provider-wide config).
        """
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "mode": "success",
                    "latency_ms": 100,
                    "responses": {
                        "eth_blockNumber": {"mode": "down"},
                    },
                }
            }
        })

        t0 = time.monotonic()
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert status == 503, f"per-method mode=down should win, got {status}"
        assert body == {}, f"down emits no body, got {body!r}"
        assert elapsed_ms >= 80, (
            f"provider-wide latency_ms=100 should still apply, elapsed={elapsed_ms:.0f}ms"
        )

    @pytest.mark.timeout(10)
    def test_per_method_mode_error_is_rejected_with_400(self, sim):
        """Per-method ``mode: "error"`` is rejected at /scenario POST time.

        Why: the per-method error semantic is owned by the existing
        ``error_stub`` / ``error`` keys on the same method-cfg dict
        (resolved in handlers_eth). Allowing ``mode: "error"`` here would
        silently shadow that path because the merged-snap fault branch
        in _apply_fault runs before handlers_eth ever reads the
        error_stub. We surface the conflict at config-time with a 400.
        """
        status, body = _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "responses": {
                        "eth_blockNumber": {"mode": "error"},
                    },
                }
            }
        })
        assert status == 400, f"expected 400 on mode=error override, got {status}"
        assert "error" in body, f"expected error payload, got {body!r}"
        assert "error" in body["error"].lower() or "mode" in body["error"].lower(), (
            f"error message should mention the offending key, got {body['error']!r}"
        )

    @pytest.mark.timeout(10)
    def test_per_method_down_records_method_req_id_and_latency_in_history(self, sim):
        """Per-method ``mode: down`` must record the real method, request_id,
        and latency_ms in the sim history — not placeholder values
        (regression for commit 924f268).

        Background
        ----------
        Provider-wide ``mode: down`` is evaluated pre-body-parse, so the
        handler has no method label or req_id available and passes
        ``method="*"``, ``req_id=None``, ``t_start=now()`` into
        ``_apply_fault`` — yielding a history entry with method ``"*"``,
        ``request_id=None``, ``latency_ms≈0``. That's expected at that
        layer.

        Per-method ``mode: down`` is different: it's the post-parse path
        in ``JSONRPCHandler.do_POST`` (server.py:535), where the body has
        already been parsed and the inherited per-method ``latency_ms``
        may have been slept. The handler must pass the *real* values into
        ``_apply_fault``, and ``_apply_fault``'s down branch
        (server.py:420-423) must record them — using ``method``, ``req_id``,
        and ``_elapsed_ms(t_start)`` rather than the provider-wide-down
        placeholders.

        An earlier draft of the per-method down branch hardcoded ``"*" /
        None / 0`` even on the post-parse path, which silently broke
        ``/history?method=X``, ``/history?request_id=Y``, and any
        latency-based assertion on per-method down outcomes. The fix
        Denis flagged in code review (924f268) routes the real method /
        req_id / elapsed time through. This test pins that behaviour so
        a future refactor can't re-introduce the placeholders quietly.

        Scenario
        --------
        Provider 1: provider-wide ``mode=success`` with a per-method
        override ``eth_blockNumber: {mode: down, latency_ms: 50}``.
        The provider-wide config is healthy, so the body is parsed
        normally and the per-method down branch fires post-parse.

        Setup
        -----
          * The autouse ``clean_state`` fixture has already POSTed
            ``/reset/all`` before the test runs, so history starts empty.
          * POST ``/scenario`` with the per-method override above.

        Act
        ---
        Send one JSON-RPC POST for ``eth_blockNumber`` with a known
        ``id`` field. Expect HTTP 503 (per-method down primitive).

        Assertions
        ----------
          * HTTP 503 (confirms the per-method down branch fired).
          * /history filtered to ``provider=1`` returns exactly 1 entry.
          * entry["method"] == "eth_blockNumber"  (NOT "*")
          * entry["request_id"] == the request id we sent  (NOT None)
          * entry["latency_ms"] >= 40  (real elapsed time covering the
            50ms inherited per-method latency — NOT the placeholder 0)
          * entry["status"] == "down"
          * /history?method=eth_blockNumber resolves to the same entry
            (filter must work, which only happens if method is correctly
            labelled).

        How to read a failure
        ---------------------
          * ``entry["method"] == "*"`` → the per-method down branch is
            back on the provider-wide-down placeholder. Check
            server.py:420-423 and the post-parse call site at
            server.py:535 — both must pass the real label through.
          * ``entry["request_id"] is None`` → same root cause; req_id was
            replaced with the placeholder ``None``.
          * ``entry["latency_ms"] == 0`` (or < 40) → ``t_start`` is being
            reset to ``now()`` inside the down branch, or ``_elapsed_ms``
            was swapped for a literal ``0``. The per-method ``latency_ms``
            sleep ran before ``_apply_fault`` (server.py:531-532), so
            elapsed should be ~50ms+.
          * The /history?method= filter returns no results → method label
            is wrong even though the bare entry might look right; check
            the filter logic in ControlHandler.do_GET (server.py:1344).
        """
        request_id = 4242
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "mode": "success",
                    "responses": {
                        "eth_blockNumber": {
                            "mode": "down",
                            "latency_ms": 50,
                        },
                    },
                }
            }
        })

        status, body = _post(
            sim["provider1"],
            {"jsonrpc": "2.0", "id": request_id, "method": "eth_blockNumber", "params": []},
        )
        assert status == 503, f"per-method down should emit 503, got {status}"
        assert body == {}, f"down emits no body, got {body!r}"

        # Read /history filtered to provider 1 — the test scenario only
        # configures provider 1, but ?provider=1 makes the assertion robust
        # if a future test edit adds traffic elsewhere. Inline urllib GET
        # rather than adding a _get helper for two call sites.
        with urllib.request.urlopen(_ctrl(sim, "/history?provider=1"), timeout=5) as resp:
            hist = json.loads(resp.read())

        # /history returns {"count": N, "history": [...]} — peel out the list.
        entries = hist["history"]
        assert isinstance(entries, list), f"unexpected /history shape: {hist!r}"
        assert len(entries) == 1, (
            f"expected exactly 1 history entry for provider 1, got {len(entries)}: {entries!r}"
        )

        entry = entries[0]
        assert entry["method"] == "eth_blockNumber", (
            f"history method must be the real method, not placeholder. "
            f"Got method={entry['method']!r}. If '*': per-method down branch "
            f"reverted to provider-wide placeholder (server.py:420-423)."
        )
        assert entry["request_id"] == request_id, (
            f"history request_id must echo the request id, not None. "
            f"Got request_id={entry['request_id']!r} (sent id={request_id}). "
            f"If None: same site as the method regression."
        )
        assert entry["latency_ms"] >= 40, (
            f"history latency_ms must reflect real elapsed time including the "
            f"inherited 50ms per-method latency, not the placeholder 0. "
            f"Got latency_ms={entry['latency_ms']}. If 0: t_start or _elapsed_ms "
            f"was bypassed in the per-method down branch."
        )
        assert entry["status"] == "down", (
            f"status should be 'down', got {entry['status']!r}"
        )

        # Filter sanity: /history?method=eth_blockNumber must resolve to this
        # entry. This only works if method was labelled correctly (above);
        # the redundant check guards against a future regression where the
        # entry stores the right method but the filter path breaks.
        with urllib.request.urlopen(
            _ctrl(sim, "/history?provider=1&method=eth_blockNumber"), timeout=5
        ) as resp:
            filtered = json.loads(resp.read())
        filtered_entries = filtered["history"]
        assert len(filtered_entries) == 1, (
            f"/history?method=eth_blockNumber must return the entry; got "
            f"{filtered_entries!r}. If empty: method label in history is wrong."
        )

    @pytest.mark.timeout(10)
    def test_composition_order_latency_first_then_fault(self, sim):
        """Q2: per-method composition is latency FIRST, then fault.

        Override: ``eth_blockNumber: {latency_ms: 200, mode: rate_limit}``.
        We assert both:

          * the response is the rate_limit envelope (HTTP 429), AND
          * the elapsed time is >= ~200ms — proving the per-method
            latency was paid before the fault response was emitted.

        Mirrors the provider-wide order (latency injection in do_POST
        before _apply_fault evaluates the fault primitives).
        """
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "mode": "success",
                    "responses": {
                        "eth_blockNumber": {
                            "latency_ms": 200,
                            "mode": "rate_limit",
                        },
                    },
                }
            }
        })

        t0 = time.monotonic()
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert status == 429, f"expected rate_limit (429), got {status}"
        assert "error" in body, f"rate_limit body should carry an error envelope, got {body!r}"
        assert body["error"]["code"] == 429
        assert elapsed_ms >= 180, (
            f"per-method latency should fire before fault, elapsed={elapsed_ms:.0f}ms"
        )
