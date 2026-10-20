"""The ``graphql`` interface over a real socket: the adapter on an ``http`` port.

One private simulator with one Sui provider, started for the module, on this
file's own block of ports. The block is below the range that the kernel gives to
client sockets. The tests send real requests and read the history route, and
they check what a router would see: the body, the headers, the drop and the
closed port.

``tests/test_listener_graphql.py`` has the same behaviours without a socket.
This file has what only a socket shows: the wire, the headers of the reply, and
a connection that ends.
"""

from __future__ import annotations

import hashlib
import json
import socket
import urllib.error
import urllib.request

import pytest

import server as server_module
from provider_simulator import topology
from provider_simulator.chains import CHAINS
from stubs_sui_graphql import SUI_BASE_CHECKPOINT, SUI_CHAIN_ID

_PROVIDER_PORT, _CONTROL_PORT, _RESP_CONTROL_PORT = 28771, 29771, 29772
_POOL = "sui-graphql-wiring-sim"
_KEY = f"{_POOL}:1"
_NAME = "SuiGraphqlWiringPrimaryProvider1"
_ROWS = ((_POOL, "sui", "1", _NAME, False, "", (("graphql", "http", _PROVIDER_PORT),)),)
_CONTROL = f"http://127.0.0.1:{_CONTROL_PORT}"
_HEAD_POLL = '{ serviceConfig { availableRange(type: "Checkpoint") { last { sequenceNumber } } } }'


@pytest.fixture(scope="module")
def simulator():
    """One simulator for the module. The table of the topology is swapped only
    while the simulator builds its registry."""
    shipped = topology.TOPOLOGY
    topology.TOPOLOGY = _ROWS
    try:
        sim = server_module.SimulatorServer(
            host="127.0.0.1",
            scenario_ttl_s=0,
            control_port=_CONTROL_PORT,
            cache_ports={},
            resp_control_port=_RESP_CONTROL_PORT,
            resp_proxy_ports={},
        )
    finally:
        topology.TOPOLOGY = shipped
    sim.start()
    try:
        sim.wait_ready(20.0)
        yield sim
    finally:
        sim.stop()


@pytest.fixture(autouse=True)
def clean_state(simulator):
    """Each test starts with no fault and no history on this pool only. A
    reset of every pool would also move the head of the other chains, which
    belong to the shared simulator of the session."""
    _control_post("/reset/all", {"pool": _POOL})
    yield
    _control_post("/reset/all", {"pool": _POOL})


# ── helpers ───────────────────────────────────────────────────────────────────


def _control_post(path: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(_CONTROL + path, data=json.dumps(body).encode(), method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _control_get(path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(_CONTROL + path, timeout=10) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _send(body: bytes, path: str = "/") -> tuple[int, bytes, dict]:
    """One POST to the provider port. Returns the status, the body and the headers."""
    request = urllib.request.Request(f"http://127.0.0.1:{_PROVIDER_PORT}{path}", data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, reply.read(), dict(reply.headers.items())
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers.items())


def _graphql(query: str, variables: dict | None = None, path: str = "/") -> tuple[int, dict, dict]:
    payload: dict = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    status, raw, headers = _send(json.dumps(payload).encode(), path)
    return status, json.loads(raw), headers


def _history() -> list[dict]:
    status, reply = _control_get(f"/history?pool={_POOL}&pid=1")
    assert status == 200, reply
    return reply["history"]


def _connect_errno(port: int) -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(2.0)
    try:
        return probe.connect_ex(("127.0.0.1", port))
    finally:
        probe.close()


# ── the wire ──────────────────────────────────────────────────────────────────


def test_a_post_to_any_path_is_answered_with_the_chain_identifier():
    for path in ("/", "/x/y"):
        status, body, headers = _graphql("{ chainIdentifier }", path=path)
        assert status == 200
        assert body == {"data": {"chainIdentifier": SUI_CHAIN_ID}}
        content_type = headers["Content-Type"]
        assert content_type.startswith("application/json")
        assert "sim-provider=" in content_type and _NAME in content_type


def test_the_history_route_shows_method_path_request_id_and_body_hash_and_finds_a_row_by_its_hash():
    sent = json.dumps({"query": "{ chainIdentifier }", "variables": {"request_id": "wire-1"}}).encode()
    assert _send(b'{"query": "{ epoch { epochId } }"}', "/a")[0] == 200
    assert _send(sent, "/x/y")[0] == 200
    last = _history()[-1]
    digest = hashlib.sha256(sent).hexdigest()
    assert (last["method"], last["path"], last["request_id"]) == ("chainIdentifier", "/x/y", "wire-1")
    assert (last["body_sha256"], last["body_len"], last["interface"]) == (digest, len(sent), "graphql")

    status, found = _control_get(f"/history?pool={_POOL}&pid=1&body_sha256={digest}")
    assert status == 200
    # ``call_order`` counts the rows that a read returns, so a filtered read cannot be compared by it.
    assert [row["ts"] for row in found["history"]] == [last["ts"]]
    status, none = _control_get(f"/history?pool={_POOL}&pid=1&body_sha256={'0' * 64}")
    assert (status, none["history"]) == (200, [])


def test_advance_moves_the_tip_over_the_wire():
    try:
        status, moved = _control_post("/advance", {"chain": "sui", "blocks": 5})
        assert status == 200
        assert moved["heads"] == {"default": SUI_BASE_CHECKPOINT + 5}
        _status, polled, _headers = _graphql(_HEAD_POLL)
        assert polled["data"]["serviceConfig"]["availableRange"]["last"]["sequenceNumber"] == SUI_BASE_CHECKPOINT + 5
    finally:
        CHAINS["sui"].head.reset()
    _status, polled, _headers = _graphql(_HEAD_POLL)
    assert polled["data"]["serviceConfig"]["availableRange"]["last"]["sequenceNumber"] == SUI_BASE_CHECKPOINT


def test_a_skip_directive_removes_the_field_over_the_wire():
    _status, body, _headers = _graphql("{ a: chainIdentifier @skip(if: true) b: epoch { epochId } }")
    assert set(body["data"]) == {"b"}


# ── faults over the wire ──────────────────────────────────────────────────────


def test_mode_down_answers_503_with_no_body():
    assert _control_post("/scenario", {"providers": {_KEY: {"mode": "down"}}})[0] == 200
    status, raw, _headers = _send(b'{"query": "{ chainIdentifier }"}')
    assert (status, raw) == (503, b"")


def test_a_mid_body_drop_sends_the_start_of_a_graphql_body_and_closes():
    assert (
        _control_post("/scenario", {"providers": {_KEY: {"mode": "drop_connection", "drop_at": "mid_body"}}})[0] == 200
    )
    body = b'{"query": "{ chainIdentifier }"}'
    request = (
        b"POST / HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nConnection: close\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    received = b""
    with socket.create_connection(("127.0.0.1", _PROVIDER_PORT), timeout=5) as connection:
        connection.sendall(request)
        while chunk := connection.recv(4096):
            received += chunk
    head, _, sent_body = received.partition(b"\r\n\r\n")
    announced = int(
        next(line.split(b":")[1] for line in head.split(b"\r\n") if line.lower().startswith(b"content-length"))
    )
    assert sent_body.startswith(b'{"data":')
    assert len(sent_body) < announced


def test_port_closed_refuses_connections_and_opens_again():
    assert _connect_errno(_PROVIDER_PORT) == 0
    assert _control_post("/scenario", {"providers": {_KEY: {"mode": "port_closed"}}})[0] == 200
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", _PROVIDER_PORT), timeout=2).close()
    assert _control_post("/reset", {"pool": _POOL})[0] == 200
    assert _connect_errno(_PROVIDER_PORT) == 0
    assert _graphql("{ chainIdentifier }")[0] == 200


def test_missing_field_corruption_reaches_a_nested_key():
    block = {"corruption_mode": "missing_field", "missing_field": "data.chainIdentifier"}
    assert _control_post("/scenario", {"providers": {_KEY: block}})[0] == 200
    status, body, _headers = _graphql("{ chainIdentifier }")
    assert (status, body) == (200, {"data": {}})


# ── a body that cannot be read ────────────────────────────────────────────────


def test_an_unreadable_body_gets_the_reply_of_sui_over_the_wire():
    not_json = b"nope"
    status, raw, headers = _send(not_json)
    assert status == 400
    assert raw.decode().startswith("InvalidRequest(Error(")
    assert headers["Content-Type"].startswith("text/plain")

    unclosed = b'{"query": "{ chainIdentifier"}'
    status, raw, _headers = _send(unclosed)
    assert status == 200
    assert json.loads(raw)["errors"][0]["extensions"] == {"code": "GRAPHQL_PARSE_FAILED"}

    for body in (not_json, unclosed):
        digest = hashlib.sha256(body).hexdigest()
        _status, found = _control_get(f"/history?pool={_POOL}&pid=1&body_sha256={digest}")
        assert [(row["method"], row["status"]) for row in found["history"]] == [("unknown", "error")]
