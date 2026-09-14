"""RESP proxy — a pass-through that controls the ROUTER's access to a store.

The router can keep its cache in a Redis or Valkey instead of in the cache
process beside it. A test needs to take that store away from the router and give
it back, and it cannot do that by stopping the store.

**This proxy controls access. It never controls the store.** The store runs
throughout. The proxy stops forwarding, so the router cannot reach it. From the
router's side that is the same as a store that is down, and the router's
experience is the thing under test.

Why a proxy rather than stopping a pod
--------------------------------------
Three reasons, and each one alone would be enough.

The simulator cannot stop a pod. Its own pod has no service account and no role
binding, so it cannot act on the cluster at all.

A stopped pod comes back empty. So a test that stopped one and started it again
would be watching the router re-fetch from the chain nodes, not watching it
recover a cache it still had. Nothing here stops, so the entries survive and a
recovery test measures a recovery.

And a stopped pod produces only ONE kind of failure. The router counts two:
``kind="timeout"`` when it could not finish, and ``kind="error"`` when it could
not reach. A proxy makes both, because it chooses between holding a connection
open and closing it.

The two kinds, precisely
------------------------
``timeout`` holds the connection and moves no bytes. The router's read waits
until its own deadline expires. Live connections stop being pumped rather than
being closed, because closing one would produce an error and the test asked for
a timeout.

``error`` closes the connection. Live connections are closed too, and a new one
is accepted and then closed straight away.

**A closed connection is not a refused one, and the difference is worth
stating.** A refused connection is what a caller gets when nothing is listening
at all — the operating system answers, not us. This proxy is always listening,
so it cannot produce that. What the router sees instead is a connection that
ends: a reset, or an end-of-file on a read. Both reach go-redis as an error and
both count as ``kind="error"``, which is what the mode is named after. It is not
the same wire event as a refusal, so do not write a test that claims it is.

Cutting off must reach connections that already exist
-----------------------------------------------------
A client holds a pool of open connections. Changing what happens to NEW
connections leaves the router using the ones it already had, and the cut-off
does nothing while appearing to work. So every live connection is tracked, and
cutting off acts on all of them.

This is the same shape as a fault that was measured on the secondary cache: a
staged change travelled to a new pod while the router kept reading its
established connection to the old one, and the test read the answer of a process
it never staged into.
"""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass

# What the proxy is doing with the router's traffic. Mutually exclusive, the same
# way a simulated provider's mode is.
FORWARDING = "forwarding"
TIMEOUT = "timeout"
ERROR = "error"

# The two ways a test can cut the router off. FORWARDING is not one of them --
# restoring is its own call, so that "cut off" and "let through" can never be
# confused for each other at a call site.
CUT_OFF_KINDS = (TIMEOUT, ERROR)

# How much is moved in one read. A cache reply is small; this only bounds the
# syscall size, never the total.
_CHUNK = 65536

# How long a pump blocks before re-checking the gate. A cut-off has to reach a
# connection that is sitting idle in the router's pool, so the pump cannot block
# on recv() for ever.
_POLL_SECONDS = 0.05


class UnknownCutOffKind(ValueError):
    """Raised for a cut-off kind outside CUT_OFF_KINDS, naming the ones that exist.

    A typo must fail loudly here. Allowed to fall through to a default it would
    silently become one kind while the test believed it had asked for the other,
    and the test would pass having measured the wrong half of the router's
    behaviour. The router's own counter separates the two, so a test that cannot
    tell them apart is not testing what it says it is.

    ``forwarding`` is rejected as well, even though it is a real state. Asking to
    "cut off, forwarding" is a contradiction, and the mistake it hides -- meaning
    to restore and calling cut_off -- would otherwise leave the store reachable
    while the test waited for a failure that never comes.
    """

    def __init__(self, kind: object) -> None:
        super().__init__(
            f"unknown cut-off kind {kind!r}; expected one of {', '.join(CUT_OFF_KINDS)} "
            f"(use restore() to let traffic through again)"
        )


@dataclass
class ProxyCounters:
    """What the proxy has done, for a test that wants to prove it was involved.

    ``accepted`` counts every connection the router opened. The three below it
    say what happened to each, and they add up to it only while no connection is
    still open -- a connection being pumped right now has been accepted and has
    not yet been counted anywhere else.

    ``closed_by_cut_off`` is the one worth reading. It is how a test proves the
    cut-off reached connections that already existed, rather than only the next
    one.
    """

    accepted: int = 0
    forwarded: int = 0
    held: int = 0
    closed_by_cut_off: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "accepted": self.accepted,
            "forwarded": self.forwarded,
            "held": self.held,
            "closed_by_cut_off": self.closed_by_cut_off,
        }


@dataclass(eq=False)
class _Live:
    """One connection the proxy is currently carrying.

    ``eq=False`` so each instance is hashed by identity and can live in a set.
    A dataclass is unhashable by default, and comparing two of these by value
    would be wrong anyway: two connections carrying the same sockets would be
    one entry, and the second to finish would remove the first's registration.

    Held so a cut-off can reach it. ``upstream`` is None while the connection is
    being held open with nothing behind it, which is what a ``timeout`` cut-off
    does to a connection opened while it is in force.
    """

    downstream: socket.socket
    upstream: socket.socket | None = None


class RespProxy:
    """A pass-through between the router and its RESP store, with an access gate.

    One instance per store. The router points at this proxy's address instead of
    the store's, and everything else about its configuration is unchanged.

    Thread-safe: the gate and the live-connection set are both under one lock,
    and every connection runs on its own thread.
    """

    def __init__(self, target_host: str, target_port: int, *, name: str = "primary") -> None:
        self.name = name
        self.target_host = target_host
        self.target_port = target_port
        self._lock = threading.Lock()
        self._state = FORWARDING
        self._live: set[_Live] = set()
        self._counters = ProxyCounters()

    # ── the gate ──────────────────────────────────────────────────────────────

    def state(self) -> str:
        with self._lock:
            return self._state

    def cut_off(self, kind: str) -> str:
        """Stop the router reaching the store, in one of the two ways.

        Acts on connections that already exist as well as the next one. See the
        module docstring for why that is not optional.
        """
        if kind not in CUT_OFF_KINDS:
            raise UnknownCutOffKind(kind)
        with self._lock:
            self._state = kind
            live = list(self._live)
            if kind == ERROR:
                self._counters.closed_by_cut_off += len(live)
        if kind == ERROR:
            for conn in live:
                _shutdown(conn.downstream)
                if conn.upstream is not None:
                    _shutdown(conn.upstream)
        return kind

    def restore(self) -> str:
        """Let traffic through again.

        Connections held open through a ``timeout`` cut-off resume pumping on
        their next poll. Connections closed by an ``error`` cut-off are gone --
        the client opens new ones, which is what a real client does.
        """
        with self._lock:
            self._state = FORWARDING
        return FORWARDING

    def counters(self) -> dict[str, int]:
        with self._lock:
            return self._counters.as_dict()

    def reset_counters(self) -> None:
        with self._lock:
            self._counters = ProxyCounters()

    def live_connections(self) -> int:
        with self._lock:
            return len(self._live)

    def as_dict(self) -> dict[str, object]:
        """Everything a test can ask about the proxy, in one reply."""
        with self._lock:
            return {
                "name": self.name,
                "state": self._state,
                "target": f"{self.target_host}:{self.target_port}",
                "live_connections": len(self._live),
                "counters": self._counters.as_dict(),
            }

    # ── the wire ──────────────────────────────────────────────────────────────

    def serve(self, downstream: socket.socket) -> None:
        """Carry one connection from the router. Runs on its own thread.

        Returns when the connection ends, whichever side ended it.
        """
        with self._lock:
            self._counters.accepted += 1
            state = self._state

        if state == ERROR:
            # Accepted and closed straight away. Not a refusal -- see the module
            # docstring, which says why the distinction matters to a test.
            with self._lock:
                self._counters.closed_by_cut_off += 1
            _shutdown(downstream)
            return

        entry = _Live(downstream=downstream)
        with self._lock:
            self._live.add(entry)
            self._counters.held += 1
        try:
            self._carry(entry)
        finally:
            with self._lock:
                self._live.discard(entry)
            _shutdown(downstream)
            if entry.upstream is not None:
                _shutdown(entry.upstream)

    def _carry(self, entry: _Live) -> None:
        """Move bytes both ways, opening the upstream connection when allowed.

        The upstream is opened lazily rather than on accept. A connection opened
        while a ``timeout`` cut-off is in force must not touch the store at all;
        opening it eagerly would leave a real connection to the store sitting
        there, and "the router cannot reach the store" would be untrue in the one
        way that matters.
        """
        entry.downstream.settimeout(_POLL_SECONDS)
        while True:
            state = self.state()

            if state == ERROR:
                return

            if state == TIMEOUT:
                # Hold the socket open and move nothing. The router's read waits
                # out its own deadline, which is the point.
                if _peer_gone(entry.downstream):
                    return
                continue

            if entry.upstream is None:
                upstream = _dial(self.target_host, self.target_port)
                if upstream is None:
                    # The store itself is unreachable. Nothing to proxy, and
                    # pretending otherwise would hide a broken deployment behind
                    # a test that reads it as a cut-off.
                    return
                upstream.settimeout(_POLL_SECONDS)
                with self._lock:
                    entry.upstream = upstream
                    self._counters.forwarded += 1

            if not self._pump_once(entry):
                return

    def _pump_once(self, entry: _Live) -> bool:
        """One poll of both directions. False when the connection is finished."""
        upstream = entry.upstream
        if upstream is None:
            return False
        for src, dst in ((entry.downstream, upstream), (upstream, entry.downstream)):
            try:
                chunk = src.recv(_CHUNK)
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return False
            if not chunk:
                return False
            # The gate can change while a read is in flight. Re-check before
            # delivering, so a cut-off does not let one last reply through.
            if self.state() != FORWARDING:
                return True
            try:
                dst.sendall(chunk)
            except OSError:
                return False
        return True


def _dial(host: str, port: int, timeout: float = 2.0) -> socket.socket | None:
    """Open a connection to the store, or None when it cannot be reached."""
    try:
        return socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return None


def _shutdown(conn: socket.socket) -> None:
    """Close a socket without caring that it may already be closed."""
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass


def _peer_gone(conn: socket.socket) -> bool:
    """True when the other side has closed, while we are holding and not reading.

    Used only by the held path. Without it a held connection whose client gave up
    would keep a thread spinning until the process ended.
    """
    try:
        chunk = conn.recv(_CHUNK, socket.MSG_PEEK)
    except (TimeoutError, socket.timeout):
        return False
    except OSError:
        return True
    return not chunk


class RespProxyRegistry:
    """The proxies this simulator is running, addressed by name.

    Named rather than numbered for the same reason the cache-sims are: a store is
    not a provider, so it has no pool slot to be numbered within, and a topology
    with more than one store has one per role.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proxies: dict[str, RespProxy] = {}

    def add(self, name: str, target_host: str, target_port: int) -> RespProxy:
        with self._lock:
            proxy = RespProxy(target_host, target_port, name=name)
            self._proxies[name] = proxy
            return proxy

    def get(self, name: str) -> RespProxy | None:
        with self._lock:
            return self._proxies.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._proxies)

    def restore_all(self) -> None:
        with self._lock:
            proxies = list(self._proxies.values())
        for proxy in proxies:
            proxy.restore()
