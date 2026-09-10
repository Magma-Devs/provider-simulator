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
from provider_simulator.cache_sim import DEFAULT_MODE as DEFAULT_CACHE_MODE
from provider_simulator.cache_sim import CacheEntry, CacheSimRegistry, UnknownMode
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
    def __init__(
        self,
        registry: Registry,
        subscriptions: WsSubscriptions,
        caches: CacheSimRegistry | None = None,
        cache_ports: "dict[str, int] | None" = None,
    ) -> None:
        self.registry = registry
        self.subscriptions = subscriptions
        # A simulator built without cache-sims answers the cache routes with a
        # 404 rather than pretending it has one. An empty registry is the same
        # thing as none for every read below.
        self.caches = caches if caches is not None else CacheSimRegistry()
        # Only the self-test below needs a port: it dials a cache-sim's own
        # listener. Every other cache route reads the sim object directly. A
        # cache with no port here is one whose listener never started, and the
        # self-test says so rather than dialling nothing.
        self.cache_ports = dict(cache_ports or {})

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
        # Cache-sims reset with everything else. A staged entry that survived
        # into the next test would be served to a test expecting a cold cache,
        # which passes for the wrong reason and reports coverage it does not
        # have. They are not pool-scoped -- a cache has no pool -- so a
        # pool-scoped reset leaves them alone rather than clearing state the
        # caller did not ask about.
        cache_names: list = []
        if pool is None:
            for name in self.caches.names():
                sim = self.caches.get(name)
                if sim is None:
                    continue
                if scenario and history:
                    sim.reset()
                elif scenario:
                    sim.stage(mode=DEFAULT_CACHE_MODE)
                elif history:
                    sim.clear_calls()
                cache_names.append(name)
        return 200, {
            "status": status,
            "pool": pool,
            "providers": sorted(p.key for p in providers),
            "chains": sorted({name for name, _ in chains}) if scenario else [],
            "caches": sorted(cache_names),
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

    # ── cache-sims ────────────────────────────────────────────────────────────
    #
    # A cache is not a provider: it has no pool and no pid, so it is addressed
    # by name and a scenario set on a chain's providers can never reach it.
    #
    # An unknown name is a 404 that lists the names that exist, never a silent
    # success. Staging into a cache nothing serves would leave the test running
    # against an unstaged one, passing while measuring nothing — the same
    # failure the pool-scoped reset above exists to prevent.
    def _unknown(self, name: str) -> dict:
        return {"error": f"unknown cache-sim {name!r}", "caches": self.caches.names()}

    def cache_stage(self, name: str, body: object) -> tuple[int, dict]:
        """Decide what the named cache-sim answers the router's next lookups.

        ``mode`` picks one behaviour and ``entry`` supplies what a hit returns.
        Both a bad mode and a hit with no entry are refused here rather than
        answered — an entry-less hit would serve a reply with no data and read
        as a cache that answered.

        ``seen_block`` appears at two levels and they mean different things. At
        the top it is the CACHE's own view of the chain head, which a miss
        reports and which persists until it is set again or the cache is reset.
        Inside ``entry`` it is the value stored with that one entry. A real
        cache reports its own head on every lookup, so the top-level one is the
        faithful knob; the entry-level one stays because a test needs to offer
        a chain head the router is supposed to refuse.
        """
        if not isinstance(body, dict):
            return 400, {"error": "request body must be a JSON object"}
        sim = self.caches.get(name)
        if sim is None:
            return 404, self._unknown(name)

        entry = body.get("entry")
        if entry is not None and not isinstance(entry, dict):
            return 400, {"error": f"entry must be an object, got {type(entry).__name__}"}
        try:
            sim.stage(
                mode=str(body.get("mode", "hit")),
                entry=CacheEntry.from_dict(entry) if entry else None,
                latency_ms=_as_int(body.get("latency_ms"), 0),
                error_status=body.get("error_status"),
                error_message=body.get("error_message"),
                malformed_body=body.get("malformed_body"),
                # Absent means "leave the head alone", not "set it to zero" —
                # a stage that silently reset it would wipe the head an earlier
                # call established, and the miss would report 0 with nothing
                # naming the cause.
                seen_block=(_as_int(body["seen_block"], 0) if "seen_block" in body else None),
            )
        except (UnknownMode, ValueError) as exc:
            return 400, {"error": str(exc)}
        return 200, {"status": "staged", "cache": sim.state()}

    def cache_reset(self, name: str) -> tuple[int, dict]:
        """Back to answering misses, with the staged entry and call log dropped."""
        sim = self.caches.get(name)
        if sim is None:
            return 404, self._unknown(name)
        sim.reset()
        return 200, {"status": "reset", "cache": sim.state()}

    def cache_clear_calls(self, name: str) -> tuple[int, dict]:
        """Drop the call log, keeping whatever is staged.

        The same split the provider reset/history-clear pair keeps: a test that
        warms an entry and then wants a clean count needs one without the other.
        """
        sim = self.caches.get(name)
        if sim is None:
            return 404, self._unknown(name)
        sim.clear_calls()
        return 200, {"status": "calls cleared", "cache": sim.state()}

    def get_cache_calls(self, name: str) -> tuple[int, dict]:
        """Every lookup the router made against this cache-sim, oldest first.

        Neither cache tier names itself in the response, so this log and the
        router's own per-tier counter are the only two witnesses that the
        secondary was reached at all — and the only way to prove it was reached
        exactly once, or not at all.
        """
        sim = self.caches.get(name)
        if sim is None:
            return 404, self._unknown(name)
        calls = sim.calls()
        return 200, {"cache": name, "count": len(calls), "calls": calls}

    def cache_selftest_write(self, name: str) -> tuple[int, dict]:
        """Send one non-read call to this cache-sim, so its record can be trusted.

        A test that asserts "the router never wrote the second tier" is reading
        an absence. An absence is only evidence when the thing it rules out
        would have shown up, and for most of this simulator's life it would not
        have: the listener registered GetRelay alone, so gRPC answered every
        other method itself and the record stayed empty whatever arrived.

        This route puts a real non-read call through the real listener and
        reports what the record then holds. A test calls it first, checks the
        call is there, resets, and only then trusts the absence it measures.

        The call is refused, exactly as a router's write would be. Nothing is
        stored and no staged entry changes -- but the call log does grow by one,
        so a caller about to count calls should clear the log afterwards with
        ``POST /cache/<name>/calls/clear``. Use that rather than ``reset``,
        which also drops whatever the caller staged.
        """
        sim = self.caches.get(name)
        if sim is None:
            return 404, self._unknown(name)

        port = self.cache_ports.get(name)
        if port is None:
            return 409, {
                "error": (
                    f"cache-sim {name!r} has no listener port, so nothing can dial it. "
                    "This simulator was built without a gRPC listener for that cache."
                ),
                "cache": name,
                "ports": self.cache_ports,
            }

        # Imported here, not at module scope: this module must stay importable
        # on a machine without grpcio, the way server.py already guards the
        # listener import.
        try:
            from provider_simulator.listeners.cache_grpc import (
                NON_READ_PROBE_METHOD,
                CacheSimDidNotRefuse,
                CacheSimUnreachable,
                send_non_read,
            )
        except ImportError as exc:
            # grpcio is optional. Without it no cache listener ever started, so
            # there is nothing to dial -- but cache_ports is filled from the
            # constants regardless, so the port check above cannot notice.
            return 409, {
                "error": (
                    f"this simulator has no gRPC support ({exc}), so cache-sim "
                    f"{name!r} has no listener and nothing can dial it."
                ),
                "cache": name,
            }

        # records_total, NOT len(calls()). The call log is a ring buffer capped
        # at HISTORY_MAX, so on a cache-sim that has served that many lookups an
        # append evicts the oldest and the LENGTH does not move. Measuring by
        # length there reports a call that did arrive as missing -- and it does
        # it only on a busy cache-sim, which is the one whose record a test most
        # wants to trust.
        before = sim.records_total()
        try:
            refused_with = send_non_read(port)
        except CacheSimDidNotRefuse as exc:
            return 500, {
                "error": str(exc),
                "cache": name,
                "method": NON_READ_PROBE_METHOD,
                "fault": "this cache-sim served a method it must refuse",
            }
        except CacheSimUnreachable as exc:
            return 502, {
                "error": str(exc),
                "cache": name,
                "method": NON_READ_PROBE_METHOD,
                "fault": "the probe call never reached the cache-sim",
            }

        after = sim.records_total()
        recorded = [c for c in sim.calls() if c.get("method") == NON_READ_PROBE_METHOD]
        if after <= before or not recorded:
            return 500, {
                "error": (
                    f"cache-sim {name!r} refused the probe call, so its listener "
                    f"saw it, but the call record does not hold it. Recorded "
                    f"calls went from {before} to {after}, and the record holds "
                    f"{len(recorded)} row(s) under {NON_READ_PROBE_METHOD!r}. "
                    f"An absence of writes read off this record would prove "
                    f"nothing."
                ),
                "cache": name,
                "method": NON_READ_PROBE_METHOD,
                "refused_with": refused_with,
                "records_total_before": before,
                "records_total_after": after,
                "fault": "the listener refused the call but the record did not keep it",
            }

        return 200, {
            "status": "the call record can see a call that is not a read",
            "cache": name,
            "method": NON_READ_PROBE_METHOD,
            "refused_with": refused_with,
            "records_total_before": before,
            "records_total_after": after,
            "recorded": recorded,
        }

    def get_cache(self, name: str) -> tuple[int, dict]:
        sim = self.caches.get(name)
        if sim is None:
            return 404, self._unknown(name)
        return 200, sim.state()

    def get_caches(self) -> tuple[int, dict]:
        sims = [self.caches.get(n) for n in self.caches.names()]
        return 200, {
            "caches": [s.state() for s in sims if s is not None],
            "count": len(sims),
        }


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
