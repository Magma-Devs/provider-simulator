"""Control API — the port-19000 surface, keyed by ``pool:pid``.

Each route is a method that takes a parsed request (a dict body or a flat query
dict) and returns ``(http_status, response_dict)``. The HTTP serving of these
(an http.server handler on the control port) is the socket adapter's job at
cut-over; keeping the routes as pure methods over the Registry makes them
unit-testable without a socket.

Clean cut (no translator): provider keys are ``"pool:pid"`` only. An old-format
key (a bare pid, or a block carrying ``chain_family``) gets a 400 that names the
new format — a stale client fails loudly, never silently.
"""

from dataclasses import fields

from provider_simulator.chains import CHAINS
from provider_simulator.domain.registry import Registry
from provider_simulator.listeners.ws import WsSubscriptions

_MODES = {"success", "error", "rate_limit", "down", "hang", "drop_connection"}
_CORRUPTION_MODES = {"truncated", "missing_field", "invalid_json", "empty_response", "wrong_type"}
_DROP_AT = {"before_headers", "after_headers", "mid_body"}
_LOGS_LAG_MODES = {"empty", "partial"}
# Two calls with the same (request_id, method) within this window are one group.
_CORRELATION_WINDOW_S = 0.050

_ENUMS = {
    "mode": _MODES,
    "corruption_mode": _CORRUPTION_MODES,
    "drop_at": _DROP_AT,
    "then_mode": _MODES,
    "logs_lag_mode": _LOGS_LAG_MODES,
}


def _normalise_responses(responses: object) -> object:
    """Normalise the ``responses`` override into the stored form.

    REST sends a list of ``[[verb, template], cfg]`` pairs (JSON has no tuple
    key); those re-tuple to ``{(verb, template): cfg}``. JSON-RPC / gRPC / TM
    send ``{method: cfg}`` already. Either shape rejects a per-method
    ``mode="error"`` — a per-method error must use ``error_stub`` / ``error``.
    """
    if isinstance(responses, list):
        out: dict = {}
        for pair in responses:
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                raise ValueError("REST responses must be [[verb, template], cfg] pairs")
            key, cfg = pair[0], pair[1]
            if not (isinstance(key, (list, tuple)) and len(key) == 2):
                raise ValueError("REST response key must be [verb, template]")
            if isinstance(cfg, dict) and cfg.get("mode") == "error":
                raise ValueError("per-method mode='error' not allowed; use error_stub or error")
            out[(key[0], key[1])] = cfg
        return out
    if isinstance(responses, dict):
        for cfg in responses.values():
            if isinstance(cfg, dict) and cfg.get("mode") == "error":
                raise ValueError("per-method mode='error' not allowed; use error_stub or error")
        return responses
    raise ValueError("responses must be an object (or a list of pairs for REST)")


class ControlApi:
    def __init__(self, registry: Registry, subscriptions: WsSubscriptions) -> None:
        self.registry = registry
        self.subscriptions = subscriptions

    # ── POST /scenario ──────────────────────────────────────────────────────
    def apply_scenario(self, body: object) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"error": "request body must be a JSON object"}
        providers = body.get("providers")
        if not isinstance(providers, dict):
            return 400, {"error": "missing 'providers' object; keys are 'pool:pid'"}

        staged = []  # (provider, scenario_updates, quirks_updates)
        for key, block in providers.items():
            if not isinstance(block, dict):
                return 400, {"error": f"scenario for {key!r} must be an object"}
            if ":" not in str(key):
                return 400, {
                    "error": (
                        f"provider key {key!r} must be 'pool:pid' — the old bare-pid + "
                        "chain_family format is no longer accepted"
                    )
                }
            pool, _, pid = str(key).partition(":")
            try:
                provider = self.registry.provider(pool, pid)
            except KeyError as exc:
                return 400, {"error": str(exc)}

            scenario_fields = {f.name for f in fields(provider.scenario)}
            quirks_fields = {f.name for f in fields(provider.quirks)}
            scenario_updates: dict = {}
            quirks_updates: dict = {}
            for field_name, value in block.items():
                if field_name == "responses":
                    try:
                        scenario_updates["responses"] = _normalise_responses(value)
                    except ValueError as exc:
                        return 400, {"error": f"{key}: {exc}"}
                elif field_name in scenario_fields:
                    err = _bad_enum(field_name, value)
                    if err:
                        return 400, {"error": f"{key}: {err}"}
                    scenario_updates[field_name] = value
                elif field_name in quirks_fields:
                    quirks_updates[field_name] = value
                else:
                    return 400, {
                        "error": (
                            f"{key}: unknown field {field_name!r} — not a scenario field "
                            f"{sorted(scenario_fields)} nor a {provider.pool.chain} quirk "
                            f"{sorted(quirks_fields)} (chain_family is gone; use the pool + "
                            "transports filter)"
                        )
                    }
            staged.append((provider, scenario_updates, quirks_updates))

        applied = {}
        for provider, scenario_updates, quirks_updates in staged:
            # A fresh fail_first_n restarts the sequence counter.
            if "fail_first_n" in scenario_updates:
                provider.reset_fail()
            provider.scenario.update(scenario_updates)
            if quirks_updates:
                provider.quirks.update(quirks_updates)
            applied[provider.key] = {**scenario_updates, **quirks_updates}
        return 200, {"status": "ok", "applied": applied}

    # ── resets ──────────────────────────────────────────────────────────────
    def reset(self) -> tuple[int, dict]:
        self._reset_heads()
        for provider in self.registry.all_providers():
            provider.scenario.reset()
            provider.reset_fail()
        return 200, {"status": "scenario reset"}

    def clear_history(self) -> tuple[int, dict]:
        for provider in self.registry.all_providers():
            provider.log.clear()
        return 200, {"status": "history cleared"}

    def reset_all(self) -> tuple[int, dict]:
        self._reset_heads()
        for provider in self.registry.all_providers():
            provider.scenario.reset()
            provider.reset_fail()
            provider.log.clear()
        return 200, {"status": "scenario reset and history cleared"}

    def _reset_heads(self) -> None:
        for chain in CHAINS.values():
            head = getattr(chain, "head", None)
            if head is not None:
                head.reset()

    # ── POST /advance ─────────────────────────────────────────────────────────
    def advance(self, body: object) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"error": "request body must be a JSON object"}
        chain_name = body.get("chain", "eth")
        chain = CHAINS.get(chain_name)
        head = getattr(chain, "head", None) if chain is not None else None
        if head is None:
            return 400, {"error": f"chain {chain_name!r} has no advanceable head"}
        if "per_second" in body:
            head.set_rate(body["per_second"])
        if "blocks" in body:
            head.bump(body["blocks"])
        return 200, {"status": "ok", "chain": chain_name, "head": head.current()}

    # ── GET /scenario, /stats ─────────────────────────────────────────────────
    def get_scenario(self) -> tuple[int, dict]:
        providers = {}
        for provider in self.registry.all_providers():
            snap = provider.scenario.snapshot()
            snap.pop("responses", None)  # write-only, like the flat /scenario (tuple keys, REST)
            providers[provider.key] = snap
        return 200, {"providers": providers}

    def get_stats(self) -> tuple[int, dict]:
        return 200, {"providers": {p.key: p.log.stats() for p in self.registry.all_providers()}}

    # ── GET /history ──────────────────────────────────────────────────────────
    def get_history(self, query: dict) -> tuple[int, dict]:
        entries: list[dict] = []
        for provider in self.registry.all_providers():
            entries.extend(provider.log.get_history())

        entries = self._filter_history(entries, query)
        entries.sort(key=lambda e: e.get("ts", 0.0))

        # call_order (1-based over the merged, ts-sorted timeline) + correlation.
        groups: dict = {}
        next_group = 0
        for order, entry in enumerate(entries, start=1):
            entry["call_order"] = order
            gkey = (entry.get("request_id"), entry.get("method"))
            prev = groups.get(gkey)
            if prev is not None and entry.get("ts", 0.0) - prev[1] <= _CORRELATION_WINDOW_S:
                entry["correlation_group"] = prev[0]
            else:
                next_group += 1
                groups[gkey] = (next_group, entry.get("ts", 0.0))
                entry["correlation_group"] = next_group

        if "last" in query:
            entries = entries[-_as_int(query["last"], len(entries)) :]
        if "max" in query:
            cap = _as_int(query["max"], -1)
            if cap < 0:
                return 400, {"error": "max must be a non-negative integer"}
            entries = entries[:cap]
        return 200, {"count": len(entries), "history": entries}

    def _filter_history(self, entries: list[dict], query: dict) -> list[dict]:
        for key in ("pool", "transport", "method", "status", "interface"):
            if key in query:
                entries = [e for e in entries if e.get(key) == query[key]]
        if "request_id" in query:
            wanted = str(query["request_id"])
            entries = [e for e in entries if str(e.get("request_id")) == wanted]
        if "from" in query:
            lo = _as_float(query["from"], float("-inf"))
            entries = [e for e in entries if e.get("ts", 0.0) >= lo]
        if "to" in query:
            hi = _as_float(query["to"], float("inf"))
            entries = [e for e in entries if e.get("ts", 0.0) <= hi]
        for key, value in query.items():
            if key.startswith("lava_header_"):
                name = key[len("lava_header_") :]
                entries = [e for e in entries if e.get("lava_headers", {}).get(name) == value]
        return entries

    # ── /ws/emit, /ws/subscriptions ───────────────────────────────────────────
    def ws_emit(self, body: object) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"error": "request body must be a JSON object"}
        sub_id = body.get("subscription_id")
        if not sub_id:
            return 400, {"error": "missing 'subscription_id'"}
        outcome = self.subscriptions.emit(sub_id, body.get("event"))
        if outcome == "unknown":
            return 404, {"error": f"no active subscription {sub_id!r}"}
        if outcome == "full":
            return 503, {"error": f"subscription {sub_id!r} queue full"}
        return 200, {"status": "emitted", "subscription_id": sub_id}

    def ws_subscriptions(self) -> tuple[int, dict]:
        return 200, {"subscriptions": self.subscriptions.list()}

    # ── health / ready ────────────────────────────────────────────────────────
    def health(self) -> tuple[int, dict]:
        return 200, {"status": "ok"}

    def ready(self) -> tuple[int, dict]:
        # Registry built = every provider present. The live TCP-port check the
        # flat /ready did is the socket adapter's job at cut-over.
        return 200, {"status": "ready", "providers": len(self.registry.all_providers())}


def _bad_enum(field_name: str, value: object) -> str:
    allowed = _ENUMS.get(field_name)
    if allowed is not None and value is not None and value not in allowed:
        return f"invalid {field_name} {value!r}; allowed: {sorted(allowed)}"
    return ""


def _as_int(value: object, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default
