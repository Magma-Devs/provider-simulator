"""
test_ws_protocol.py — RFC 6455 codec round-trip tests (no network) for ws_protocol.py.

The codec is foundational: bugs here would surface identically on the server and
the test client (both consume ws_protocol). Catching them in isolation here
keeps integration failures in test_simulator_ws.py interpretable.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from provider_simulator.listeners import ws_protocol


def test_handshake_accept_rfc6455_example():
    """RFC 6455 §1.3 worked example.

    Given the client key "dGhlIHNhbXBsZSBub25jZQ==", the server MUST reply
    with Sec-WebSocket-Accept "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=".
    """
    accept = ws_protocol.compute_accept("dGhlIHNhbXBsZSBub25jZQ==")
    assert accept == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_frame_opcodes_are_rfc6455_values():
    assert ws_protocol.OPCODE_CONTINUATION == 0x0
    assert ws_protocol.OPCODE_TEXT == 0x1
    assert ws_protocol.OPCODE_BINARY == 0x2
    assert ws_protocol.OPCODE_CLOSE == 0x8
    assert ws_protocol.OPCODE_PING == 0x9
    assert ws_protocol.OPCODE_PONG == 0xA


def test_frame_dataclass_roundtrip_fields():
    f = ws_protocol.Frame(fin=True, opcode=ws_protocol.OPCODE_TEXT, payload=b"hello")
    assert f.fin is True
    assert f.opcode == 0x1
    assert f.payload == b"hello"


def test_encode_text_frame_short_payload_unmasked():
    """Server frames are unmasked. 5-byte payload fits in 7-bit length."""
    out = ws_protocol.encode_frame(ws_protocol.OPCODE_TEXT, b"hello", mask=False)
    # FIN=1, RSV=0, opcode=0x1 → first byte = 0x81
    # MASK=0, length=5 → second byte = 0x05
    # No mask key, payload follows.
    assert out == b"\x81\x05hello"


def test_encode_pong_frame_zero_payload():
    out = ws_protocol.encode_frame(ws_protocol.OPCODE_PONG, b"", mask=False)
    # FIN=1, opcode=0xA → 0x8A. MASK=0, length=0 → 0x00.
    assert out == b"\x8a\x00"


def test_encode_text_frame_medium_payload_16bit_length():
    """Payload length 126..65535 uses the 2-byte extended length field."""
    payload = b"A" * 200
    out = ws_protocol.encode_frame(ws_protocol.OPCODE_TEXT, payload, mask=False)
    # 0x81 | 0x7E (126 → 16-bit length follows) | 0x00C8 (200 big-endian) | payload
    assert out[0] == 0x81
    assert out[1] == 0x7E
    assert out[2:4] == b"\x00\xc8"
    assert out[4:] == payload


def test_encode_text_frame_large_payload_64bit_length():
    """Payload length >= 65536 uses the 8-byte extended length field."""
    payload = b"B" * 70000
    out = ws_protocol.encode_frame(ws_protocol.OPCODE_TEXT, payload, mask=False)
    # 0x81 | 0x7F (127 → 64-bit length follows) | 0x0000000000011170 | payload
    assert out[0] == 0x81
    assert out[1] == 0x7F
    assert out[2:10] == b"\x00\x00\x00\x00\x00\x01\x11\x70"
    assert out[10:] == payload


def test_encode_client_frame_is_masked():
    """When mask=True the MASK bit is set and a 4-byte key is inserted before payload."""
    out = ws_protocol.encode_frame(ws_protocol.OPCODE_TEXT, b"hi", mask=True)
    assert out[0] == 0x81  # FIN=1, opcode=text
    assert out[1] & 0x80  # MASK bit set
    assert out[1] & 0x7F == 2  # length=2 in low 7 bits
    key = out[2:6]
    masked = out[6:]
    assert bytes(b ^ key[i % 4] for i, b in enumerate(masked)) == b"hi"


def _recv_from_bytes(buf: bytearray):
    """Build a recv-callable that returns n bytes from buf each call."""

    def recv(n: int) -> bytes:
        if n <= 0:
            return b""
        chunk = bytes(buf[:n])
        del buf[:n]
        return chunk

    return recv


def test_parse_masked_client_text_frame():
    """Client→server frames are masked. parse_frame unmasks transparently."""
    encoded = ws_protocol.encode_frame(ws_protocol.OPCODE_TEXT, b"ping", mask=True)
    recv = _recv_from_bytes(bytearray(encoded))
    frame = ws_protocol.parse_frame(recv)
    assert frame.fin is True
    assert frame.opcode == ws_protocol.OPCODE_TEXT
    assert frame.payload == b"ping"


def test_parse_unmasked_server_text_frame():
    """parse_frame also accepts unmasked frames (used by the client side of the codec)."""
    encoded = ws_protocol.encode_frame(ws_protocol.OPCODE_TEXT, b"pong", mask=False)
    recv = _recv_from_bytes(bytearray(encoded))
    frame = ws_protocol.parse_frame(recv)
    assert frame.payload == b"pong"


def test_parse_close_frame():
    encoded = ws_protocol.encode_frame(ws_protocol.OPCODE_CLOSE, b"", mask=True)
    recv = _recv_from_bytes(bytearray(encoded))
    frame = ws_protocol.parse_frame(recv)
    assert frame.opcode == ws_protocol.OPCODE_CLOSE


def test_parse_medium_length_frame():
    payload = b"X" * 300
    encoded = ws_protocol.encode_frame(ws_protocol.OPCODE_TEXT, payload, mask=True)
    recv = _recv_from_bytes(bytearray(encoded))
    frame = ws_protocol.parse_frame(recv)
    assert frame.payload == payload


def test_parse_fragmented_text_frame():
    """A TEXT frame split across two fragments must be reassembled into one Frame."""
    # First fragment: FIN=0, opcode=TEXT, 3-byte payload "hel"
    frag1 = b"\x01\x03hel"
    # Second fragment: FIN=1, opcode=CONTINUATION, 2-byte payload "lo"
    frag2 = b"\x80\x02lo"
    recv = _recv_from_bytes(bytearray(frag1 + frag2))
    frame = ws_protocol.parse_frame(recv)
    assert frame.opcode == ws_protocol.OPCODE_TEXT
    assert frame.payload == b"hello"
    assert frame.fin is True


def test_build_handshake_response_format():
    """build_handshake_response returns a complete HTTP 101 response."""
    body = ws_protocol.build_handshake_response("dGhlIHNhbXBsZSBub25jZQ==")
    # Must start with the 101 status line
    assert body.startswith(b"HTTP/1.1 101 Switching Protocols\r\n")
    # Must carry the canonical headers
    assert b"Upgrade: websocket\r\n" in body
    assert b"Connection: Upgrade\r\n" in body
    assert b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n" in body
    # Must end with an empty line (\r\n\r\n) terminating the headers
    assert body.endswith(b"\r\n\r\n")


import stubs_ws


def test_subscribe_methods_table_has_four_chains():
    """All four named subscribe methods from the spec must be present."""
    assert set(stubs_ws.SUBSCRIBE_METHODS.keys()) == {
        "eth_subscribe",
        "subscribe",
        "accountSubscribe",
        "logsSubscribe",
    }


def test_build_event_frame_eth_envelope():
    out = stubs_ws.build_event_frame("eth_subscription", "0xdead", {"number": "0x1"})
    assert out == {
        "jsonrpc": "2.0",
        "method": "eth_subscription",
        "params": {"subscription": "0xdead", "result": {"number": "0x1"}},
    }


def test_build_event_frame_solana_envelope_int_subscription():
    """Solana envelopes use integer subscription ids; we convert from hex."""
    out = stubs_ws.build_event_frame("solana_logs", "0x10", {"value": 1})
    assert out["params"]["subscription"] == 16
