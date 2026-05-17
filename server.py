"""
HTTP JSON-RPC Provider Simulator

Three independent JSON-RPC servers (ports 18545 / 18546 / 18547)
plus one control API (port 19000).

Each provider's behaviour is changed at runtime via POST /scenario.

Supported modes per provider:
  success           — returns {"jsonrpc":"2.0","result":"..."} with optional latency
  error             — returns {"jsonrpc":"2.0","error":{"code":…,"message":"…"}}
                      Configurable via error_code (default -32000),
                      error_message (default "Internal error"),
                      and http_status (default 200).
  rate_limit        — returns HTTP 429
  down              — returns HTTP 503 (router treats provider as unavailable)
  error_probability — randomly returns error on X% of requests (0.0–1.0)

Control API:
  POST /scenario   {"providers": {"1": {"mode": "error", "error_code": -32601,
                     "error_message": "Method not found", "http_status": 200}}}
  POST /reset      {}
  GET  /scenario   → current state of all providers
  GET  /health     → {"status": "ok"}
  GET  /stats      → call counts and per-status breakdown per provider
  GET  /history    → ordered call log — supports filtering:
                       ?last=60          last 60 seconds
                       ?from=<ts>        from unix timestamp
                       ?to=<ts>          to unix timestamp
                       ?provider=1       single provider (1/2/3)
                       ?method=eth_call  specific RPC method
                       ?status=error     success | error | rate_limit | down
                       ?max=N            return at most N most-recent entries (MAG-1822)
                     params are combinable: ?last=120&provider=2&status=error
"""

import datetime
import json
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import handlers_btc
import handlers_eth
import handlers_rest
import handlers_tendermintrpc
import handlers_ws
from constants import (
    ALL_PROVIDER_PORTS,
    BACKUP_PROVIDER_PORTS,
    CONTROL_PORT,
    GRPC_BACKUP_PORTS,
    GRPC_PROVIDER_PORTS,
    HISTORY_MAX,
    PROVIDER_PORTS,
    REST_BACKUP_PORTS,
    REST_PORTS,
    TM_BACKUP_PORTS,
    TM_PORTS,
    WS_BACKUP_PORTS,
    WS_PORTS,
)
from stubs_rest import REST_METHOD_DEFAULTS


# ── Wire-payload normalisation ────────────────────────────────────────────────

def _normalise_responses(raw: Any) -> Dict[Any, Any]:
    """Normalise a ``responses`` wire payload into a dict the handlers can use.

    JSON-RPC tests send ``responses`` as a JSON object keyed by method name:

        {"eth_blockNumber": {"result": "0xff"}}

    REST tests (MAG-1777) cannot use JSON objects because their keys are
    ``(verb, path_template)`` tuples — JSON has no tuple type and no
    non-string object keys. The wire payload for REST is a list of
    ``[[verb, template], cfg]`` pairs:

        [[["GET", "/cosmos/.../blocks/latest"], {"status": 503, "body": {...}}]]

    This helper accepts either shape and re-tuples REST keys. Mixed payloads
    (both string-keyed JSON-RPC entries and list-keyed REST entries in the
    same provider) are NOT supported intentionally — a provider has one
    chain_family at a time.

    MAG-1821 — validation pass for per-method override entries:

      A per-method entry may carry ``mode`` ∈ {success, down, hang,
      drop_connection, rate_limit}. ``mode == "error"`` is rejected here so
      the /scenario POST returns 400 with a clear message — error semantics
      are already covered by the per-method ``error_stub`` / ``error`` keys
      (resolved inside handlers_eth.handle / handlers_rest.handle), and
      mixing them at the snap layer would silently shadow the catalogue
      path. The same rule applies to both shapes — the JSON-RPC dict path
      and the REST list path (MAG-1821 follow-up: per-method overrides
      were extended to WS + REST handlers; WS reuses the dict shape).

      Other unknown keys are forwarded as-is so the helpers can evolve the
      override shape without re-versioning the wire payload.
    """
    if isinstance(raw, list):
        out: Dict[Any, Any] = {}
        for entry in raw:
            # Each entry must be a 2-element [key, cfg] pair. Key is the
            # 2-element [verb, template] list (re-tupled here).
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            key, cfg = entry
            # MAG-1821 follow-up — reject per-(verb, template) mode="error"
            # on REST too. Same rationale as the dict-shape branch below:
            # error semantics are owned by the per-path ``error`` key
            # consumed in handlers_rest.handle; allowing mode="error" here
            # would silently shadow that path via the merged-snap fault
            # branch in _apply_fault.
            if isinstance(cfg, dict) and cfg.get("mode") == "error":
                raise ValueError(
                    f"per-method mode=\"error\" is not supported "
                    f"(key={key!r}); use responses[{key!r}] = "
                    f"{{'error': {{...}}}} instead"
                )
            if isinstance(key, (list, tuple)) and len(key) == 2:
                out[(key[0], key[1])] = cfg
            else:
                # Fallback: stringify so a malformed payload doesn't crash
                # state.update — the handler will simply miss the override.
                out[str(key)] = cfg
        return out
    if isinstance(raw, dict):
        # MAG-1821 — reject per-method mode="error" overrides at /scenario
        # time. Use the per-method error_stub / error keys (handlers_eth)
        # for chain-domain errors instead.
        for method_name, cfg in raw.items():
            if isinstance(cfg, dict) and cfg.get("mode") == "error":
                raise ValueError(
                    f"per-method mode=\"error\" is not supported "
                    f"(method={method_name!r}); use responses[{method_name!r}] = "
                    f"{{'error_stub': '<name>'}} or {{'error': {{...}}}} instead"
                )
            # MAG-1846 — body+status override validation, JSON-RPC only.
            # The override returns {status, body} directly and bypasses the
            # healthy stub. Reject at /scenario time: non-2xx status (use
            # mode=error for those), body not a dict (json.dumps needs one),
            # or body+mode combined (they describe different outcomes —
            # custom success vs. fault — combining them would silently pick
            # one).
            #
            # REST per-path overrides reach this dict-branch after the list
            # form is normalised to a dict with tuple keys ((verb, path)).
            # REST has its own per-path body+status semantic that pre-dates
            # MAG-1846 and intentionally allows non-2xx statuses (handlers_rest
            # owns that wire-shape contract), so we only apply this validation
            # to string-keyed (JSON-RPC method-name) entries.
            if isinstance(method_name, str) and isinstance(cfg, dict) and "body" in cfg:
                body_val = cfg["body"]
                if not isinstance(body_val, dict):
                    raise ValueError(
                        f"per-method body override must be a dict "
                        f"(method={method_name!r}); got type {type(body_val).__name__}"
                    )
                if "mode" in cfg:
                    raise ValueError(
                        f"per-method body and mode are mutually exclusive "
                        f"(method={method_name!r}); set one or the other, not both"
                    )
                status_val = cfg.get("status", 200)
                if not (isinstance(status_val, int) and 200 <= status_val <= 299):
                    raise ValueError(
                        f"per-method body override status must be a 2xx int "
                        f"(method={method_name!r}); got status={status_val!r}. "
                        f"Use mode=\"error\" + http_status for non-2xx response shapes."
                    )
        return raw
    # Unknown shape — clear responses rather than crash.
    return {}


# ── Per-method config resolution (MAG-1821) ───────────────────────────────────

# Fault-decision keys that a per-method override may shadow on the snap.
# Kept narrow on purpose: only the keys that _apply_fault (and the latency
# pre-step in do_POST) actually consult. Adding a key here is a deliberate
# choice — silently mirroring every snap field would let unrelated config
# (chain_family, blocks_behind, …) leak into the per-method path.
_METHOD_OVERRIDE_KEYS = (
    "mode",
    "latency_ms",
    "error_probability",
    "error_code",
    "error_message",
    "http_status",
    "drop_at",
    # MAG-1846 — per-method body+status override. When "body" is set on the
    # resolved method cfg the JSONRPCHandler emits {status, body} directly
    # and skips _apply_fault + the chain-handler success path. "status"
    # defaults to 200 when omitted (enforced at the call site).
    "body",
    "status",
)


def _resolve_method_config(
    method: str,
    snap: Dict[str, Any],
    responses: Dict[Any, Any],
) -> Dict[Any, Any]:
    """Return a snap-shaped dict merged with the per-method override (if any).

    Resolution rules (MAG-1821):

      - For each key in ``_METHOD_OVERRIDE_KEYS``, prefer
        ``responses[method][key]`` when present, otherwise fall back to
        ``snap[key]``. This is the per-key fallback contract — a partial
        per-method entry inherits provider-wide fault keys it doesn't
        override (e.g. setting only ``{"mode": "down"}`` for a method
        keeps the provider-wide ``latency_ms`` in effect).
      - All other snap keys are passed through unchanged so the caller
        (history accounting, success-path handlers reading
        ``corruption_mode`` / ``blocks_behind`` / etc.) sees a single
        merged dict.
      - ``method == "*"`` (pre-body-parse down branch) and an empty /
        non-dict ``responses[method]`` short-circuit to the raw snap.
      - Unknown keys inside ``responses[method]`` are silently ignored —
        forward-compatibility so adding a new override knob doesn't break
        older snap shapes.
    """
    if method == "*" or not responses:
        return snap
    method_cfg = responses.get(method)
    if not isinstance(method_cfg, dict):
        return snap
    merged = dict(snap)
    for key in _METHOD_OVERRIDE_KEYS:
        if key in method_cfg:
            merged[key] = method_cfg[key]
    return merged


# ── Provider state ────────────────────────────────────────────────────────────



@dataclass
class ProviderState:
    mode: str = "success"               # success | error | rate_limit | down
    latency_ms: int = 0
    error_probability: float = 0.0
    error_code: int = -32000            # JSON-RPC error code when mode="error"
    error_message: str = "Internal error"  # JSON-RPC error message when mode="error"
    http_status: int = 200              # HTTP status code for error responses (200 = JSON-RPC body error)
    responses: Dict[str, Any] = field(default_factory=dict)
    corruption_mode: Optional[str] = None     # one of: None, "truncated", "missing_field", "invalid_json", "empty_response", "wrong_type"
    missing_field: Optional[str] = None       # field-name slot — which top-level field to target when corruption_mode is "missing_field" (omit it) or "wrong_type" (swap its type). Defaults to "result" for wrong_type when unset.
    blocks_behind: int = 0    # 0 = current head; positive = behind; negative = ahead
    drop_at: str = "before_headers"   # one of: "before_headers", "after_headers", "mid_body"; only applies when mode="drop_connection"
    chain_family: str = "eth"   # one of: "eth", "btc", "grpc", "rest", "tendermintrpc", "ws"; selects which chain-specific handler module dispatches the success-branch response. Default "eth" preserves backward-compat. "grpc" → handlers_grpc on ports 18548-50. "rest" → handlers_rest on ports 18551-53 (MAG-1777). "tendermintrpc" → handlers_tendermintrpc on ports 18554-56 (MAG-1841). "ws" → handlers_ws on ports 18557-59 (MAG-1801) for WebSocket-style providers with subscription lifecycle — the handler delegates non-subscription methods back to handlers_eth.handle / handlers_btc.handle so request/response semantics are identical to HTTP JSON-RPC; subscription frames are wrapped in chain-specific envelopes from stubs_ws.
    # MAG-1791: provider-stale-on-getLogs primitive — head-fresh but logs-indexing-lagged.
    # Models the real production failure mode where providers update eth_blockNumber
    # immediately but index logs in a separate pipeline that can fall behind seconds-to-minutes.
    # None = unaffected (today's behaviour). Set an int = "this provider has only indexed logs
    # up through block <N>"; eth_getLogs queries that touch a higher range return either an
    # empty array (logs_lag_mode="empty") or only logs with blockNumber <= N (mode="partial").
    # eth_blockNumber is unaffected: it keeps reporting current head — that's the whole point
    # of this primitive (head-fresh + logs-lagged is the divergence we want to expose).
    logs_indexed_up_to: Optional[int] = None
    # logs_lag_mode: one of "empty" / "partial". Only consulted when logs_indexed_up_to is set.
    logs_lag_mode: str = "empty"
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # call history — each entry: {ts, method, status, latency_ms}
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAX), repr=False)
    # all-time counters — never capped, survives history ring-buffer rollover
    total_calls: int = 0
    calls_by_status: Dict[str, int] = field(default_factory=dict, repr=False)

    def snapshot(self) -> dict:
        """Return a thread-safe copy of the mutable config fields.
        Used by JSONRPCHandler at the start of every request so the handler works
        on a stable snapshot even if a test updates the state mid-request."""
        with self.lock:
            return {
                "mode":              self.mode,
                "latency_ms":        self.latency_ms,
                "error_probability": self.error_probability,
                "error_code":        self.error_code,
                "error_message":     self.error_message,
                "http_status":       self.http_status,
                "corruption_mode":   self.corruption_mode,
                "missing_field":     self.missing_field,
                "blocks_behind":     self.blocks_behind,
                "drop_at":           self.drop_at,
                "chain_family":      self.chain_family,
                "logs_indexed_up_to": self.logs_indexed_up_to,
                "logs_lag_mode":      self.logs_lag_mode,
            }

    def update(self, cfg: dict) -> None:
        """Apply a partial config dict received from POST /scenario.
        Only keys present in cfg are updated; omitted keys keep their current value.
        Acquires the lock so updates are atomic and safe to call from any thread."""
        with self.lock:
            self.mode              = cfg.get("mode",              self.mode)
            self.latency_ms        = cfg.get("latency_ms",        self.latency_ms)
            self.error_probability = cfg.get("error_probability", self.error_probability)
            self.error_code        = cfg.get("error_code",        self.error_code)
            self.error_message     = cfg.get("error_message",     self.error_message)
            self.http_status       = cfg.get("http_status",       self.http_status)
            self.corruption_mode   = cfg.get("corruption_mode",   self.corruption_mode)
            self.missing_field     = cfg.get("missing_field",     self.missing_field)
            self.blocks_behind     = cfg.get("blocks_behind",     self.blocks_behind)
            self.drop_at           = cfg.get("drop_at",           self.drop_at)
            self.chain_family      = cfg.get("chain_family",      self.chain_family)
            # MAG-1791: backward-compat — missing keys keep current value, so
            # /scenario payloads that don't carry logs_indexed_up_to / logs_lag_mode
            # leave existing provider state untouched (defaults to None / "empty").
            self.logs_indexed_up_to = cfg.get("logs_indexed_up_to", self.logs_indexed_up_to)
            self.logs_lag_mode      = cfg.get("logs_lag_mode",      self.logs_lag_mode)
            if "responses" in cfg:
                self.responses = _normalise_responses(cfg["responses"])

    def reset_scenario(self) -> None:
        """Reset only the scenario config fields back to startup defaults (mode, latency, responses).
        Does NOT touch the call history or counters.
        Called by POST /reset — use between test scenarios to put providers back to healthy."""
        with self.lock:
            self.mode              = "success"
            self.latency_ms        = 0
            self.error_probability = 0.0
            self.error_code        = -32000
            self.error_message     = "Internal error"
            self.http_status       = 200
            self.responses         = {}
            self.corruption_mode   = None
            self.missing_field     = None
            self.blocks_behind     = 0
            self.drop_at           = "before_headers"
            self.chain_family      = "eth"
            # MAG-1791: reset clears the eth_getLogs stale-indexing primitive
            # so a /reset between tests restores full logs availability.
            self.logs_indexed_up_to = None
            self.logs_lag_mode      = "empty"

    def clear_history(self) -> None:
        """Wipe the in-memory call buffer and reset all-time counters to zero.
        Does NOT touch the scenario config (mode, latency, responses).
        Called by POST /history/clear — use before a specific request to isolate its history."""
        with self.lock:
            self.history.clear()
            self.total_calls       = 0
            self.calls_by_status   = {}

    def push_call_to_buffer(self, method: str, status: str, latency_ms: int,
                             request_id: object = None, lava_headers: dict = None) -> None:
        """Push one call record into the in-memory ring-buffer and update all-time counters.

        Storage is entirely in RAM — nothing is written to disk or any logging framework.
        The ring-buffer (deque) automatically drops the oldest entry once it reaches
        HISTORY_MAX (default 2000; override via SIM_HISTORY_MAX env at pod startup)
        entries. All-time counters (total_calls, calls_by_status) are never capped
        and survive buffer rollovers.

        Args:
            method:       JSON-RPC method name, e.g. "eth_blockNumber". Use "*" for
                          requests that were rejected before the body was parsed (mode=down).
            status:       Outcome string — "success" | "error" | "rate_limit" | "down".
            latency_ms:   Simulated delay that was injected before the response, in ms.
                          0 when no latency was configured or the request was rejected early.
            request_id:   The JSON-RPC ``id`` field from the request body (echoed back in
                          the response). ``None`` for down-mode rejections where the body
                          is never parsed.
            lava_headers: Dict of all ``lava-*`` HTTP request headers provided by the router.
                          ``{}`` if no lava headers were sent.
        """
        now = time.time()
        with self.lock:
            self.history.append({
                "ts":            now,
                "time":          datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.") + f"{int(now % 1 * 1000):03d} UTC",
                "request_id":    request_id,
                "method":        method,
                "status":        status,
                "latency_ms":    latency_ms,
                "lava_headers":  lava_headers or {},
            })
            self.total_calls += 1
            self.calls_by_status[status] = self.calls_by_status.get(status, 0) + 1

    def stats(self) -> dict:
        """Return a thread-safe snapshot of the all-time call counters for this provider.
        Counters are never reset (unlike the ring-buffer which is cleared on reset()).
        Used by GET /stats to show cumulative traffic since the pod started."""
        with self.lock:
            return {
                "total_requests_all_time":    self.total_calls,
                "total_calls":                self.total_calls,  # alias for convenience
                "requests_by_status_all_time": dict(self.calls_by_status),
                "calls_by_status":             dict(self.calls_by_status),  # alias for convenience
                "history_ring_buffer_entries": len(self.history),  # max = HISTORY_MAX
            }

    def get_history(self) -> list:
        """Return a thread-safe copy of the in-memory ring-buffer as a plain list.
        The returned list is a snapshot — mutations to it do not affect the buffer.
        Used by ControlHandler.do_GET() to build the /history response."""
        with self.lock:
            return list(self.history)


# ── Fault-injection helper (chain-agnostic) ───────────────────────────────────
#
# Extracted from JSONRPCHandler.do_POST (MAG-1777). Both the JSON-RPC handler
# and the REST handler call this with their parsed-request context. The helper
# evaluates the same 5 fault primitives in the same order, records the outcome
# in history, and returns a structured dict the caller turns into a wire
# response in its chain's native shape (JSON-RPC envelope vs REST JSON object).
#
# Why a dict and not Optional[Tuple[int, dict]]: down / hang / drop_connection
# need wire-level actions (no body / sleep+close / partial-write+close) that a
# raw (status, body) tuple can't express. The dict's "kind" field tells the
# caller which wire action to perform; rate_limit and error carry status +
# error_code + error_message so the caller composes a chain-appropriate body.
# History accounting lives in the helper so callers don't duplicate it.


def _apply_fault(
    state: "ProviderState",
    snap: Dict[str, Any],
    method: str,
    req_id: Any,
    lava_headers: Dict[str, str],
    t_start: float,
) -> Optional[Dict[str, Any]]:
    """Evaluate post-parse fault primitives and emit history.

    Args:
        state:        The live ProviderState (used to push history records).
        snap:         ProviderState.snapshot() taken at request start; the
                      evaluation uses the snapshot so a mid-request /scenario
                      update can't change the outcome of an in-flight request.
        method:       Resolved method name. For JSON-RPC this is the body
                      "method" field; for REST it's "<VERB> <path_template>"
                      (built by the caller). Used only for history accounting,
                      not for fault decisions.
        req_id:       JSON-RPC id (or X-Request-Id / sim sequence number for
                      REST). Echoed back in the response by the caller when
                      relevant; None for down-mode rejections where no body
                      is parsed.
        lava_headers: Captured lava-* request headers; stored on the history
                      entry for later /history filtering.
        t_start:      time.monotonic() value at request entry, used to compute
                      latency on fault outcomes that count time-to-emit.

    Returns:
        None when no fault triggered — caller proceeds to chain-specific
        success-path handlers (handlers_eth / handlers_btc / handlers_rest).
        Otherwise a dict describing the fault:

          {"kind": "down"}
            Caller MUST emit 503 with no body. History already recorded with
            method="*" (down is pre-body-parse, so no method is known).

          {"kind": "hang"}
            Caller MUST sleep 30s then close the connection. History recorded
            with status="hang", latency_ms=0.

          {"kind": "drop", "drop_at": str}
            Caller MUST perform the partial-write dance per drop_at
            ("before_headers" / "after_headers" / "mid_body") and close.
            History recorded.

          {"kind": "rate_limit", "status": 429, "error_code": 429,
           "error_message": "Too many requests"}
            Caller composes a chain-appropriate body and sends it. History
            recorded.

          {"kind": "error", "status": int, "error_code": int,
           "error_message": str}
            Caller composes a chain-appropriate error body. History recorded.
    """
    # 1. Outage — for provider-wide down, the pre-parse caller passes
    #    method="*", req_id=None, t_start=now() so this records a "*" entry
    #    with ~0ms latency. For per-method down (post-parse path) the caller
    #    passes the actual method / req_id and the original t_start, so any
    #    inherited per-method latency_ms already slept is reflected in
    #    _elapsed_ms(t_start) and /history?method=X resolves correctly.
    if snap["mode"] == "down":
        state.push_call_to_buffer(method, "down", _elapsed_ms(t_start),
                                  request_id=req_id, lava_headers=lava_headers)
        return {"kind": "down"}

    # 2. Hang — accept request, sleep "forever". 30s is long enough for any
    #    reasonable client read timeout to fire; finite so the thread eventually
    #    exits and we don't leak threads if the client disconnects.
    if snap["mode"] == "hang":
        state.push_call_to_buffer(method, "hang", 0,
                                  request_id=req_id, lava_headers=lava_headers)
        return {"kind": "hang"}

    # 3. Drop connection — close socket at one of three points.
    if snap["mode"] == "drop_connection":
        drop_at = snap.get("drop_at", "before_headers")
        state.push_call_to_buffer(method, "drop_connection",
                                  _elapsed_ms(t_start),
                                  request_id=req_id, lava_headers=lava_headers)
        return {"kind": "drop", "drop_at": drop_at}

    # 4. Rate limit — HTTP 429.
    if snap["mode"] == "rate_limit":
        state.push_call_to_buffer(method, "rate_limit", _elapsed_ms(t_start),
                                  request_id=req_id, lava_headers=lava_headers)
        return {
            "kind": "rate_limit",
            "status": 429,
            "error_code": 429,
            "error_message": "Too many requests",
        }

    # 5. Probabilistic / forced error — configurable code, message, HTTP status.
    if snap["mode"] == "error" or random.random() < snap["error_probability"]:
        state.push_call_to_buffer(method, "error", _elapsed_ms(t_start),
                                  request_id=req_id, lava_headers=lava_headers)
        return {
            "kind": "error",
            "status": snap.get("http_status", 200),
            "error_code": snap.get("error_code", -32000),
            "error_message": snap.get("error_message", "Internal error"),
        }

    return None


def _elapsed_ms(t_start: float) -> int:
    """Return the integer milliseconds elapsed since t_start (time.monotonic())."""
    return int((time.monotonic() - t_start) * 1000)


# ── Cross-transport isolation helpers (MAG-1837 / MAG-1838) ───────────────────
#
# ProviderState is shared across all transports (JSON-RPC, REST, gRPC, WS,
# Tendermint-RPC) for the same provider id, so fields like ``corruption_mode``
# and the ``mode`` fault primitive are chain-agnostic on the snap. Without an
# explicit gate a value set for one transport (e.g. chain_family="eth") would
# also fire on the others. These helpers narrow the read to the transport
# that owns the request: the handler passes its own ``chain_family`` values
# and gets back ``None`` whenever the snap was authored for some other
# transport, so the surrounding logic falls through to its normal success
# path.

def _corruption_for(snap: Dict[str, Any], *chain_families: str) -> Optional[str]:
    """Return ``snap["corruption_mode"]`` only when the snap's
    ``chain_family`` is one of ``chain_families``; otherwise ``None``.

    Used at every transport's reply call site (JSON-RPC, REST, Tendermint, WS)
    so a corruption authored for one transport can't leak into another.
    Mirrors MAG-1836's _apply_grpc_fault gate (early-return on mismatch).
    """
    if snap.get("chain_family") in chain_families:
        return snap.get("corruption_mode")
    return None


def _missing_field_for(snap: Dict[str, Any], *chain_families: str) -> Optional[str]:
    """Return ``snap["missing_field"]`` only when the snap's ``chain_family``
    is one of ``chain_families``; otherwise ``None``.

    ``missing_field`` is the companion slot to ``corruption_mode`` (it names
    which top-level field to clear / type-swap). Gating both together keeps
    the cross-transport contract tight — if corruption is suppressed for a
    transport, the companion slot is suppressed too.
    """
    if snap.get("chain_family") in chain_families:
        return snap.get("missing_field")
    return None


# ── JSON-RPC handler ──────────────────────────────────────────────────────────

class JSONRPCHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        """Handle every incoming JSON-RPC POST request for one simulated provider.

        Decision flow (evaluated in order, first match wins):
          1. mode == "down"          → 503, no body parsed (via _apply_fault).
          2. latency_ms > 0          → sleep before continuing.
          3. mode == "hang"          → sleep 30s, close (via _apply_fault).
          4. mode == "drop_connection" → partial write + close (via _apply_fault).
          5. mode == "rate_limit"    → 429 JSON-RPC error (via _apply_fault).
          6. mode == "error" or
             random() < error_prob   → JSON-RPC error body (via _apply_fault).
          7. custom response defined → return configured result.
          8. default stub            → return METHOD_DEFAULTS value.

        Every branch (via _apply_fault or in-line success path) calls
        push_call_to_buffer so the outcome is always recorded in the in-memory
        ring-buffer regardless of which path was taken.
        """
        t_start = time.monotonic()
        state: ProviderState = self.server.state
        snap = state.snapshot()

        # Capture all lava-* headers from the router
        lava_headers = {
            k: v for k, v in self.headers.items()
            if k.lower().startswith("lava-")
        }

        # Cross-transport isolation (MAG-1838) — inverse of MAG-1836.
        # ``ProviderState`` is shared across JSON-RPC, REST, gRPC, WS, and
        # Tendermint-RPC for the same provider id. The fault primitives in
        # _apply_fault (down / hang / drop_connection / rate_limit / error)
        # are chain-agnostic, so without an explicit gate a fault authored
        # for the gRPC port (chain_family="grpc") would also kill the
        # JSON-RPC port for that provider. JSON-RPC owns chain_family
        # values "eth" and "btc" (success-path dispatch lives below); any
        # other value means the fault was set for a different transport
        # and the JSON-RPC port should fall through to its normal success
        # response. ``jsonrpc_owns_snap`` is False in that case and
        # short-circuits both the pre-parse ``down`` branch immediately
        # below and the post-parse fault evaluation further down.
        jsonrpc_owns_snap = snap.get("chain_family") in ("eth", "btc")

        # Pre-parse fault check: provider-wide down mode doesn't read the body.
        # Down is the only pre-body-parse fault — there is no per-method
        # variant at this layer because the method label isn't known yet
        # (body unparsed). A per-method down lives behind the merged-config
        # path below and applies on the post-parse branch.
        if jsonrpc_owns_snap and snap["mode"] == "down":
            fault = _apply_fault(state, snap, "*", None, lava_headers, t_start)
            self._emit_jsonrpc_fault(fault, req_id=None,
                                     corruption_mode=_corruption_for(snap, "eth", "btc"),
                                     missing_field=_missing_field_for(snap, "eth", "btc"))
            return

        # Parse the request body before latency/fault evaluation so the
        # method label is available for per-method override resolution
        # (MAG-1821). Parsing first is safe: it's a small in-memory JSON
        # load that doesn't depend on any provider config.
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}
        req_id = body.get("id", 1)
        method = body.get("method", "unknown")

        # Merge per-method overrides into the snap (MAG-1821). When no
        # override applies, ``method_snap is snap`` and behaviour matches
        # pre-MAG-1821 exactly. Order matches the provider-wide path:
        # latency FIRST, then fault — so a per-method ``latency_ms`` is
        # paid even on a per-method rate_limit / drop response.
        method_snap = _resolve_method_config(method, snap, state.responses)

        if method_snap["latency_ms"] > 0:
            time.sleep(method_snap["latency_ms"] / 1000.0)

        # MAG-1846 — per-method body override. When set, return the configured
        # {status, body} directly and bypass _apply_fault + the chain-handler
        # success path. Validation at /scenario time (_normalise_responses)
        # guarantees: status is 2xx, body is a dict, and "mode" is not also
        # set on this method entry, so we can read body unconditionally here
        # without worrying about silently shadowing a fault primitive.
        if "body" in method_snap and method_snap.get("body") is not None:
            override_body = method_snap["body"]
            override_status = method_snap.get("status", 200)
            self._reply(override_status, override_body,
                        corruption_mode=_corruption_for(snap, "eth", "btc"),
                        missing_field=_missing_field_for(snap, "eth", "btc"))
            # History status is always "success" on the body-override path.
            # The override is validated 2xx-only at /scenario time, so the
            # HTTP-level outcome is always successful. Body is arbitrary
            # test-supplied content and may not follow JSON-RPC conventions,
            # so inferring status from body shape (e.g. checking for an
            # "error" key) would surprise testers — a body of
            # {"success": false, "error": "..."} is still an HTTP success
            # from the sim's perspective, and /history?status=error should
            # not match it. Test authors who need /history?status=error
            # should use mode="error" instead.
            state.push_call_to_buffer(method, "success", _elapsed_ms(t_start),
                                      request_id=req_id, lava_headers=lava_headers)
            return

        # Post-parse fault evaluation. _apply_fault records history internally.
        # MAG-1838 — only evaluate the fault ladder when the snap was authored
        # for the JSON-RPC transport (chain_family in {"eth","btc"}). Faults
        # set for any other transport pass through to the success-path below.
        if jsonrpc_owns_snap:
            fault = _apply_fault(state, method_snap, method, req_id, lava_headers, t_start)
        else:
            fault = None
        if fault is not None:
            self._emit_jsonrpc_fault(fault, req_id=req_id,
                                     corruption_mode=_corruption_for(snap, "eth", "btc"),
                                     missing_field=_missing_field_for(snap, "eth", "btc"))
            return

        # Success — delegate the chain-specific success path to a handler module.
        #
        # Fault branches above (down / hang / drop / rate-limit / forced or
        # probabilistic error) are chain-agnostic and stay in _apply_fault.
        # Only the method-lookup + result-shape logic is chain-specific — we
        # pick the handler module based on snap["chain_family"]. Default "eth"
        # preserves backward-compat for every payload that doesn't set
        # chain_family.
        #
        # The handler returns the status + response envelope; this layer is
        # responsible for I/O (corruption hooks, history accounting).
        if snap.get("chain_family") == "btc":
            status, response_body = handlers_btc.handle(state, body, snap, lava_headers)
        else:
            status, response_body = handlers_eth.handle(state, body, snap, lava_headers)
        emit_status = "error" if "error" in response_body else "success"
        self._reply(status, response_body,
                    corruption_mode=_corruption_for(snap, "eth", "btc"),
                    missing_field=_missing_field_for(snap, "eth", "btc"))
        state.push_call_to_buffer(method, emit_status, _elapsed_ms(t_start),
                                  request_id=req_id, lava_headers=lava_headers)

    def _emit_jsonrpc_fault(self, fault: Dict[str, Any], req_id: Any,
                             corruption_mode: Optional[str] = None,
                             missing_field: Optional[str] = None) -> None:
        """Translate a fault dict from ``_apply_fault`` into a JSON-RPC wire reply.

        Each fault "kind" maps to a specific wire action:

        - ``down`` — emit HTTP 503 with no body. Mirrors the router-treats-as-
          unavailable semantic.
        - ``hang`` — sleep 30s, then close the socket. The 30s upper bound is
          long enough for any reasonable client read timeout while still being
          finite so we don't leak threads when the client disconnects.
        - ``drop`` — close the socket at one of three points (before_headers /
          after_headers / mid_body). Exceptions during the partial write are
          swallowed because the client may have already disconnected.
        - ``rate_limit`` — HTTP 429 with a JSON-RPC error envelope.
        - ``error`` — caller-configured HTTP status with a JSON-RPC error
          envelope. The id field is echoed from the request body when known.
        """
        kind = fault["kind"]

        if kind == "down":
            self.send_response(503)
            self.end_headers()
            return

        if kind == "hang":
            time.sleep(30)
            try:
                self.connection.close()
            except Exception:
                pass
            return

        if kind == "drop":
            drop_at = fault.get("drop_at", "before_headers")
            try:
                if drop_at == "after_headers":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "100")  # promise body we won't send
                    self.end_headers()
                elif drop_at == "mid_body":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "100")  # promise body
                    self.end_headers()
                    self.wfile.write(b'{"jsonrpc":"2.0",')  # ~half a body
                    self.wfile.flush()
                # before_headers (default) — fall through, no headers sent
            except Exception:
                pass  # client may have already disconnected, ignore
            try:
                self.connection.close()
            except Exception:
                pass
            return

        # rate_limit / error — JSON-RPC error envelope.
        self._reply(
            fault["status"],
            {"jsonrpc": "2.0", "id": req_id,
             "error": {"code": fault["error_code"],
                       "message": fault["error_message"]}},
            corruption_mode=corruption_mode,
            missing_field=missing_field,
        )

    @staticmethod
    def _elapsed_ms(t_start: float) -> int:
        """Return the integer milliseconds elapsed since t_start (from time.monotonic())."""
        return _elapsed_ms(t_start)

    def _reply(self, status: int, data: dict,
               corruption_mode: Optional[str] = None,
               missing_field: Optional[str] = None):
        """Serialise data as JSON and write a complete HTTP response.
        If corruption_mode is set, alter the body before/after serialization."""
        # Apply structural corruption (modify the dict before serialization)
        if corruption_mode == "missing_field" and missing_field:
            data = {k: v for k, v in data.items() if k != missing_field}
        elif corruption_mode == "empty_response":
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return  # no body
        elif corruption_mode == "wrong_type":
            # Swap the type of a target field so a caller that expects e.g. a
            # hex-string sees an int (or vice versa). Target field comes from
            # the missing_field slot (reused for "which field to corrupt");
            # default to "result" since that's the JSON-RPC success-shape
            # carrier and the most common test target.
            target_field = missing_field or "result"
            if target_field in data:
                current = data[target_field]
                if isinstance(current, bool):
                    # Order matters: bool is a subclass of int — check first.
                    data[target_field] = 1 if current else 0
                elif isinstance(current, str):
                    data[target_field] = 12345
                elif isinstance(current, (int, float)):
                    data[target_field] = "wrong_type_value"
                else:
                    # dict / list / None — fall through to a string sentinel.
                    data[target_field] = "wrong_type_value"

        body = json.dumps(data).encode()

        # Apply byte-level corruption (after serialization)
        if corruption_mode == "truncated" and len(body) > 10:
            body = body[:-10]
        elif corruption_mode == "invalid_json":
            body = b"}{ {{ not valid json"

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        """Suppress the default per-request stdout logging from BaseHTTPRequestHandler."""
        pass


# ── REST handler (MAG-1777) ───────────────────────────────────────────────────
#
# Peer to JSONRPCHandler. Same ProviderState, same fault primitives (via
# _apply_fault), different verb-routing surface: GET / POST / PUT / DELETE
# instead of POST-only. Each verb method shares the same do_* skeleton:
#
#   1. Capture lava-* request headers.
#   2. Build (verb, path) + parsed query + body (if Content-Length > 0).
#   3. Resolve a request_id — prefer X-Request-Id from the router, else fall
#      back to a sim-side monotonically increasing counter so /history's
#      correlation_group still has a stable key per call.
#   4. Run _apply_fault (snap snapshot is taken inside the helper's caller).
#   5. On no fault, match (verb, path) against the compiled route table.
#      404 if no match.
#   6. Dispatch to handlers_rest.handle for the success-path body.
#   7. Record history with method=f"{verb} {template}" so /history filters
#      stay grep-friendly.


# Module-level sim-side request-id counter. Used when the router doesn't send
# X-Request-Id (e.g. test code calling the simulator directly). Atomic
# increment via a lock so two parallel threads see distinct ids.
_REST_REQUEST_ID_COUNTER = 0
_REST_REQUEST_ID_LOCK = threading.Lock()


# ── WebSocket subscription registry (MAG-1801) ────────────────────────────────
#
# Module-level because subscriptions are per-CONNECTION runtime state, not
# per-provider configuration. ProviderState stays for /scenario-driven config;
# subscriptions live here, indexed by sub_id (32-hex-char string handed back
# to the client when it eth_subscribes). /ws/emit on the control server does a
# (sub_id) → SubscriptionHandle lookup and puts a wire-encoded event frame on
# the matching connection's out_queue. /reset does NOT touch this registry —
# resetting scenario config should not tear down live connections.

import queue


@dataclass
class SubscriptionHandle:
    """One active WS subscription. Created on eth_subscribe / subscribe /
    accountSubscribe / logsSubscribe, removed on the matching unsubscribe or
    on connection close.
    """
    sub_id: str               # 32-hex string handed back to the client
    provider_id: str          # "1" | "2" | "3"
    method: str               # e.g. "newHeads", "logs", "accountSubscribe"
    chain: str                # "eth" | "tendermint" | "solana"
    envelope: str             # one of stubs_ws SUBSCRIBE_METHODS envelope names
    out_queue: "queue.Queue[bytes]"  # frames the writer thread will sendall()
    closed: threading.Event   # set when the reader thread exits


_WS_SUBSCRIPTIONS: Dict[str, SubscriptionHandle] = {}
_WS_SUBSCRIPTIONS_LOCK = threading.Lock()


def _register_ws_subscription(handle: SubscriptionHandle) -> None:
    """Store a fresh subscription handle. Idempotent on sub_id."""
    with _WS_SUBSCRIPTIONS_LOCK:
        _WS_SUBSCRIPTIONS[handle.sub_id] = handle


def _unregister_ws_subscription(sub_id: str) -> Optional[SubscriptionHandle]:
    """Pop and return the handle for sub_id, or None if missing."""
    with _WS_SUBSCRIPTIONS_LOCK:
        return _WS_SUBSCRIPTIONS.pop(sub_id, None)


def _lookup_ws_subscription(sub_id: str) -> Optional[SubscriptionHandle]:
    """Return the live handle for sub_id without removing it."""
    with _WS_SUBSCRIPTIONS_LOCK:
        return _WS_SUBSCRIPTIONS.get(sub_id)


def _all_ws_subscriptions() -> list:
    """Return a snapshot of every active subscription as a list of dicts.
    Used by GET /ws/subscriptions."""
    with _WS_SUBSCRIPTIONS_LOCK:
        return [
            {
                "subscription_id": h.sub_id,
                "provider": h.provider_id,
                "method": h.method,
                "chain": h.chain,
                "queue_depth": h.out_queue.qsize(),
            }
            for h in _WS_SUBSCRIPTIONS.values()
        ]


def _next_sim_request_id() -> int:
    """Return the next sim-side monotonically increasing request id (thread-safe)."""
    global _REST_REQUEST_ID_COUNTER
    with _REST_REQUEST_ID_LOCK:
        _REST_REQUEST_ID_COUNTER += 1
        return _REST_REQUEST_ID_COUNTER


def _compile_route(template: str) -> "re.Pattern[str]":
    """Compile a path template like ``/cosmos/.../blocks/{height}`` into a regex.

    Each ``{var}`` placeholder becomes a named capture group ``(?P<var>[^/]+)``
    so the matcher can peel path params off without a second parse pass. The
    regex is anchored at both ends — partial matches don't count.

    Why hand-rolled and not a third-party router: stdlib-only constraint
    (Q2-A from the MAG-1777 design). 25 LOC of compiled regex covers every
    Cosmos REST path shape we need; no need for a Werkzeug-style mini-framework.
    """
    pattern = re.sub(r"\{([^}/]+)\}", lambda m: rf"(?P<{m.group(1)}>[^/]+)", template)
    return re.compile(rf"^{pattern}$")


def _build_rest_routes() -> List[Tuple[str, "re.Pattern[str]", str]]:
    """Compile every (verb, path_template) key in REST_METHOD_DEFAULTS into a
    matchable route table.

    Returns a list of ``(verb_uppercase, compiled_regex, template_str)`` tuples.
    Module-level so the compile cost is paid once at import time, not per request.
    """
    routes: List[Tuple[str, "re.Pattern[str]", str]] = []
    for (verb, template), _stub in REST_METHOD_DEFAULTS.items():
        routes.append((verb.upper(), _compile_route(template), template))
    return routes


# Compiled once at module import. Re-compile by reloading the module if the
# stub table changes (only happens in development, not at runtime).
_REST_ROUTES: List[Tuple[str, "re.Pattern[str]", str]] = _build_rest_routes()


class RestHandler(BaseHTTPRequestHandler):
    """REST surface for the provider simulator (MAG-1777).

    Shares ``ProviderState`` with ``JSONRPCHandler`` so a /scenario update
    targeting one provider is visible to both handlers regardless of which
    port the test hits. Verb-routing is the structurally new piece:
    BaseHTTPRequestHandler dispatches do_GET / do_POST / etc., and each
    method funnels into a common _handle pipeline that runs fault checks
    and matches the URL against the compiled route table.
    """

    # ── Verb dispatch ─────────────────────────────────────────────────────────

    def do_GET(self):      self._handle("GET")
    def do_POST(self):     self._handle("POST")
    def do_PUT(self):      self._handle("PUT")
    def do_DELETE(self):   self._handle("DELETE")

    def do_HEAD(self):
        """HEAD = GET without the body. Build the GET response, then strip the body.

        The wfile suppression is achieved by overwriting ``_reply`` for this
        single request via the ``_head_mode`` instance flag — keeps the rest
        of the pipeline unchanged.
        """
        self._head_mode = True
        try:
            self._handle("GET")
        finally:
            self._head_mode = False

    def do_OPTIONS(self):
        """OPTIONS returns the set of verbs registered for this path.

        Per RFC 7231, the response carries an ``Allow`` header listing the
        verbs the server accepts for the request URI. If the URI matches no
        registered template the response is 404.
        """
        path = urlparse(self.path).path
        allowed: List[str] = []
        for verb, regex, _template in _REST_ROUTES:
            if regex.match(path):
                if verb not in allowed:
                    allowed.append(verb)
        if not allowed:
            self._reply(404, {"code": "not_found", "method": "OPTIONS", "path": path})
            return
        # HEAD is implied by GET; OPTIONS itself is always allowed.
        if "GET" in allowed and "HEAD" not in allowed:
            allowed.append("HEAD")
        allowed.append("OPTIONS")
        self.send_response(204)
        self.send_header("Allow", ", ".join(allowed))
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── Shared pipeline ───────────────────────────────────────────────────────

    def _handle(self, verb: str) -> None:
        """Run one REST request end-to-end.

        Fault evaluation → (404 if no route match) → handlers_rest.handle
        → wire-reply via _reply. History accounting is delegated to
        _apply_fault for fault branches, and emitted inline for the success
        branch. The method label stored in history is ``f"{verb} {template}"``
        (or ``f"{verb} {path}"`` when no template matched) so /history's
        existing ?method= filter keeps working without code changes on the
        control API side.
        """
        t_start = time.monotonic()
        state: ProviderState = self.server.state
        snap = state.snapshot()

        # Lava-* request headers — used for /history filtering and threaded
        # through to handlers_rest so a future test can assert on header
        # propagation.
        lava_headers = {
            k: v for k, v in self.headers.items()
            if k.lower().startswith("lava-")
        }

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # X-Request-Id wins; fall back to sim-side sequence number so every
        # call still gets a stable correlation_group in /history.
        req_id: Any = self.headers.get("X-Request-Id") or _next_sim_request_id()

        # Pre-route fault: provider-wide down doesn't need the (verb,
        # template) key. Mirrors JSONRPCHandler's pre-body-parse down
        # branch. Per-(verb, template) down lives in the post-route
        # merged-snap branch below.
        if snap["mode"] == "down":
            fault = _apply_fault(state, snap, "*", None, lava_headers, t_start)
            self._emit_rest_fault(fault)
            return

        # Read body for verbs that may carry one. GET/HEAD/DELETE typically
        # don't, but the HTTP spec doesn't forbid it — be permissive.
        body: Any = None
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                # Malformed body — leave as None so handlers_rest can decide
                # whether to 400. Don't crash the dispatcher.
                body = None

        # Route match before latency / fault evaluation. We need the matched
        # (verb, template) before the per-method merge so MAG-1821-style
        # overrides keyed by ``(verb, template)`` can shadow provider-wide
        # latency_ms and fault keys. Order matches JSON-RPC: parse first,
        # then merge per-method, then sleep, then fault.
        match_result = self._match_route(verb, path)
        if match_result is None:
            # Unmatched path — no per-method merge to attempt (the (verb,
            # path) key isn't a registered template). Honor only the
            # provider-wide latency / fault snap.
            if snap["latency_ms"] > 0:
                time.sleep(snap["latency_ms"] / 1000.0)
            method_label = f"{verb} {path}"
            fault = _apply_fault(state, snap, method_label, req_id,
                                 lava_headers, t_start)
            if fault is not None:
                self._emit_rest_fault(fault)
                return
            # Genuine 404 — record so /history shows the miss.
            self._reply(404, {"code": "not_found", "method": verb, "path": path})
            state.push_call_to_buffer(method_label, "not_found",
                                      _elapsed_ms(t_start),
                                      request_id=req_id, lava_headers=lava_headers)
            return

        template, path_params = match_result
        method_label = f"{verb} {template}"

        # Merge per-(verb, template) overrides into the snap (MAG-1821
        # follow-up). When no override applies for this route,
        # ``method_snap is snap`` and behaviour matches pre-follow-up
        # exactly. Order matches JSON-RPC: latency FIRST, then fault — so
        # a per-route ``latency_ms`` is paid even on a per-route
        # rate_limit / drop response. Per-key fallback: a partial
        # per-route entry inherits provider-wide fault keys it doesn't
        # override.
        method_snap = _resolve_method_config(
            (verb, template), snap, state.responses
        )

        if method_snap["latency_ms"] > 0:
            time.sleep(method_snap["latency_ms"] / 1000.0)

        fault = _apply_fault(state, method_snap, method_label, req_id,
                             lava_headers, t_start)
        if fault is not None:
            self._emit_rest_fault(fault)
            return

        # Success path — chain-specific dispatch (REST handler).
        status, response_body = handlers_rest.handle(
            state, verb, template, path_params, query, body, snap, lava_headers
        )
        emit_status = "error" if (isinstance(response_body, dict) and "error" in response_body) else "success"
        # MAG-1837 — only apply corruption_mode if the snap was authored for
        # the REST transport. A corruption set on chain_family="eth" must not
        # leak into the REST port.
        self._reply(status, response_body,
                    corruption_mode=_corruption_for(snap, "rest"),
                    missing_field=_missing_field_for(snap, "rest"))
        state.push_call_to_buffer(method_label, emit_status, _elapsed_ms(t_start),
                                  request_id=req_id, lava_headers=lava_headers)

    # ── Routing ───────────────────────────────────────────────────────────────

    def _match_route(self, verb: str, path: str
                     ) -> Optional[Tuple[str, Dict[str, str]]]:
        """Match ``(verb, path)`` against ``_REST_ROUTES``.

        Returns ``(template_str, path_params)`` on first match, else None.
        Path params come straight from the regex's named groups. The match
        is exact (compiled with ``^...$``) so trailing slashes and extra
        segments don't accidentally pass.
        """
        for route_verb, regex, template in _REST_ROUTES:
            if route_verb != verb:
                continue
            m = regex.match(path)
            if m is not None:
                return template, m.groupdict()
        return None

    # ── Wire emission ─────────────────────────────────────────────────────────

    def _emit_rest_fault(self, fault: Optional[Dict[str, Any]]) -> None:
        """Translate a fault dict from ``_apply_fault`` into a REST wire reply.

        REST bodies are bare JSON objects (no JSON-RPC envelope), so rate_limit
        and error compose a small ``{"code": ..., "message": ...}`` body
        instead of the ``{"jsonrpc": "2.0", "id": ..., "error": ...}`` shape.
        Wire-level kinds (``down`` / ``hang`` / ``drop``) are identical to the
        JSON-RPC equivalents — fault kind, not chain, drives the wire action.
        """
        if fault is None:
            return

        kind = fault["kind"]

        if kind == "down":
            self.send_response(503)
            self.end_headers()
            return

        if kind == "hang":
            time.sleep(30)
            try:
                self.connection.close()
            except Exception:
                pass
            return

        if kind == "drop":
            drop_at = fault.get("drop_at", "before_headers")
            try:
                if drop_at == "after_headers":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                elif drop_at == "mid_body":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    self.wfile.write(b'{"block":')
                    self.wfile.flush()
                # before_headers — fall through, no headers sent.
            except Exception:
                pass
            try:
                self.connection.close()
            except Exception:
                pass
            return

        # rate_limit / error — REST shape: {"code": ..., "message": ...}.
        # No id echo, no envelope. Caller-configured corruption hooks still
        # apply because the body is a plain JSON object the corruption layer
        # already knows how to mutate. MAG-1837 — gate on chain_family="rest"
        # so a corruption authored for another transport can't reach here.
        snap = self.server.state.snapshot()
        body = {"code": fault["error_code"], "message": fault["error_message"]}
        self._reply(fault["status"], body,
                    corruption_mode=_corruption_for(snap, "rest"),
                    missing_field=_missing_field_for(snap, "rest"))

    def _reply(self, status: int, data: Any,
               corruption_mode: Optional[str] = None,
               missing_field: Optional[str] = None) -> None:
        """Serialise ``data`` as JSON and write a complete HTTP response.

        Mirrors JSONRPCHandler._reply: applies corruption_mode hooks (empty
        body / missing field / wrong type) before serialisation and
        byte-level corruption (truncated / invalid_json) after. The only
        REST-specific tweak is that ``missing_field`` can be a dotted path
        (``"block.header.height"``) — the helper walks the path and removes
        the leaf when the surrounding dicts exist.

        HEAD requests use the same code path but skip the body write — the
        caller sets ``self._head_mode = True`` for the duration of the
        request.
        """
        if not isinstance(data, dict):
            # REST occasionally returns non-dict (lists, scalars). Wrap so the
            # corruption hooks below have somewhere consistent to operate on.
            body_data: Any = data
            structural_only = False
        else:
            body_data = data
            structural_only = True

        # Structural corruption (dict mutations) — only meaningful when body is dict.
        if structural_only:
            if corruption_mode == "missing_field" and missing_field:
                body_data = _remove_dotted_path(body_data, missing_field)
            elif corruption_mode == "empty_response":
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            elif corruption_mode == "wrong_type":
                target = missing_field or next(iter(body_data.keys()), None)
                if target and target in body_data:
                    current = body_data[target]
                    if isinstance(current, bool):
                        body_data[target] = 1 if current else 0
                    elif isinstance(current, str):
                        body_data[target] = 12345
                    elif isinstance(current, (int, float)):
                        body_data[target] = "wrong_type_value"
                    else:
                        body_data[target] = "wrong_type_value"

        raw = json.dumps(body_data).encode()

        # Byte-level corruption.
        if corruption_mode == "truncated" and len(raw) > 10:
            raw = raw[:-10]
        elif corruption_mode == "invalid_json":
            raw = b"}{ {{ not valid json"

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not getattr(self, "_head_mode", False):
            self.wfile.write(raw)

    def log_message(self, *_):
        """Suppress the default per-request stdout logging from BaseHTTPRequestHandler."""
        pass


def _remove_dotted_path(data: Dict[str, Any], path: str) -> Dict[str, Any]:
    """Return ``data`` with the dotted-path key removed.

    ``path`` is a dot-separated string of object keys. Nested dicts are
    cloned along the descent so the caller's original dict isn't mutated.
    Missing intermediate keys cause the helper to return ``data`` unchanged.
    """
    if not path:
        return data
    segments = path.split(".")
    if len(segments) == 1:
        return {k: v for k, v in data.items() if k != segments[0]}
    head, rest = segments[0], ".".join(segments[1:])
    if not isinstance(data.get(head), dict):
        return data
    return {**data, head: _remove_dotted_path(data[head], rest)}


# ── Tendermint-RPC handler ────────────────────────────────────────────────────
#
# Peer to JSONRPCHandler / RestHandler. Same ProviderState, same fault
# primitives (via _apply_fault), Tendermint-specific wire shapes:
#
#   * GET  /<method>?<param=value>...        — URI form (CometBFT-native)
#   * POST /                                  — JSON-RPC body form
#   * GET  /ws / GET /websocket               — WS upgrade (out of scope —
#                                               the smart-router proxies these
#                                               to the upstream; sim doesn't
#                                               implement subscribe yet)
#
# Both GET and POST return a JSON-RPC envelope:
#
#     {"jsonrpc":"2.0","id":N,"result":{...}}     # success
#     {"jsonrpc":"2.0","id":N,"error":{...}}      # error
#
# The envelope wrapping happens in this handler (after handlers_tendermintrpc
# returns the bare result body) so the chain-domain module stays focused on
# response content rather than transport.


# Module-level Tendermint request-id counter for GET requests (the URI form
# has no body / no native id; CometBFT historically uses -1). Atomic via lock.
_TM_REQUEST_ID_COUNTER = 0
_TM_REQUEST_ID_LOCK = threading.Lock()


def _next_tm_request_id() -> int:
    """Return the next sim-side monotonically increasing TM request id (thread-safe)."""
    global _TM_REQUEST_ID_COUNTER
    with _TM_REQUEST_ID_LOCK:
        _TM_REQUEST_ID_COUNTER += 1
        return _TM_REQUEST_ID_COUNTER


class TendermintHandler(BaseHTTPRequestHandler):
    """Tendermint-RPC (CometBFT) surface for the provider simulator (MAG-1841).

    Shares ``ProviderState`` with the other handlers so a /scenario update
    targeting one provider is visible to all transports regardless of which
    port the test hits. Both GET and POST funnel into a common ``_handle``
    pipeline that:

    1. Snapshots state, captures lava-* headers, extracts (method, params, id).
    2. Applies fault primitives via ``_apply_fault`` (down / hang / drop /
       rate_limit / error).
    3. On success, dispatches to ``handlers_tendermintrpc.handle()``.
    4. Wraps the result in a JSON-RPC envelope.
    5. Writes the wire reply through ``_reply`` with corruption hooks.

    History accounting mirrors the JSON-RPC handler: success / error / rate_limit
    are recorded post-fault; down is recorded pre-body-parse with method="*".
    """

    # ── Verb dispatch ─────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    # ── Shared pipeline ───────────────────────────────────────────────────────

    def _handle(self, verb: str) -> None:
        """Run one Tendermint-RPC request end-to-end.

        Fault evaluation → method/params extraction → handlers_tendermintrpc.handle
        → JSON-RPC envelope wrap → wire-reply via _reply. History accounting
        is delegated to _apply_fault for fault branches and emitted inline
        for the success branch.
        """
        t_start = time.monotonic()
        state: ProviderState = self.server.state
        snap = state.snapshot()

        # Lava-* request headers — used for /history filtering, threaded
        # through to handlers_tendermintrpc for symmetry with other handlers.
        lava_headers = {
            k: v for k, v in self.headers.items() if k.lower().startswith("lava-")
        }

        # 1. Outage gate — return 503 with no body. ``method`` not yet known.
        if snap["mode"] == "down":
            fault = _apply_fault(state, snap, "*", None, lava_headers, t_start)
            self._emit_tm_fault(fault, request_id=None)
            return

        # 2. Optional latency injection — same shape as JSON-RPC.
        if snap["latency_ms"] > 0:
            time.sleep(snap["latency_ms"] / 1000.0)

        # 3. Parse the wire — method + params + request_id depend on the verb.
        try:
            method, params, request_id = self._extract_method_params(verb)
        except _TmParseError as exc:
            # Malformed request — JSON-RPC -32700 (parse error). Compose a
            # bare envelope inline; this path is rare so it doesn't justify
            # threading through _apply_fault.
            err_body = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            self._reply(400, err_body)
            state.push_call_to_buffer(
                "*",
                "parse_error",
                _elapsed_ms(t_start),
                request_id=None,
                lava_headers=lava_headers,
            )
            return

        method_label = method  # For history filtering — matches JSON-RPC convention.

        # 4. Post-parse fault primitives — hang / drop / rate_limit / error.
        fault = _apply_fault(state, snap, method_label, request_id, lava_headers, t_start)
        if fault is not None:
            self._emit_tm_fault(fault, request_id=request_id)
            return

        # 5. Success path — chain-specific dispatch.
        normalized = handlers_tendermintrpc._normalize_tm_params(verb, params)
        http_status, result_body = handlers_tendermintrpc.handle(
            state, method, normalized, snap, lava_headers
        )

        # 6. Wrap in JSON-RPC envelope. If the handler returned an error dict
        #    (unknown method, per-method override with "error" key), the
        #    envelope carries ``error`` rather than ``result``.
        if isinstance(result_body, dict) and "error" in result_body:
            envelope = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": result_body["error"],
            }
            history_status = "error"
        else:
            envelope = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result_body,
            }
            history_status = "success"

        # MAG-1837 — gate corruption_mode on chain_family="tendermintrpc"
        # so a corruption authored for another transport doesn't reach the
        # Tendermint port.
        self._reply(
            http_status,
            envelope,
            corruption_mode=_corruption_for(snap, "tendermintrpc"),
            missing_field=_missing_field_for(snap, "tendermintrpc"),
        )
        state.push_call_to_buffer(
            method_label,
            history_status,
            _elapsed_ms(t_start),
            request_id=request_id,
            lava_headers=lava_headers,
        )

    # ── Wire parsing ──────────────────────────────────────────────────────────

    def _extract_method_params(self, verb: str) -> Tuple[str, Any, Any]:
        """Pull ``(method, raw_params, request_id)`` from the wire.

        GET shape:
            URI ``/<method>?<param=value>...``. ``method`` is the path
            basename; ``raw_params`` is ``parse_qs`` output (Dict[str, List[str]]).
            ``request_id`` is the sim-side counter (CometBFT historically
            uses -1 for GET responses; we use a positive counter so /history
            correlation_group stays stable).

        POST shape:
            JSON-RPC body ``{"jsonrpc":"2.0","id":N,"method":"...","params":{...}}``.
            ``method`` from the body; ``raw_params`` from ``params`` (dict
            for named params, list for positional — Tendermint uses named).
            ``request_id`` from the body's ``id`` field.

        Raises:
            _TmParseError: malformed POST body or missing method.
        """
        if verb == "GET":
            parsed = urlparse(self.path)
            path = parsed.path.strip("/")
            # Strip query suffix (path already has it removed by urlparse).
            # Path is the method name; nested paths (e.g. /debug/foo) get rejected
            # downstream by the handler's unknown-method branch.
            method = path
            if not method:
                raise _TmParseError("GET URI has no method (empty path)")
            raw_params = parse_qs(parsed.query)
            request_id = _next_tm_request_id()
            return method, raw_params, request_id

        # POST
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            raise _TmParseError("POST body is empty")
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise _TmParseError(f"POST body not valid JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise _TmParseError(f"POST body not a JSON object: {type(body).__name__}")
        method = body.get("method")
        if not isinstance(method, str) or not method:
            raise _TmParseError("POST body missing 'method' field")
        raw_params = body.get("params")
        request_id = body.get("id")
        return method, raw_params, request_id

    # ── Wire emission ─────────────────────────────────────────────────────────

    def _emit_tm_fault(self, fault: Optional[Dict[str, Any]], request_id: Any) -> None:
        """Translate a fault dict from ``_apply_fault`` into a Tendermint wire reply.

        Tendermint wraps rate_limit and error in a JSON-RPC envelope
        (``{"jsonrpc":"2.0","id":...,"error":{"code":...,"message":...}}``).
        Wire-level kinds (``down`` / ``hang`` / ``drop``) are identical to
        the JSON-RPC equivalents — fault kind, not chain, drives the wire
        action.
        """
        if fault is None:
            return

        kind = fault["kind"]

        if kind == "down":
            self.send_response(503)
            self.end_headers()
            return

        if kind == "hang":
            time.sleep(30)
            try:
                self.connection.close()
            except Exception:
                pass
            return

        if kind == "drop":
            drop_at = fault.get("drop_at", "before_headers")
            try:
                if drop_at == "after_headers":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                elif drop_at == "mid_body":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    self.wfile.write(b'{"jsonrpc":"2.0",')
                    self.wfile.flush()
                # before_headers — fall through, no headers sent.
            except Exception:
                pass
            try:
                self.connection.close()
            except Exception:
                pass
            return

        # rate_limit / error — Tendermint wire is a JSON-RPC envelope.
        # MAG-1837 — gate corruption_mode on chain_family="tendermintrpc".
        snap = self.server.state.snapshot()
        envelope = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": fault["error_code"],
                "message": fault["error_message"],
            },
        }
        self._reply(
            fault["status"],
            envelope,
            corruption_mode=_corruption_for(snap, "tendermintrpc"),
            missing_field=_missing_field_for(snap, "tendermintrpc"),
        )

    def _reply(
        self,
        status: int,
        data: Any,
        corruption_mode: Optional[str] = None,
        missing_field: Optional[str] = None,
    ) -> None:
        """Serialise ``data`` as JSON and write a complete HTTP response.

        Mirrors RestHandler._reply: applies corruption_mode hooks (empty
        body / missing field / wrong type) before serialisation and
        byte-level corruption (truncated / invalid_json) after. ``missing_field``
        accepts dotted paths (``"result.sync_info.latest_block_height"``)
        so tests can target nested envelope fields without rewriting the
        helper.
        """
        if not isinstance(data, dict):
            body_data: Any = data
            structural_only = False
        else:
            body_data = data
            structural_only = True

        if structural_only:
            if corruption_mode == "missing_field" and missing_field:
                body_data = _remove_dotted_path(body_data, missing_field)
            elif corruption_mode == "empty_response":
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            elif corruption_mode == "wrong_type":
                target = missing_field or next(iter(body_data.keys()), None)
                if target and target in body_data:
                    current = body_data[target]
                    if isinstance(current, bool):
                        body_data[target] = 1 if current else 0
                    elif isinstance(current, str):
                        body_data[target] = 12345
                    elif isinstance(current, (int, float)):
                        body_data[target] = "wrong_type_value"
                    else:
                        body_data[target] = "wrong_type_value"

        raw = json.dumps(body_data).encode()

        if corruption_mode == "truncated" and len(raw) > 10:
            raw = raw[:-10]
        elif corruption_mode == "invalid_json":
            raw = b"}{ {{ not valid json"

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_):
        """Suppress the default per-request stdout logging from BaseHTTPRequestHandler."""
        pass


class _TmParseError(ValueError):
    """Raised by TendermintHandler when the wire shape is malformed.

    Caught inside ``_handle`` so the dispatcher emits a JSON-RPC parse-error
    envelope instead of letting the exception bubble up to the HTTP server
    (which would emit a 500 with no body).
    """


# ── Control API handler ───────────────────────────────────────────────────────

class ControlHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        """Handle POST requests on the control API.

        Routes:
          POST /scenario       — update per-provider config from the request body.
                                 Body: {"providers": {"1": {...}, "2": {...}}}
          POST /reset          — reset scenario config only (mode, latency, responses → defaults).
                                 Does NOT clear history.
          POST /history/clear  — wipe call history and counters only.
                                 Does NOT touch scenario config.
          POST /reset/all      — reset scenario config AND clear history.

        Returns 404 for any unrecognised path.
        """
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}

        if self.path == "/scenario":
            # MAG-1821 — validation errors raised inside state.update (via
            # _normalise_responses) come back as ValueError. Surface them
            # as 400 so the test sees a clear message instead of a 500.
            #
            # All-or-nothing application: pre-normalise every provider's
            # responses dict (and resolve the target state) FIRST so a
            # ValueError raised on provider N doesn't leave providers
            # 1..N-1 with partially-applied scalar fields. Only after the
            # full validation pass succeeds do we mutate any state.
            providers_payload = body.get("providers", {})
            staged: list = []
            try:
                for pid, cfg in providers_payload.items():
                    state = self.server.provider_states.get(str(pid))
                    if state is None:
                        continue
                    staged_cfg = dict(cfg)
                    if "responses" in staged_cfg:
                        # Pre-normalise so ValueError fires here, before any
                        # state.update mutates scalar fields. The result is
                        # already a dict so state.update's re-call of
                        # _normalise_responses is an idempotent no-op.
                        staged_cfg["responses"] = _normalise_responses(
                            staged_cfg["responses"]
                        )
                    staged.append((state, staged_cfg))
            except ValueError as exc:
                self._reply(400, {"error": str(exc)})
                return
            for state, staged_cfg in staged:
                state.update(staged_cfg)
            self._reply(200, {"status": "ok"})

        elif self.path == "/reset":
            for state in self.server.provider_states.values():
                state.reset_scenario()
            self._reply(200, {"status": "scenario reset"})

        elif self.path == "/history/clear":
            for state in self.server.provider_states.values():
                state.clear_history()
            self._reply(200, {"status": "history cleared"})

        elif self.path == "/reset/all":
            for state in self.server.provider_states.values():
                state.reset_scenario()
                state.clear_history()
            self._reply(200, {"status": "scenario reset and history cleared"})

        elif self.path == "/ws/emit":
            sub_id = body.get("subscription_id")
            event = body.get("event")
            if not sub_id:
                self._reply(400, {"error": "missing field: subscription_id"})
                return
            if event is None:
                event = {}

            handle = _lookup_ws_subscription(sub_id)
            if handle is None or handle.closed.is_set():
                self._reply(404, {"error": "unknown subscription"})
                return

            import stubs_ws
            wrapped = stubs_ws.build_event_frame(handle.envelope, sub_id, event)
            import handlers_ws as _hws
            frame_bytes = _hws._text_frame(wrapped)

            try:
                handle.out_queue.put_nowait(frame_bytes)
            except queue.Full:
                self._reply(503, {"error": "queue full"})
                return

            # Record the push in history so /history reflects pushed events.
            state = self.server.provider_states.get(handle.provider_id)
            if state is not None:
                state.push_call_to_buffer(
                    f"{handle.envelope} push", "success", 0,
                    request_id=sub_id, lava_headers={},
                )

            self._reply(200, {"status": "emitted", "subscription_id": sub_id})

        else:
            self._reply(404, {"error": "unknown path"})

    def do_GET(self):
        """Handle GET requests on the control API.

        Routes:
          GET /health    — liveness probe, always returns {"status": "ok"}.
          GET /scenario  — current snapshot of all provider configs.
          GET /stats     — all-time call counters and per-status breakdown per provider.
          GET /history   — merged, time-sorted call buffer across all providers.
                           Supports query params: last, from, to, provider, method, status,
                           request_id.
                           Every entry includes a call_order field (1 = first attempted)
                           and a request_id field (echoes the JSON-RPC id from the request).

        Returns 404 for any unrecognised path.
        """
        if self.path == "/health":
            self._reply(200, {"status": "ok"})

        elif self.path == "/scenario":
            self._reply(200, {
                "providers": {pid: s.snapshot()
                              for pid, s in self.server.provider_states.items()}
            })

        elif self.path == "/stats":
            # Per-provider call counts and status breakdown.
            # Use this to see if one provider is being skipped or hammered.
            self._reply(200, {
                "providers": {pid: s.stats()
                              for pid, s in self.server.provider_states.items()}
            })

        elif self.path == "/history" or self.path.startswith("/history?"):
            # Supported query params (all optional, combinable):
            #   ?from=<unix_ts>         — include only calls at or after this timestamp
            #   ?to=<unix_ts>           — include only calls at or before this timestamp
            #   ?last=<seconds>         — shorthand: calls in the last N seconds
            #   ?provider=<id>          — filter to a single provider (1, 2, or 3)
            #   ?method=<name>          — filter to a specific RPC method
            #   ?status=<name>          — filter by status (success, error, rate_limit, down)
            #   ?request_id=<id>        — filter by the JSON-RPC id echoed in the request
            #   ?lava_header_*=<value>  — filter by lava header name (e.g. lava_header_lava_stateful_api=true)
            #   ?max=<N>                — return at most N most-recent entries (MAG-1822).
            #                             Applied AFTER all other filters and AFTER
            #                             call_order assignment, so each entry keeps its
            #                             true 1-based timeline index even when sliced.
            #                             max=0 → []; max<0 → 400; non-int → 400.
            #
            # Each entry in the response includes:
            #   call_order        — 1-based position in the merged timeline (sorted by ts).
            #                       call_order=1 is the provider the router tried FIRST,
            #                       call_order=2 is the second attempt, etc.
            #   correlation_group — groups calls by (request_id, method) within 50ms window.
            #                       calls from same relay have same correlation_group.
            #   request_id        — the JSON-RPC id from the request body (None for down-mode)
            #   lava_headers      — dict of all lava-* headers sent by the router (empty dict if none)
            #
            # Examples:
            #   /history?last=60
            #   /history?from=1774534600&to=1774534700
            #   /history?last=120&provider=2
            #   /history?last=60&status=error
            #   /history?request_id=42
            #   /history?last=60&lava_header_lava_stateful_api=true
            #   /history?max=50            — tail 50 most-recent across all providers
            qs = parse_qs(urlparse(self.path).query)

            t_from        = float(qs["from"][0])      if "from"       in qs else None
            t_to          = float(qs["to"][0])         if "to"         in qs else None
            last_secs     = float(qs["last"][0])       if "last"       in qs else None
            f_provider    = qs["provider"][0]          if "provider"   in qs else None
            f_method      = qs["method"][0]            if "method"     in qs else None
            f_status      = qs["status"][0]            if "status"     in qs else None
            f_request_id  = qs["request_id"][0]        if "request_id" in qs else None

            # ?max=N — MAG-1822 tail-slicing. Parsed here so a malformed value
            # short-circuits with 400 before we do any history work.
            f_max = None
            if "max" in qs:
                raw_max = qs["max"][0]
                try:
                    parsed_max = int(raw_max)
                except (TypeError, ValueError):
                    self._reply(400, {
                        "error": "invalid_max",
                        "message": f"max must be a non-negative integer, got {raw_max!r}",
                    })
                    return
                if parsed_max < 0:
                    self._reply(400, {
                        "error": "invalid_max",
                        "message": f"max must be >= 0, got {parsed_max}",
                    })
                    return
                f_max = parsed_max

            # Extract lava header filters: ?lava_header_lava_stateful_api=true becomes {"lava-stateful-api": "true"}
            f_lava_headers = {}
            for param in qs:
                if param.startswith("lava_header_"):
                    header_name = param.replace("lava_header_", "").replace("_", "-")
                    header_value = qs[param][0]
                    f_lava_headers[header_name] = header_value

            if last_secs is not None:
                t_from = time.time() - last_secs

            all_calls = []
            for pid, s in self.server.provider_states.items():
                if f_provider and pid != f_provider:
                    continue
                for entry in s.get_history():
                    if t_from        and entry["ts"] < t_from:                      continue
                    if t_to          and entry["ts"] > t_to:                        continue
                    if f_method      and entry["method"] != f_method:               continue
                    if f_status      and entry["status"] != f_status:               continue
                    if f_request_id  and str(entry.get("request_id")) != f_request_id: continue
                    # Check lava header filters (all must match)
                    if f_lava_headers:
                        entry_headers = entry.get("lava_headers", {})
                        if not all(entry_headers.get(k) == v for k, v in f_lava_headers.items()):
                            continue
                    all_calls.append({"provider": pid, **entry})

            all_calls.sort(key=lambda x: x["ts"])
            
            # Assign correlation_group: group calls by (request_id, method) within 50ms window
            correlation_map = {}  # (request_id, method) → (last_ts, group_id)
            group_counter = 0
            
            for entry in all_calls:
                key = (entry.get("request_id"), entry["method"])
                
                if key in correlation_map:
                    last_ts, group_id = correlation_map[key]
                    if entry["ts"] - last_ts < 0.050:  # 50ms window
                        entry["correlation_group"] = group_id
                    else:
                        # New relay started (same request_id+method but >50ms apart)
                        group_counter += 1
                        entry["correlation_group"] = group_counter
                        correlation_map[key] = (entry["ts"], group_counter)
                else:
                    # First call with this (request_id, method)
                    group_counter += 1
                    entry["correlation_group"] = group_counter
                    correlation_map[key] = (entry["ts"], group_counter)
            
            # Assign call_order within the merged timeline
            for i, entry in enumerate(all_calls, start=1):
                entry["call_order"] = i

            # MAG-1822 — ?max=N tail slice. Applied last so each kept entry's
            # call_order still reflects its position in the full filtered
            # timeline (e.g. /history?max=10 on a 50-call history returns
            # entries with call_order 41..50, not 1..10). max=0 yields [].
            if f_max is not None:
                all_calls = all_calls[-f_max:] if f_max > 0 else []

            self._reply(200, {"count": len(all_calls), "history": all_calls})

        elif self.path == "/ws/subscriptions":
            self._reply(200, {"subscriptions": _all_ws_subscriptions()})

        else:
            self._reply(404, {"error": "unknown path"})

    def _reply(self, status: int, data: dict):
        """Serialise data as JSON and write a complete HTTP response with correct headers.

        Args:
            status: HTTP status code (e.g. 200, 404).
            data:   Response payload — will be JSON-encoded and sent as the body.
        """
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        """Suppress the default per-request stdout logging from BaseHTTPRequestHandler."""
        pass


# ── Server startup ────────────────────────────────────────────────────────────



def main():
    """Start all simulator servers and block until interrupted.

    Spins up the full surface matrix in daemon threads:

      JSON-RPC (six listeners — three primary + three backup)
        - Primary  : ports 18545 / 18546 / 18547 (pids 1-3)
        - Backup   : ports 18560 / 18561 / 18562 (pids 4-6)
        See PROVIDER_PORTS / BACKUP_PROVIDER_PORTS / ALL_PROVIDER_PORTS in
        constants.py — the simulator process is identical across both pools,
        the only difference is the smart-router-side `is_backup: true` flag
        in values_sim.yml.

      gRPC (six listeners — three primary + three backup)
        - Primary  : ports 18548 / 18549 / 18550 (pids 1-3, shared state
                     with the matching JSON-RPC primary)
        - Backup   : ports 18563 / 18564 / 18565 (pids 7-9, distinct state)

      REST (six listeners — three primary + three backup)
        - Primary  : ports 18551 / 18552 / 18553 (pids 1-3, shared state)
        - Backup   : ports 18566 / 18567 / 18568 (pids 10-12)

      Tendermint-RPC (six listeners — three primary + three backup)
        - Primary  : ports 18554 / 18555 / 18556 (pids 1-3, shared state)
        - Backup   : ports 18569 / 18570 / 18571 (pids 13-15)

      WebSocket (six listeners — three primary + three backup)
        - Primary  : ports 18557 / 18558 / 18559 (pids 1-3, shared state)
        - Backup   : ports 18572 / 18573 / 18574 (pids 16-18)

      One ControlHandler server (port 19000) for scenario config, reset,
      history, and /ws/emit.

    State sharing model
    -------------------
    Primary tier pids (1-3) get ONE ProviderState each that backs every
    primary surface (JSON-RPC + REST + gRPC + TM + WS at the same time).
    A /scenario POST targeting pid "1" reconfigures all five primary
    transports for that provider — this is what mixed-chain tests rely
    on. Backup pids (4-18) each get their own ProviderState instance per
    surface: pid "4" is the JSON-RPC backup #1; pid "7" is the gRPC
    backup #1; these are independent. The control API keys by pid string
    globally, so distinct pids guarantee distinct configuration.

    Mixed-chain scenarios (a /scenario payload setting chain_family="eth"
    on provider 1 and chain_family="rest" on provider 2) work for the
    primary tier because each primary provider's state is shared across
    surfaces. Backup-tier /scenario calls instead address the specific
    backup pool by pid (no chain_family ambiguity — the pid range itself
    encodes the surface).

    Blocks on thread.join() and shuts all servers down cleanly on
    KeyboardInterrupt.
    """
    # Primary-tier states are shared across surfaces (one ProviderState backs
    # JSON-RPC + REST + gRPC + TM + WS for the same pid). Backup-tier states
    # are independent per surface — distinct pids guarantee distinct state.
    states = {pid: ProviderState() for pid in ALL_PROVIDER_PORTS}
    for pid in GRPC_BACKUP_PORTS:
        states[pid] = ProviderState()
    for pid in REST_BACKUP_PORTS:
        states[pid] = ProviderState()
    for pid in TM_BACKUP_PORTS:
        states[pid] = ProviderState()
    for pid in WS_BACKUP_PORTS:
        states[pid] = ProviderState()

    servers = []
    for pid, port in ALL_PROVIDER_PORTS.items():
        # ThreadingHTTPServer so a slow/hanging request on one provider doesn't
        # block its own subsequent requests or the other providers' threads.
        srv = ThreadingHTTPServer(("0.0.0.0", port), JSONRPCHandler)
        srv.daemon_threads = True
        srv.state       = states[pid]
        srv.provider_id = pid          # available as self.server.provider_id in handler
        servers.append(srv)

    # REST servers (MAG-1777). Primary tier shares ProviderState with the
    # matching JSON-RPC primary (pids 1-3), so a /scenario update on
    # provider 1 changes how both the JSON-RPC port (18545) and the REST
    # port (18551) reply. Each server gets its own RestHandler instance
    # because BaseHTTPRequestHandler is per-request.
    for pid, port in REST_PORTS.items():
        rest_srv = ThreadingHTTPServer(("0.0.0.0", port), RestHandler)
        rest_srv.daemon_threads = True
        rest_srv.state       = states[pid]
        rest_srv.provider_id = pid
        servers.append(rest_srv)

    # REST backup tier (pids 10-12 → 18566-18568). Independent ProviderState
    # per backup pid — the smart-router only routes to this pool after the
    # primary REST pool is exhausted on a request (is_backup: true in
    # values_sim.yml).
    for pid, port in REST_BACKUP_PORTS.items():
        rest_srv = ThreadingHTTPServer(("0.0.0.0", port), RestHandler)
        rest_srv.daemon_threads = True
        rest_srv.state       = states[pid]
        rest_srv.provider_id = pid
        servers.append(rest_srv)

    # Tendermint-RPC servers (MAG-1841). Same pattern as REST — primary tier
    # shares ProviderState with the JSON-RPC primary; backup tier is its
    # own pool with distinct pids 13-15.
    for pid, port in TM_PORTS.items():
        tm_srv = ThreadingHTTPServer(("0.0.0.0", port), TendermintHandler)
        tm_srv.daemon_threads = True
        tm_srv.state       = states[pid]
        tm_srv.provider_id = pid
        servers.append(tm_srv)

    for pid, port in TM_BACKUP_PORTS.items():
        tm_srv = ThreadingHTTPServer(("0.0.0.0", port), TendermintHandler)
        tm_srv.daemon_threads = True
        tm_srv.state       = states[pid]
        tm_srv.provider_id = pid
        servers.append(tm_srv)

    # WS servers (MAG-1801). Primary tier shares ProviderState with the
    # JSON-RPC primary (pids 1-3); backup tier (pids 16-18) gets its own
    # pool. Each server gets its own WsHandler instance because
    # BaseHTTPRequestHandler is per-request.
    for pid, port in WS_PORTS.items():
        ws_srv = ThreadingHTTPServer(("0.0.0.0", port), handlers_ws.WsHandler)
        ws_srv.daemon_threads = True
        ws_srv.state       = states[pid]
        ws_srv.provider_id = pid
        servers.append(ws_srv)

    for pid, port in WS_BACKUP_PORTS.items():
        ws_srv = ThreadingHTTPServer(("0.0.0.0", port), handlers_ws.WsHandler)
        ws_srv.daemon_threads = True
        ws_srv.state       = states[pid]
        ws_srv.provider_id = pid
        servers.append(ws_srv)

    ctrl = HTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    ctrl.provider_states = states
    servers.append(ctrl)

    print("Provider simulator started")
    for pid, port in PROVIDER_PORTS.items():
        print(f"  provider {pid:>2} (jsonrpc,        primary) → :{port}")
    for pid, port in BACKUP_PROVIDER_PORTS.items():
        print(f"  provider {pid:>2} (jsonrpc,        backup)  → :{port}")
    for pid, port in GRPC_PROVIDER_PORTS.items():
        print(f"  provider {pid:>2} (grpc,           primary) → :{port}")
    for pid, port in GRPC_BACKUP_PORTS.items():
        print(f"  provider {pid:>2} (grpc,           backup)  → :{port}")
    for pid, port in REST_PORTS.items():
        print(f"  provider {pid:>2} (rest,           primary) → :{port}")
    for pid, port in REST_BACKUP_PORTS.items():
        print(f"  provider {pid:>2} (rest,           backup)  → :{port}")
    for pid, port in TM_PORTS.items():
        print(f"  provider {pid:>2} (tendermintrpc,  primary) → :{port}")
    for pid, port in TM_BACKUP_PORTS.items():
        print(f"  provider {pid:>2} (tendermintrpc,  backup)  → :{port}")
    for pid, port in WS_PORTS.items():
        print(f"  provider {pid:>2} (ws,             primary) → :{port}")
    for pid, port in WS_BACKUP_PORTS.items():
        print(f"  provider {pid:>2} (ws,             backup)  → :{port}")
    print(f"  control API  → :{CONTROL_PORT}")
    print(f"  GET /stats   → call counts per provider")
    print(f"  GET /history → ordered call log (who was tried first)")

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]

    # gRPC servers (MAG-1780). Each runs its own asyncio loop on a daemon
    # thread, sharing the same ProviderState instance with the matching
    # JSON-RPC port (primary tier) or a dedicated ProviderState (backup
    # tier) so /scenario applies as expected per pid. Import locally so a
    # missing grpcio dep doesn't break the JSON-RPC-only path (e.g. in
    # tests that don't install gRPC extras).
    try:
        import grpc_server  # local import keeps gRPC dep optional
        for pid, port in GRPC_PROVIDER_PORTS.items():
            threads.append(threading.Thread(
                target=grpc_server.run_grpc_in_thread,
                args=(port, states[pid]),
                daemon=True,
                name=f"grpc-provider-{pid}",
            ))
        for pid, port in GRPC_BACKUP_PORTS.items():
            threads.append(threading.Thread(
                target=grpc_server.run_grpc_in_thread,
                args=(port, states[pid]),
                daemon=True,
                name=f"grpc-backup-{pid}",
            ))
    except ImportError as exc:
        print(f"  gRPC servers DISABLED — grpcio import failed: {exc}")

    for t in threads:
        t.start()

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        for s in servers:
            s.shutdown()


# Entry point lives in run.py — see the docstring there. server.py is a
# library module; running it directly would duplicate module-level state
# (e.g. _WS_SUBSCRIPTIONS) across __main__ and a second `server` import.

