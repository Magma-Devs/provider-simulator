"""A reply that starts, holds, and then finishes.

``pause_at`` / ``pause_ms`` hold a reply part way through and then complete it.
The Content-Length stays honest, so the client keeps reading and receives the
whole body late. That is the opposite of ``drop_at``, which announces a size it
never delivers and closes the connection.

The reason the field exists: a router with a per-attempt window needs a node
whose answer BEGINS inside the window and ENDS outside it. Neither existing
fault produces that. ``latency_ms`` answers late in one piece, ``hang`` never
answers, and ``drop_connection`` answers partly and then gives up.

``mode`` stays ``success`` on purpose. The reply is correct; only its delivery
is split. So a pause composes with a success the same way corruption does.
"""

import json
import socket
import time

import pytest

from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners import JsonRpcListener, RawRequest
from provider_simulator.topology import port_of

from .conftest import CONTROL_URL  # noqa: F401  (imported for symmetry with siblings)

HTTP = Endpoint("jsonrpc", "http", 18545)
WS = Endpoint("jsonrpc", "ws", 18557)

_P1_PORT = port_of("eth-sim", "1")


# ── plan level: no socket, so these run with the unit tests ───────────────────


def _listener(endpoint=HTTP):
    provider = Pool(name="eth-sim", chain="eth").add_provider("1", [HTTP, WS])
    return JsonRpcListener(provider, endpoint), provider


def _serve(listener, method="eth_blockNumber"):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method}).encode()
    return listener.serve(RawRequest(body=body, headers={}))


def test_pause_reaches_the_plan_without_changing_the_action():
    """A pause is carried on the plan and the reply stays a normal success.

    If the action or the status moved, the pause would be a fault rather than a
    slow delivery, and every assertion built on "the router gets a correct
    answer, late" would be measuring something else.
    """
    listener, provider = _listener()
    provider.scenario.update({"pause_at": "mid_body", "pause_ms": 250})
    res = _serve(listener)
    assert res.action == "respond"
    assert res.status == 200
    assert res.pause_at == "mid_body"
    assert res.pause_ms == 250


def test_a_pause_does_not_change_the_body():
    """The body is byte-identical with and without a pause.

    This is the claim that makes ``mode: success`` correct. A pause changes how
    a body reaches the wire, never what it says.
    """
    listener, provider = _listener()
    clean = _serve(listener).body
    provider.scenario.update({"pause_at": "mid_body", "pause_ms": 250})
    paused = _serve(listener).body
    assert paused == clean


def test_no_pause_configured_leaves_the_plan_alone():
    """The control for the two tests above: absent the setting, no pause."""
    listener, _ = _listener()
    res = _serve(listener)
    assert res.pause_at is None
    assert res.pause_ms == 0


def test_the_transports_filter_scopes_a_pause():
    """A pause aimed at the ws wire must not hold an http reply.

    Every other field in the block is scoped by this filter. A field that
    ignored it would fire on endpoints the caller never named, and the failure
    would appear on an unrelated test sharing the provider.
    """
    listener, provider = _listener(HTTP)
    provider.scenario.update({"pause_at": "mid_body", "pause_ms": 250, "transports": ["ws"]})
    assert _serve(listener).pause_at is None

    ws_listener = JsonRpcListener(listener.provider, WS)
    assert _serve(ws_listener).pause_at == "mid_body"


# ── control API validation ────────────────────────────────────────────────────


def _set(sim, **extra):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"{sim['control']}/scenario",
        data=json.dumps({"providers": {"eth-sim:1": dict(extra)}}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


@pytest.fixture(autouse=True)
def _clean(sim):
    import urllib.request

    def reset():
        req = urllib.request.Request(
            f"{sim['control']}/reset/all",
            data=json.dumps({"pool": "eth-sim"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()

    reset()
    yield
    reset()


def test_both_positions_are_accepted(sim):
    """The positive control for the three refusals below.

    Without it, a validator that refused everything would pass them all.
    """
    for position in ("after_headers", "mid_body"):
        status, body = _set(sim, pause_at=position, pause_ms=10)
        assert status == 200, f"{position} was refused: {body}"


def test_a_pause_reads_back_from_the_control_api(sim):
    """A setting that stores but does not read back is invisible.

    Every other scenario field is readable on ``GET /scenario``, which is how a
    test confirms a fault was armed before it measures anything. A new field
    absent from that reply would look like a fault that never applied.
    """
    import urllib.request

    _set(sim, pause_at="mid_body", pause_ms=750)
    with urllib.request.urlopen(f"{sim['control']}/scenario", timeout=10) as resp:
        stored = json.loads(resp.read().decode())

    providers = stored.get("providers", stored)
    mine = providers["eth-sim:1"]
    assert mine["pause_at"] == "mid_body"
    assert mine["pause_ms"] == 750


def test_before_headers_is_refused_and_names_the_right_field(sim):
    """A pause before the headers is what ``latency_ms`` already means.

    The message must name ``latency_ms``, not only list the allowed values. The
    caller is not guessing at a vocabulary — they want a delay before the reply,
    and a generic "allowed:" list leaves them to find the right field alone.
    """
    status, body = _set(sim, pause_at="before_headers", pause_ms=10)
    assert status == 400
    assert "latency_ms" in body["error"]


def test_an_unknown_position_is_refused(sim):
    status, body = _set(sim, pause_at="somewhere")
    assert status == 400
    assert "pause_at" in body["error"]


def test_a_negative_pause_is_refused(sim):
    status, body = _set(sim, pause_ms=-1)
    assert status == 400
    assert "pause_ms" in body["error"]


# ── over a real socket: the behaviour the field exists for ────────────────────


def _request_bytes():
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber"}).encode()
    return (
        b"POST / HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + payload
    )


def _read_timed(port, deadline_s=30.0):
    """Send one request and record, for every chunk, WHEN it arrived and HOW MUCH
    OF THE BODY had arrived by then.

    Counting body bytes per chunk is what makes this instrument able to fail.
    Timing alone cannot: "headers, hold, whole body" and "headers plus half the
    body, hold, the rest" both start early and finish late. Only the body count
    in the FIRST chunk tells them apart — ``after_headers`` sends none, and
    ``mid_body`` sends some but not all.

    Returns ``(marks, headers, body)`` where ``marks`` is a list of
    ``(seconds_since_send, body_bytes_received_so_far)`` and ``headers`` is
    lower-cased.
    """
    conn = socket.create_connection(("127.0.0.1", port), timeout=deadline_s)
    try:
        started = time.monotonic()
        conn.sendall(_request_bytes())
        raw = b""
        marks = []
        expected = None
        while True:
            piece = conn.recv(65536)
            if not piece:
                break
            raw += piece
            head, sep, body_so_far = raw.partition(b"\r\n\r\n")
            if sep:
                if expected is None:
                    for line in head.split(b"\r\n"):
                        if line.lower().startswith(b"content-length:"):
                            expected = int(line.split(b":", 1)[1].strip())
                marks.append((time.monotonic() - started, len(body_so_far)))
                if expected is not None and len(body_so_far) >= expected:
                    break
            else:
                marks.append((time.monotonic() - started, 0))
    finally:
        conn.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.decode().strip().lower()] = v.decode().strip()
    return marks, headers, body


def test_a_mid_body_pause_sends_part_of_the_body_then_holds_then_finishes(sim):
    """THE test this field exists for, and the only one that can fail for the
    right reason.

    Three weaker tests would pass without the feature working. ``latency_ms``
    alone delivers the whole body late. An ``after_headers`` pause delivers the
    headers early and the whole body late. Both start early and finish late, so
    timing alone proves nothing.

    What proves it is the FIRST chunk carrying SOME of the body and NOT ALL of
    it. That is a reply the client has begun reading and cannot finish — the
    state a router with a per-attempt window has to be put in.
    """
    pause_ms = 1500
    _set(sim, pause_at="mid_body", pause_ms=pause_ms)
    marks, headers, body = _read_timed(_P1_PORT)

    total = int(headers["content-length"])
    pause_s = pause_ms / 1000.0
    # The headers and the first body bytes arrive as SEPARATE reads, so marks[0]
    # is headers-only. What matters is the state the client was left in while the
    # hold ran, which is the last thing that arrived early.
    early = [m for m in marks if m[0] < pause_s * 0.5]
    assert early, f"nothing arrived before the hold: marks={marks}"
    held_at, held_bytes = early[-1]
    last_at, last_bytes = marks[-1]

    assert 0 < held_bytes < total, (
        f"while the hold ran the client had {held_bytes} of {total} body bytes; "
        f"mid_body must leave it with SOME and not ALL. marks={marks}"
    )
    assert held_at < pause_s * 0.5, f"the reply did not start early: {held_at:.3f}s"
    assert last_at >= pause_s * 0.9, f"the reply did not finish late: {last_at:.3f}s"

    # Complete and correct, which is what separates a pause from a drop.
    assert last_bytes == total
    assert json.loads(body)["result"] == "0x1312D00"
    assert total == len(body), "Content-Length must describe the WHOLE body"


def test_an_after_headers_pause_sends_no_body_before_the_hold(sim):
    """Headers out, then a hold, then the whole body in one piece.

    The assertion that matters is ``first_body_bytes == 0``. It is what makes
    this position different from ``mid_body``, and without it this test and the
    one above would accept each other's behaviour.
    """
    pause_ms = 1200
    _set(sim, pause_at="after_headers", pause_ms=pause_ms)
    marks, headers, body = _read_timed(_P1_PORT)

    total = int(headers["content-length"])
    pause_s = pause_ms / 1000.0
    early = [m for m in marks if m[0] < pause_s * 0.5]
    assert early, f"nothing arrived before the hold: marks={marks}"
    held_at, held_bytes = early[-1]
    last_at, last_bytes = marks[-1]

    assert held_bytes == 0, f"after_headers sent {held_bytes} body bytes before the hold: marks={marks}"
    assert held_at < pause_s * 0.5, f"the headers were late: {held_at:.3f}s"
    assert last_at >= pause_s * 0.9, f"the body was not held: {last_at:.3f}s"
    assert last_bytes == total
    assert json.loads(body)["result"] == "0x1312D00"


def test_without_a_pause_the_whole_reply_arrives_at_once(sim):
    """The negative control. It proves the instrument can see the difference.

    If an un-paused reply also looked split, every assertion above would be
    accepting a shape the feature did not create.
    """
    marks, headers, body = _read_timed(_P1_PORT)
    total = int(headers["content-length"])
    assert marks[-1][1] == total
    assert marks[-1][0] < 1.0, f"an un-paused reply was slow: {marks[-1][0]:.3f}s"
    # No gap anywhere: every byte arrived inside the same instant.
    assert marks[-1][0] - marks[0][0] < 0.5, f"an un-paused reply was split: marks={marks}"
    assert json.loads(body)["result"] == "0x1312D00"


def test_latency_alone_delays_the_reply_and_never_splits_it(sim):
    """``latency_ms`` is not a substitute, and this is the test that says so.

    If latency alone produced a split delivery, ``pause_at`` would be redundant
    and should not exist. It delays the FIRST byte and delivers the body whole.
    """
    _set(sim, latency_ms=1200)
    marks, headers, body = _read_timed(_P1_PORT)
    total = int(headers["content-length"])
    assert marks[0][0] >= 1.0, f"latency_ms did not delay the first byte: {marks[0][0]:.3f}s"
    assert marks[-1][1] == total
    # Nothing was held back: once it started, it finished at once.
    assert marks[-1][0] - marks[0][0] < 0.5, f"latency_ms split the reply: marks={marks}"
    assert json.loads(body)["result"] == "0x1312D00"


# ── the two cases an adversary review found, both now guarded ─────────────────


def _seconds_until_the_connection_closes(port, deadline_s=30.0):
    """Send one request and return how long the server kept the connection open.

    This exists because ``_read_timed`` cannot see a hold on an EMPTY body. It
    stops as soon as the body is complete, and a ``Content-Length: 0`` reply is
    complete on its first chunk — so it returns at once however long the server
    waits. Reading to end-of-file measures the hold instead, which works because
    the request asks for ``Connection: close``.
    """
    conn = socket.create_connection(("127.0.0.1", port), timeout=deadline_s)
    try:
        started = time.monotonic()
        conn.sendall(_request_bytes())
        raw = b""
        while True:
            piece = conn.recv(65536)
            if not piece:
                break
            raw += piece
        return time.monotonic() - started, raw
    finally:
        conn.close()


def _heal(sim):
    """Clear the pool's scenario. ``_set(sim)`` with no fields sends an empty
    update, which changes nothing — so a test that must un-arm a fault mid-body
    has to reset rather than re-set."""
    import urllib.request

    req = urllib.request.Request(
        f"{sim['control']}/reset/all",
        data=json.dumps({"pool": "eth-sim"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10).read()


@pytest.mark.parametrize("position", ["mid_body", "after_headers"])
def test_a_pause_holds_even_when_the_reply_has_no_body(sim, position):
    """An empty body must not make a pause vanish, and the two positions must agree.

    `empty_response` corruption and an HTTP HEAD both produce a reply with no
    body, so there is no "part way through" for `mid_body` to hold at. The caller
    still asked for the reply to be held, and holding is closer to that than
    answering at once.

    The two positions must reach the SAME answer here, because the scenario is
    identical apart from a field whose meaning has run out. A pause that fires
    for one position and vanishes for the other, on one scenario, with nothing
    said, reads as a provider that simply answered quickly.
    """
    pause_ms = 1200
    _set(sim, corruption_mode="empty_response", pause_at=position, pause_ms=pause_ms)
    elapsed, raw = _seconds_until_the_connection_closes(_P1_PORT)

    _, _, body = raw.partition(b"\r\n\r\n")
    assert body == b"", f"empty_response should send no body, got {len(body)} bytes"
    assert elapsed >= pause_ms / 1000.0 * 0.9, (
        f"the pause vanished on an empty body at {position}: the connection closed "
        f"after {elapsed:.3f}s, expected at least {pause_ms / 1000.0 * 0.9:.3f}s"
    )


def test_the_provider_keeps_serving_after_a_client_leaves_mid_hold(sim):
    """A caller abandoning the request part way through a hold is the EXPECTED case,
    and the provider must keep serving afterwards.

    The whole purpose of a long pause is to outlast the caller's own window, so
    the caller giving up is the normal outcome rather than an error. The write
    that follows the hold then goes to a socket whose peer has gone.

    What this test proves: the provider serves the NEXT request normally. It
    would catch a wedged provider or a worker thread that never unwound, and it
    fails on the second request rather than the first.

    **What it does NOT prove, and the name says so on purpose.** It does not
    exercise the ``OSError`` path around that write. Removing the guard leaves
    this test passing on macOS: the write reaches the kernel buffer and never
    raises, measured with a 50-byte body and again with an 8 MB one. Linux
    reports a closed peer on a write differently, so the CI runner may reach the
    path this machine cannot. The guard is kept because ``_drop`` guards the same
    case for the same reason and it costs two lines — not because a test here
    holds it.
    """
    _set(sim, pause_at="mid_body", pause_ms=3000)
    conn = socket.create_connection(("127.0.0.1", _P1_PORT), timeout=30)
    try:
        conn.sendall(_request_bytes())
        early = conn.recv(65536)
        assert early, "nothing arrived before the hold, so the abandon is not the case under test"
    finally:
        conn.close()  # leave, mid-hold

    time.sleep(3000 / 1000.0 + 0.5)  # let the hold expire and the second write happen

    _heal(sim)
    marks, headers, body = _read_timed(_P1_PORT)
    assert json.loads(body)["result"] == "0x1312D00", "the provider stopped serving after a client left"
    assert marks[-1][0] < 1.0, f"the provider answered slowly afterwards: {marks[-1][0]:.3f}s"
