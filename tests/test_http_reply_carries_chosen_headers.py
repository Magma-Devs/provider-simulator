"""A provider adds the headers that its scenario chose to each HTTP reply.

A real provider can answer a 429 with ``Retry-After``, and a smart-router reads
that header to decide how long it holds the provider off. The scenario field
``reply_headers`` makes a simulated provider send it, or any other header, as
``{name: value}``.

The HTTP adapter writes the headers on each reply of a targeted ``http``
endpoint that has a status line: a success, a fault, the bodiless 503 of
``down`` and the headers of a reply that ``drop_connection`` cuts. A chosen
header replaces the header of the same name that the simulator writes itself,
such as ``Date``. ``tests/test_domain_scenario.py`` and
``tests/test_control_api.py`` hold the tests of the headers that are refused.

The helpers that ask a provider and set a scenario are the ones of
``tests/test_http_reply_names_its_provider.py``, which tests another header of
the same replies.
"""

import http.client
import json

import pytest

from tests.test_http_reply_names_its_provider import (
    _ETH_KEY,
    _HTTP_INTERFACES,
    _SUCCESS_REQUEST,
    _ask,
    _eth_provider,
    _first_provider_on,
    _http_ports,
    _providers,
    _send,
    _set_scenario,
)

_CHOSEN = {"Retry-After": "7", "X-RateLimit-Remaining": "0"}


@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario config AND clear history before and after every test."""
    _send("POST", f"{sim['control']}/reset/all", {})
    yield
    _send("POST", f"{sim['control']}/reset/all", {})


@pytest.mark.parametrize("interface", _HTTP_INTERFACES)
def test_a_rate_limit_reply_carries_the_chosen_headers(sim, interface):
    """The case the field exists for: a 429 with ``Retry-After``. The reply
    still names the provider in its Content-Type, so a chosen header takes
    nothing away."""
    key, provider, port = _first_provider_on(sim, interface)
    status, _, headers = _ask(provider, interface, port)
    assert status == 200 and headers.get_all("Retry-After") is None, "a provider with no scenario sends no Retry-After"

    _set_scenario(sim, key, mode="rate_limit", reply_headers=_CHOSEN)
    status, raw, headers = _ask(provider, interface, port)
    assert status == 429, f"{key} did not answer the rate limit: HTTP {status} {raw!r}"
    for name, value in _CHOSEN.items():
        assert headers.get_all(name) == [value], f"{key}: {name} is {headers.get_all(name)!r}, chosen {value!r}"
    assert f"sim-provider={provider['name']}" in headers["Content-Type"], headers["Content-Type"]


def test_a_success_reply_carries_the_chosen_headers(sim):
    provider, port = _eth_provider(sim)
    _set_scenario(sim, _ETH_KEY, reply_headers=_CHOSEN)
    status, _, headers = _ask(provider, "jsonrpc", port)
    assert status == 200
    for name, value in _CHOSEN.items():
        assert headers.get_all(name) == [value], f"{name} is {headers.get_all(name)!r}, chosen {value!r}"


def test_the_bodiless_reply_of_down_carries_the_chosen_headers(sim):
    """A 503 can carry ``Retry-After`` too. The reply keeps having no body and no Content-Type."""
    provider, port = _eth_provider(sim)
    _set_scenario(sim, _ETH_KEY, mode="down", reply_headers=_CHOSEN)
    status, raw, headers = _ask(provider, "jsonrpc", port)
    assert status == 503 and raw == b""
    assert headers.get_all("Retry-After") == ["7"], headers.items()
    assert headers.get_all("Content-Type") is None, headers.items()


def test_the_headers_of_a_reply_that_drop_connection_cuts_carry_the_chosen_headers(sim):
    """``after_headers`` sends a status line and headers, then closes with no body."""
    provider, port = _eth_provider(sim)
    _set_scenario(sim, _ETH_KEY, mode="drop_connection", drop_at="after_headers", reply_headers=_CHOSEN)
    method, _, body = _SUCCESS_REQUEST[(provider["chain"], "jsonrpc")]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, "/", body=json.dumps(body), headers={"Content-Type": "application/json"})
        reply = connection.getresponse()
        assert reply.status == 200
        assert reply.headers.get_all("Retry-After") == ["7"], reply.headers.items()
        with pytest.raises(http.client.IncompleteRead):
            reply.read()
    finally:
        connection.close()


def test_a_chosen_date_replaces_the_date_that_the_simulator_writes(sim):
    """A router measures an HTTP-date ``Retry-After`` against the ``Date`` of the
    provider. So a test of a skewed clock chooses both, and the reply must hold
    one ``Date``: the chosen one."""
    provider, port = _eth_provider(sim)
    chosen = {"Retry-After": "Wed, 04 Nov 2026 10:00:30 GMT", "Date": "Wed, 04 Nov 2026 10:00:00 GMT"}
    _set_scenario(sim, _ETH_KEY, mode="rate_limit", reply_headers=chosen)
    status, _, headers = _ask(provider, "jsonrpc", port)
    assert status == 429
    assert headers.get_all("Date") == [chosen["Date"]], headers.items()
    assert headers.get_all("Retry-After") == [chosen["Retry-After"]], headers.items()


def test_an_endpoint_outside_the_filter_of_the_block_sends_no_chosen_header(sim):
    """A provider that serves jsonrpc over http on two ports. The block names one
    port, so the other port answers as if no header were chosen."""
    key, provider = next(
        (key, provider) for key, provider in _providers(sim).items() if len(_http_ports(provider, "jsonrpc")) >= 2
    )
    first, second = _http_ports(provider, "jsonrpc")[:2]
    _set_scenario(sim, key, reply_headers=_CHOSEN, ports=[first])

    status, _, headers = _ask(provider, "jsonrpc", first)
    assert status == 200 and headers.get_all("Retry-After") == ["7"], f"{key} on port {first}: {headers.items()}"
    status, _, headers = _ask(provider, "jsonrpc", second)
    assert status == 200 and headers.get_all("Retry-After") is None, f"{key} on port {second}: {headers.items()}"


def test_a_reset_removes_the_chosen_headers(sim):
    provider, port = _eth_provider(sim)
    _set_scenario(sim, _ETH_KEY, reply_headers=_CHOSEN)
    _, _, headers = _ask(provider, "jsonrpc", port)
    assert headers.get_all("Retry-After") == ["7"], "the headers must be on the reply before the reset"
    _send("POST", f"{sim['control']}/reset/all", {})
    status, _, headers = _ask(provider, "jsonrpc", port)
    assert status == 200 and headers.get_all("Retry-After") is None, headers.items()
