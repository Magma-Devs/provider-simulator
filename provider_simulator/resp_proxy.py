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

A client can abandon a connection without closing it
----------------------------------------------------
Every exit from the carry loop needs one side to close, to error, or the gate to
shut. None of those happens when a client simply stops using a connection and
leaves it open, and the router does exactly that to every connection whose
operation timed out while it could not reach the store.

Measured on the local cluster: while a ``timeout`` cut-off was in force the
carried count climbed by two every ten seconds, and after the gate was restored
it never fell again -- still there many minutes later, while fresh connections
came and went normally. Inside the pod those sockets read ESTABLISHED on both
ends, not CLOSE_WAIT, so there was no close to be missed. The peer was alive and
was never going to speak again.

So a connection that carries no bytes for ``idle_seconds`` is given up. A real
Redis has ``timeout`` for the same reason, which makes the proxy more faithful
to the thing it stands in for rather than less.

**The idle clock does not run while the gate is cut off**, and that is not a
detail. Holding a connection open and moving nothing is the whole definition of
the ``timeout`` kind. Reclaiming one would end it, the router would read a
connection that CLOSED rather than one that HUNG, and the two kinds it counts
separately would collapse into one.
"""

from __future__ import annotations

import select
import socket
import threading
import time
from dataclasses import dataclass, field

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

# How long ONE delivery may take. Separate from the poll interval on purpose, and
# the two were the same value until it was measured: a socket carries one
# timeout, so the poll interval was also the deadline for sendall, and a reply
# that could not be written in 50 milliseconds raised and the connection was torn
# down. A 4 MB cached reply arrived truncated at about 540 KB, and the cut moved
# when the interval moved, which is what proved the cause.
#
# A cache reply can be large -- a full block, or an eth_getLogs page -- so the
# delivery deadline has to be a delivery deadline rather than a liveness knob.
_SEND_TIMEOUT_SECONDS = 30.0

# How long a carried connection may move no bytes, while the gate is OPEN, before
# the proxy gives up on it. Zero disables the reclaimer entirely.
#
# Why this number. It bounds what a cut-off leaves behind to about two minutes
# rather than the life of the process, which is what it cost before this existed,
# and it is twelve times the router's own ten-second store ping
# (``respCacheHealthInterval`` in the smart-router source) so the connection
# carrying that ping is never near it.
#
# **A pooled connection the client is holding in reserve CAN reach it**, and that
# is not a defect to be designed around. It is exactly what a real Redis with
# ``timeout`` set does to an idle client, and a client meets it by opening
# another one. Saying so here because the opposite reading -- that the limit is
# chosen so no wanted connection is ever taken -- is tempting and wrong.
#
# It is deliberately not tight, because the two mistakes do not cost the same.
# Carrying a connection nobody wants wastes a thread. Taking one somebody does
# makes the router's store drop it mid-run, and that arrives in a test as
# behaviour of the router.
RESP_PROXY_IDLE_SECONDS = 120.0

# Linux signals "the peer closed its end" with POLLRDHUP, and only when asked.
# macOS has no such flag and reports POLLHUP instead, so this is 0 there and the
# POLLHUP branch does the work. Both are needed; neither alone is portable.
_POLLRDHUP = getattr(select, "POLLRDHUP", 0)


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

    ``accepted`` counts every connection the router opened. ``carried`` counts
    the ones the gate let through to be carried, ``forwarded`` the ones that
    went on to open a connection to the store, and ``closed_by_cut_off`` the
    ones a cut-off ended.

    **They do not partition ``accepted``.** One ordinary connection increments
    three of them. Do not write a test that adds them up.

    ``closed_by_cut_off`` is the one worth reading. It is how a test proves the
    cut-off reached connections that already existed, rather than only the next
    one.

    ``closed_when_idle`` counts the ones the proxy gave up on because they
    carried no bytes for ``idle_seconds`` while the gate was open. A climbing
    number is not a fault -- it is the reclaimer doing its job on connections a
    client abandoned without closing. It climbing during NORMAL traffic would be,
    because it would mean connections somebody still wanted were being taken.

    **All five are cumulative and none is ever decremented**, so none of them is
    a count of what is happening right now. ``live_connections()`` answers that.
    An earlier version called ``carried`` "held", which read as a live state and
    was not one -- it counted every connection the proxy took on, including ones
    it went on to forward normally.
    """

    accepted: int = 0
    forwarded: int = 0
    carried: int = 0
    closed_by_cut_off: int = 0
    closed_when_idle: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "accepted": self.accepted,
            "forwarded": self.forwarded,
            "carried": self.carried,
            "closed_by_cut_off": self.closed_by_cut_off,
            "closed_when_idle": self.closed_when_idle,
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

    ``last_active`` is when this connection last moved a byte, and it is what
    separates a connection somebody is still using from one that was abandoned
    without being closed. Only this connection's own thread reads or writes it,
    so it needs no lock -- and keeping it off the one lock matters, because every
    carried connection already takes that lock many times a second just to read
    the gate.
    """

    downstream: socket.socket
    upstream: socket.socket | None = None
    last_active: float = field(default_factory=time.monotonic)


class RespProxy:
    """A pass-through between the router and its RESP store, with an access gate.

    One instance per store. The router points at this proxy's address instead of
    the store's, and everything else about its configuration is unchanged.

    Thread-safe: the gate and the live-connection set are both under one lock,
    and every connection runs on its own thread.
    """

    def __init__(
        self,
        target_host: str,
        target_port: int,
        *,
        name: str = "primary",
        idle_seconds: float = RESP_PROXY_IDLE_SECONDS,
    ) -> None:
        self.name = name
        self.target_host = target_host
        self.target_port = target_port
        self._idle_seconds = idle_seconds
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
            self._counters.carried += 1
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
                #
                # Stamping the clock here is what stops the idle reclaimer from
                # running while the gate is shut. Moving no bytes is this kind's
                # definition, so a reclaim would end the connection, the router
                # would read a close rather than a hang, and the two kinds it
                # counts separately would become one.
                entry.last_active = time.monotonic()
                if _peer_gone(entry.downstream):
                    return
                # The wait is not optional, and leaving it out is a real fault
                # rather than a tidiness one. ``_peer_gone`` blocks for the poll
                # interval only while nothing is pending. The normal case here
                # is the opposite: the router has already sent a request and is
                # waiting for a reply, so the socket is readable, the peek
                # returns at once, and this loop would spin on bytes it is
                # deliberately not reading -- a whole core per held connection.
                time.sleep(_POLL_SECONDS)
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

            if self._idle_expired(entry):
                # The client is still there and has stopped speaking. Nothing
                # will ever close this, so the proxy does.
                #
                # **Read the gate again, and under the same lock as the counter.**
                # The state at the top of this iteration is already stale: a whole
                # pump has run since, which is two poll intervals and longer under
                # contention. A cut-off landing in that window would otherwise be
                # answered with a CLOSE by the iteration already in flight, and a
                # test that staged a hang would measure an error. Proven rather
                # than argued: with the re-read removed, a connection one poll
                # short of its limit was reclaimed after ``cut_off(TIMEOUT)``
                # returned in 40 trials out of 40.
                with self._lock:
                    reclaim = self._state == FORWARDING
                    if reclaim:
                        self._counters.closed_when_idle += 1
                if reclaim:
                    return
                # The gate shut under us. Go round again and let the branch that
                # owns this kind deal with it.
                continue

    def _idle_expired(self, entry: _Live) -> bool:
        """True when this connection has carried no bytes for the idle limit.

        Says nothing about the gate. The caller re-reads that before acting,
        because the state it read at the top of the iteration can be stale by
        the time this answers.
        """
        if self._idle_seconds <= 0:
            return False
        return time.monotonic() - entry.last_active > self._idle_seconds

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
            if not self._send_if_forwarding(dst, chunk):
                return False
            # Bytes moved, so this connection is in use. Stamped AFTER the
            # delivery rather than before it: a single reply may take up to
            # ``_SEND_TIMEOUT_SECONDS`` to write, so a stamp taken first is
            # already that stale when the write returns. Written without the
            # lock on purpose -- only this connection's own thread touches it,
            # and the one lock is already the busiest thing here.
            entry.last_active = time.monotonic()
        return True

    def _send_if_forwarding(self, dst: socket.socket, chunk: bytes) -> bool:
        """Deliver a chunk, but only while the gate is open.

        The check and the write are under ONE hold of the lock. Two steps let a
        cut-off land between them, and one last reply would cross a cut-off that
        promises to move no bytes -- which is exactly the promise a test reads.

        Holding the lock across ``sendall`` makes a concurrent ``cut_off`` wait
        for the write. That is the right way round: the writes here are single
        cache replies, and a cut-off that returned while a write was still in
        flight would be telling the caller something untrue.

        False means the connection is finished. A closed gate is not a failure --
        the bytes are dropped and the connection stays.
        """
        with self._lock:
            if self._state != FORWARDING:
                return True
            try:
                # The socket's own timeout is the poll interval, which is far too
                # short to write a large reply. Raise it for the write and put it
                # back, so liveness stays responsive and delivery gets a real
                # deadline.
                dst.settimeout(_SEND_TIMEOUT_SECONDS)
                dst.sendall(chunk)
            except OSError:
                return False
            finally:
                try:
                    dst.settimeout(_POLL_SECONDS)
                except OSError:
                    pass
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
    keeps its thread and its file descriptor until the gate is restored or the
    process ends.

    **A peek alone cannot answer this, and the first version of this function got
    it exactly backwards.** It peeked and returned "gone" only for an empty read.
    A held connection normally has an unread request sitting in it -- that is the
    whole situation the held path creates -- so the peek returned those same bytes
    for ever and the answer was always "still here". The guard was blind in the
    only case it ever saw. Measured: a client that sent a request and then closed
    left one connection live indefinitely, while a client that sent nothing was
    released correctly.

    So ask the operating system whether the socket has hung up, which it can
    answer with bytes still unread, and keep the peek for the no-data case.

    **The two operating systems answer that differently, and CI found it.** On
    macOS a full close raises POLLHUP even with unread data waiting. Linux does
    not: with data still readable it reports POLLIN, and signals the peer's close
    through POLLRDHUP, which has to be asked for. So the first version of this
    passed on a developer's machine and failed on the Linux runner, on exactly
    the test written for it. POLLRDHUP does not exist on macOS, hence the
    getattr rather than a direct reference.
    """
    hung_up = select.POLLHUP | select.POLLERR | select.POLLNVAL | _POLLRDHUP
    try:
        poller = select.poll()
        poller.register(conn, select.POLLIN | hung_up)
        for _fd, event in poller.poll(0):
            if event & hung_up:
                return True
            if event & select.POLLIN:
                # Readable. Empty means end-of-file; anything else is the request
                # this path is deliberately not reading.
                return not conn.recv(_CHUNK, socket.MSG_PEEK)
    except (TimeoutError, socket.timeout):
        return False
    except (OSError, ValueError):
        return True
    return False


class RespProxyRegistry:
    """The proxies this simulator is running, addressed by name.

    Named rather than numbered for the same reason the cache-sims are: a store is
    not a provider, so it has no pool slot to be numbered within, and a topology
    with more than one store has one per role.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proxies: dict[str, RespProxy] = {}

    def add(
        self,
        name: str,
        target_host: str,
        target_port: int,
        *,
        idle_seconds: float = RESP_PROXY_IDLE_SECONDS,
    ) -> RespProxy:
        """Register a proxy. ``idle_seconds`` is carried through deliberately.

        Without it this is the only route a deployed simulator has to a proxy, so
        the shipped value would be the one value no test could ever set, and the
        off switch would be unreachable from anywhere real.
        """
        with self._lock:
            proxy = RespProxy(target_host, target_port, name=name, idle_seconds=idle_seconds)
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
