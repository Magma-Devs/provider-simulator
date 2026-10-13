"""Every HTTP reply of a simulated provider names that provider in its Content-Type.

A smart-router sits between the simulator and a test. The router drops every
reply header of a provider except a short list, and ``Content-Type`` is on that
list. So the name of the provider travels as a parameter of that header:

    Content-Type: application/json; sim-provider=EthPrimaryProvider1

The name is ``Provider.name``, the one that ``GET /providers`` reports. The
parameter comes after each parameter that the media type has. The reply body
stays as it is, a reply with no ``Content-Type`` gets none, and a reply of the
control API carries no such parameter.

The address of the caller follows the name, as the parameter ``sim-caller``.
``tests/test_http_reply_names_its_caller.py`` has the tests of that parameter.
"""

import http.client
import json
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

import pytest

_BLOCKS_LATEST = "/cosmos/base/tendermint/v1beta1/blocks/latest"

# The provider that the tests of one eth reply ask.
_ETH_KEY = "eth-sim:1"

# Each reply of a provider port also carries the address of its caller, after the
# provider name. The tests of this file send from this machine.
_CALLER = "; sim-caller=127.0.0.1"


def _rpc(method: str) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": []}


# For each chain and interface: the verb, the path and the body of one request
# that a provider with no scenario answers with a success.
_SUCCESS_REQUEST = {
    ("eth", "jsonrpc"): ("POST", "", _rpc("eth_blockNumber")),
    ("btc", "jsonrpc"): ("POST", "", _rpc("getblockcount")),
    ("ln", "jsonrpc"): ("POST", "", _rpc("getinfo")),
    ("solana", "jsonrpc"): ("POST", "", _rpc("getSlot")),
    ("lava", "rest"): ("GET", _BLOCKS_LATEST, None),
    ("lava", "tendermintrpc"): ("POST", "", {"jsonrpc": "2.0", "id": 1, "method": "status"}),
}

_HTTP_INTERFACES = ["jsonrpc", "rest", "tendermintrpc"]


def _send(method: str, url: str, body: dict | None = None):
    """Send one request. Return the status, the body bytes and the reply headers."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            return reply.status, reply.read(), reply.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers


def _providers(sim) -> dict:
    """The providers that ``GET /providers`` of the control API reports."""
    status, raw, _ = _send("GET", f"{sim['control']}/providers")
    assert status == 200, f"GET /providers answered HTTP {status}"
    return json.loads(raw)["providers"]


def _http_ports(provider: dict, interface: str) -> list[int]:
    """Every port where the provider serves ``interface`` over http. One provider can have two."""
    return [
        endpoint["port"]
        for endpoint in provider["endpoints"]
        if (endpoint["interface"], endpoint["transport"]) == (interface, "http")
    ]


def _first_provider_on(sim, interface: str):
    """The key, the entry and one port of the first provider that serves ``interface`` over http."""
    for key, provider in _providers(sim).items():
        ports = _http_ports(provider, interface)
        if ports:
            return key, provider, ports[0]
    raise AssertionError(f"GET /providers reports no provider with an http endpoint on {interface}")


def _eth_provider(sim):
    """The entry and the jsonrpc port of the provider ``_ETH_KEY``."""
    provider = _providers(sim)[_ETH_KEY]
    return provider, _http_ports(provider, "jsonrpc")[0]


def _ask(provider: dict, interface: str, port: int):
    """Send the success request of the provider's chain to one of its ports."""
    pair = (provider["chain"], interface)
    assert pair in _SUCCESS_REQUEST, f"add a success request for {pair} to _SUCCESS_REQUEST of this file"
    method, path, body = _SUCCESS_REQUEST[pair]
    return _send(method, f"http://127.0.0.1:{port}{path}", body)


def _set_scenario(sim, key: str, **scenario) -> None:
    status, raw, _ = _send("POST", f"{sim['control']}/scenario", {"providers": {key: scenario}})
    assert status == 200, f"POST /scenario for {key} answered HTTP {status}: {raw!r}"


@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario config AND clear history before and after every test."""
    _send("POST", f"{sim['control']}/reset/all", {})
    yield
    _send("POST", f"{sim['control']}/reset/all", {})


# ── A success reply on each HTTP interface ────────────────────────────────────


@pytest.mark.parametrize("interface", _HTTP_INTERFACES)
def test_a_success_reply_carries_the_name_that_get_providers_reports_for_that_provider(sim, interface):
    """Every provider is asked on every port where it serves the interface over http.

    The expected value has the name as a plain token. So a provider whose name
    needs quotes fails here, with the two spellings side by side.
    """
    checked = 0
    for key, provider in _providers(sim).items():
        for port in _http_ports(provider, interface):
            status, raw, headers = _ask(provider, interface, port)
            assert status == 200, f"{key} on port {port} did not answer a success: HTTP {status} {raw!r}"
            if interface != "rest":
                assert "result" in json.loads(raw), f"{key} on port {port} did not answer a success: {raw!r}"
            expected = f"application/json; sim-provider={provider['name']}{_CALLER}"
            assert headers["Content-Type"] == expected, f"{key} on port {port}"
            checked += 1
    assert checked, f"GET /providers reports no provider with an http endpoint on {interface}"


def test_a_provider_with_two_addresses_answers_with_its_name_on_both(sim):
    """The success test above asks every port. This test shows that a provider with two ports exists."""
    asked = 0
    for key, provider in _providers(sim).items():
        ports = _http_ports(provider, "jsonrpc")
        if len(ports) < 2:
            continue
        for port in ports:
            _, _, headers = _ask(provider, "jsonrpc", port)
            expected = f"application/json; sim-provider={provider['name']}{_CALLER}"
            assert headers["Content-Type"] == expected, f"{key} on port {port}"
            asked += 1
    assert asked, "GET /providers reports no provider that serves jsonrpc over http on two ports"


def test_two_providers_of_one_pool_each_answer_with_their_own_name(sim):
    providers = _providers(sim)
    first, second = providers["eth-sim:1"], providers["eth-sim:2"]
    assert first["name"] != second["name"], "the two providers must have two names for this test to mean anything"
    seen = []
    for provider in (first, second):
        _, _, headers = _ask(provider, "jsonrpc", _http_ports(provider, "jsonrpc")[0])
        assert headers["Content-Type"] == f"application/json; sim-provider={provider['name']}{_CALLER}"
        seen.append(headers["Content-Type"])
    assert seen[0] != seen[1]


# ── A reply that is not a success ─────────────────────────────────────────────


@pytest.mark.parametrize("interface", _HTTP_INTERFACES)
def test_an_error_reply_carries_the_provider_name(sim, interface):
    key, provider, port = _first_provider_on(sim, interface)
    _set_scenario(sim, key, mode="error")
    status, raw, headers = _ask(provider, interface, port)
    # A REST error body is {"code": ..., "message": ...}. A JSON-RPC and a
    # Tendermint error body is an envelope with an "error" member.
    error_member = "code" if interface == "rest" else "error"
    assert error_member in json.loads(raw), f"{key} did not answer an error: HTTP {status} {raw!r}"
    assert headers["Content-Type"] == f"application/json; sim-provider={provider['name']}{_CALLER}"


def test_a_jsonrpc_rate_limit_reply_keeps_its_charset_and_carries_the_provider_name(sim):
    """The body of this reply is plain text, so the media type has a parameter of its own."""
    provider, port = _eth_provider(sim)
    _set_scenario(sim, _ETH_KEY, mode="rate_limit")
    status, _, headers = _ask(provider, "jsonrpc", port)
    assert status == 429
    assert headers["Content-Type"] == f"text/plain; charset=utf-8; sim-provider={provider['name']}{_CALLER}"


def test_a_reply_with_an_http_status_override_carries_the_provider_name(sim):
    provider, port = _eth_provider(sim)
    _set_scenario(sim, _ETH_KEY, responses={"eth_blockNumber": {"error_stub": "revert", "http_status": 503}})
    status, _, headers = _ask(provider, "jsonrpc", port)
    assert status == 503
    assert headers["Content-Type"] == f"application/json; sim-provider={provider['name']}{_CALLER}"


def test_a_reply_with_a_body_that_is_not_json_carries_the_provider_name(sim):
    provider, port = _eth_provider(sim)
    _set_scenario(sim, _ETH_KEY, mode="success", corruption_mode="invalid_json")
    status, raw, headers = _ask(provider, "jsonrpc", port)
    assert status == 200
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    assert headers["Content-Type"] == f"application/json; sim-provider={provider['name']}{_CALLER}"


def test_a_reply_with_a_null_body_carries_the_provider_name(sim):
    provider, port = _eth_provider(sim)
    _set_scenario(sim, _ETH_KEY, mode="success", corruption_mode="null_body")
    status, raw, headers = _ask(provider, "jsonrpc", port)
    assert status == 200
    assert raw == b"null"
    assert headers["Content-Type"] == f"application/json; sim-provider={provider['name']}{_CALLER}"


@pytest.mark.parametrize("drop_at", ["after_headers", "mid_body"])
def test_a_reply_that_is_cut_after_its_headers_carries_the_provider_name(sim, drop_at):
    """The simulator sends the whole header block and then closes the connection."""
    provider, port = _eth_provider(sim)
    _set_scenario(sim, _ETH_KEY, mode="drop_connection", drop_at=drop_at)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        body = json.dumps(_rpc("eth_blockNumber")).encode()
        connection.request("POST", "/", body=body, headers={"Content-Type": "application/json"})
        reply = connection.getresponse()
        assert reply.headers["Content-Type"] == f"application/json; sim-provider={provider['name']}{_CALLER}"
        # The reply is the cut one: it ends before the length that its header promised.
        with pytest.raises(http.client.IncompleteRead):
            reply.read()
    finally:
        connection.close()


def test_the_error_page_for_a_verb_that_the_port_does_not_serve_carries_the_provider_name(sim):
    """No code of the simulator writes this reply: ``http.server`` does, for a GET on a jsonrpc port."""
    provider, port = _eth_provider(sim)
    status, raw, headers = _send("GET", f"http://127.0.0.1:{port}/")
    assert status == 501
    assert raw, "the error page has a body, so it has a Content-Type"
    page_type = BaseHTTPRequestHandler.error_content_type
    assert headers["Content-Type"] == f"{page_type}; sim-provider={provider['name']}{_CALLER}"


def test_a_rest_options_reply_with_a_body_carries_the_provider_name(sim):
    """No listener plans an OPTIONS reply. The one for a path with no route has
    a body, so it has a ``Content-Type``."""
    _, provider, port = _first_provider_on(sim, "rest")
    status, _, headers = _send("OPTIONS", f"http://127.0.0.1:{port}/unknown")
    assert status == 404
    assert headers["Content-Type"] == f"application/json; sim-provider={provider['name']}{_CALLER}"


# ── What stays as it is ───────────────────────────────────────────────────────


def test_the_body_of_a_reply_is_the_same_bytes_with_the_parameter_on_the_header(sim):
    """The name is on the header only. A cross-validation of a router compares
    reply bodies, so a name in a body would make two honest providers disagree."""
    provider, port = _eth_provider(sim)
    url = f"http://127.0.0.1:{port}"

    _, raw, _ = _send("POST", url, _rpc("eth_chainId"))
    assert raw == b'{"jsonrpc": "2.0", "id": 1, "result": "0x1"}'
    assert provider["name"].encode() not in raw

    _set_scenario(sim, _ETH_KEY, mode="rate_limit")
    _, raw, _ = _send("POST", url, _rpc("eth_chainId"))
    assert raw == b"Rate limit exceeded. Reduce your request rate, or use an API key for a higher limit."


def test_a_down_reply_has_no_content_type_header(sim):
    provider, port = _eth_provider(sim)
    _set_scenario(sim, _ETH_KEY, mode="down")
    status, _, headers = _ask(provider, "jsonrpc", port)
    assert status == 503
    assert headers["Content-Type"] is None


def test_a_rest_options_reply_with_no_body_has_no_content_type_header(sim):
    _, _, port = _first_provider_on(sim, "rest")
    status, raw, headers = _send("OPTIONS", f"http://127.0.0.1:{port}{_BLOCKS_LATEST}")
    assert status == 204
    assert raw == b""
    assert "GET" in headers["Allow"]
    assert headers["Content-Type"] is None


@pytest.mark.parametrize("path", ["/providers", "/health", "/scenario", "/stats", "/history", "/no-such-route"])
def test_a_control_api_reply_carries_no_provider_name(sim, path):
    _, _, headers = _send("GET", f"{sim['control']}{path}")
    assert headers["Content-Type"] == "application/json"


def test_a_control_api_reply_to_a_post_carries_no_provider_name(sim):
    _, _, headers = _send("POST", f"{sim['control']}/reset/all", {})
    assert headers["Content-Type"] == "application/json"


# ── The value of the parameter ────────────────────────────────────────────────


def test_a_name_of_token_characters_is_written_as_a_plain_token():
    from server import _content_type_with_provider_name

    assert (
        _content_type_with_provider_name("application/json", "EthPrimaryProvider1")
        == "application/json; sim-provider=EthPrimaryProvider1"
    )


def test_a_name_with_a_character_that_is_not_a_token_character_is_written_as_a_quoted_string():
    from server import _content_type_with_provider_name

    assert (
        _content_type_with_provider_name("application/json", "Eth Primary 1")
        == 'application/json; sim-provider="Eth Primary 1"'
    )


def test_a_quote_and_a_backslash_in_a_quoted_name_each_get_a_backslash_before_them():
    from server import _content_type_with_provider_name

    assert (
        _content_type_with_provider_name("application/json", 'a"b\\c') == 'application/json; sim-provider="a\\"b\\\\c"'
    )


def test_a_parameter_of_the_media_type_stays_before_the_provider_name():
    from server import _content_type_with_provider_name

    assert (
        _content_type_with_provider_name("text/plain; charset=utf-8", "EthPrimaryProvider1")
        == "text/plain; charset=utf-8; sim-provider=EthPrimaryProvider1"
    )


def test_an_empty_provider_name_adds_no_parameter():
    from server import _content_type_with_provider_name

    assert _content_type_with_provider_name("application/json", "") == "application/json"
