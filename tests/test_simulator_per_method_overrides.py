"""
MAG-1821 + MAG-1846 — per-method overrides for the JSON-RPC provider simulator.

These tests exercise the per-method override path on the simulator's
``responses`` dict: a test can set ``latency_ms``, ``mode``, or
``rate_limit`` (and the keys those modes need) for one specific JSON-RPC
method on a provider, while every other method on the same provider keeps
the provider-wide config. MAG-1846 extends this with ``body`` + ``status``
override keys that emit a custom 2xx response and bypass the chain handler.

What they cover
---------------
  MAG-1821:
    1. per-method ``mode: down``           — fires only on the named method.
    2. per-method ``latency_ms``           — fires only on the named method.
    3. per-key fallback                    — partial entries inherit
                                              provider-wide fault keys.
    4. ``mode == "error"`` is rejected     — /scenario POST returns 400.
    5. composition order — latency FIRST   — latency paid even on fault.

  MAG-1846 (TestPerMethodBodyOverride):
    6. body+status returns custom shape    — exact wire bytes, no chain
                                              handler involvement.
    7. status defaults to 200              — omitting ``status`` is fine.
    8. non-2xx status is rejected          — 4xx/5xx via mode=error path.
    9. body+latency composes               — latency first, then body.
   10. body+mode mutually exclusive        — 400 from /scenario.
   11. body bypasses healthy stub          — no stub fields leak through.

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


# ─────────────────────────────────────────────────────────────────────────────
# MAG-1846 — per-method body+status override behaviour
# ─────────────────────────────────────────────────────────────────────────────

class TestPerMethodBodyOverride:
    """Per-method body+status override (MAG-1846).

    Extends MAG-1821's per-method ``responses`` dict with two optional keys:

      * ``status`` — HTTP status code, must be 2xx (200-299), default 200.
      * ``body``   — response body dict, JSON-encoded onto the wire.

    When ``body`` is set on a method entry, the sim returns ``{status, body}``
    directly and bypasses the healthy-stub lookup in handlers_eth /
    handlers_btc. ``body`` is mutually exclusive with ``mode`` (validated at
    /scenario POST time); it composes with ``latency_ms`` in the documented
    order (latency first, then body).

    Why: lets a test pin a provider to "200 OK with this exact body" without
    routing through the chain-specific success-shape builder. Specifically
    unblocks ``test_status_200_with_body_error_passes_through`` in
    smart-router-automation, where the router needs to receive a 200 carrying
    an application-level failure body (e.g. ``{"success": false, ...}``).
    """

    @pytest.mark.timeout(10)
    def test_body_override_returns_custom_status_and_body(self, sim):
        """Given a per-method override with ``status: 200`` and a custom
        ``body``, the sim returns exactly that wire shape — HTTP 200 and the
        body verbatim — instead of the chain-handler success stub.

        Background
        ----------
        Without this override, every ``eth_blockNumber`` response on the eth
        chain_family is built by ``handlers_eth.handle``. Tests that need to
        assert router behaviour against a non-standard success body (e.g. a
        provider that returns 200 OK with ``{"success": false, "error": ...}``)
        had no way to drive the sim into that shape.

        Setup
        -----
        Provider 1 is left at its default ``mode: success``. POST /scenario
        with a per-method ``responses`` entry pinning ``eth_blockNumber`` to
        ``{"status": 200, "body": {"success": False, "error": "oops"}}``.

        Act
        ---
        Send one JSON-RPC POST for ``eth_blockNumber``.

        Assertions
        ----------
          * HTTP status == 200 (the override status).
          * Response body equals the override dict exactly — no extra keys
            from the chain handler, no JSON-RPC wrapping, no result field.

        How to read a failure
        ---------------------
          * Status 200 but body has ``"result"`` / ``"jsonrpc"`` keys → the
            chain handler still ran; the bypass branch in do_POST wasn't
            taken. Check the ``"body" in method_snap`` guard at
            server.py do_POST.
          * Status != 200 → the override status wasn't read; check
            ``method_snap.get("status", 200)`` resolution.
        """
        override_body = {"success": False, "error": "oops"}
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "mode": "success",
                    "responses": {
                        "eth_blockNumber": {
                            "status": 200,
                            "body": override_body,
                        },
                    },
                }
            }
        })

        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200, f"expected override status 200, got {status}"
        assert body == override_body, (
            f"expected exact override body {override_body!r}, got {body!r}. "
            f"Extra keys indicate the chain handler ran instead of the bypass."
        )

    @pytest.mark.timeout(10)
    def test_body_override_default_status_is_200(self, sim):
        """When ``status`` is omitted on the body override, the sim defaults
        to HTTP 200.

        Background
        ----------
        The body override declares its 2xx-only contract at /scenario time;
        leaving ``status`` unset should not require the test to spell out
        the default. The handler resolves ``method_snap.get("status", 200)``.

        Setup
        -----
        Provider 1: ``mode: success`` + per-method override
        ``eth_blockNumber: {"body": {...}}`` (no ``status`` key).

        Act
        ---
        Send one JSON-RPC POST.

        Assertions
        ----------
          * HTTP status == 200 (the default).
          * Body matches the override.
        """
        override_body = {"custom": True}
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "mode": "success",
                    "responses": {
                        "eth_blockNumber": {"body": override_body},
                    },
                }
            }
        })

        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200, f"expected default status 200, got {status}"
        assert body == override_body, f"expected {override_body!r}, got {body!r}"

    @pytest.mark.timeout(10)
    def test_body_override_rejects_non_2xx_status(self, sim):
        """Setting ``status`` outside the 2xx band is rejected at /scenario
        POST time with HTTP 400.

        Background
        ----------
        The body override is for "this 200 OK response with a custom body"
        scenarios. Non-2xx response shapes (4xx / 5xx) are already covered
        by ``mode: "error"`` + ``http_status`` / ``error_code`` /
        ``error_message`` — that path owns the error-envelope wire shape.
        Letting body+status pretend to be a fault would silently bypass the
        envelope contract; we reject at config-time instead.

        Setup
        -----
        Issue POST /scenario with
        ``eth_blockNumber: {"status": 500, "body": {"oops": True}}``.

        Assertions
        ----------
          * /scenario returns HTTP 400.
          * Response body carries an ``"error"`` key with a message that
            mentions ``status`` or ``2xx``, so the test failure points at
            the right validation rule.
        """
        status, body = _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "responses": {
                        "eth_blockNumber": {
                            "status": 500,
                            "body": {"oops": True},
                        },
                    },
                }
            }
        })
        assert status == 400, f"expected 400 for non-2xx body override status, got {status}"
        assert "error" in body, f"expected error payload, got {body!r}"
        err_msg = body["error"].lower()
        assert "status" in err_msg or "2xx" in err_msg, (
            f"error should mention status / 2xx, got {body['error']!r}"
        )

    @pytest.mark.timeout(10)
    def test_body_override_with_latency_applies_latency_first(self, sim):
        """A body override composes with ``latency_ms`` in the same order as
        every other per-method primitive: latency FIRST, then the body
        response (Q2 of MAG-1821 carried forward).

        Background
        ----------
        Latency is injected once in do_POST, before any branch decides what
        to emit. So a per-method ``{latency_ms: 200, body: {...}}`` should
        sleep for ~200ms before writing the override body to the wire — the
        same composition order as ``{latency_ms: 200, mode: rate_limit}``
        (already pinned by ``test_composition_order_latency_first_then_fault``).

        Setup
        -----
        Provider 1: ``mode: success`` + per-method override
        ``eth_blockNumber: {"latency_ms": 200, "body": {"ok": True}}``.

        Act
        ---
        Send one JSON-RPC POST, measure elapsed wall-clock around it.

        Assertions
        ----------
          * HTTP status == 200 (override default).
          * Body equals the override.
          * Elapsed time >= ~180ms — proving latency was paid before
            the body was emitted. 180ms (not exactly 200ms) leaves slack
            for HTTP framing / scheduler jitter.
        """
        override_body = {"ok": True}
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "mode": "success",
                    "responses": {
                        "eth_blockNumber": {
                            "latency_ms": 200,
                            "body": override_body,
                        },
                    },
                }
            }
        })

        t0 = time.monotonic()
        status, body = _rpc(sim["provider1"], "eth_blockNumber")
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

        Background
        ----------
        ``body`` describes a custom success response; ``mode`` describes a
        fault primitive (down / hang / drop_connection / rate_limit). They
        describe different outcomes, and the wire can only emit one. Letting
        both coexist would force the sim to silently pick — instead, the
        config is rejected up front.

        Setup
        -----
        Issue POST /scenario with
        ``eth_blockNumber: {"mode": "down", "body": {"ok": True}}``.

        Assertions
        ----------
          * /scenario returns HTTP 400.
          * Response body carries an ``"error"`` with a message that
            mentions ``body`` and ``mode`` (or "mutually exclusive"), so a
            failure surfaces the rule.
        """
        status, body = _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "responses": {
                        "eth_blockNumber": {
                            "mode": "down",
                            "body": {"ok": True},
                        },
                    },
                }
            }
        })
        assert status == 400, f"expected 400 for body+mode combination, got {status}"
        assert "error" in body, f"expected error payload, got {body!r}"
        err_msg = body["error"].lower()
        assert ("body" in err_msg and "mode" in err_msg) or "mutually exclusive" in err_msg, (
            f"error should mention body+mode conflict, got {body['error']!r}"
        )

    @pytest.mark.timeout(10)
    def test_body_override_bypasses_healthy_stub(self, sim):
        """The body override returns exactly the configured payload — no
        keys from the healthy stub leak through, even when the chosen body
        doesn't look like a valid JSON-RPC response.

        Background
        ----------
        A regression-shaped test: if a future refactor wires the body
        override through the chain handler (instead of bypassing it), the
        handler might "helpfully" merge stub fields into the response.
        Pinning an obviously non-RPC-shaped body proves the bypass is
        absolute — nothing else gets added.

        Setup
        -----
        Provider 1: ``mode: success`` + per-method override
        ``eth_blockNumber: {"body": {"completely": "unrelated",
        "fields": [1, 2, 3]}}`` — no ``result`` / ``jsonrpc`` / ``id`` keys,
        so a chain-handler leak would be obvious.

        Act
        ---
        Send one JSON-RPC POST.

        Assertions
        ----------
          * HTTP status == 200.
          * Response body equals the override dict exactly — same keys,
            same values, no extras. ``"jsonrpc"`` / ``"result"`` / ``"id"``
            must be absent (their presence would prove the healthy stub
            was merged in).
        """
        override_body = {"completely": "unrelated", "fields": [1, 2, 3]}
        _post(_ctrl(sim, "/scenario"), {
            "providers": {
                "1": {
                    "mode": "success",
                    "responses": {
                        "eth_blockNumber": {"body": override_body},
                    },
                }
            }
        })

        status, body = _rpc(sim["provider1"], "eth_blockNumber")
        assert status == 200, f"expected status 200, got {status}"
        assert body == override_body, (
            f"override body must be returned verbatim. "
            f"Expected {override_body!r}, got {body!r}. "
            f"Extra keys would indicate the chain handler merged stub fields."
        )
        # Belt-and-braces: explicitly assert the canonical JSON-RPC envelope
        # keys are absent. If a future refactor adds a "helpful" wrap, this
        # catches it even if the override dict accidentally collides on keys.
        for leaked_key in ("jsonrpc", "result", "id"):
            assert leaked_key not in body, (
                f"healthy-stub key {leaked_key!r} leaked into body override response: {body!r}"
            )
