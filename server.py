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

chain_family (per provider) tags which surface's faults the scenario owns:
one word — a blockchain (eth / btc / ln / solana) or a connection type
(grpc / rest / tendermintrpc / ws), never both at once. The listener port
picks the response handler; chain_family only gates content-fault firing.
mode="down" fires on every surface regardless. Unknown values → HTTP 400.

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
import os
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import handlers_btc
import handlers_eth
import handlers_lnd
import handlers_rest
import handlers_solana
import handlers_tendermintrpc
import handlers_ws
import stubs_solana
from constants import (
    BTC_PRIMARY_PORTS,
    CONTROL_PORT,
    ETH_ALL_PORTS,
    ETH_BACKUP_PORTS,
    ETH_PRIMARY_PORTS,
    ETH_SOLO_PORTS,
    GRPC_BACKUP_PORTS,
    GRPC_PRIMARY_PORTS,
    HISTORY_MAX,
    LN_PRIMARY_PORTS,
    REST_BACKUP_PORTS,
    REST_PRIMARY_PORTS,
    SOLANA_PRIMARY_PORTS,
    SOLANA_SOLO_PORTS,
    TM_BACKUP_PORTS,
    TM_PRIMARY_PORTS,
    WS_BACKUP_PORTS,
    WS_PRIMARY_PORTS,
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
                    f'per-method mode="error" is not supported '
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
                    f'per-method mode="error" is not supported '
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
                        f'Use mode="error" + http_status for non-2xx response shapes.'
                    )
        return raw
    # Unknown shape — clear responses rather than crash.
    return {}


# ── Scenario input validation (strict) ────────────────────────────────────────
#
# A POST /scenario used to silently ignore anything it didn't recognise: an
# unknown provider id was skipped, an unknown field was dropped, an invalid
# ``mode`` fell through to the success path, and an out-of-range
# ``error_probability`` or a negative ``latency_ms`` was applied as-is. A typo'd
# scenario therefore "succeeded" (HTTP 200) while configuring nothing, and the
# test ran green against an unconfigured provider. The constants + validator
# below make /scenario reject bad input with HTTP 400 so a typo fails loudly.

# The fields a provider config may set. Mirrors the keys read in
# ProviderState.update(); anything else is a typo.
_SCENARIO_FIELDS = frozenset(
    {
        "mode",
        "latency_ms",
        "error_probability",
        "error_code",
        "error_message",
        "http_status",
        "responses",
        "corruption_mode",
        "missing_field",
        "blocks_behind",
        "solana_slot_block_gap",
        "solana_slot_offset",
        "drop_at",
        "chain_family",
        "logs_indexed_up_to",
        "logs_lag_mode",
        "solana_unknown_method_mode",
        "fail_first_n",
        "then_mode",
    }
)
_SCENARIO_MODES = frozenset(
    {
        "success",
        "error",
        "rate_limit",
        "down",
        "hang",
        "drop_connection",
    }
)
_SCENARIO_CORRUPTION_MODES = frozenset(
    {
        "truncated",
        "missing_field",
        "invalid_json",
        "empty_response",
        "wrong_type",
        "invalid_proto",
    }
)
_SCENARIO_DROP_AT = frozenset({"before_headers", "after_headers", "mid_body"})
_SCENARIO_CHAIN_FAMILIES = frozenset(
    {
        "eth",
        "btc",
        "ln",
        "solana",
        "grpc",
        "rest",
        "tendermintrpc",
        "ws",
    }
)
_SCENARIO_LOGS_LAG_MODES = frozenset({"empty", "partial"})


def _validate_scenario_cfg(pid: Any, cfg: Any) -> None:
    """Validate one provider's POST /scenario config; raise ValueError (→ 400)
    on any unknown field, invalid enum value, or out-of-range number.

    Only shape / enum / range are checked here. Per-method ``responses`` entries
    keep their own validation in _normalise_responses.
    """
    if not isinstance(cfg, dict):
        raise ValueError(f"provider {pid!r} config must be an object, got {type(cfg).__name__}")
    unknown = set(cfg) - _SCENARIO_FIELDS
    if unknown:
        raise ValueError(
            f"provider {pid!r}: unknown scenario field(s) {sorted(unknown)}; "
            f"allowed fields are {sorted(_SCENARIO_FIELDS)}"
        )
    if "mode" in cfg and cfg["mode"] not in _SCENARIO_MODES:
        raise ValueError(
            f"provider {pid!r}: invalid mode {cfg['mode']!r}; "
            f"allowed: {sorted(_SCENARIO_MODES)}"
        )
    if (
        cfg.get("corruption_mode") is not None
        and cfg["corruption_mode"] not in _SCENARIO_CORRUPTION_MODES
    ):
        raise ValueError(
            f"provider {pid!r}: invalid corruption_mode {cfg['corruption_mode']!r}; "
            f"allowed: {sorted(_SCENARIO_CORRUPTION_MODES)} (or null)"
        )
    if "drop_at" in cfg and cfg["drop_at"] not in _SCENARIO_DROP_AT:
        raise ValueError(
            f"provider {pid!r}: invalid drop_at {cfg['drop_at']!r}; "
            f"allowed: {sorted(_SCENARIO_DROP_AT)}"
        )
    if "chain_family" in cfg and cfg["chain_family"] not in _SCENARIO_CHAIN_FAMILIES:
        raise ValueError(
            f"provider {pid!r}: invalid chain_family {cfg['chain_family']!r}; "
            f"allowed: {sorted(_SCENARIO_CHAIN_FAMILIES)}"
        )
    if "logs_lag_mode" in cfg and cfg["logs_lag_mode"] not in _SCENARIO_LOGS_LAG_MODES:
        raise ValueError(
            f"provider {pid!r}: invalid logs_lag_mode {cfg['logs_lag_mode']!r}; "
            f"allowed: {sorted(_SCENARIO_LOGS_LAG_MODES)}"
        )
    if "error_probability" in cfg:
        ep = cfg["error_probability"]
        # bool is a subclass of int — reject it so True/False can't masquerade
        # as a probability.
        if isinstance(ep, bool) or not isinstance(ep, (int, float)) or not (0.0 <= ep <= 1.0):
            raise ValueError(
                f"provider {pid!r}: error_probability must be a number in "
                f"[0.0, 1.0], got {ep!r}"
            )
    if "latency_ms" in cfg:
        lm = cfg["latency_ms"]
        if isinstance(lm, bool) or not isinstance(lm, int) or lm < 0:
            raise ValueError(
                f"provider {pid!r}: latency_ms must be a non-negative integer, " f"got {lm!r}"
            )
    if "solana_unknown_method_mode" in cfg and cfg["solana_unknown_method_mode"] not in (
        "error",
        "null",
    ):
        raise ValueError(
            f"provider {pid!r}: invalid solana_unknown_method_mode "
            f"{cfg['solana_unknown_method_mode']!r}; allowed: ['error', 'null']"
        )
    if "then_mode" in cfg and cfg["then_mode"] not in _SCENARIO_MODES:
        raise ValueError(
            f"provider {pid!r}: invalid then_mode {cfg['then_mode']!r}; "
            f"allowed: {sorted(_SCENARIO_MODES)}"
        )
    if "fail_first_n" in cfg:
        fn = cfg["fail_first_n"]
        if isinstance(fn, bool) or not isinstance(fn, int) or fn < 0:
            raise ValueError(
                f"provider {pid!r}: fail_first_n must be a non-negative integer, " f"got {fn!r}"
            )


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
    mode: str = "success"  # success | error | rate_limit | down
    latency_ms: int = 0
    error_probability: float = 0.0
    error_code: int = -32000  # JSON-RPC error code when mode="error"
    error_message: str = "Internal error"  # JSON-RPC error message when mode="error"
    http_status: int = 200  # HTTP status code for error responses (200 = JSON-RPC body error)
    responses: Dict[str, Any] = field(default_factory=dict)
    corruption_mode: Optional[str] = (
        None  # one of: None, "truncated", "missing_field", "invalid_json", "empty_response", "wrong_type", "invalid_proto" (invalid_proto is implemented by the gRPC listener only)
    )
    missing_field: Optional[str] = (
        None  # field-name slot — which top-level field to target when corruption_mode is "missing_field" (omit it) or "wrong_type" (swap its type). Defaults to "result" for wrong_type when unset.
    )
    blocks_behind: int = 0  # 0 = current head; positive = behind; negative = ahead
    # MAG-2231: Solana getLatestBlockhash slot ↔ lastValidBlockHeight gap.
    # handlers_solana emits result.context.slot = S and
    # result.value.lastValidBlockHeight = S - solana_slot_block_gap. The two
    # numbers feed the router's two different reads: per-user seenBlock comes
    # from context.slot, the endpoint chain-tracker value from
    # lastValidBlockHeight. The default mirrors the ~22M real-mainnet gap and
    # exceeds the router's 50-block consistency threshold, reproducing the
    # "No pairings available" filter (MAG-1591). Sourced from stubs_solana so
    # the field, the handler fallback, and /reset all share one number. Only
    # read by handlers_solana.handle — i.e. the Solana listeners (18582-18584
    # primary, 18585 solo); every other handler ignores this field.
    solana_slot_block_gap: int = stubs_solana.SOLANA_DEFAULT_SLOT_BLOCK_GAP
    # MAG-2233 #1: per-provider Solana slot offset for multi-slot divergence.
    # handlers_solana reports slot = SOLANA_BASE_SLOT + solana_slot_offset for
    # this provider; lastValidBlockHeight stays slot - solana_slot_block_gap, so
    # the gap applies on top of the offset. Default 0 keeps every provider at the
    # shared base slot (identical to pre-MAG-2233 behaviour). A test sets distinct
    # offsets per provider (e.g. one current, two stale-behind) so the router's
    # Solana consistency filter can keep the current provider and drop the stale
    # ones. Negative = behind the base slot, positive = ahead. Only read by
    # handlers_solana.handle — i.e. the Solana listeners (18582-18584 primary,
    # 18585 solo); every other handler ignores this field.
    solana_slot_offset: int = 0
    # Opt-in unknown-method behaviour on Solana. "null" (default) keeps the
    # parse-friendly {"result": null} for an unrecognised method (backward-
    # compat); "error" makes handlers_solana return a real -32601 method-not-
    # found so the router's Solana error classifier can be exercised.
    solana_unknown_method_mode: str = "null"
    # Deterministic "fail the first N calls, then recover" fault. When
    # fail_first_n > 0, the first N requests on the OWNING JSON-RPC listener
    # (the one whose handler_chain_family matches this snap's chain_family)
    # use the configured ``mode`` (the fault); every request after uses
    # ``then_mode`` (default "success"). The window counts owning-listener
    # calls only — every other surface (REST / Tendermint-RPC / WS / gRPC /
    # non-owning JSON-RPC pools) observes the elapsed window via
    # _effective_mode without advancing it, so cross-transport traffic can't
    # burn the first-N budget, yet a provider-wide mode="down" still clears
    # on every surface once the owning listener has consumed the window.
    # Makes retry-then-recover / circuit-breaker paths repeatable without
    # relying on random error_probability.
    fail_first_n: int = 0
    then_mode: str = "success"
    _fail_counter: int = field(default=0, repr=False)  # consumed by fail_first_n
    drop_at: str = (
        "before_headers"  # one of: "before_headers", "after_headers", "mid_body"; only applies when mode="drop_connection"
    )
    # chain_family — which surface's faults this provider's scenario owns. ONE
    # word: either a blockchain ("eth", "btc", "ln", "solana") or a connection
    # type ("grpc", "rest", "tendermintrpc", "ws"). The two groups share this
    # single field, so a scenario cannot express a blockchain AND a connection
    # type together — e.g. "Solana over WebSocket" is not expressible today.
    #
    # What it does NOT do: pick the response handler. The success-path handler
    # is selected by LISTENER PORT at startup (MAG-2089); this field only gates
    # the CONTENT fault primitives (error / rate_limit / hang / drop_connection
    # / corruption / latency) — each listener fires them only when the snap's
    # chain_family matches its own. Exception: mode="down" fires on every
    # surface regardless (MAG-2092), because reachability is provider-wide.
    # Values are validated against _SCENARIO_CHAIN_FAMILIES; unknown → HTTP 400.
    #
    # Per-value breadcrumbs: "btc" → handlers_btc, ports 18575-77 (MAG-1716);
    # "ln" → handlers_lnd, 18578-80 (MAG-1726); "solana" → handlers_solana,
    # 18582-84 (MAG-2231; slot vs lastValidBlockHeight separated by
    # solana_slot_block_gap); "grpc" → handlers_grpc, 18548-50; "rest" →
    # handlers_rest, 18551-53 (MAG-1777); "tendermintrpc" → 18554-56
    # (MAG-1841); "ws" → handlers_ws, 18557-59 (MAG-1801) — WS delegates
    # non-subscription calls back to handlers_eth / handlers_btc. Default
    # "eth" preserves backward-compat.
    chain_family: str = "eth"
    # MAG-1791: provider-stale-on-getLogs primitive — head-fresh but logs-indexing-lagged.
    # Models the real production failure mode where providers update eth_blockNumber
    # immediately but index logs in a separate pipeline that can fall behind seconds-to-minutes.
    # None = unaffected (today's behaviour). Set an int = "this provider has only indexed logs
    # up through block <N>"; eth_getLogs queries that touch a higher range return either an
    # empty array (logs_lag_mode="empty") or only logs with blockNumber <= N (mode="partial").
    # eth_blockNumber is unaffected: it keeps reporting current head — that's the whole point
    # of this primitive (head-fresh + logs-lagged is the divergence we want to expose).
    # Only read by handlers_eth.handle's eth_getLogs branch (the ETH JSON-RPC listeners;
    # a WS eth_getLogs reaches the same branch because handlers_ws delegates non-subscription
    # methods to handlers_eth); every other handler ignores this field.
    logs_indexed_up_to: Optional[int] = None
    # logs_lag_mode: one of "empty" / "partial". Only consulted when logs_indexed_up_to is set.
    # Read in the same handlers_eth.handle eth_getLogs branch as logs_indexed_up_to;
    # every other handler ignores this field.
    logs_lag_mode: str = "empty"
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # call history — each entry: {ts, method, status, latency_ms}
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAX), repr=False)
    # all-time counters — never capped, survives history ring-buffer rollover
    total_calls: int = 0
    calls_by_status: Dict[str, int] = field(default_factory=dict, repr=False)
    # MAG-1832 P2 follow-up. Bumped by clear_history() so the in-place update
    # path in push_call_to_buffer can detect when /history/clear or /reset/all
    # ran between record_arrival and the final update. Without this signal the
    # detached stub would be mutated without being re-appended to self.history,
    # breaking the total_calls == len(history) invariant for the cleared-then-
    # completed request. See push_call_to_buffer entry-branch for the recovery
    # path. /reset/all calls clear_history() per state, so bumping here covers
    # both reset routes (no separate /reset/all bump needed).
    _reset_generation: int = field(default=0, repr=False)
    # MAG-2022: timestamp of the last /scenario write touching this provider.
    # Used by the background TTL sweep to revert stale state (e.g., a leftover
    # mode=hang from a prior test session) before a router pod restart can hit it.
    last_scenario_write_at: float = field(default_factory=time.time, repr=False)

    def snapshot(self) -> dict:
        """Return a thread-safe copy of the mutable config fields.
        Used by JSONRPCHandler at the start of every request so the handler works
        on a stable snapshot even if a test updates the state mid-request."""
        with self.lock:
            return {
                "mode": self.mode,
                "latency_ms": self.latency_ms,
                "error_probability": self.error_probability,
                "error_code": self.error_code,
                "error_message": self.error_message,
                "http_status": self.http_status,
                "corruption_mode": self.corruption_mode,
                "missing_field": self.missing_field,
                "blocks_behind": self.blocks_behind,
                "solana_slot_block_gap": self.solana_slot_block_gap,
                "solana_slot_offset": self.solana_slot_offset,
                "solana_unknown_method_mode": self.solana_unknown_method_mode,
                "fail_first_n": self.fail_first_n,
                "then_mode": self.then_mode,
                "drop_at": self.drop_at,
                "chain_family": self.chain_family,
                "logs_indexed_up_to": self.logs_indexed_up_to,
                "logs_lag_mode": self.logs_lag_mode,
            }

    def update(self, cfg: dict) -> None:
        """Apply a partial config dict received from POST /scenario.
        Only keys present in cfg are updated; omitted keys keep their current value.
        Acquires the lock so updates are atomic and safe to call from any thread."""
        with self.lock:
            self.mode = cfg.get("mode", self.mode)
            self.latency_ms = cfg.get("latency_ms", self.latency_ms)
            self.error_probability = cfg.get("error_probability", self.error_probability)
            self.error_code = cfg.get("error_code", self.error_code)
            self.error_message = cfg.get("error_message", self.error_message)
            self.http_status = cfg.get("http_status", self.http_status)
            self.corruption_mode = cfg.get("corruption_mode", self.corruption_mode)
            self.missing_field = cfg.get("missing_field", self.missing_field)
            self.blocks_behind = cfg.get("blocks_behind", self.blocks_behind)
            # MAG-2231: backward-compat — a /scenario payload that omits
            # solana_slot_block_gap leaves the existing per-provider value
            # untouched (the field default at construction is
            # stubs_solana.SOLANA_DEFAULT_SLOT_BLOCK_GAP).
            self.solana_slot_block_gap = cfg.get(
                "solana_slot_block_gap", self.solana_slot_block_gap
            )
            # MAG-2233 #1: backward-compat — a /scenario payload that omits
            # solana_slot_offset leaves the existing per-provider value untouched
            # (the field default at construction is 0 = base slot, no divergence).
            self.solana_slot_offset = cfg.get("solana_slot_offset", self.solana_slot_offset)
            self.solana_unknown_method_mode = cfg.get(
                "solana_unknown_method_mode", self.solana_unknown_method_mode
            )
            self.then_mode = cfg.get("then_mode", self.then_mode)
            if "fail_first_n" in cfg:
                # A fresh fail_first_n scenario restarts the count from zero.
                self.fail_first_n = cfg["fail_first_n"]
                self._fail_counter = 0
            self.drop_at = cfg.get("drop_at", self.drop_at)
            self.chain_family = cfg.get("chain_family", self.chain_family)
            # MAG-1791: backward-compat — missing keys keep current value, so
            # /scenario payloads that don't carry logs_indexed_up_to / logs_lag_mode
            # leave existing provider state untouched (defaults to None / "empty").
            self.logs_indexed_up_to = cfg.get("logs_indexed_up_to", self.logs_indexed_up_to)
            self.logs_lag_mode = cfg.get("logs_lag_mode", self.logs_lag_mode)
            if "responses" in cfg:
                self.responses = _normalise_responses(cfg["responses"])
            # MAG-2022: bump the write timestamp so the TTL sweep treats this
            # provider as fresh and won't revert it for at least SIM_SCENARIO_TTL_SECONDS.
            self.last_scenario_write_at = time.time()

    def reset_scenario(self) -> None:
        """Reset only the scenario config fields back to startup defaults (mode, latency, responses).
        Does NOT touch the call history or counters.
        Called by POST /reset — use between test scenarios to put providers back to healthy."""
        with self.lock:
            self.mode = "success"
            self.latency_ms = 0
            self.error_probability = 0.0
            self.error_code = -32000
            self.error_message = "Internal error"
            self.http_status = 200
            self.responses = {}
            self.corruption_mode = None
            self.missing_field = None
            self.blocks_behind = 0
            # MAG-2231: reset restores the default Solana slot/blockHeight gap
            # so a /reset between tests clears any per-test override. Same source
            # as the field default — the shared stubs_solana constant.
            self.solana_slot_block_gap = stubs_solana.SOLANA_DEFAULT_SLOT_BLOCK_GAP
            # MAG-2233 #1: reset restores offset 0 so a /reset between tests clears
            # any per-test slot divergence and returns every provider to the base slot.
            self.solana_slot_offset = 0
            self.solana_unknown_method_mode = "null"
            self.fail_first_n = 0
            self.then_mode = "success"
            self._fail_counter = 0
            self.drop_at = "before_headers"
            self.chain_family = "eth"
            # MAG-1791: reset clears the eth_getLogs stale-indexing primitive
            # so a /reset between tests restores full logs availability.
            self.logs_indexed_up_to = None
            self.logs_lag_mode = "empty"

    def consume_fail_counter(self) -> int:
        """Atomically increment and return this provider's request counter for
        the fail_first_n sequenced fault. Called once per OWNING-listener
        JSON-RPC request to decide whether this call is still within the
        first-N failing window. Non-owning transports never consume — they
        observe the window via peek_fail_counter instead."""
        with self.lock:
            self._fail_counter += 1
            return self._fail_counter

    def peek_fail_counter(self) -> int:
        """Return this provider's sequenced-fault request counter WITHOUT
        incrementing it. Non-owning transports use this to observe how far the
        owning listener has advanced the fail_first_n window while leaving the
        count untouched (only owning-listener requests may burn the budget)."""
        with self.lock:
            return self._fail_counter

    def clear_history(self) -> None:
        """Wipe the in-memory call buffer and reset all-time counters to zero.
        Does NOT touch the scenario config (mode, latency, responses).
        Called by POST /history/clear — use before a specific request to isolate its history.

        MAG-1832 P2 follow-up: bumps _reset_generation so any in-flight stub
        from record_arrival can be detected as detached when its eventual
        push_call_to_buffer(entry=...) update arrives. /reset/all iterates
        provider states and calls clear_history per state, so this single
        bump covers both /history/clear and /reset/all reset paths.
        """
        with self.lock:
            self.history.clear()
            self.total_calls = 0
            self.calls_by_status = {}
            self._reset_generation += 1

    def record_arrival(
        self, lava_headers: dict = None, chain: Optional[str] = None, port: Optional[int] = None
    ) -> dict:
        """Push an in-flight stub entry the moment a request arrives and return the
        dict so the caller can update it once method / status / latency are known.

        MAG-1832 (cancel-during-response race). PR #22 closed the
        sleep-then-write window by moving ``push_call_to_buffer`` ahead of the
        latency sleep. But the handler still does work BEFORE that push call
        — most notably ``self.rfile.read(Content-Length)``, which blocks on
        socket I/O. When the router cancels the request (TCP RST after a hedge
        peer returned first), that read raises ``ConnectionResetError`` and the
        handler dies before the history entry is written, so /history misses
        the cancelled peer entirely. The invariant
        ``Lava-Retries + 1 == history_count`` then fails.

        Recording arrival as the very first handler action — before body parse,
        scenario merge, fault evaluation, or any sleep — guarantees the entry
        exists no matter when the cancellation lands. Subsequent stages mutate
        this dict in place (via ``push_call_to_buffer(..., entry=stub)``) to
        fill in method / req_id / final status / configured latency.

        The stub is recorded with method ``"*"``, status ``"in_flight"``,
        latency_ms 0, request_id None. If the cancellation lands before the
        update, the entry stays as ``in_flight`` — that's strictly better than
        no entry at all because:
          - The router-vs-sim consistency invariant only counts entries, not
            their final status.
          - The /history filter ``?status=in_flight`` can surface cancellations
            for diagnosis without changing the contract for any other test.
        """
        now = time.time()
        entry = {
            "ts": now,
            "time": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S."
            )
            + f"{int(now % 1 * 1000):03d} UTC",
            "request_id": None,
            "method": "*",
            "status": "in_flight",
            "latency_ms": 0,
            "lava_headers": lava_headers or {},
            # Listener identity (MAG-2236). ProviderState is shared across every
            # chain/transport that maps to one provider pid, so it can't know
            # which listener served the request. The handler passes its own
            # chain_family and bound port so /history can be filtered by
            # listener instead of by the shared pid. None when the caller
            # doesn't supply them (backward-compatible).
            "chain": chain,
            "port": port,
        }
        with self.lock:
            self.history.append(entry)
            self.total_calls += 1
            self.calls_by_status["in_flight"] = self.calls_by_status.get("in_flight", 0) + 1
            # MAG-1832 P2: stamp the current reset generation so push_call_to_buffer
            # can detect a clear_history()/reset_all that ran between now and the
            # in-place update. See _reset_generation field comment.
            entry["_reset_gen"] = self._reset_generation
        return entry

    def push_call_to_buffer(
        self,
        method: str,
        status: str,
        latency_ms: int,
        request_id: object = None,
        lava_headers: dict = None,
        entry: Optional[dict] = None,
        chain: Optional[str] = None,
        port: Optional[int] = None,
    ) -> None:
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
            entry:        MAG-1832. When supplied, update this existing entry (returned by
                          ``record_arrival``) in place instead of pushing a new dict. Status,
                          method, latency_ms always overwrite the previous value. The
                          ``calls_by_status`` counter is decremented on the old status and
                          incremented on the new one so the bookkeeping stays consistent with
                          the deque. request_id / lava_headers overwrite only when the caller
                          passes a non-None value, so stages that don't know one of them yet
                          (e.g. a body-parse failure that knows req_id stays None) don't wipe
                          a value an earlier stage already filled in.
            chain:        MAG-2236. The serving listener's chain_family (e.g. "eth" /
                          "solana" / "btc" / "rest"). Stamped onto the history entry so
                          /history can be filtered per listener instead of per shared pid.
                          On the ``entry`` update paths it overwrites only when non-None,
                          matching request_id / lava_headers — the value ``record_arrival``
                          already stamped is preserved when the caller passes None.
            port:         MAG-2236. The serving listener's bound TCP port. Same stamping
                          rules as ``chain``.
        """
        now = time.time()
        if entry is not None:
            with self.lock:
                # MAG-1832 P2 follow-up. If clear_history()/reset_all ran between
                # record_arrival and now, the stub was evicted from self.history
                # and the counters were reset. The detached dict's old status is
                # stale — re-append the stub and re-bump counters so total_calls
                # and len(history) stay consistent for the now-completed request.
                if entry.get("_reset_gen") != self._reset_generation:
                    entry["method"] = method
                    entry["latency_ms"] = latency_ms
                    entry["status"] = status
                    if request_id is not None:
                        entry["request_id"] = request_id
                    if lava_headers is not None:
                        entry["lava_headers"] = lava_headers
                    if chain is not None:
                        entry["chain"] = chain
                    if port is not None:
                        entry["port"] = port
                    entry["_reset_gen"] = self._reset_generation
                    self.history.append(entry)
                    self.total_calls += 1
                    self.calls_by_status[status] = self.calls_by_status.get(status, 0) + 1
                    return
                old_status = entry["status"]
                entry["method"] = method
                entry["latency_ms"] = latency_ms
                if request_id is not None:
                    entry["request_id"] = request_id
                if lava_headers is not None:
                    entry["lava_headers"] = lava_headers
                if chain is not None:
                    entry["chain"] = chain
                if port is not None:
                    entry["port"] = port
                if old_status != status:
                    self.calls_by_status[old_status] = max(
                        0, self.calls_by_status.get(old_status, 0) - 1
                    )
                    self.calls_by_status[status] = self.calls_by_status.get(status, 0) + 1
                    entry["status"] = status
            return
        with self.lock:
            self.history.append(
                {
                    "ts": now,
                    "time": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S."
                    )
                    + f"{int(now % 1 * 1000):03d} UTC",
                    "request_id": request_id,
                    "method": method,
                    "status": status,
                    "latency_ms": latency_ms,
                    "lava_headers": lava_headers or {},
                    "chain": chain,
                    "port": port,
                }
            )
            self.total_calls += 1
            self.calls_by_status[status] = self.calls_by_status.get(status, 0) + 1

    def stats(self) -> dict:
        """Return a thread-safe snapshot of the all-time call counters for this provider.
        Counters are never reset (unlike the ring-buffer which is cleared on reset()).
        Used by GET /stats to show cumulative traffic since the pod started."""
        with self.lock:
            return {
                "total_requests_all_time": self.total_calls,
                "total_calls": self.total_calls,  # alias for convenience
                "requests_by_status_all_time": dict(self.calls_by_status),
                "calls_by_status": dict(self.calls_by_status),  # alias for convenience
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
    entry: Optional[Dict[str, Any]] = None,
    chain: Optional[str] = None,
    port: Optional[int] = None,
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
    # Latency value to stamp on history entries: the *configured* latency_ms,
    # NOT _elapsed_ms(t_start). MAG-1832 inverted the order so the JSON-RPC
    # handler now writes history BEFORE any latency sleep, which means
    # _elapsed_ms here would be ~0 even when a 5s latency was configured —
    # misleading on /history filters. Recording the configured value reflects
    # "the latency the request would have taken had it run to completion",
    # consistent with the success-path recording at the JSON-RPC handler.
    # For pre-parse provider-wide down the caller passes the request-entry
    # snap, so configured == provider-wide latency_ms (which the sim skips
    # paying on the down fast path — the recorded value is still meaningful
    # as the configured value, not as wall time).
    recorded_latency_ms = snap.get("latency_ms", 0)

    # 1. Outage — for provider-wide down, the pre-parse caller passes
    #    method="*", req_id=None. For per-method down (post-parse path) the
    #    caller passes the actual method / req_id; either way /history?method=X
    #    resolves correctly because the method label is taken from the caller.
    if snap["mode"] == "down":
        state.push_call_to_buffer(
            method,
            "down",
            recorded_latency_ms,
            request_id=req_id,
            lava_headers=lava_headers,
            entry=entry,
            chain=chain,
            port=port,
        )
        return {"kind": "down"}

    # 2. Hang — accept request, sleep "forever". 30s is long enough for any
    #    reasonable client read timeout to fire; finite so the thread eventually
    #    exits and we don't leak threads if the client disconnects.
    if snap["mode"] == "hang":
        state.push_call_to_buffer(
            method,
            "hang",
            0,
            request_id=req_id,
            lava_headers=lava_headers,
            entry=entry,
            chain=chain,
            port=port,
        )
        return {"kind": "hang"}

    # 3. Drop connection — close socket at one of three points.
    if snap["mode"] == "drop_connection":
        drop_at = snap.get("drop_at", "before_headers")
        state.push_call_to_buffer(
            method,
            "drop_connection",
            recorded_latency_ms,
            request_id=req_id,
            lava_headers=lava_headers,
            entry=entry,
            chain=chain,
            port=port,
        )
        return {"kind": "drop", "drop_at": drop_at}

    # 4. Rate limit — HTTP 429.
    if snap["mode"] == "rate_limit":
        state.push_call_to_buffer(
            method,
            "rate_limit",
            recorded_latency_ms,
            request_id=req_id,
            lava_headers=lava_headers,
            entry=entry,
            chain=chain,
            port=port,
        )
        return {
            "kind": "rate_limit",
            "status": 429,
            "error_code": 429,
            "error_message": "Too many requests",
        }

    # 5. Probabilistic / forced error — configurable code, message, HTTP status.
    if snap["mode"] == "error" or random.random() < snap["error_probability"]:
        state.push_call_to_buffer(
            method,
            "error",
            recorded_latency_ms,
            request_id=req_id,
            lava_headers=lava_headers,
            entry=entry,
            chain=chain,
            port=port,
        )
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


def _mode_for(snap: Dict[str, Any], *chain_families: str) -> Optional[str]:
    """Return ``snap["mode"]`` only when the snap's ``chain_family`` is one
    of ``chain_families``; otherwise ``None``.

    Companion to ``_corruption_for`` / ``_missing_field_for``. The ``mode``
    field drives the fault primitives evaluated by ``_apply_fault`` (down /
    hang / drop_connection / rate_limit / error). Without an explicit gate,
    a ``mode="error"`` set for one transport (e.g. ``chain_family="btc"``)
    fires on requests dispatched to every other transport that shares the
    same provider id — the cross-transport leak surfaced in the
    2026-05-18 suite triage as ~37 spurious failures.

    Helpers return ``None`` for the mismatch case so callers can write the
    same shape they already use for ``_corruption_for``:

        mode = _mode_for(snap, "rest")
        if mode == "down": ...
        if mode == "error" or ...: ...

    The JSON-RPC handler uses the ``jsonrpc_owns_snap`` short-circuit
    instead (logically equivalent — it skips the whole ``_apply_fault``
    call when the snap was authored for a different transport).
    """
    if snap.get("chain_family") in chain_families:
        return snap.get("mode")
    return None


def _effective_mode(state: "ProviderState", snap: Dict[str, Any]) -> str:
    """Return the ``mode`` a NON-OWNING surface should act on, with the
    sequenced fault (``fail_first_n`` / ``then_mode``) taken into account.

    The fail_first_n window is measured in OWNING-listener requests: only the
    JSON-RPC listener whose ``handler_chain_family`` matches the snap's
    ``chain_family`` advances the counter (via consume_fail_counter). Every
    other surface — REST, Tendermint-RPC, WS, gRPC, and JSON-RPC listeners of
    other chain families — never advances the window; it only observes it
    here, by peeking the counter without incrementing.

    Once the owning listener has consumed the whole window (peeked counter has
    reached fail_first_n), ``then_mode`` (default "success") is in effect for
    observers; until then the configured ``mode`` is. Without this
    read-through, a sequenced provider-wide fault such as mode="down" (honored
    on every transport because reachability is provider-wide) would pin every
    non-owning surface at 503 forever: they read the raw ``snap["mode"]`` and
    would never see the recovery the owning listener already reached.

    The observer threshold is ``peek >= fail_first_n`` on purpose: the owning
    consumer recovers on its (N+1)-th call (``consume() > N``), so the moment
    N owning calls have happened, the NEXT request on ANY surface belongs to
    the recovered phase — observer and owner may never disagree about which
    phase the provider is in.
    """
    fail_first_n = snap.get("fail_first_n", 0)
    if fail_first_n > 0 and state.peek_fail_counter() >= fail_first_n:
        return snap.get("then_mode", "success")
    return snap["mode"]


# ── JSON-RPC handler ──────────────────────────────────────────────────────────


class JSONRPCHandler(BaseHTTPRequestHandler):

    # Socket timeout (seconds) honoured by BaseHTTPRequestHandler. It caps the
    # otherwise-unbounded rfile.read(Content-Length) so a client that opens a
    # connection and stalls mid-body can't pin this worker thread + file
    # descriptor forever under sustained load.
    timeout = 30

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

        # MAG-2089 — handler dispatch is now PORT-DERIVED on JSON-RPC.
        # Each listener server is attached at startup with two attributes:
        #
        #   handler_chain_family  — one of "eth", "btc", "ln"; identifies
        #                            which JSON-RPC chain this port serves.
        #   handler_module        — the dispatch module (handlers_eth /
        #                            handlers_btc / handlers_lnd) called on
        #                            the success path.
        #
        # Defaults (when unset) are "eth" / handlers_eth so any direct
        # ``ThreadingHTTPServer(..., JSONRPCHandler)`` construction outside
        # ``main()`` behaves the same as before this change. ETH listeners
        # at ETH_PRIMARY_PORTS / ETH_BACKUP_PORTS leave the defaults in
        # place; BTC listeners (BTC_PRIMARY_PORTS) and LN listeners
        # (LN_PRIMARY_PORTS) override both attributes at bootstrap.
        #
        # The fault-injection ladder and the corruption helpers are gated
        # to ``handler_chain_family`` so a fault primitive set for one
        # transport (e.g. ``chain_family="grpc"``) doesn't fire on a JSON-RPC
        # request, AND a BTC-scenario fault doesn't fire on an ETH listener
        # (this last property is what MAG-2089 fixes — previously the ETH
        # listener evaluated the fault for any chain_family in {eth,btc,ln},
        # so a BTC test that left mode=hang on a provider would also hang
        # an ETH listener using the same shared ProviderState).
        listener_family = getattr(self.server, "handler_chain_family", "eth")
        handler_module = getattr(self.server, "handler_module", handlers_eth)
        listener_port = self.server.server_address[1]

        # Capture all lava-* headers from the router
        lava_headers = {k: v for k, v in self.headers.items() if k.lower().startswith("lava-")}

        # MAG-1832 — close the cancel-during-response race. Record arrival as
        # the very first state-mutating action, BEFORE the body is read off the
        # socket, the scenario is consulted, or any fault is evaluated.
        # ``self.rfile.read(Content-Length)`` blocks on socket I/O and is the
        # cancellation window the router's hedge mechanism rides — when a
        # faster peer wins, the router sends a TCP RST and the read here
        # raises ``ConnectionResetError``. Without an arrival stub the
        # cancelled peer's call is invisible in /history, breaking the
        # invariant ``Lava-Retries + 1 == history_count``.
        #
        # The stub is later updated in place (status, method, latency_ms,
        # request_id) by ``_apply_fault`` or the success branches via
        # ``push_call_to_buffer(..., entry=arrival)``. If a cancellation lands
        # before any of those updates fire, the entry stays as ``in_flight``
        # which is strictly better than no entry for the invariant.
        arrival = state.record_arrival(
            lava_headers=lava_headers, chain=listener_family, port=listener_port
        )

        # Cross-transport isolation (MAG-1838 → MAG-2089).
        # ``ProviderState`` is shared across JSON-RPC, REST, gRPC, WS, and
        # Tendermint-RPC for the same provider id. The fault primitives in
        # _apply_fault (down / hang / drop_connection / rate_limit / error)
        # are chain-agnostic, so without an explicit gate a fault authored
        # for one transport's chain_family would also fire on every other
        # transport sharing the same ProviderState. The gate is the
        # listener's own ``handler_chain_family``: each JSON-RPC listener
        # only fires faults that match its OWN chain_family. ETH listener
        # owns "eth"; BTC listener owns "btc"; LN listener owns "ln". Any
        # other ``chain_family`` value on the snap (or a non-matching one)
        # short-circuits both the pre-parse ``down`` branch immediately
        # below and the post-parse fault evaluation further down.
        #
        # Exception (MAG-2092): ``mode="down"`` is honored on every
        # transport because reachability is provider-wide; per-transport
        # isolation only applies to content modes (error / corrupt /
        # hang / rate_limit / latency / drop_connection). Under this
        # exemption a BTC provider in mode=down still 503s the ETH
        # JSON-RPC port — consistent with the universal "down means
        # unreachable" semantic shipped for WS / gRPC / REST / TM.
        jsonrpc_owns_snap = snap.get("chain_family") == listener_family

        # Sequenced fault (fail_first_n): ONLY the listener that owns this snap's
        # chain_family consumes the counter + applies the sequence, so a request
        # on a different transport's listener (gated out below) does not burn the
        # provider's first-N budget. The first N owned requests use the configured
        # mode; every owned request after switches to then_mode (default success).
        if jsonrpc_owns_snap and snap.get("fail_first_n", 0) > 0:
            if state.consume_fail_counter() > snap["fail_first_n"]:
                snap["mode"] = snap.get("then_mode", "success")
        elif not jsonrpc_owns_snap:
            # Non-owning listener: observe the sequenced-fault window without
            # advancing it. Mirrors the owning rewrite above so the universal
            # mode="down" checks below see then_mode once the owning listener
            # has consumed the window, instead of 503ing this port forever.
            snap["mode"] = _effective_mode(state, snap)

        jsonrpc_run_fault = jsonrpc_owns_snap or snap["mode"] == "down"

        # Pre-parse fault check: provider-wide down mode doesn't read the body.
        # Down is the only pre-body-parse fault — there is no per-method
        # variant at this layer because the method label isn't known yet
        # (body unparsed). A per-method down lives behind the merged-config
        # path below and applies on the post-parse branch.
        if jsonrpc_run_fault and snap["mode"] == "down":
            fault = _apply_fault(
                state,
                snap,
                "*",
                None,
                lava_headers,
                t_start,
                entry=arrival,
                chain=listener_family,
                port=listener_port,
            )
            self._emit_jsonrpc_fault(
                fault,
                req_id=None,
                corruption_mode=_corruption_for(snap, listener_family),
                missing_field=_missing_field_for(snap, listener_family),
            )
            return

        # Parse the request body before latency/fault evaluation so the
        # method label is available for per-method override resolution
        # (MAG-1821), and so method/req_id are available when history is
        # written ahead of any latency sleep (MAG-1832). Parsing first is
        # safe: it's a small in-memory JSON load that doesn't depend on any
        # provider config.
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        # A JSON-RPC batch is a top-level array. We do not support batch, but we
        # must not call dict methods on a list (``body.get(...)`` below) — that
        # raises AttributeError and breaks the socket mid-response. Answer with a
        # single Invalid-Request error and record the attempt so the arrival stub
        # in /history reaches a terminal status instead of staying in_flight.
        if isinstance(body, list):
            state.push_call_to_buffer(
                "batch",
                "error",
                0,
                request_id=None,
                lava_headers=lava_headers,
                entry=arrival,
                chain=listener_family,
                port=listener_port,
            )
            self._reply(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "batch requests are not supported"},
                },
            )
            return

        req_id = body.get("id", 1)
        method = body.get("method", "unknown")

        # Merge per-method overrides into the snap (MAG-1821). When no
        # override applies, ``method_snap is snap`` and behaviour matches
        # pre-MAG-1821 exactly. Order matches the provider-wide path:
        # latency FIRST, then fault — so a per-method ``latency_ms`` is
        # paid even on a per-method rate_limit / drop response.
        method_snap = _resolve_method_config(method, snap, state.responses)

        # MAG-1846 — per-method body override. When set, return the configured
        # {status, body} directly and bypass _apply_fault + the chain-handler
        # success path. Validation at /scenario time (_normalise_responses)
        # guarantees: status is 2xx, body is a dict, and "mode" is not also
        # set on this method entry, so we can read body unconditionally here
        # without worrying about silently shadowing a fault primitive.
        if "body" in method_snap and method_snap.get("body") is not None:
            override_body = method_snap["body"]
            override_status = method_snap.get("status", 200)
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
            #
            # MAG-1832: push history BEFORE the latency sleep so a router-side
            # cancel mid-sleep still records the call. Mirrors the success and
            # fault paths below. Recorded latency_ms is the configured value
            # (what the call would have taken had it run to completion).
            # The arrival stub from record_arrival is updated in place so the
            # cancel-during-response race (cancel BEFORE this push fires) still
            # leaves an entry for the invariant.
            state.push_call_to_buffer(
                method,
                "success",
                method_snap["latency_ms"],
                request_id=req_id,
                lava_headers=lava_headers,
                entry=arrival,
                chain=listener_family,
                port=listener_port,
            )
            if method_snap["latency_ms"] > 0:
                time.sleep(method_snap["latency_ms"] / 1000.0)
            self._reply(
                override_status,
                override_body,
                corruption_mode=_corruption_for(snap, listener_family),
                missing_field=_missing_field_for(snap, listener_family),
            )
            return

        # Post-parse fault evaluation. _apply_fault records history internally.
        # Fault paths record FIRST then optionally sleep — mirrors the
        # hang-mode pattern in _apply_fault (write first, sleep second), so
        # a router-side cancel mid-latency-sleep still records the call in
        # /history (MAG-1832). _apply_fault stamps the configured latency on
        # the history entry (not elapsed) so per-method-override callers see
        # the latency the call *would have taken*.
        # MAG-2089 — only evaluate the fault ladder when the snap's
        # chain_family matches THIS listener's chain_family. Faults set for
        # any other transport pass through to the success-path below.
        # MAG-2092 — but always honor mode="down" regardless of
        # chain_family because reachability is provider-wide.
        if jsonrpc_owns_snap or method_snap["mode"] == "down":
            fault = _apply_fault(
                state,
                method_snap,
                method,
                req_id,
                lava_headers,
                t_start,
                entry=arrival,
                chain=listener_family,
                port=listener_port,
            )
        else:
            fault = None
        if fault is not None:
            if method_snap["latency_ms"] > 0:
                time.sleep(method_snap["latency_ms"] / 1000.0)
            self._emit_jsonrpc_fault(
                fault,
                req_id=req_id,
                corruption_mode=_corruption_for(snap, listener_family),
                missing_field=_missing_field_for(snap, listener_family),
            )
            return

        # Success — delegate the chain-specific success path to a handler
        # module. Fault branches above (down / hang / drop / rate-limit /
        # forced or probabilistic error) are chain-agnostic and stay in
        # _apply_fault. The success path is dispatched per LISTENER PORT
        # via ``handler_module`` (MAG-2089): the ETH listener always calls
        # handlers_eth, the BTC listener always calls handlers_btc, the LN
        # listener always calls handlers_lnd. The snap's ``chain_family``
        # field is NOT consulted here — it stayed in the payload solely for
        # fault-primitive gating on the non-JSON-RPC transports (REST / gRPC
        # / TM / WS), which still read it via ``_corruption_for`` /
        # ``_missing_field_for`` / ``_mode_for``.
        #
        # The handler returns the status + response envelope; this layer is
        # responsible for I/O (corruption hooks, history accounting).
        status, response_body = handler_module.handle(state, body, snap, lava_headers)
        emit_status = "error" if "error" in response_body else "success"
        # MAG-1832: write history BEFORE the latency sleep so a router-side
        # cancel mid-sleep (hedge ticker firing) still records the call.
        # Mirrors the hang-mode pattern in _apply_fault (write first, sleep
        # second). latency_ms recorded is the configured post-MAG-1821-override
        # value — that's what the call *would have taken* had it run to
        # completion. The arrival stub is updated in place so the entry exists
        # regardless of whether the cancellation lands before or after this
        # update — the cancel-during-response race is closed.
        state.push_call_to_buffer(
            method,
            emit_status,
            method_snap["latency_ms"],
            request_id=req_id,
            lava_headers=lava_headers,
            entry=arrival,
            chain=listener_family,
            port=listener_port,
        )
        if method_snap["latency_ms"] > 0:
            time.sleep(method_snap["latency_ms"] / 1000.0)
        self._reply(
            status,
            response_body,
            corruption_mode=_corruption_for(snap, listener_family),
            missing_field=_missing_field_for(snap, listener_family),
        )

    def _emit_jsonrpc_fault(
        self,
        fault: Dict[str, Any],
        req_id: Any,
        corruption_mode: Optional[str] = None,
        missing_field: Optional[str] = None,
    ) -> None:
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
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": fault["error_code"], "message": fault["error_message"]},
            },
            corruption_mode=corruption_mode,
            missing_field=missing_field,
        )

    @staticmethod
    def _elapsed_ms(t_start: float) -> int:
        """Return the integer milliseconds elapsed since t_start (from time.monotonic())."""
        return _elapsed_ms(t_start)

    def _reply(
        self,
        status: int,
        data: dict,
        corruption_mode: Optional[str] = None,
        missing_field: Optional[str] = None,
    ):
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

    sub_id: str  # 32-hex string handed back to the client
    provider_id: str  # "1" | "2" | "3"
    method: str  # e.g. "newHeads", "logs", "accountSubscribe"
    chain: str  # "eth" | "tendermint" | "solana"
    envelope: str  # one of stubs_ws SUBSCRIBE_METHODS envelope names
    out_queue: "queue.Queue[bytes]"  # frames the writer thread will sendall()
    closed: threading.Event  # set when the reader thread exits


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

    # Socket timeout (seconds) honoured by BaseHTTPRequestHandler. Caps the
    # otherwise-unbounded request-body read so a stalled client can't hold a
    # worker thread + file descriptor indefinitely under sustained load.
    timeout = 30

    # ── Verb dispatch ─────────────────────────────────────────────────────────

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

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

        # Listener identity for /history stamping (MAG-2236). REST listeners
        # don't set handler_chain_family in the bootstrap, so fall back to the
        # transport name "rest"; the bound port is unique per REST listener.
        listener_chain = getattr(self.server, "handler_chain_family", "rest")
        listener_port = self.server.server_address[1]

        # Lava-* request headers — used for /history filtering and threaded
        # through to handlers_rest so a future test can assert on header
        # propagation.
        lava_headers = {k: v for k, v in self.headers.items() if k.lower().startswith("lava-")}

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # X-Request-Id wins; fall back to sim-side sequence number so every
        # call still gets a stable correlation_group in /history.
        req_id: Any = self.headers.get("X-Request-Id") or _next_sim_request_id()

        # Cross-transport isolation — mirrors MAG-1838's jsonrpc_owns_snap
        # gate. ``ProviderState`` is shared across all transports for the
        # same provider id, so a fault authored for one transport leaks
        # onto every other transport that reads ``snap["mode"]`` without
        # a chain_family check. The REST handler owns chain_family="rest";
        # for any other value the fault ladder is skipped and the request
        # falls through to its normal success path. Surfaced as ~37 spurious
        # failures in the 2026-05-18 suite triage when a BTC test set
        # ``chain_family="btc", mode="error"`` and a subsequent REST test
        # got the BTC error instead of a healthy REST response.
        #
        # Exception (MAG-2092): ``mode="down"`` is honored on every
        # transport because reachability is provider-wide; per-transport
        # isolation only applies to content modes (error / corrupt /
        # hang / rate_limit / latency / drop_connection). Under this
        # exemption an ETH provider in mode=down still 503s the REST
        # port — consistent with the universal "down means unreachable"
        # semantic.
        rest_owns_snap = snap.get("chain_family") == "rest"
        if not rest_owns_snap:
            # Non-owning surface: observe the sequenced-fault window
            # (fail_first_n) without advancing it — only the owning JSON-RPC
            # listener consumes the counter. Rewriting the snapshot's mode
            # before the gates below (and before the per-route merge) means
            # a provider-wide down clears here once the owning listener has
            # consumed the window, instead of pinning this port at 503
            # forever, while an explicit per-route mode override still wins.
            snap["mode"] = _effective_mode(state, snap)
        rest_run_fault = rest_owns_snap or snap["mode"] == "down"

        # Pre-route fault: provider-wide down doesn't need the (verb,
        # template) key. Mirrors JSONRPCHandler's pre-body-parse down
        # branch. Per-(verb, template) down lives in the post-route
        # merged-snap branch below. Gated on rest_run_fault so a down
        # set on any chain_family 503s the REST port (MAG-2092), while
        # non-down content faults still only fire when chain_family="rest".
        if rest_run_fault and snap["mode"] == "down":
            fault = _apply_fault(
                state,
                snap,
                "*",
                None,
                lava_headers,
                t_start,
                chain=listener_chain,
                port=listener_port,
            )
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

        # Route match before fault / latency evaluation. We need the matched
        # (verb, template) before the per-method merge so MAG-1821-style
        # overrides keyed by ``(verb, template)`` can shadow provider-wide
        # latency_ms and fault keys. Order matches JSON-RPC: parse first,
        # then merge per-method, then fault, then write history, then sleep
        # (MAG-1832 — history before sleep so a router-side cancel
        # mid-latency-sleep still records the call).
        match_result = self._match_route(verb, path)
        if match_result is None:
            # Unmatched path — no per-method merge to attempt (the (verb,
            # path) key isn't a registered template). Honor only the
            # provider-wide latency / fault snap. Fault eval is gated on
            # rest_owns_snap so a fault authored for another transport
            # doesn't override a genuine 404.
            method_label = f"{verb} {path}"
            # MAG-2092: also fire the fault ladder on mode=down regardless
            # of chain_family so an unmatched URI still 503s a downed
            # provider before the 404 path runs.
            fault = (
                _apply_fault(
                    state,
                    snap,
                    method_label,
                    req_id,
                    lava_headers,
                    t_start,
                    chain=listener_chain,
                    port=listener_port,
                )
                if rest_run_fault
                else None
            )
            if fault is not None:
                if snap["latency_ms"] > 0:
                    time.sleep(snap["latency_ms"] / 1000.0)
                self._emit_rest_fault(fault)
                return
            # Genuine 404 — record so /history shows the miss. Push BEFORE
            # the sleep so a cancel mid-latency-sleep still records the
            # 404 (MAG-1832). Recorded latency_ms is the configured value.
            state.push_call_to_buffer(
                method_label,
                "not_found",
                snap["latency_ms"],
                request_id=req_id,
                lava_headers=lava_headers,
                chain=listener_chain,
                port=listener_port,
            )
            if snap["latency_ms"] > 0:
                time.sleep(snap["latency_ms"] / 1000.0)
            self._reply(404, {"code": "not_found", "method": verb, "path": path})
            return

        template, path_params = match_result
        method_label = f"{verb} {template}"

        # Merge per-(verb, template) overrides into the snap (MAG-1821
        # follow-up). When no override applies for this route,
        # ``method_snap is snap`` and behaviour matches pre-follow-up
        # exactly. Per-key fallback: a partial per-route entry inherits
        # provider-wide fault keys it doesn't override.
        method_snap = _resolve_method_config((verb, template), snap, state.responses)

        # Fault evaluation BEFORE the latency sleep — mirrors the JSON-RPC
        # post-parse fault branch (MAG-1832). _apply_fault records history
        # internally with the configured latency_ms. If a fault triggers,
        # we still pay the configured latency on the wire before emitting,
        # so wire timing is unchanged for the router. Gated on
        # rest_owns_snap so faults authored for other transports pass
        # through to the success-path below. MAG-2092: but always honor
        # mode="down" regardless of chain_family because reachability is
        # provider-wide.
        run_fault_ladder = rest_owns_snap or method_snap["mode"] == "down"
        fault = (
            _apply_fault(
                state,
                method_snap,
                method_label,
                req_id,
                lava_headers,
                t_start,
                chain=listener_chain,
                port=listener_port,
            )
            if run_fault_ladder
            else None
        )
        if fault is not None:
            if method_snap["latency_ms"] > 0:
                time.sleep(method_snap["latency_ms"] / 1000.0)
            self._emit_rest_fault(fault)
            return

        # Success path — chain-specific dispatch (REST handler).
        status, response_body = handlers_rest.handle(
            state, verb, template, path_params, query, body, snap, lava_headers
        )
        emit_status = (
            "error" if (isinstance(response_body, dict) and "error" in response_body) else "success"
        )
        # MAG-1832: write history BEFORE the latency sleep so a router-side
        # cancel mid-sleep still records the call. latency_ms recorded is
        # the configured post-MAG-1821-override value.
        state.push_call_to_buffer(
            method_label,
            emit_status,
            method_snap["latency_ms"],
            request_id=req_id,
            lava_headers=lava_headers,
            chain=listener_chain,
            port=listener_port,
        )
        if method_snap["latency_ms"] > 0:
            time.sleep(method_snap["latency_ms"] / 1000.0)
        # MAG-1837 — only apply corruption_mode if the snap was authored for
        # the REST transport. A corruption set on chain_family="eth" must not
        # leak into the REST port.
        self._reply(
            status,
            response_body,
            corruption_mode=_corruption_for(snap, "rest"),
            missing_field=_missing_field_for(snap, "rest"),
        )

    # ── Routing ───────────────────────────────────────────────────────────────

    def _match_route(self, verb: str, path: str) -> Optional[Tuple[str, Dict[str, str]]]:
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
        self._reply(
            fault["status"],
            body,
            corruption_mode=_corruption_for(snap, "rest"),
            missing_field=_missing_field_for(snap, "rest"),
        )

    def _reply(
        self,
        status: int,
        data: Any,
        corruption_mode: Optional[str] = None,
        missing_field: Optional[str] = None,
    ) -> None:
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

    # Socket timeout (seconds) honoured by BaseHTTPRequestHandler. Caps the
    # otherwise-unbounded request-body read so a stalled client can't hold a
    # worker thread + file descriptor indefinitely under sustained load.
    timeout = 30

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

        # Listener identity for /history stamping (MAG-2236). Tendermint
        # listeners don't set handler_chain_family in the bootstrap, so fall
        # back to the chain_family value this handler gates on; the bound port
        # is unique per Tendermint listener.
        listener_chain = getattr(self.server, "handler_chain_family", "tendermintrpc")
        listener_port = self.server.server_address[1]

        # Lava-* request headers — used for /history filtering, threaded
        # through to handlers_tendermintrpc for symmetry with other handlers.
        lava_headers = {k: v for k, v in self.headers.items() if k.lower().startswith("lava-")}

        # Cross-transport isolation — mirrors MAG-1838's jsonrpc_owns_snap
        # gate. ``ProviderState`` is shared across all transports for the
        # same provider id, so a fault authored for one transport leaks
        # onto every other transport that reads ``snap["mode"]`` without
        # a chain_family check. The Tendermint handler owns
        # chain_family="tendermintrpc"; for any other value the fault
        # ladder is skipped and the request falls through to its normal
        # success path. Surfaced in the 2026-05-18 suite triage as one of
        # the leak paths feeding the ~37 spurious failures.
        #
        # Exception (MAG-2092): ``mode="down"`` is honored on every
        # transport because reachability is provider-wide; per-transport
        # isolation only applies to content modes (error / corrupt /
        # hang / rate_limit / latency / drop_connection). Under this
        # exemption an ETH provider in mode=down still 503s the
        # Tendermint port — consistent with the universal "down means
        # unreachable" semantic.
        tm_owns_snap = snap.get("chain_family") == "tendermintrpc"
        if not tm_owns_snap:
            # Non-owning surface: observe the sequenced-fault window
            # (fail_first_n) without advancing it — only the owning JSON-RPC
            # listener consumes the counter. Once that window has elapsed the
            # snapshot's mode reads as then_mode, so a provider-wide down
            # clears here instead of pinning this port at 503 forever.
            snap["mode"] = _effective_mode(state, snap)
        tm_run_fault = tm_owns_snap or snap["mode"] == "down"

        # 1. Outage gate — return 503 with no body. ``method`` not yet known.
        #    Gated on tm_run_fault so a down set on any chain_family
        #    503s the Tendermint port (MAG-2092), while non-down content
        #    faults still only fire when chain_family="tendermintrpc".
        if tm_run_fault and snap["mode"] == "down":
            fault = _apply_fault(
                state,
                snap,
                "*",
                None,
                lava_headers,
                t_start,
                chain=listener_chain,
                port=listener_port,
            )
            self._emit_tm_fault(fault, request_id=None)
            return

        # 2. Parse the wire BEFORE the latency sleep — so method/request_id
        #    are available when history is written ahead of any sleep
        #    (MAG-1832, mirrors the JSON-RPC do_POST fix).
        try:
            method, params, request_id = self._extract_method_params(verb)
        except _TmParseError as exc:
            # Malformed request — JSON-RPC -32700 (parse error). Record
            # history BEFORE the wire reply so a router-side cancel still
            # leaves a trace. No latency on this path (parse fault is
            # synchronous and immediate).
            state.push_call_to_buffer(
                "*",
                "parse_error",
                0,
                request_id=None,
                lava_headers=lava_headers,
                chain=listener_chain,
                port=listener_port,
            )
            err_body = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            self._reply(400, err_body)
            return

        method_label = method  # For history filtering — matches JSON-RPC convention.

        # 3. Post-parse fault primitives — hang / drop / rate_limit / error.
        #    _apply_fault records history internally with the configured
        #    latency_ms. If a fault triggers, we still pay the configured
        #    latency on the wire before emitting, so wire timing is unchanged.
        #    Gated on tm_owns_snap so faults authored for other transports
        #    pass through to the success-path below. MAG-2092: but always
        #    honor mode="down" regardless of chain_family.
        run_fault_ladder = tm_owns_snap or snap["mode"] == "down"
        fault = (
            _apply_fault(
                state,
                snap,
                method_label,
                request_id,
                lava_headers,
                t_start,
                chain=listener_chain,
                port=listener_port,
            )
            if run_fault_ladder
            else None
        )
        if fault is not None:
            if snap["latency_ms"] > 0:
                time.sleep(snap["latency_ms"] / 1000.0)
            self._emit_tm_fault(fault, request_id=request_id)
            return

        # 4. Success path — chain-specific dispatch.
        normalized = handlers_tendermintrpc._normalize_tm_params(verb, params)
        http_status, result_body = handlers_tendermintrpc.handle(
            state, method, normalized, snap, lava_headers
        )

        # 5. Wrap in JSON-RPC envelope. If the handler returned an error dict
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

        # 6. MAG-1832: write history BEFORE the latency sleep so a router-side
        #    cancel mid-sleep still records the call. latency_ms recorded is
        #    the configured value (what the call would have taken).
        state.push_call_to_buffer(
            method_label,
            history_status,
            snap["latency_ms"],
            request_id=request_id,
            lava_headers=lava_headers,
            chain=listener_chain,
            port=listener_port,
        )
        if snap["latency_ms"] > 0:
            time.sleep(snap["latency_ms"] / 1000.0)
        # MAG-1837 — gate corruption_mode on chain_family="tendermintrpc"
        # so a corruption authored for another transport doesn't reach the
        # Tendermint port.
        self._reply(
            http_status,
            envelope,
            corruption_mode=_corruption_for(snap, "tendermintrpc"),
            missing_field=_missing_field_for(snap, "tendermintrpc"),
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

    # Socket timeout for a single request read. Without it, a client that
    # connects and stalls mid-request holds the handler open indefinitely.
    # Paired with the threaded control server in main(), a stalled client no
    # longer blocks other /scenario / /reset / /history calls.
    timeout = 30

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
          POST /advance        — (MAG-1897) advance the simulated eth head so a stale
                                 provider's sync lag stays visible to the router optimizer.
                                 Body: {"per_second": R} (continuous) | {"blocks": N} (one-time).
                                 Opt-in; default head static. Reset by /reset and /reset/all.

        Returns 404 for any unrecognised path.
        """
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        # The control routes below read body.get(...); a non-object JSON body
        # (list / scalar) would raise AttributeError and break the socket. Guard
        # it with a clear 400 — the same way JSONRPCHandler guards a batch body.
        if not isinstance(body, dict):
            self._reply(
                400, {"error": (f"request body must be a JSON object, got {type(body).__name__}")}
            )
            return

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
            if not isinstance(providers_payload, dict):
                self._reply(
                    400,
                    {
                        "error": (
                            "'providers' must be an object mapping provider id -> config, "
                            f"got {type(providers_payload).__name__}"
                        )
                    },
                )
                return
            staged: list = []
            try:
                for pid, cfg in providers_payload.items():
                    state = self.server.provider_states.get(str(pid))
                    if state is None:
                        raise ValueError(
                            f"unknown provider id {pid!r}; this simulator has "
                            f"providers {sorted(self.server.provider_states)}"
                        )
                    _validate_scenario_cfg(pid, cfg)
                    staged_cfg = dict(cfg)
                    if "responses" in staged_cfg:
                        # Pre-normalise so ValueError fires here, before any
                        # state.update mutates scalar fields. The result is
                        # already a dict so state.update's re-call of
                        # _normalise_responses is an idempotent no-op.
                        staged_cfg["responses"] = _normalise_responses(staged_cfg["responses"])
                    effective_family = staged_cfg.get("chain_family", state.chain_family)
                    staged.append((str(pid), state, staged_cfg, effective_family))
            except ValueError as exc:
                self._reply(400, {"error": str(exc)})
                return
            for _pid, state, staged_cfg, _family in staged:
                state.update(staged_cfg)
            applied = {pid: {"chain_family": family} for pid, _state, _staged_cfg, family in staged}
            self._reply(200, {"status": "ok", "applied": applied})

        elif self.path == "/reset":
            import handlers_eth

            handlers_eth.reset_eth_head()  # MAG-1897: reset advancing eth head
            for state in self.server.provider_states.values():
                state.reset_scenario()
            self._reply(200, {"status": "scenario reset"})

        elif self.path == "/history/clear":
            for state in self.server.provider_states.values():
                state.clear_history()
            self._reply(200, {"status": "history cleared"})

        elif self.path == "/reset/all":
            import handlers_eth

            handlers_eth.reset_eth_head()  # MAG-1897: reset advancing eth head
            for state in self.server.provider_states.values():
                state.reset_scenario()
                state.clear_history()
            self._reply(200, {"status": "scenario reset and history cleared"})

        elif self.path == "/advance":
            # MAG-1897: advance the simulated eth head so a stale provider's sync
            # lag stays visible to the router's optimizer (its forward-only sync
            # ratchet only releases as the head moves). Opt-in; default head static.
            #   {"per_second": R}  -> enable (R>0) / freeze (R<=0) continuous advance
            #   {"blocks": N}      -> one-time bump of the head by N blocks
            import handlers_eth

            if "per_second" in body:
                handlers_eth.set_eth_advance(float(body.get("per_second") or 0))
            if "blocks" in body:
                handlers_eth.bump_eth_head(int(body.get("blocks") or 0))
            self._reply(200, {"status": "ok", "eth_head": handlers_eth.current_eth_head()})

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
            # MAG-2236: this is a control-plane injection, not a request served
            # by a chain listener, so there's no bound listener port to record
            # (port=None). chain="ws" because the event is delivered over the
            # WS transport, matching how the WS listener stamps its own
            # /history entries.
            state = self.server.provider_states.get(handle.provider_id)
            if state is not None:
                state.push_call_to_buffer(
                    f"{handle.envelope} push",
                    "success",
                    0,
                    request_id=sub_id,
                    lava_headers={},
                    chain="ws",
                    port=None,
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

        elif self.path == "/ready":
            # Real readiness check: confirms every JSON-RPC / gRPC / REST /
            # Tendermint-RPC / WebSocket listener is actually accepting TCP
            # connections, not just that the python process started. Wired
            # to the chart's readinessProbe so kube-proxy stops routing
            # Service traffic to a pod that's still binding ports.
            #
            # Without this gate, the smart-router's earliest relay attempts
            # race the simulator's listener-bind step. They fail with
            # connection-refused, get translated to "HTTP 503
            # NODE_SERVICE_UNAVAILABLE" by the router's error classifier,
            # and the providers go straight onto the blocked list. The
            # suite then runs against a router whose pairing pool is
            # poisoned from the start.
            import socket

            from constants import (
                BTC_PRIMARY_PORTS,
                ETH_BACKUP_PORTS,
                ETH_PRIMARY_PORTS,
                ETH_SOLO_PORTS,
                GRPC_BACKUP_PORTS,
                GRPC_PRIMARY_PORTS,
                LN_PRIMARY_PORTS,
                REST_BACKUP_PORTS,
                REST_PRIMARY_PORTS,
                SOLANA_PRIMARY_PORTS,
                SOLANA_SOLO_PORTS,
                TM_BACKUP_PORTS,
                TM_PRIMARY_PORTS,
                WS_BACKUP_PORTS,
                WS_PRIMARY_PORTS,
            )

            all_ports = sorted(
                {
                    *ETH_PRIMARY_PORTS.values(),
                    *ETH_BACKUP_PORTS.values(),
                    *ETH_SOLO_PORTS.values(),
                    *BTC_PRIMARY_PORTS.values(),
                    *LN_PRIMARY_PORTS.values(),
                    *SOLANA_PRIMARY_PORTS.values(),
                    *SOLANA_SOLO_PORTS.values(),
                    *GRPC_PRIMARY_PORTS.values(),
                    *GRPC_BACKUP_PORTS.values(),
                    *REST_PRIMARY_PORTS.values(),
                    *REST_BACKUP_PORTS.values(),
                    *TM_PRIMARY_PORTS.values(),
                    *TM_BACKUP_PORTS.values(),
                    *WS_PRIMARY_PORTS.values(),
                    *WS_BACKUP_PORTS.values(),
                }
            )
            missing = []
            for port in all_ports:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.1)
                try:
                    if s.connect_ex(("127.0.0.1", port)) != 0:
                        missing.append(port)
                finally:
                    s.close()
            if missing:
                self._reply(
                    503,
                    {
                        "status": "not_ready",
                        "listening": len(all_ports) - len(missing),
                        "expected": len(all_ports),
                        "missing_ports": missing,
                    },
                )
            else:
                self._reply(
                    200,
                    {
                        "status": "ready",
                        "listening": len(all_ports),
                        "expected": len(all_ports),
                    },
                )

        elif self.path == "/scenario":
            self._reply(
                200,
                {
                    "providers": {
                        pid: s.snapshot() for pid, s in self.server.provider_states.items()
                    }
                },
            )

        elif self.path == "/stats":
            # Per-provider call counts and status breakdown.
            # Use this to see if one provider is being skipped or hammered.
            self._reply(
                200,
                {"providers": {pid: s.stats() for pid, s in self.server.provider_states.items()}},
            )

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

            t_from = float(qs["from"][0]) if "from" in qs else None
            t_to = float(qs["to"][0]) if "to" in qs else None
            last_secs = float(qs["last"][0]) if "last" in qs else None
            f_provider = qs["provider"][0] if "provider" in qs else None
            f_method = qs["method"][0] if "method" in qs else None
            f_status = qs["status"][0] if "status" in qs else None
            f_request_id = qs["request_id"][0] if "request_id" in qs else None

            # ?max=N — MAG-1822 tail-slicing. Parsed here so a malformed value
            # short-circuits with 400 before we do any history work.
            f_max = None
            if "max" in qs:
                raw_max = qs["max"][0]
                try:
                    parsed_max = int(raw_max)
                except (TypeError, ValueError):
                    self._reply(
                        400,
                        {
                            "error": "invalid_max",
                            "message": f"max must be a non-negative integer, got {raw_max!r}",
                        },
                    )
                    return
                if parsed_max < 0:
                    self._reply(
                        400,
                        {
                            "error": "invalid_max",
                            "message": f"max must be >= 0, got {parsed_max}",
                        },
                    )
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
                    if t_from is not None and entry["ts"] < t_from:
                        continue
                    if t_to is not None and entry["ts"] > t_to:
                        continue
                    if f_method and entry["method"] != f_method:
                        continue
                    if f_status and entry["status"] != f_status:
                        continue
                    if f_request_id and str(entry.get("request_id")) != f_request_id:
                        continue
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


def _scenario_ttl_sweep(
    states: Dict[str, ProviderState], ttl_s: int, interval_s: float = 120.0
) -> None:
    """Background daemon (MAG-2022): every interval_s, revert any provider whose
    scenario hasn't been written-to in > ttl_s seconds back to defaults.
    Prevents stale state (e.g., mode=hang from a prior test) from surviving
    a router pod restart and breaking the router's startup validation.
    Only reverts non-default state — providers in mode='success' are skipped.

    Wake interval default 120 s (was 60 s — halved wake-up frequency since the
    default TTL is much longer than the wake interval, so checking every minute
    was overkill)."""
    while True:
        time.sleep(interval_s)
        now = time.time()
        for pid, state in list(states.items()):
            with state.lock:
                age = now - state.last_scenario_write_at
                non_default = state.mode != "success"
            if age > ttl_s and non_default:
                state.reset_scenario()
                print(f"[ttl-sweep] reverted provider {pid} (idle {age:.0f}s > {ttl_s}s TTL)")


class _SimThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer for every provider listener.

    request_queue_size is the OS listen() backlog — how many freshly-arrived
    connections the kernel holds before a worker thread accepts them. The
    stdlib default is 5, which is far too shallow for the burst of concurrent
    relays the router can fan out under load: once the queue fills, new
    connections are refused or stall, which presents as the simulator
    "hanging". 128 gives ample headroom for those bursts.

    daemon_threads=True keeps the per-request worker threads from blocking
    interpreter shutdown — same behaviour every listener set explicitly before.
    """

    request_queue_size = 128
    daemon_threads = True


def main():
    """Start all simulator servers and block until interrupted.

    Spins up the full surface matrix in daemon threads:

      JSON-RPC ETH (six listeners — three primary + three backup)
        - Primary  : ports 18545 / 18546 / 18547 (pids 1-3, handler=eth)
        - Backup   : ports 18560 / 18561 / 18562 (pids 4-6, handler=eth)
        See ETH_PRIMARY_PORTS / ETH_BACKUP_PORTS / ETH_ALL_PORTS in
        constants.py — the simulator process is identical across both pools,
        the only difference is the smart-router-side `is_backup: true` flag
        in values_sim.yml.

      JSON-RPC BTC (three listeners — primary only) — MAG-2089
        - Primary  : ports 18575 / 18576 / 18577 (pids 1-3, handler=btc)
        - ProviderState shared with the matching ETH primary pid.
          Handler dispatch is port-derived; the BTC listener at port 18575
          always calls handlers_btc regardless of the snap's chain_family.

      JSON-RPC LN  (three listeners — primary only) — MAG-2089
        - Primary  : ports 18578 / 18579 / 18580 (pids 1-3, handler=ln)
        - ProviderState shared with the matching ETH primary pid.
          Handler dispatch is port-derived; the LN listener at port 18578
          always calls handlers_lnd regardless of the snap's chain_family.

      JSON-RPC Solana (three listeners — primary only) — MAG-2231
        - Primary  : ports 18582 / 18583 / 18584 (pids 1-3, handler=solana)
        - ProviderState shared with the matching ETH primary pid.
          Handler dispatch is port-derived; the Solana listener at port 18582
          always calls handlers_solana regardless of the snap's chain_family.
          The success path emits result.context.slot vs
          result.value.lastValidBlockHeight separated by solana_slot_block_gap
          (default 21_900_000) to reproduce the MAG-1591 consistency-filter bug.

      JSON-RPC Solana solo (one listener — no backup) — MAG-2239
        - Solo     : port 18585 (pid 20, handler=solana)
        - Distinct ProviderState (NOT shared with the Solana primary pool).
          The Solana analogue of the ETH solo listener (port 18581, pid 19) —
          the single-Solana-endpoint customer-outage shape. Isolated so a
          /scenario POST on the solo router can't disturb the primary pool.

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
    states = {pid: ProviderState() for pid in ETH_ALL_PORTS}
    for pid in GRPC_BACKUP_PORTS:
        states[pid] = ProviderState()
    for pid in REST_BACKUP_PORTS:
        states[pid] = ProviderState()
    for pid in TM_BACKUP_PORTS:
        states[pid] = ProviderState()
    for pid in WS_BACKUP_PORTS:
        states[pid] = ProviderState()
    # Solana solo pid (20) is NOT in ETH_ALL_PORTS — it gets its own
    # ProviderState, the same way each backup pool above does. A dedicated
    # state keeps the solo Solana listener's /scenario config independent of
    # the Solana primary pool (pids 1-3).
    for pid in SOLANA_SOLO_PORTS:
        states[pid] = ProviderState()

    servers = []
    for pid, port in ETH_ALL_PORTS.items():
        # ThreadingHTTPServer so a slow/hanging request on one provider doesn't
        # block its own subsequent requests or the other providers' threads.
        # ETH listener pool: ``handler_chain_family`` defaults to "eth" and
        # ``handler_module`` defaults to ``handlers_eth`` inside JSONRPCHandler,
        # so the ETH listeners don't need to set them explicitly — leaving the
        # defaults documents intent on read.
        srv = _SimThreadingHTTPServer(("0.0.0.0", port), JSONRPCHandler)
        srv.state = states[pid]
        srv.provider_id = pid  # available as self.server.provider_id in handler
        srv.handler_chain_family = "eth"
        srv.handler_module = handlers_eth
        servers.append(srv)

    # BTC JSON-RPC primary listeners (MAG-2089). Each runs JSONRPCHandler with
    # ``handler_chain_family="btc"`` so the fault-injection ladder fires only
    # on snaps authored for BTC, AND ``handler_module=handlers_btc`` so the
    # success path always dispatches to BTC regardless of any other
    # ``chain_family`` written into the snap. The ProviderState is shared with
    # the matching ETH primary pid (1-3), so a single ``/scenario`` POST that
    # sets ``chain_family="btc"`` on pid "1" reconfigures both the ETH port
    # (which will ignore the BTC fault) and the BTC port (which acts on it).
    for pid, port in BTC_PRIMARY_PORTS.items():
        btc_srv = _SimThreadingHTTPServer(("0.0.0.0", port), JSONRPCHandler)
        btc_srv.state = states[pid]
        btc_srv.provider_id = pid
        btc_srv.handler_chain_family = "btc"
        btc_srv.handler_module = handlers_btc
        servers.append(btc_srv)

    # LN JSON-RPC primary listeners (MAG-2089). Same shape as the BTC pool:
    # dedicated listener pool, port-derived handler dispatch, fault gating
    # on ``chain_family="ln"``. Shares ProviderState with the matching ETH
    # primary pid (1-3) so a mixed-chain scenario can independently faulted
    # the ETH and LN listeners that share a pid.
    for pid, port in LN_PRIMARY_PORTS.items():
        ln_srv = _SimThreadingHTTPServer(("0.0.0.0", port), JSONRPCHandler)
        ln_srv.state = states[pid]
        ln_srv.provider_id = pid
        ln_srv.handler_chain_family = "ln"
        ln_srv.handler_module = handlers_lnd
        servers.append(ln_srv)

    # Solana JSON-RPC primary listeners (MAG-2231). Same shape as the BTC / LN
    # pools: dedicated listener pool, port-derived handler dispatch, fault
    # gating on ``chain_family="solana"``. Shares ProviderState with the
    # matching ETH primary pid (1-3) so a single /scenario POST that sets
    # ``solana_slot_block_gap`` on pid "1" is visible from the Solana port.
    # These ports are deliberately NOT in ETH_ALL_PORTS — that union is
    # bound by the ETH-default loop above (handlers_eth), so adding Solana
    # there would (a) double-bind 18582-18584 and (b) route them to the ETH
    # handler. The dedicated loop here is what gives them handlers_solana,
    # exactly mirroring how BTC_PRIMARY_PORTS / LN_PRIMARY_PORTS are wired.
    for pid, port in SOLANA_PRIMARY_PORTS.items():
        sol_srv = _SimThreadingHTTPServer(("0.0.0.0", port), JSONRPCHandler)
        sol_srv.state = states[pid]
        sol_srv.provider_id = pid
        sol_srv.handler_chain_family = "solana"
        sol_srv.handler_module = handlers_solana
        servers.append(sol_srv)

    # Solana JSON-RPC solo listener (MAG-2239). Same shape as the Solana
    # primary pool — JSONRPCHandler with ``handler_chain_family="solana"`` and
    # ``handler_module=handlers_solana`` — but a single provider (pid 20) on a
    # dedicated port (18585) with its OWN ProviderState (set above), NOT shared
    # with the primary pool. This isolation is the point: a /scenario POST on
    # the solo Solana router reconfigures only pid 20, so it can't disturb the
    # solana-sim-router's primary pool (pids 1-3). Like the primary pool, this
    # port is deliberately NOT in ETH_ALL_PORTS (which the ETH-default loop
    # owns) — the dedicated loop here is what binds it to handlers_solana.
    for pid, port in SOLANA_SOLO_PORTS.items():
        # _SimThreadingHTTPServer (not bare ThreadingHTTPServer) so this solo
        # listener gets the same 128-deep listen backlog as every other pool —
        # the bare server defaults to a backlog of 5, which drops connections
        # under burst.
        sol_solo_srv = _SimThreadingHTTPServer(("0.0.0.0", port), JSONRPCHandler)
        sol_solo_srv.state = states[pid]
        sol_solo_srv.provider_id = pid
        sol_solo_srv.handler_chain_family = "solana"
        sol_solo_srv.handler_module = handlers_solana
        servers.append(sol_solo_srv)

    # REST servers (MAG-1777). Primary tier shares ProviderState with the
    # matching JSON-RPC primary (pids 1-3), so a /scenario update on
    # provider 1 changes how both the JSON-RPC port (18545) and the REST
    # port (18551) reply. Each server gets its own RestHandler instance
    # because BaseHTTPRequestHandler is per-request.
    for pid, port in REST_PRIMARY_PORTS.items():
        rest_srv = _SimThreadingHTTPServer(("0.0.0.0", port), RestHandler)
        rest_srv.state = states[pid]
        rest_srv.provider_id = pid
        servers.append(rest_srv)

    # REST backup tier (pids 10-12 → 18566-18568). Independent ProviderState
    # per backup pid — the smart-router only routes to this pool after the
    # primary REST pool is exhausted on a request (is_backup: true in
    # values_sim.yml).
    for pid, port in REST_BACKUP_PORTS.items():
        rest_srv = _SimThreadingHTTPServer(("0.0.0.0", port), RestHandler)
        rest_srv.state = states[pid]
        rest_srv.provider_id = pid
        servers.append(rest_srv)

    # Tendermint-RPC servers (MAG-1841). Same pattern as REST — primary tier
    # shares ProviderState with the JSON-RPC primary; backup tier is its
    # own pool with distinct pids 13-15.
    for pid, port in TM_PRIMARY_PORTS.items():
        tm_srv = _SimThreadingHTTPServer(("0.0.0.0", port), TendermintHandler)
        tm_srv.state = states[pid]
        tm_srv.provider_id = pid
        servers.append(tm_srv)

    for pid, port in TM_BACKUP_PORTS.items():
        tm_srv = _SimThreadingHTTPServer(("0.0.0.0", port), TendermintHandler)
        tm_srv.state = states[pid]
        tm_srv.provider_id = pid
        servers.append(tm_srv)

    # WS servers (MAG-1801). Primary tier shares ProviderState with the
    # JSON-RPC primary (pids 1-3); backup tier (pids 16-18) gets its own
    # pool. Each server gets its own WsHandler instance because
    # BaseHTTPRequestHandler is per-request.
    for pid, port in WS_PRIMARY_PORTS.items():
        ws_srv = _SimThreadingHTTPServer(("0.0.0.0", port), handlers_ws.WsHandler)
        ws_srv.state = states[pid]
        ws_srv.provider_id = pid
        servers.append(ws_srv)

    for pid, port in WS_BACKUP_PORTS.items():
        ws_srv = _SimThreadingHTTPServer(("0.0.0.0", port), handlers_ws.WsHandler)
        ws_srv.state = states[pid]
        ws_srv.provider_id = pid
        servers.append(ws_srv)

    # Threaded (not single-threaded HTTPServer) so one stalled or slow control
    # client can't block every other /scenario / /reset / /history / /ws/emit
    # call. Paired with ControlHandler.timeout, a hung client's socket read is
    # also bounded so its worker thread frees.
    ctrl = _SimThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    ctrl.provider_states = states
    servers.append(ctrl)

    # MAG-2022: background TTL sweep — revert any per-provider scenario state
    # that has been idle for > SIM_SCENARIO_TTL_SECONDS back to defaults.
    # Prevents stale state (e.g., mode=hang from a prior test) from surviving
    # a router pod restart and breaking the router's startup validation.
    # Default TTL 900 s (15 min) — long enough that no realistic single test
    # has a scenario sit untouched that long, short enough that orphaned
    # state is cleared within one test session window in most cases.
    # Set SIM_SCENARIO_TTL_SECONDS=0 to disable.
    scenario_ttl_s = int(os.environ.get("SIM_SCENARIO_TTL_SECONDS", "900"))
    if scenario_ttl_s > 0:
        sweep_thread = threading.Thread(
            target=_scenario_ttl_sweep,
            args=(states, scenario_ttl_s),
            daemon=True,
            name="scenario-ttl-sweep",
        )
        sweep_thread.start()
        print(
            f"[ttl-sweep] started — scenario TTL = {scenario_ttl_s}s "
            f"(set SIM_SCENARIO_TTL_SECONDS=0 to disable)"
        )

    print("Provider simulator started")
    for pid, port in ETH_PRIMARY_PORTS.items():
        print(f"  provider {pid:>2} (jsonrpc-eth,    primary) → :{port}")
    for pid, port in ETH_BACKUP_PORTS.items():
        print(f"  provider {pid:>2} (jsonrpc-eth,    backup)  → :{port}")
    for pid, port in ETH_SOLO_PORTS.items():
        print(f"  provider {pid:>2} (jsonrpc-eth,    solo)    → :{port}")
    for pid, port in BTC_PRIMARY_PORTS.items():
        print(f"  provider {pid:>2} (jsonrpc-btc,    primary) → :{port}")
    for pid, port in LN_PRIMARY_PORTS.items():
        print(f"  provider {pid:>2} (jsonrpc-ln,     primary) → :{port}")
    for pid, port in SOLANA_PRIMARY_PORTS.items():
        print(f"  provider {pid:>2} (jsonrpc-solana, primary) → :{port}")
    for pid, port in SOLANA_SOLO_PORTS.items():
        print(f"  provider {pid:>2} (jsonrpc-solana, solo)    → :{port}")
    for pid, port in GRPC_PRIMARY_PORTS.items():
        print(f"  provider {pid:>2} (grpc,           primary) → :{port}")
    for pid, port in GRPC_BACKUP_PORTS.items():
        print(f"  provider {pid:>2} (grpc,           backup)  → :{port}")
    for pid, port in REST_PRIMARY_PORTS.items():
        print(f"  provider {pid:>2} (rest,           primary) → :{port}")
    for pid, port in REST_BACKUP_PORTS.items():
        print(f"  provider {pid:>2} (rest,           backup)  → :{port}")
    for pid, port in TM_PRIMARY_PORTS.items():
        print(f"  provider {pid:>2} (tendermintrpc,  primary) → :{port}")
    for pid, port in TM_BACKUP_PORTS.items():
        print(f"  provider {pid:>2} (tendermintrpc,  backup)  → :{port}")
    for pid, port in WS_PRIMARY_PORTS.items():
        print(f"  provider {pid:>2} (ws,             primary) → :{port}")
    for pid, port in WS_BACKUP_PORTS.items():
        print(f"  provider {pid:>2} (ws,             backup)  → :{port}")
    print(f"  control API  → :{CONTROL_PORT}")
    print("  GET /stats   → call counts per provider")
    print("  GET /history → ordered call log (who was tried first)")

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]

    # gRPC servers (MAG-1780). Each runs its own asyncio loop on a daemon
    # thread, sharing the same ProviderState instance with the matching
    # JSON-RPC port (primary tier) or a dedicated ProviderState (backup
    # tier) so /scenario applies as expected per pid. Import locally so a
    # missing grpcio dep doesn't break the JSON-RPC-only path (e.g. in
    # tests that don't install gRPC extras).
    try:
        import grpc_server  # local import keeps gRPC dep optional

        for pid, port in GRPC_PRIMARY_PORTS.items():
            threads.append(
                threading.Thread(
                    target=grpc_server.run_grpc_in_thread,
                    args=(port, states[pid]),
                    daemon=True,
                    name=f"grpc-provider-{pid}",
                )
            )
        for pid, port in GRPC_BACKUP_PORTS.items():
            threads.append(
                threading.Thread(
                    target=grpc_server.run_grpc_in_thread,
                    args=(port, states[pid]),
                    daemon=True,
                    name=f"grpc-backup-{pid}",
                )
            )
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
