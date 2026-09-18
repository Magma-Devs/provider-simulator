"""RESP control API — the four things a test can do to the router's store.

Read, cut off, restore, flush. Every route returns ``(status, dict)``, the same
shape ``ControlApi`` uses, so the HTTP adapter in ``server.py`` stays a parser
and a dispatcher and every decision lives here.

**There is no write, and that is the design.** A test that can plant an entry
will plant one instead of making the router store it, and then it measures the
store rather than the router. ``flush`` can only empty the store; it cannot put a
chosen entry in.

Why this is a listener of its own
---------------------------------
It sits on its own port beside the provider control API rather than inside it.
The two answer about different things -- one about simulated chain nodes, one
about a store the router keeps its cache in -- and a caller that reaches one has
no business reaching the other by accident.

It is still the SAME pod and the same process. A second deployment would be two
things to keep in step across two clusters, which is exactly the drift the
cluster-parity guard exists to catch.

Telling an empty store from an unreachable one
----------------------------------------------
This is the failure this file is most careful about. A reader that answers "no
entries" when it could not connect passes every test ever written on it, because
"the store is empty" and "I never asked" look identical to the caller.

So an unreachable store is ``503`` with the reason, never ``200`` with an empty
list. A reachable store holding nothing is ``200`` with ``count: 0``. A test can
tell those apart, and one of them means its own setup is broken rather than the
router.
"""

from __future__ import annotations

from provider_simulator.resp_proxy import (
    CUT_OFF_KINDS,
    RESP_PROXY_IDLE_SECONDS,
    NegativeLatency,
    RespProxy,
    RespProxyRegistry,
    UnknownCutOffKind,
)
from provider_simulator.resp_store import RespStore, RespStoreError

# What a caller gets back when it names a store that is not running here.
# Listing what does exist, rather than a bare 404, because the usual cause is a
# simulator started without this feature and the caller cannot see that from the
# outside.
_NO_SUCH = "unknown RESP store {name!r}"


class RespControlApi:
    """Routes for reading and interrupting the router's access to a RESP store.

    Holds one proxy and one reader per named store. The proxy sits between the
    router and the store; the reader goes straight to the store, which is what
    lets a test read an entry while the router is cut off from it.
    """

    def __init__(self, proxies: RespProxyRegistry | None = None) -> None:
        self.proxies = proxies if proxies is not None else RespProxyRegistry()
        self._stores: dict[str, RespStore] = {}

    def register(
        self,
        name: str,
        target_host: str,
        target_port: int,
        *,
        username: str | None = None,
        password: str | None = None,
        db: int = 0,
        idle_seconds: float = RESP_PROXY_IDLE_SECONDS,
    ) -> RespProxy:
        """Run a proxy and a reader for one store, both pointed at the same place.

        The credentials reach the READER only. The proxy needs none: it moves the
        router's bytes without reading them, and the router carries its own.

        ``idle_seconds`` is how long the proxy carries a connection that moves no
        bytes while it is forwarding, before giving up on it. It is threaded all
        the way down so the deployed value is one a test can also set; a default
        that only production can reach is a default nothing checks.
        """
        proxy = self.proxies.add(name, target_host, target_port, idle_seconds=idle_seconds)
        self._stores[name] = RespStore(target_host, target_port, username=username, password=password, db=db)
        return proxy

    def names(self) -> list[str]:
        return self.proxies.names()

    # ── read ──────────────────────────────────────────────────────────────────

    def get_entries(self, name: str, query: dict) -> tuple[int, dict]:
        """Every key in the store, with its value and remaining lifetime.

        Reads the store directly, never through the proxy, so it answers while
        the router is cut off.

        The default pattern is every key rather than the router's ``sr:`` prefix.
        A reader that silently filtered would answer "empty" for a store whose
        prefix was configured differently, and the caller would read that as the
        router having stored nothing.
        """
        store = self._stores.get(name)
        if store is None:
            return 404, self._unknown(name)
        pattern = query.get("pattern", "*")
        try:
            entries = store.entries(pattern)
        except RespStoreError as exc:
            return 503, {
                "error": str(exc),
                "store": name,
                "target": store.target(),
                "read": "failed",
                "note": "this is not an empty store; the reader could not complete",
            }
        return 200, {
            "store": name,
            "target": store.target(),
            "pattern": pattern,
            "count": len(entries),
            "entries": [entry.as_dict() for entry in entries],
        }

    def get_state(self, name: str) -> tuple[int, dict]:
        """What the proxy is doing, and whether the store answers.

        ``reachable`` is measured here and now with a PING straight to the store.
        It is deliberately separate from the proxy's state: a cut-off router and
        a dead store look the same from the router's side, and this is the one
        place that can tell them apart.
        """
        proxy = self.proxies.get(name)
        store = self._stores.get(name)
        if proxy is None or store is None:
            return 404, self._unknown(name)
        state = dict(proxy.as_dict())
        state["store_reachable"] = store.ping()
        return 200, state

    def get_stores(self) -> tuple[int, dict]:
        return 200, {"stores": self.names()}

    # ── cut off, restore, flush ───────────────────────────────────────────────

    def cut_off(self, name: str, body: dict) -> tuple[int, dict]:
        """Stop the router reaching the store, in one of the two ways.

        ``kind`` is required rather than defaulted. The router's own counter
        separates a store it could not finish with from one it could not reach,
        so a caller that did not choose has not said which half it is testing.
        """
        proxy = self.proxies.get(name)
        if proxy is None:
            return 404, self._unknown(name)
        kind = body.get("kind")
        if kind is None:
            return 400, {
                "error": "cut-off needs a kind",
                "expected": list(CUT_OFF_KINDS),
                "note": "the router counts a store it could not finish with separately "
                "from one it could not reach; choose which one this test means",
            }
        try:
            proxy.cut_off(kind)
        except UnknownCutOffKind as exc:
            return 400, {"error": str(exc), "expected": list(CUT_OFF_KINDS)}
        return 200, {"status": "cut off", "proxy": proxy.as_dict()}

    def restore(self, name: str) -> tuple[int, dict]:
        """Let the router reach the store again."""
        proxy = self.proxies.get(name)
        if proxy is None:
            return 404, self._unknown(name)
        proxy.restore()
        return 200, {"status": "restored", "proxy": proxy.as_dict()}

    def set_latency(self, name: str, body: dict) -> tuple[int, dict]:
        """How long this store takes to answer, in milliseconds.

        Separate from the gate: the gate says whether the router can reach the
        store at all, and this says how quickly it answers when it can. A test
        can set both.

        The reply carries the proxy's whole state, so a caller can confirm the
        value landed rather than trust that the call meant what it asked for. A
        latency that did not apply is the worst failure this route has: the
        router then answers from its cache and the test reads a hit, which looks
        like the router handling a slow store correctly.
        """
        proxy = self.proxies.get(name)
        if proxy is None:
            return 404, self._unknown(name)
        raw = body.get("ms")
        if raw is None:
            return 400, {
                "error": "latency needs an ms value",
                "note": "how many milliseconds the store takes to answer; 0 is instant",
            }
        try:
            latency_ms = int(raw)
        except (TypeError, ValueError, OverflowError):
            # OverflowError is what int() raises for a float infinity, not a
            # ValueError -- and Python's own json module both emits and parses
            # the non-standard literal `Infinity`, so this is reachable from a
            # real request body, not only from a test.
            return 400, {"error": f"ms must be a whole number of milliseconds, got {raw!r}"}
        try:
            proxy.set_latency(latency_ms)
        except NegativeLatency as exc:
            return 400, {"error": str(exc)}
        return 200, {"status": "latency set", "proxy": proxy.as_dict()}

    def flush(self, name: str, query: dict | None = None) -> tuple[int, dict]:
        """Remove the entries matching a pattern, so a test starts from nothing.

        Goes straight to the store. It works while the router is cut off, which
        is what a test needs when it is setting up the next case without first
        putting the router back.

        **Scoped by a pattern, and it used to send a bare FLUSHDB.** That empties
        the whole logical database and knows nothing about the router's
        ``key-prefix``. The read path already defaults to every key BECAUSE
        prefixes vary, so the reading half knew prefixes mattered while the
        destroying half did not. The day two routers share a store, one test's
        flush takes the other's cache with it. Pass ``?pattern=sr:*`` to remove
        one router's entries and leave the rest.
        """
        store = self._stores.get(name)
        if store is None:
            return 404, self._unknown(name)
        pattern = (query or {}).get("pattern", "*")
        try:
            removed = store.delete_matching(pattern)
        except RespStoreError as exc:
            return 503, {"error": str(exc), "store": name, "target": store.target()}
        return 200, {
            "status": "flushed",
            "store": name,
            "target": store.target(),
            "pattern": pattern,
            "removed": removed,
        }

    def reset_counters(self, name: str) -> tuple[int, dict]:
        """Zero the proxy's counters without changing what it is doing."""
        proxy = self.proxies.get(name)
        if proxy is None:
            return 404, self._unknown(name)
        proxy.reset_counters()
        return 200, {"status": "counters reset", "proxy": proxy.as_dict()}

    # ── health ────────────────────────────────────────────────────────────────

    def health(self) -> tuple[int, dict]:
        """The listener is up. Says nothing about the store — see get_state."""
        return 200, {"status": "ok", "stores": self.names()}

    def _unknown(self, name: str) -> dict:
        return {"error": _NO_SUCH.format(name=name), "stores": self.names()}
