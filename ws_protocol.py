"""
ws_protocol.py — RFC 6455 codec for the WebSocket transport (MAG-1801).

Pure-function module: handshake hash, Frame dataclass, encode_frame, parse_frame.
Reused by the server (handlers_ws.py) and the test client (tests/ws_client.py)
so codec bugs surface in tests/test_ws_protocol.py rather than masquerading as
integration failures.

No dependencies outside stdlib. RFC 6455 §1.3 (handshake hash) and §5.2 (frame
format) are the only specs you need to read alongside this file.
"""

from __future__ import annotations

import base64
import hashlib

# RFC 6455 §1.3 — server MUST concatenate this fixed GUID to the client's
# Sec-WebSocket-Key, SHA-1 the result, and base64-encode the digest.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def compute_accept(client_key: str) -> str:
    """Compute the Sec-WebSocket-Accept header value for a given client key.

    Args:
        client_key: The Sec-WebSocket-Key header value sent by the client.
                    Already base64-encoded; we never decode it — we just
                    concatenate the GUID string and hash the bytes.

    Returns:
        Base64-encoded SHA-1 digest, ASCII string suitable for direct use
        as the Sec-WebSocket-Accept response header value.
    """
    raw = (client_key + _WS_GUID).encode("ascii")
    # SHA-1 is mandated by the RFC for handshake identity, not used as a
    # security primitive. usedforsecurity=False keeps this working on
    # FIPS-mode hosts where SHA-1 is otherwise blocked.
    digest = hashlib.sha1(raw, usedforsecurity=False).digest()
    return base64.b64encode(digest).decode("ascii")


from dataclasses import dataclass


# RFC 6455 §5.2 opcode field values. We only emit TEXT / PONG ourselves; we
# accept TEXT / CLOSE / PING / CONTINUATION from clients.
OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


@dataclass
class Frame:
    """A fully-assembled WebSocket message.

    The codec exposes assembled messages to callers — fragmentation
    (continuation frames) is hidden by parse_frame, which buffers the
    continuation chain and returns one Frame with the concatenated payload.
    """
    fin: bool
    opcode: int
    payload: bytes


import os
import struct


def encode_frame(opcode: int, payload: bytes, *, mask: bool = False) -> bytes:
    """Encode one complete WebSocket frame (always FIN=1).

    Args:
        opcode:  One of the OPCODE_* constants.
        payload: Bytes payload. Length determines 7/16/64-bit length encoding.
        mask:    True for client→server frames (test client). False for
                 server→client frames (the simulator's writer thread). RFC 6455
                 §5.1 — clients MUST mask, servers MUST NOT.

    Returns:
        Complete frame bytes, ready for ``socket.sendall``.
    """
    if opcode < 0 or opcode > 0xF:
        raise ValueError(f"opcode out of range: {opcode}")

    # First byte: FIN=1, RSV1-3=0, opcode in low nibble.
    b1 = 0x80 | (opcode & 0x0F)

    length = len(payload)
    if length < 126:
        len_field = bytes([(0x80 if mask else 0x00) | length])
    elif length < 1 << 16:
        len_field = bytes([(0x80 if mask else 0x00) | 126]) + struct.pack(">H", length)
    else:
        len_field = bytes([(0x80 if mask else 0x00) | 127]) + struct.pack(">Q", length)

    if mask:
        key = os.urandom(4)
        masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        return bytes([b1]) + len_field + key + masked
    return bytes([b1]) + len_field + payload


class FrameParseError(Exception):
    """Raised when the recv callable returns fewer bytes than required."""


def _recv_exact(recv, n: int) -> bytes:
    """Read exactly n bytes from a recv callable; raise if the peer closed early."""
    buf = b""
    while len(buf) < n:
        chunk = recv(n - len(buf))
        if not chunk:
            raise FrameParseError(f"connection closed; got {len(buf)} of {n} bytes")
        buf += chunk
    return buf


def parse_frame(recv) -> Frame:
    """Parse one WebSocket message from a recv callable.

    Handles RFC 6455 §5.4 fragmentation transparently: continuation frames are
    concatenated until FIN=1 is seen, then one Frame is returned with the
    assembled payload. The returned Frame's opcode is the opcode of the FIRST
    frame in the chain (continuation frames inherit it).

    Args:
        recv: A callable taking an int (max bytes to read) and returning bytes.
              Typically ``self.connection.recv`` from a BaseHTTPRequestHandler.

    Returns:
        Frame(fin=True, opcode=..., payload=concatenated).

    Raises:
        FrameParseError: if the peer closes before a complete frame arrives.
    """
    assembled = bytearray()
    first_opcode = None

    while True:
        header = _recv_exact(recv, 2)
        fin = (header[0] & 0x80) != 0
        opcode = header[0] & 0x0F
        masked = (header[1] & 0x80) != 0
        length = header[1] & 0x7F

        if length == 126:
            length = struct.unpack(">H", _recv_exact(recv, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", _recv_exact(recv, 8))[0]

        mask_key = _recv_exact(recv, 4) if masked else None
        payload = _recv_exact(recv, length) if length else b""
        if masked and payload:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        if first_opcode is None:
            first_opcode = opcode
        # Control frames (opcode >= 0x8) cannot be fragmented per RFC 6455 §5.5;
        # always FIN=1 in practice. Treat them as standalone messages.
        if opcode >= 0x8:
            return Frame(fin=fin, opcode=opcode, payload=payload)

        assembled.extend(payload)
        if fin:
            return Frame(fin=True, opcode=first_opcode, payload=bytes(assembled))
        # else: keep reading continuation frames


def build_handshake_response(client_key: str) -> bytes:
    """Build the full HTTP 101 response bytes for a successful WS upgrade.

    Caller is responsible for writing this to the socket via sendall(). After
    sendall() returns, the connection is "promoted" — all subsequent traffic
    is framed per RFC 6455.

    Args:
        client_key: The Sec-WebSocket-Key request header value.

    Returns:
        Bytes ready for sendall(). Includes the terminating CRLF CRLF.
    """
    accept = compute_accept(client_key)
    return (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + accept.encode("ascii") + b"\r\n"
        b"\r\n"
    )
