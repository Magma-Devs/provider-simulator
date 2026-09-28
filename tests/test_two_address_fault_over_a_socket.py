"""The two-address fault, exercised on a real socket against the REAL pool.

Why this file exists, stated plainly because it is the gap that let two defects
ship: every other test of the ``ports`` filter calls ``fault_policy`` in process,
and the two-address provider those tests describe is one they build themselves.
So the pool the feature was written for, ``eth-failover-twoaddr-sim``, was
referenced by no test at all. Rename it, retire it, give its second endpoint the
wrong port, or have the registry hand both addresses one listener, and every test
stayed green while the feature was dead.

``transports`` has had a test of this shape since it shipped —
``test_simulator_backup_listeners.py`` faults one wire and reads the OTHER wire's
HTTP status over a socket. This is the same idea for an address.

The pool is read from the topology rather than written down here, which is the
opposite choice from the in-process tests and deliberate: their value is that they
survive the pool being renamed, and this one's value is that it does not. If the
pool stops existing, or stops having two addresses on one door, this file must
fail rather than quietly pass.
"""

import json
import urllib.error
import urllib.request

import pytest

from provider_simulator.topology import TOPOLOGY, ports_of

POOL = "eth-failover-twoaddr-sim"


def _two_addresses() -> list[int]:
    """The pool's two jsonrpc/http ports, from the table.

    Fails rather than skips when the shape is gone. A skip here would read as
    "nothing to check" and is exactly how a dead feature stays green.
    """
    pools = {row[0] for row in TOPOLOGY}
    assert POOL in pools, (
        f"{POOL} is not in the topology. If it was renamed, rename it here too; if it "
        f"was retired, the ports filter has no shipped pool that needs it and this file "
        f"should be reconsidered rather than deleted quietly."
    )
    found = sorted(ports_of(POOL, "1", "jsonrpc", "http"))
    assert len(found) == 2, (
        f"{POOL}:1 serves jsonrpc/http on {len(found)} address(es), {found}. This whole "
        f"file is about a provider with TWO addresses on ONE door; with any other number "
        f"there is nothing here to test and the pool's purpose has changed."
    )
    return found


def _post(url: str, body: dict) -> tuple[int, dict | str]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else {})
        except (ConnectionResetError, OSError, json.JSONDecodeError):
            # A `down` reply is a bodiless 503 and the server closes the socket.
            return e.code, {}


def _rpc(port: int) -> tuple[int, object]:
    """One JSON-RPC call to one address. net_version needs no chain state."""
    return _post(
        f"http://127.0.0.1:{port}",
        {"jsonrpc": "2.0", "id": 1, "method": "net_version", "params": []},
    )


@pytest.fixture(autouse=True)
def only_this_pool(sim):
    """Reset THIS pool before and after, so a neighbour's fault cannot be read as
    this test's result and this test's fault cannot reach a neighbour."""
    _post(f"{sim['control']}/reset/all", {"pool": POOL})
    yield
    _post(f"{sim['control']}/reset/all", {"pool": POOL})


def test_both_addresses_answer_with_nothing_set(sim):
    """The baseline, and it is not optional. Without it "one address went silent"
    proves nothing, because an address that never answered would satisfy the next
    test too."""
    for port in _two_addresses():
        status, body = _rpc(port)
        assert status == 200, f"port {port} must answer with no fault set, got {status}"
        assert "result" in body, f"port {port} returned {body!r}"


def test_a_fault_with_no_ports_filter_silences_both_addresses(sim):
    """The behaviour before the filter existed, pinned. It is also the control for
    the two tests below: it shows a fault CAN reach both, so their asymmetry is the
    filter working rather than one address being unreachable."""
    first, second = _two_addresses()
    status, _ = _post(f"{sim['control']}/scenario", {"providers": {f"{POOL}:1": {"mode": "down"}}})
    assert status == 200
    for port in (first, second):
        code, _ = _rpc(port)
        assert code == 503, f"port {port} must be down with no filter, got {code}"


@pytest.mark.parametrize("silence_index", [0, 1])
def test_a_ports_filter_silences_only_the_named_address(sim, silence_index):
    """The whole point of the pool, over a socket.

    Run for BOTH addresses. A bug that always faulted the first endpoint, or the
    lowest port, passes one of these and fails the other — which is why it is
    parametrised rather than written once.
    """
    addresses = _two_addresses()
    silenced = addresses[silence_index]
    serving = addresses[1 - silence_index]

    status, body = _post(
        f"{sim['control']}/scenario",
        {"providers": {f"{POOL}:1": {"mode": "down", "ports": [silenced]}}},
    )
    assert status == 200, f"control API refused the ports filter: {body!r}"
    assert body["applied"][f"{POOL}:1"]["ports"] == [silenced]

    code, _ = _rpc(silenced)
    assert code == 503, f"port {silenced} was named and must be down, got {code}"

    code, reply = _rpc(serving)
    assert code == 200, (
        f"port {serving} was NOT named and must keep serving, got {code}. A fault "
        f"reaching both addresses is the defect this filter exists to fix."
    )
    assert "result" in reply


def test_a_port_the_provider_does_not_serve_is_refused_over_the_socket(sim):
    """The refusal has to survive the HTTP layer, not only the in-process call.

    An earlier version of this code raised TypeError for a mis-shaped ports value,
    which is not a ValueError, so the handler's guard never saw it and the client's
    connection was dropped with no answer. A dropped connection reads as a broken
    simulator rather than a refused caller.
    """
    first, _ = _two_addresses()
    unserved = 19999
    status, body = _post(
        f"{sim['control']}/scenario",
        {"providers": {f"{POOL}:1": {"mode": "down", "ports": [unserved]}}},
    )
    assert status == 400, f"expected a refusal, got {status} {body!r}"
    assert str(unserved) in str(body)
    # Nothing was applied: both addresses still answer.
    for port in _two_addresses():
        code, _ = _rpc(port)
        assert code == 200, f"a refused request must change nothing; port {port} gave {code}"


@pytest.mark.parametrize("bad", [18638, True, "18638", [], {}])
def test_a_misshaped_ports_value_is_answered_not_dropped(sim, bad):
    """Every one of these must come back as an HTTP 400 with a body.

    The failure being guarded is not "the wrong answer" but "no answer": a bare int
    and a bool are not iterable, and iterating them raised TypeError out of the
    handler. A string is worse — iterable, so "18638" became five one-character
    ports and the refusal named them.
    """
    status, body = _post(
        f"{sim['control']}/scenario",
        {"providers": {f"{POOL}:1": {"mode": "down", "ports": bad}}},
    )
    assert status == 400, f"ports={bad!r} must be refused with 400, got {status}"
    assert "ports" in str(body).lower(), f"the refusal must name the field: {body!r}"
    # And the provider is untouched.
    for port in _two_addresses():
        code, _ = _rpc(port)
        assert code == 200, f"port {port} must be unaffected by a refused request"


def test_a_ports_filter_survives_a_later_unrelated_post(sim):
    """The simulator keeps ONE scenario per provider and a POST MERGES into it.

    So a ports filter armed in one call is still in force for the next call, which
    did not mention it. This is documented for transports in the automation repo's
    control client and was not for ports. Pinned here because the surprise is
    silent: the second fault looks provider-wide and is not.
    """
    first, second = _two_addresses()
    _post(
        f"{sim['control']}/scenario",
        {"providers": {f"{POOL}:1": {"mode": "down", "ports": [first]}}},
    )
    status, _ = _post(f"{sim['control']}/scenario", {"providers": {f"{POOL}:1": {"mode": "error"}}})
    assert status == 200

    # The error is scoped to the address named in the EARLIER call.
    code, body = _rpc(first)
    assert code == 200 and "error" in body, f"port {first} should carry the error, got {code} {body!r}"
    code, body = _rpc(second)
    assert code == 200 and "result" in body, (
        f"port {second} was never named, so it must still serve success; got {body!r}. "
        f"If this fails the merge behaviour changed, which is a real change to pin, not to silence."
    )
