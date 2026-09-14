"""A tiny in-process RESP server, so the RESP tests need no Redis.

This repository installs nothing outside the standard library except gRPC, and
CI installs nothing else either. A test that needed a real Redis would skip
there while passing on a developer's machine, and a skipped test and a passing
one look the same in a green run.

It speaks only what ``RespStore`` sends: PING, SCAN, GET, TTL, FLUSHDB. Anything
else is answered with an error, loudly, so a command added to the reader without
being added here fails rather than being quietly ignored.

**It is a test double for the STORE, not a second implementation of one.** It
holds a dict. Expiry is a stored deadline that is checked on read. Nothing here
should grow features; if a test needs behaviour a real Redis has and this does
not, that test wants a real Redis.
"""

from __future__ import annotations

import socket
import socketserver
import threading
import time

_CRLF = b"\r\n"


class FakeRespStore:
    """A RESP server on a random free port, holding keys in a dict.

    Start it with ``start()`` and stop it with ``stop()``. ``port`` is the bound
    port, which is chosen by the operating system so two tests never collide.
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, float] = {}
        self.commands: list[list[str]] = []
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> "FakeRespStore":
        store = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                store._serve(self.request)

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = _Server(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> "FakeRespStore":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # ── what a test puts in it ────────────────────────────────────────────────

    def put(self, key: str, value: str, ttl: int | None = None) -> None:
        """Place a key, the way the ROUTER would have.

        Only a test uses this. The control API deliberately exposes no write, so
        nothing reachable from outside the simulator can plant an entry.
        """
        self.values[key] = value
        if ttl is None:
            # Redis drops an existing expiry when SET rewrites a key without
            # one. Leaving the old deadline behind would make a test's
            # "permanent" entry vanish partway through, and the reader would be
            # blamed for a store that had quietly expired it.
            self.expiries.pop(key, None)
        else:
            self.expiries[key] = time.monotonic() + ttl

    # ── the wire ──────────────────────────────────────────────────────────────

    def _serve(self, conn: socket.socket) -> None:
        buf = bytearray()
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while True:
                parsed = _take_command(buf)
                if parsed is None:
                    break
                parts, used = parsed
                del buf[:used]
                self.commands.append(parts)
                try:
                    conn.sendall(self._reply(parts))
                except OSError:
                    return

    def _reply(self, parts: list[str]) -> bytes:
        if not parts:
            return _err("empty command")
        name = parts[0].upper()
        if name == "PING":
            return b"+PONG\r\n"
        if name == "FLUSHDB":
            self.values.clear()
            self.expiries.clear()
            return b"+OK\r\n"
        if name == "GET":
            value = self.values.get(parts[1]) if len(parts) > 1 else None
            if value is None or self._expired(parts[1]):
                return b"$-1\r\n"
            return _bulk(value)
        if name == "TTL":
            key = parts[1] if len(parts) > 1 else ""
            if key not in self.values or self._expired(key):
                return b":-2\r\n"
            if key not in self.expiries:
                return b":-1\r\n"
            return f":{max(0, int(self.expiries[key] - time.monotonic()))}\r\n".encode()
        if name == "SCAN":
            return self._scan(parts)
        return _err(f"unknown command '{parts[0]}'")

    def _scan(self, parts: list[str]) -> bytes:
        pattern = "*"
        for i, part in enumerate(parts):
            if part.upper() == "MATCH" and i + 1 < len(parts):
                pattern = parts[i + 1]
        keys = [k for k in sorted(self.values) if not self._expired(k) and _glob(pattern, k)]
        # One round: cursor straight back to 0. A real server may page; the
        # reader handles that and this does not need to prove it does.
        body = [b"*2\r\n", _bulk("0"), f"*{len(keys)}\r\n".encode()]
        body += [_bulk(k) for k in keys]
        return b"".join(body)

    def _expired(self, key: str) -> bool:
        deadline = self.expiries.get(key)
        return deadline is not None and time.monotonic() >= deadline


def _bulk(text: str) -> bytes:
    raw = text.encode()
    return b"$" + str(len(raw)).encode() + _CRLF + raw + _CRLF


def _err(message: str) -> bytes:
    return b"-ERR " + message.encode() + _CRLF


def _glob(pattern: str, key: str) -> bool:
    """Match only the two shapes the reader ever sends: ``*`` and ``prefix*``."""
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return key.startswith(pattern[:-1])
    return key == pattern


def _take_command(buf: bytearray) -> tuple[list[str], int] | None:
    """Pull one complete RESP array off the buffer, or None when it is short.

    Returns the command's parts and how many bytes it used.
    """
    if not buf or buf[:1] != b"*":
        return None
    end = buf.find(_CRLF)
    if end == -1:
        return None
    count = int(buf[1:end])
    pos = end + 2
    parts: list[str] = []
    for _ in range(count):
        if buf[pos : pos + 1] != b"$":
            return None
        head = buf.find(_CRLF, pos)
        if head == -1:
            return None
        length = int(buf[pos + 1 : head])
        start = head + 2
        if len(buf) < start + length + 2:
            return None
        parts.append(bytes(buf[start : start + length]).decode())
        pos = start + length + 2
    return parts, pos
