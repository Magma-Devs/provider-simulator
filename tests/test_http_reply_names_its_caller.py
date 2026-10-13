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
import urllib.request

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


def test_a_connection_with_no_peer_address_gives_an_empty_address():
    # A socket that is not TCP gives a handler a text or an empty value here.
    assert _caller_address("") == ""
    assert _caller_address(()) == ""
