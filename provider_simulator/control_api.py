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

from provider_simulator.build_info import build_info
from provider_simulator.chains import CHAINS
from provider_simulator.domain.registry import Registry
from provider_simulator.listeners.ws import WsSubscriptions

_MODES = {"success", "error", "rate_limit", "down", "hang", "drop_connection"}
_CORRUPTION_MODES = {
    "truncated",
    "missing_field",
    "invalid_json",
    "empty_response",
    "wrong_type",
    "null_body",  # the whole wire body becomes the literal ``null``
    "invalid_proto",  # gRPC-only wire corruption; other listeners never emit it
}
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
    "unknown_method_mode": {"null", "error"},
}


def _bad_number(field_name: str, value: object) -> str:
    """Range/type validation for the numeric scenario fields, so a typo'd
    payload fails with a 400 instead of silently mis-configuring a provider.
    bool is rejected explicitly — it subclasses int and would masquerade as
    a number."""
    if field_name == "error_probability":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
            return f"error_probability must be a number in [0.0, 1.0], got {value!r}"
    if field_name in ("latency_ms", "fail_first_n"):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return f"{field_name} must be a non-negative integer, got {value!r}"
    return ""


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
        for method_name, cfg in responses.items():
            if not isinstance(cfg, dict):
                continue
            if cfg.get("mode") == "error":
                raise ValueError("per-method mode='error' not allowed; use error_stub or error")
            # Canned {status, body} success override (JSON-RPC method entries
            # only — REST re-tupled entries own their body+status semantics).
            # The body must be a JSON object, must not combine with a fault
            # mode (they describe different outcomes), and the status must be
            # 2xx — non-2xx shapes are what mode='error' + http_status is for.
            if isinstance(method_name, str) and "body" in cfg:
                if not isinstance(cfg["body"], dict):
                    raise ValueError(f"per-method body override must be a dict (method={method_name!r})")
                if "mode" in cfg:
                    raise ValueError(f"per-method body and mode are mutually exclusive (method={method_name!r})")
                status_val = cfg.get("status", 200)
                if not (isinstance(status_val, int) and not isinstance(status_val, bool) and 200 <= status_val <= 299):
                    raise ValueError(
                        f"per-method body override status must be a 2xx int "
                        f"(method={method_name!r}), got {status_val!r}"
                    )
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
                    err = _bad_enum(field_name, value) or _bad_number(field_name, value)
                    if err:
                        return 400, {"error": f"{key}: {err}"}
                    scenario_updates[field_name] = value
                elif field_name in quirks_fields:
                    err = _bad_enum(field_name, value)
                    if err:
                        return 400, {"error": f"{key}: {err}"}
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
            # `responses` is write-only on the wire (REST entries re-tuple to
            # (verb, template) keys, which JSON cannot carry) — echo every
            # other resolved field.
            applied[provider.key] = {
                k: v for k, v in {**scenario_updates, **quirks_updates}.items() if k != "responses"
            }
        return 200, {"status": "ok", "applied": applied}

    # ── resets ──────────────────────────────────────────────────────────────
    # Every reset takes an optional ``pool``. Without one it clears everything,
    # exactly as it always has. With one it touches that pool's providers only,
    # so one router's clean-up can no longer reach into another router's
    # providers.
    #
    # Block heads are a weaker guarantee, and the difference matters. A head is
    # one value per CHAIN, shared by every pool on that chain. Scoping moves the
    # heads of the chains that pool serves instead of every chain, but seven
    # pools serve eth, so an eth-sim reset still rewinds the head an
    # eth-solo-sim test is watching. Providers are isolated; heads are narrowed.
    #
    # Only the scenario reset moves a head at all — clearing history leaves
    # every head alone.
    #
    # An unknown pool name is a 400 that lists the pools that exist. Resetting
    # nothing and reporting success is the failure this scoping exists to
    # prevent: the test still runs, still passes, and measures the previous
    # test's leftovers.
    def reset(self, pool: str | None = None) -> tuple[int, dict]:
        return self._perform_reset(pool, scenario=True, history=False, status="scenario reset")

    def clear_history(self, pool: str | None = None) -> tuple[int, dict]:
        return self._perform_reset(pool, scenario=False, history=True, status="history cleared")

    def reset_all(self, pool: str | None = None) -> tuple[int, dict]:
        return self._perform_reset(pool, scenario=True, history=True, status="scenario reset and history cleared")

    def _perform_reset(self, pool: str | None, *, scenario: bool, history: bool, status: str) -> tuple[int, dict]:
        """Clear the requested state over the requested scope.

        The reply names the scope it actually cleared — the pool (``null`` for
        a whole-simulator reset), every provider key it touched and every chain
        whose head it moved. A caller can therefore check what happened rather
        than trust that the call meant what it asked for.
        """
        providers, chains, error = self._scope(pool)
        if error:
            return 400, {"error": error}
        if scenario:
            for _, chain in chains:
                head = getattr(chain, "head", None)
                if head is not None:
                    head.reset()
        for provider in providers:
            if scenario:
                provider.scenario.reset()
                provider.quirks.reset()
                provider.reset_fail()
            if history:
                provider.log.clear()
        return 200, {
            "status": status,
            "pool": pool,
            "providers": sorted(p.key for p in providers),
            "chains": sorted({name for name, _ in chains}) if scenario else [],
        }

    def _scope(self, pool: str | None) -> tuple[list, list, str]:
        """Resolve a pool name into the providers and chains a reset may touch.

        ``None`` means the whole simulator: every provider, every chain. A pool
        name means that pool's providers and the single chain it serves (a pool
        serves exactly one chain — ``build_registry`` refuses a pool that
        declares two). Chains come back as ``(name, chain)`` pairs so the reply
        can name them.

        Returns ``(providers, chains, error)``; a non-empty error means the
        pool does not exist and nothing was touched.
        """
        if pool is None:
            return self.registry.all_providers(), list(CHAINS.items()), ""
        if not isinstance(pool, str):
            return [], [], f"pool must be a string, got {type(pool).__name__}"
        if pool not in self.registry.pools:
            return [], [], f"no pool {pool!r}; pools are {sorted(self.registry.pools)!r}"
        resolved = self.registry.pools[pool]
        chain = CHAINS.get(resolved.chain)
        chains = [(resolved.chain, chain)] if chain is not None else []
        return list(resolved.providers.values()), chains, ""

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

    # ── GET /scenario, /stats, /topology ──────────────────────────────────────
    def get_scenario(self) -> tuple[int, dict]:
        providers = {}
        for provider in self.registry.all_providers():
            snap = provider.scenario.snapshot()
            snap.pop("responses", None)  # write-only, like the flat /scenario (tuple keys, REST)
            # Quirks read back flat, exactly as they are written — one dict per
            # provider on the wire, no nesting.
            snap.update(provider.quirks.snapshot())
            # Beside the fault settings, so a reader of this reply sees the
            # name the router reports rather than only a pool and a slot.
            snap["name"] = provider.name
            providers[provider.key] = snap
        return 200, {"providers": providers}

    def get_stats(self) -> tuple[int, dict]:
        return 200, {"providers": {p.key: {**p.log.stats(), "name": p.name} for p in self.registry.all_providers()}}

    def get_providers(self, query: dict) -> tuple[int, dict]:
        """Every fact about every provider, keyed pool then colon then slot.

        A test could ask this simulator which ports a provider listens on and
        nothing else. Everything else it needed — what a provider is called,
        which pool a router uses, which cross-validation group a provider is
        in — was written by hand in the test code, and a written copy can be
        wrong while staying quiet.

        Four filters, in the style /history already accepts. A filter only ever
        narrows the set; the shape of an entry never changes, so a caller does
        not have to handle two shapes.

        ``name`` is the one a test actually needs: it reads a name out of a
        response header and has to learn which slot that was, so it can send
        that provider a fault. It matches case-insensitively, because the chart
        lowercases every name before the router sees it.

        An unknown pool is an error rather than an empty set. A pool name that
        does not exist is a typo, and answering nothing would let a test believe
        it had filtered correctly and found nothing — the exact silence this
        endpoint exists to end.
        """
        pool_filter = query.get("pool")
        if pool_filter is not None and pool_filter not in self.registry.pools:
            return 400, {"error": (f"no pool {pool_filter!r}; pools are {sorted(self.registry.pools)!r}")}

        pid_filter = query.get("pid")
        name_filter = query.get("name")
        backup_filter = query.get("is_backup")
        want_backup = None if backup_filter is None else str(backup_filter).lower() == "true"

        providers = {}
        for provider in self.registry.all_providers():
            if pool_filter is not None and provider.pool.name != pool_filter:
                continue
            if pid_filter is not None and provider.pid != str(pid_filter):
                continue
            if want_backup is not None and provider.is_backup is not want_backup:
                continue
            if name_filter is not None and provider.name.lower() != str(name_filter).lower():
                continue
            providers[provider.key] = {
                "pool": provider.pool.name,
                "pid": provider.pid,
                "chain": provider.pool.chain,
                "name": provider.name,
                "is_backup": provider.is_backup,
                "group_label": provider.group_label,
                "endpoints": [
                    {"interface": ep.interface, "transport": ep.transport, "port": ep.port} for ep in provider.endpoints
                ],
            }
        return 200, {"providers": providers}

    def get_topology(self) -> tuple[int, dict]:
        """Every pool, its chain, and each provider's endpoints — the
        validated Registry built at startup, read back as-is. Pure read: no
        parameters, no state, nothing to validate (build_registry already did
        that)."""
        topology = {}
        for pool_name, pool in self.registry.pools.items():
            providers = {
                pid: [
                    {"interface": ep.interface, "transport": ep.transport, "port": ep.port} for ep in provider.endpoints
                ]
                for pid, provider in pool.providers.items()
            }
            # `names` sits BESIDE `providers`, never inside it. Two readers in
            # the automation project read a per-slot value as a list of
            # endpoints, and one raises on purpose when it is not a list.
            # Nesting the name would break both.
            names = {pid: provider.name for pid, provider in pool.providers.items()}
            topology[pool_name] = {
                "chain": pool.chain,
                "providers": providers,
                "names": names,
            }
        return 200, {"topology": topology}

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
            tail = _as_int(query["last"], len(entries))
            # A slice of [-0:] is the WHOLE list — guard so last=0 means none.
            entries = entries[-tail:] if tail > 0 else []
        if "max" in query:
            cap = _as_int(query["max"], -1)
            if cap < 0:
                return 400, {"error": "max must be a non-negative integer"}
            # The cap keeps the TAIL — the most recent calls — and the kept
            # entries retain their full-timeline call_order.
            entries = entries[-cap:] if cap > 0 else []
        return 200, {"count": len(entries), "history": entries}

    def _filter_history(self, entries: list[dict], query: dict) -> list[dict]:
        for key in ("pool", "pid", "transport", "method", "status", "interface"):
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
                # Header names are case-insensitive on the wire (clients may
                # title-case them) — match accordingly.
                name = key[len("lava_header_") :].lower()
                entries = [
                    e
                    for e in entries
                    if any(
                        header.lower() == name and header_value == value
                        for header, header_value in e.get("lava_headers", {}).items()
                    )
                ]
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

    # ── version ───────────────────────────────────────────────────────────────
    def version(self) -> tuple[int, dict]:
        """Which build this is: release tag, commit, and which of the three
        states it is in. Stamped into the image at build time; see
        provider_simulator/build_info.py. Always 200: "I do not know what I am"
        is an answer, not a failure, and a probe should not treat it as one."""
        return 200, build_info()


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
