"""
ws_client.py — stdlib sync WebSocket test client for tests/test_simulator_ws.py.

Reuses ws_protocol for the codec; only adds the client-side handshake-send
and the client-side mandatory masking on outbound frames. Intended for tests
only — not a general-purpose WS client.

Usage:
    with WsClient("127.0.0.1", 28557, "/ws") as c:
        c.send_json({"jsonrpc":"2.0","method":"eth_blockNumber","id":1})
        reply = c.recv_json()
        assert reply["result"].startswith("0x")
"""

from __future__ import annotations

import base64
import json
import os
import socket
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from provider_simulator.listeners import ws_protocol


class WsClient:
    """Minimal blocking WebSocket client used by the simulator's WS tests."""

    def __init__(self, host: str, port: int, path: str = "/ws", *, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.path = path
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None

    def __enter__(self) -> "WsClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        except Exception:
            pass

    def connect(self) -> None:
        """Open a TCP socket and complete the WS upgrade handshake."""
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        ).encode("ascii")
        self.sock.sendall(req)
        # Read until \r\n\r\n
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("server closed during handshake")
            buf += chunk
        status_line = buf.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise ConnectionError(f"handshake refused: {status_line!r}")
        expected = ws_protocol.compute_accept(key)
        if expected.encode("ascii") not in buf:
            raise ConnectionError("bad Sec-WebSocket-Accept")

    def send_json(self, obj: Any) -> None:
        """Send one TEXT frame with the JSON-serialised object as payload."""
        if self.sock is None:
            raise RuntimeError("not connected")
        payload = json.dumps(obj).encode("utf-8")
        self.sock.sendall(ws_protocol.encode_frame(ws_protocol.OPCODE_TEXT, payload, mask=True))

    def recv_json(self, timeout: Optional[float] = None) -> Any:
        """Receive one TEXT frame and JSON-parse the payload.

        Raises socket.timeout if no frame arrives within the configured timeout.
        """
        if self.sock is None:
            raise RuntimeError("not connected")
        if timeout is not None:
            self.sock.settimeout(timeout)
        try:
            frame = ws_protocol.parse_frame(self.sock.recv)
        finally:
            if timeout is not None:
                self.sock.settimeout(self.timeout)
        return json.loads(frame.payload.decode("utf-8"))

    def recv_raw(self, timeout: Optional[float] = None) -> ws_protocol.Frame:
        """Receive one frame as raw bytes (for testing non-TEXT or corrupted output)."""
        if self.sock is None:
            raise RuntimeError("not connected")
        if timeout is not None:
            self.sock.settimeout(timeout)
        try:
            return ws_protocol.parse_frame(self.sock.recv)
        finally:
            if timeout is not None:
                self.sock.settimeout(self.timeout)

    def close(self) -> None:
        """Send a CLOSE frame and close the underlying socket."""
        if self.sock is None:
            return
        try:
            self.sock.sendall(ws_protocol.encode_frame(ws_protocol.OPCODE_CLOSE, b"", mask=True))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None
