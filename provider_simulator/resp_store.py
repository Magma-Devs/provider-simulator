"""RESP store reader — opens the router's cache and reads what is in it.

Every other cache this project tests is closed. A test infers what was stored
from what was served, because there is no way in. A Redis or Valkey answers
``SCAN``, so here a test can read the keys, the values and the remaining
lifetimes directly.

That is the largest single thing this module buys, and it is new in kind: "what
did the router store" becomes a question with an answer, separately from "what
did the router serve".

**It reads the store directly, never through the proxy.** That is deliberate and
it is what lets a test read an entry WHILE the router is cut off from the store,
and so prove the entry was never lost rather than assume it.

There is no write
-----------------
``SET`` is absent, and its absence is the design rather than an omission. A test
that can plant an entry will plant one instead of making the router store it,
and then it is measuring the store rather than the router. The only command here
that changes anything is ``FLUSHDB``, which empties the store so a test can start
from nothing -- it cannot put a chosen entry in.

Why a hand-written client
-------------------------
This repository installs nothing outside the standard library except gRPC. Adding
``redis-py`` for five commands -- PING, SCAN, GET, TTL and FLUSHDB -- would be
the largest dependency here for the smallest surface. RESP2 is a short protocol
and only the replies those five produce need decoding.

What the keys look like
-----------------------
The router prefixes every key with its configured ``key-prefix``, ``sr`` by
default. Two shapes exist::

    sr:chaintip:ETH1                  the chain tip this router tracks
    sr:rel:f:ETH1:<hash>:<block>      a cached answer; ``f`` marks finalized

**Ask for a finalized block when you intend to look.** A finalized entry lives
about an hour. An entry for ``latest`` lives under a second and is gone before a
test can read it, which reads as an empty store and is not.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

# RESP2's five reply types, by their leading byte.
_SIMPLE = b"+"
_ERROR = b"-"
_INTEGER = b":"
_BULK = b"$"
_ARRAY = b"*"

_CRLF = b"\r\n"

# How many keys one SCAN round asks for. A hint, not a limit -- the server may
# return more or fewer, which is why the cursor loop below does not assume.
_SCAN_COUNT = 500

# A guard on the cursor loop. SCAN terminates when the cursor returns to 0, and a
# store being written to while it is scanned can legitimately take several
# rounds. This bounds a loop that would otherwise be unbounded if a reply were
# ever malformed.
_MAX_SCAN_ROUNDS = 1000

# The two TTL answers that are not durations. The store uses them as sentinels:
# -1 for a key that never expires, -2 for a key that is not there at all.
TTL_NO_EXPIRY = -1
TTL_NO_SUCH_KEY = -2


class RespStoreError(RuntimeError):
    """The store could not be reached, or answered something undecodable.

    Raised rather than returning an empty result, and that choice is the whole
    point. A reader that answers "no keys" when it could not connect passes every
    test written on it: "the store is empty" and "I never asked" produce the same
    answer, and only one of them is a measurement.
    """


@dataclass
class Entry:
    """One key in the store, with its value and how long it has left.

    ``ttl`` follows the server's own convention, which is worth knowing because
    two of its values are not durations: ``-1`` means the key has no expiry, and
    ``-2`` means the key was gone by the time its lifetime was asked for.
    """

    key: str
    value: str
    ttl: int

    def as_dict(self) -> dict[str, object]:
        return {"key": self.key, "value": self.value, "ttl": self.ttl}


class RespStore:
    """A read-only client for one RESP store.

    One connection per call rather than a pool. These calls happen once per test,
    not once per request, so a pool would be complexity nothing here needs.
    """

    def __init__(self, host: str, port: int, *, timeout: float = 2.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def target(self) -> str:
        return f"{self.host}:{self.port}"

    # ── the four things the control service exposes ───────────────────────────

    def ping(self) -> bool:
        """True when the store answers. Used to say whether it is reachable."""
        try:
            return self._command(b"PING") == "PONG"
        except RespStoreError:
            return False

    def scan(self, pattern: str = "*") -> list[str]:
        """Every key matching the pattern, using SCAN rather than KEYS.

        ``KEYS`` blocks the server for the whole scan. ``SCAN`` does not, and the
        habit matters more than the size of our store: a command that is safe on
        a test store and dangerous on a customer's is the wrong one to write down
        in a repository people copy from.
        """
        cursor = "0"
        found: list[str] = []
        seen: set[str] = set()
        for _ in range(_MAX_SCAN_ROUNDS):
            reply = self._command(
                b"SCAN", cursor.encode(), b"MATCH", pattern.encode(), b"COUNT", str(_SCAN_COUNT).encode()
            )
            if not isinstance(reply, list) or len(reply) != 2:
                raise RespStoreError(f"SCAN answered {reply!r}, which is not a cursor and a key list")
            cursor = _as_text(reply[0])
            batch = reply[1]
            if not isinstance(batch, list):
                raise RespStoreError(f"SCAN answered a {type(batch).__name__} where a key list belongs")
            for raw in batch:
                key = _as_text(raw)
                # SCAN can return the same key twice across rounds. That is the
                # server's documented behaviour, not a fault.
                if key not in seen:
                    seen.add(key)
                    found.append(key)
            if cursor == "0":
                return sorted(found)
        raise RespStoreError(f"SCAN did not finish in {_MAX_SCAN_ROUNDS} rounds")

    def get(self, key: str) -> str | None:
        """The value at a key, or None when the key is not there."""
        reply = self._command(b"GET", key.encode())
        if reply is None:
            return None
        return _as_text(reply)

    def ttl(self, key: str) -> int:
        """Seconds left on a key. -1 means no expiry, -2 means no key."""
        reply = self._command(b"TTL", key.encode())
        if not isinstance(reply, int):
            raise RespStoreError(f"TTL answered {reply!r} where a number belongs")
        return reply

    def entries(self, pattern: str = "*") -> list[Entry]:
        """Every matching key with its value and remaining lifetime.

        A key can expire between the scan and the read of its value. That is a
        real store behaving normally, so such a key is dropped rather than
        reported with a missing value.

        It can also expire between the value read and the lifetime read, which
        is the same race one step later and needs the same answer. The store
        reports that as ``ttl == -2``, "no such key", so an entry that comes back
        holding a value and a -2 is a key that left while we were looking at it.
        Reporting it would put a key in the answer that is not in the store, and
        a caller reading the count would be told the router stored something it
        no longer has.
        """
        out: list[Entry] = []
        for key in self.scan(pattern):
            value = self.get(key)
            if value is None:
                continue
            ttl = self.ttl(key)
            if ttl == TTL_NO_SUCH_KEY:
                continue
            out.append(Entry(key=key, value=value, ttl=ttl))
        return out

    def flushdb(self) -> None:
        """Empty the store, so a test starts from nothing.

        The one command here that changes the store. It can only remove; it
        cannot put a chosen entry in, which is what keeps a test honest about
        where its entry came from.
        """
        self._command(b"FLUSHDB")

    # ── the wire ──────────────────────────────────────────────────────────────

    def _command(self, *parts: bytes) -> object:
        """Send one command and decode one reply."""
        try:
            conn = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise RespStoreError(f"cannot reach the RESP store at {self.target()}: {exc}") from exc
        try:
            conn.settimeout(self.timeout)
            conn.sendall(_encode(parts))
            return _read_reply(_Reader(conn))
        except OSError as exc:
            raise RespStoreError(f"the RESP store at {self.target()} stopped answering: {exc}") from exc
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _encode(parts: tuple[bytes, ...]) -> bytes:
    """Build one RESP array of bulk strings — the shape every command takes."""
    out = [b"*", str(len(parts)).encode(), _CRLF]
    for part in parts:
        out += [b"$", str(len(part)).encode(), _CRLF, part, _CRLF]
    return b"".join(out)


class _Reader:
    """Buffered line/byte reads over one socket.

    A hand-rolled buffer rather than ``socket.makefile`` so a short read cannot
    silently truncate a bulk string: every read here either returns exactly what
    was asked for or raises.
    """

    def __init__(self, conn: socket.socket) -> None:
        self._conn = conn
        self._buf = bytearray()

    def line(self) -> bytes:
        while _CRLF not in self._buf:
            chunk = self._conn.recv(65536)
            if not chunk:
                raise RespStoreError("the store closed the connection mid-reply")
            self._buf += chunk
        cut = self._buf.index(_CRLF)
        line = bytes(self._buf[:cut])
        del self._buf[: cut + 2]
        return line

    def exactly(self, count: int) -> bytes:
        while len(self._buf) < count + 2:
            chunk = self._conn.recv(65536)
            if not chunk:
                raise RespStoreError("the store closed the connection mid-reply")
            self._buf += chunk
        body = bytes(self._buf[:count])
        del self._buf[: count + 2]
        return body


def _read_reply(reader: _Reader) -> object:
    """Decode one RESP2 reply into a Python value."""
    line = reader.line()
    if not line:
        raise RespStoreError("the store sent an empty reply line")
    kind, rest = line[:1], line[1:]

    if kind == _SIMPLE:
        return rest.decode()
    if kind == _ERROR:
        raise RespStoreError(f"the store refused the command: {rest.decode()}")
    if kind == _INTEGER:
        return int(rest)
    if kind == _BULK:
        length = int(rest)
        if length == -1:
            return None
        return reader.exactly(length)
    if kind == _ARRAY:
        count = int(rest)
        if count == -1:
            return None
        return [_read_reply(reader) for _ in range(count)]
    raise RespStoreError(f"the store sent a reply this reader does not know: {line!r}")


def _as_text(raw: object) -> str:
    """Render a decoded reply element as text.

    Values in the store are bytes the router wrote. They are JSON in practice,
    so decoding them as UTF-8 is right; anything that is not is shown rather than
    dropped, because a test looking at a surprising value needs to see it.
    """
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)
