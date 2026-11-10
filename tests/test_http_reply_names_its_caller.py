"""Every HTTP reply of a simulated provider carries the address of its caller.

A smart-router sits between the simulator and a test. The router passes the
``Content-Type`` of a provider reply on to its own caller. So the address of
the process that asked the provider travels as a parameter of that header,
after the name of the provider:

    Content-Type: application/json; sim-provider=EthPrimaryProvider1; sim-caller=10.42.0.26

Behind a router the caller is the router. Where each router pod has an address
of its own, the address tells which pod sent the request.

``tests/test_http_reply_names_its_provider.py`` sends each kind of reply and
compares the whole header, with both parameters. This file has the tests of the
address itself.
"""

import json
import urllib.error
import urllib.request

import pytest

from provider_simulator.domain.scenario import ScenarioConfig
from server import _caller_address, _content_type_with_caller_address

# One provider of the eth pool, and the request it answers with a success.
_ETH_KEY = "eth-sim:1"
_BLOCK_NUMBER = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}


def _eth_port(sim) -> int:
    """The jsonrpc http port of the provider ``_ETH_KEY``, from ``GET /providers``."""
    with urllib.request.urlopen(f"{sim['control']}/providers", timeout=5) as reply:
        provider = json.loads(reply.read())["providers"][_ETH_KEY]
    ports = [
        endpoint["port"]
        for endpoint in provider["endpoints"]
        if (endpoint["interface"], endpoint["transport"]) == ("jsonrpc", "http")
    ]
    assert ports, f"GET /providers reports no jsonrpc http port for {_ETH_KEY}"
    return ports[0]


def test_a_reply_carries_the_address_that_the_request_came_from(sim):
    request = urllib.request.Request(
        f"http://127.0.0.1:{_eth_port(sim)}",
        data=json.dumps(_BLOCK_NUMBER).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as reply:
        assert reply.status == 200
        assert reply.headers.get_param("sim-caller") == "127.0.0.1"
        # The address comes after the provider name, so a reader of the name is not disturbed.
        names = [name for name, _ in reply.headers.get_params()]
        assert names == ["application/json", "sim-provider", "sim-caller"], reply.headers["Content-Type"]


def test_an_ipv4_address_is_written_as_a_plain_token():
    assert (
        _content_type_with_caller_address("application/json; sim-provider=EthPrimaryProvider1", "10.42.0.26")
        == "application/json; sim-provider=EthPrimaryProvider1; sim-caller=10.42.0.26"
    )


def test_an_ipv6_address_is_written_as_a_quoted_string():
    assert (
        _content_type_with_caller_address("application/json", "fd00::1a") == 'application/json; sim-caller="fd00::1a"'
    )


def test_an_empty_address_adds_no_parameter():
    assert _content_type_with_caller_address("application/json", "") == "application/json"


def test_the_address_is_the_host_of_the_peer_of_the_connection():
    assert _caller_address(("10.42.0.26", 51234)) == "10.42.0.26"


def test_an_ipv4_peer_of_an_ipv6_socket_gives_the_ipv4_address():
    assert _caller_address(("::ffff:10.42.0.26", 51234, 0, 0)) == "10.42.0.26"


def test_an_ipv6_peer_gives_its_ipv6_address():
    assert _caller_address(("fd00::1a", 51234, 0, 0)) == "fd00::1a"


@pytest.mark.parametrize(
    "peer",
    [("10.42.0.26", 51234), ("::ffff:10.42.0.26", 51234, 0, 0), ("fd00::1a", 51234, 0, 0), ("::1", 51234, 0, 0)],
    ids=["ipv4", "ipv4-of-an-ipv6-socket", "ipv6", "ipv6-loopback"],
)
def test_the_address_that_a_reply_reports_is_accepted_as_a_callers_filter(peer):
    """A test copies the address from ``sim-caller`` into the ``callers`` filter. The two
    must spell an address the same way, or the filter refuses what the reply reported."""
    reported = _caller_address(peer)
    ScenarioConfig().update({"callers": [reported]})


def test_a_connection_with_no_peer_address_gives_an_empty_address():
    # A socket that is not TCP gives a handler a text or an empty value here.
    assert _caller_address("") == ""
    assert _caller_address(()) == ""


# ── a fault reaches only the caller that the callers filter names ─────────────
#
# The address that the filter names is the one that a reply reports as ``sim-caller``:
# a test reads it from a reply, and aims a fault at that pod. The tests of this
# section send from this machine, so the caller is 127.0.0.1, and the other pod is an
# address that no request comes from.

_THIS_MACHINE = "127.0.0.1"
_OTHER_POD = "10.42.0.26"

# For each interface served over http: the verb, the path and the body of one request that a
# provider with no scenario answers with a success.
_REQUEST = {
    "jsonrpc": ("POST", "", _BLOCK_NUMBER),
    "rest": ("GET", "/cosmos/base/tendermint/v1beta1/blocks/latest", None),
    "tendermintrpc": ("POST", "", {"jsonrpc": "2.0", "id": 1, "method": "status"}),
    "graphql": ("POST", "", {"query": "{ chainIdentifier }"}),
}


def _control(sim, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{sim['control']}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as reply:
        return reply.status, json.loads(reply.read())


def _set(sim, key: str, **block) -> None:
    status, body = _control(sim, "POST", "/scenario", {"providers": {key: block}})
    assert status == 200, f"POST /scenario for {key} answered HTTP {status}: {body!r}"


def _status_of_call(method: str, url: str, body: dict | None):
    """Send one request. Return the status and the reply headers."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"} if data else {}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            return reply.status, reply.headers
    except urllib.error.HTTPError as error:
        return error.code, error.headers


def _http_endpoints(sim, key: str) -> dict[str, int]:
    """The port of each transport that the provider ``key`` serves, from ``GET /providers``."""
    _, body = _control(sim, "GET", "/providers")
    return {endpoint["transport"]: endpoint["port"] for endpoint in body["providers"][key]["endpoints"]}


@pytest.fixture
def clean_state(sim):
    """Reset every scenario before and after a test that sets one."""
    _control(sim, "POST", "/reset/all", {})
    yield
    _control(sim, "POST", "/reset/all", {})


def test_a_fault_reaches_the_call_of_the_listed_address_and_not_the_call_of_another(sim, clean_state):
    """The whole reason the filter exists, over a socket. A filter that was ignored faults the
    second call, and a filter that was inert leaves the first call healthy."""
    url = f"http://127.0.0.1:{_eth_port(sim)}"
    method, _, body = _REQUEST["jsonrpc"]
    status, headers = _status_of_call(method, url, body)
    caller = headers.get_param("sim-caller")
    assert (status, caller) == (200, _THIS_MACHINE), "the baseline answers, and the reply names its caller"

    _set(sim, _ETH_KEY, mode="down", callers=[caller])
    assert _status_of_call(method, url, body)[0] == 503, "the listed address must fault"

    _set(sim, _ETH_KEY, callers=[_OTHER_POD])
    assert _status_of_call(method, url, body)[0] == 200, "a call from another address must get the healthy answer"

    _set(sim, _ETH_KEY, callers=None)
    assert _status_of_call(method, url, body)[0] == 503, "with the filter cleared every caller faults again"


def test_the_callers_filter_ands_with_the_ports_and_the_transports_filters(sim, clean_state):
    """Each of the three filters, failing alone, leaves the call healthy."""
    ports = _http_endpoints(sim, _ETH_KEY)
    url = f"http://127.0.0.1:{ports['http']}"
    method, _, body = _REQUEST["jsonrpc"]

    _set(sim, _ETH_KEY, mode="down", callers=[_THIS_MACHINE], ports=[ports["http"]], transports=["http"])
    assert _status_of_call(method, url, body)[0] == 503, "all three filters match"

    _set(sim, _ETH_KEY, callers=[_OTHER_POD])
    assert _status_of_call(method, url, body)[0] == 200, "only the caller fails"
    _set(sim, _ETH_KEY, callers=[_THIS_MACHINE], ports=[ports["ws"]], transports=None)
    assert _status_of_call(method, url, body)[0] == 200, "only the port fails"
    _set(sim, _ETH_KEY, ports=None, transports=["ws"])
    assert _status_of_call(method, url, body)[0] == 200, "only the transport fails"


@pytest.mark.parametrize("interface", list(_REQUEST))
def test_the_listed_address_is_faulted_on_each_interface_served_over_http(sim, clean_state, interface):
    _, body = _control(sim, "GET", "/providers")
    key, provider = next(
        (key, provider)
        for key, provider in body["providers"].items()
        if any((e["interface"], e["transport"]) == (interface, "http") for e in provider["endpoints"])
    )
    port = next(e["port"] for e in provider["endpoints"] if (e["interface"], e["transport"]) == (interface, "http"))
    method, path, request_body = _REQUEST[interface]
    url = f"http://127.0.0.1:{port}{path}"
    assert _status_of_call(method, url, request_body)[0] == 200, f"{key} must answer with no fault"

    _set(sim, key, mode="down", callers=[_THIS_MACHINE])
    assert _status_of_call(method, url, request_body)[0] == 503, f"{key} must fault the listed address"
    _set(sim, key, callers=[_OTHER_POD])
    assert _status_of_call(method, url, request_body)[0] == 200, f"{key} must answer another address"
