"""The URLs themselves — the interface the automation helpers are built against.

Every other RESP test calls ``RespControlApi`` directly. That proves the
decisions are right and proves nothing at all about the routes, which are what a
caller on the other side of the cluster actually types. A rename there breaks
every helper and no test that calls the object would notice.

So this file speaks HTTP to a real listener. The four routes below are the
agreed contract:

    GET  /resp/keys      what the router stored, with values and lifetimes
    POST /resp/cutoff    {"kind": "timeout"} holds, {"kind": "error"} ends
    POST /resp/restore   let the router reach the store again
    POST /resp/flush     empty the store

``?store=`` names which store, and may be left out while there is only one.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

import server
from provider_simulator.resp_control import RespControlApi
from tests.resp_fake_store import FakeRespStore


@pytest.fixture
def store():
    with FakeRespStore() as fake:
        yield fake


@pytest.fixture
def listener(store):
    """The real control listener, on a port the operating system picks."""
    control = RespControlApi()
    control.register("primary", "127.0.0.1", store.port)
    srv = server._RespControlServer(("127.0.0.1", 0), server._RespControlHandler)
    srv.resp_control = control
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _get(base: str, path: str) -> tuple[int, dict]:
    return _call(urllib.request.Request(base + path, method="GET"))


def _post(base: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    raw = json.dumps(body or {}).encode()
    request = urllib.request.Request(base + path, data=raw, method="POST")
    request.add_header("Content-Type", "application/json")
    return _call(request)


def _call(request: urllib.request.Request) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# ── the four agreed routes ────────────────────────────────────────────────────


def test_get_resp_keys_reads_the_store(store, listener):
    store.put("sr:rel:f:ETH1:abc:1", '{"result":"0x1"}', ttl=3600)
    status, payload = _get(listener, "/resp/keys")
    assert status == 200
    assert payload["count"] == 1
    assert payload["entries"][0]["key"] == "sr:rel:f:ETH1:abc:1"
    assert payload["entries"][0]["value"] == '{"result":"0x1"}'
    assert payload["entries"][0]["ttl"] > 0


def test_get_resp_keys_on_an_empty_store_answers_200_with_nothing(listener):
    status, payload = _get(listener, "/resp/keys")
    assert status == 200
    assert payload["count"] == 0


def test_get_resp_keys_on_an_unreachable_store_answers_503(store, listener):
    """The distinction the whole feature rests on, proved through the route
    rather than only through the object behind it."""
    store.stop()
    status, payload = _get(listener, "/resp/keys")
    assert status == 503
    assert payload["read"] == "failed"


def test_a_pattern_narrows_what_the_route_returns(store, listener):
    store.put("sr:chaintip:ETH1", "20000000")
    store.put("elsewhere:key", "value")
    status, payload = _get(listener, "/resp/keys?pattern=sr:*")
    assert status == 200
    assert [e["key"] for e in payload["entries"]] == ["sr:chaintip:ETH1"]


def test_post_resp_cutoff_takes_both_kinds(listener):
    for kind in ("timeout", "error"):
        status, payload = _post(listener, "/resp/cutoff", {"kind": kind})
        assert status == 200
        assert payload["proxy"]["state"] == kind


def test_post_resp_cutoff_without_a_kind_is_refused(listener):
    status, payload = _post(listener, "/resp/cutoff", {})
    assert status == 400
    assert payload["expected"] == ["timeout", "error"]


def test_post_resp_restore_puts_the_gate_back(listener):
    _post(listener, "/resp/cutoff", {"kind": "error"})
    status, payload = _post(listener, "/resp/restore")
    assert status == 200
    assert payload["proxy"]["state"] == "forwarding"


def test_post_resp_flush_empties_the_store(store, listener):
    store.put("sr:one", "1")
    assert _get(listener, "/resp/keys")[1]["count"] == 1
    status, _ = _post(listener, "/resp/flush")
    assert status == 200
    assert _get(listener, "/resp/keys")[1]["count"] == 0


# ── naming a store ────────────────────────────────────────────────────────────


def test_the_store_may_be_named_and_may_be_left_out_while_there_is_one(store, listener):
    store.put("sr:one", "1")
    assert _get(listener, "/resp/keys")[1]["count"] == 1
    assert _get(listener, "/resp/keys?store=primary")[1]["count"] == 1


def test_naming_a_store_that_is_not_running_answers_404(listener):
    status, payload = _get(listener, "/resp/keys?store=nope")
    assert status == 404
    assert payload["stores"] == ["primary"]


# ── everything else ───────────────────────────────────────────────────────────


def test_get_resp_state_reports_the_gate_and_the_store(store, listener):
    status, payload = _get(listener, "/resp/state")
    assert status == 200
    assert payload["state"] == "forwarding"
    assert payload["store_reachable"] is True


def test_get_resp_state_says_no_when_the_store_is_gone(store, listener):
    """The negative, run rather than reasoned about.

    ``store_reachable`` is the only field any route here measures instead of
    reporting back what somebody configured, which makes it the only one that
    can be wrong quietly. A check hard-wired to answer "reachable" passes every
    test written on it, and the positive above would not notice.

    So the store is stopped for real and the route is asked again.
    """
    assert _get(listener, "/resp/state")[1]["store_reachable"] is True
    store.stop()
    status, payload = _get(listener, "/resp/state")
    assert status == 200
    assert payload["store_reachable"] is False


def test_cutting_the_router_off_leaves_the_store_reachable(store, listener):
    """Two different things, and the route exists to keep them apart.

    Cutting off stops the ROUTER reaching the store. It must not change whether
    the store answers, because the control service reads the store directly and
    never through the proxy — which is exactly what lets a test read an entry
    while the router cannot.

    A test that expected a cut-off to report the store unreachable would be
    asking for the design to be broken. Written down here because the request
    that prompted this test asked for the negative "with the proxy cut off", and
    that is the one arrangement where the answer must stay yes.
    """
    _post(listener, "/resp/cutoff", {"kind": "error"})
    status, payload = _get(listener, "/resp/state")
    assert status == 200
    assert payload["state"] == "error"
    assert payload["store_reachable"] is True


def test_health_answers_without_a_store_name(listener):
    status, payload = _get(listener, "/health")
    assert status == 200
    assert payload["stores"] == ["primary"]


def test_an_unknown_action_is_a_404_that_lists_the_real_ones(listener):
    status, payload = _get(listener, "/resp/entries")
    assert status == 404
    assert payload["actions"] == ["keys", "state"]


def test_there_is_no_route_that_puts_an_entry_in(listener):
    """The design, checked at the surface a caller can actually reach.

    Every plausible spelling of a write, because the claim is that no such route
    exists rather than that one particular name is absent.
    """
    for path in ("/resp/set", "/resp/put", "/resp/write", "/resp/entry", "/resp/stage"):
        status, _ = _post(listener, path, {"key": "sr:planted", "value": "x"})
        assert status == 404, f"{path} answered {status}; nothing may plant an entry"


def test_a_write_attempt_leaves_the_store_untouched(store, listener):
    """The other half of the test above. A 404 that still had a side effect
    would satisfy a status-code check and defeat the rule."""
    _post(listener, "/resp/set", {"key": "sr:planted", "value": "x"})
    assert _get(listener, "/resp/keys")[1]["count"] == 0
