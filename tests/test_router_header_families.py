"""The two families of the router's header names, served at once.

A router before v1.7.0 sends ``lava-*`` headers; a router of v1.7.0 or later
sends ``router-*`` headers. A history row keeps each family in a field of its
own, ``GET /history`` filters by each, and the 101 reply to an upgrade names
the provider in a header of each. No port is opened here: the listeners, the
log and the control API are called in process.
"""

import json

import pytest

from provider_simulator.control_api import ControlApi
from provider_simulator.domain.call_log import CallLog
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.domain.registry import build_registry
from provider_simulator.domain.router_headers import ROUTER_HEADER_FAMILIES, headers_by_family
from provider_simulator.listeners import JsonRpcListener, RawRequest
from provider_simulator.listeners.grpc import GrpcListener
from provider_simulator.listeners.ws import JsonRpcWsListener, WsSubscriptions
from server import _upgrade_headers

_HTTP = Endpoint("jsonrpc", "http", 18545)
_WS = Endpoint("jsonrpc", "ws", 18557)
_GRPC = Endpoint("grpc", "http2", 18548)

# A request with headers of both families, in several letter cases, and headers
# of neither family: one whose name holds a family word after another start,
# one whose word has no hyphen, and one with an underscore for the hyphen.
_SENT = {
    "Router-Guid": "r1",
    "ROUTER-STICKINESS": "s1",
    "lava-guid": "l1",
    "Lava-Stateful-Api": "true",
    "X-Router-Guid": "no",
    "X-Lava-Guid": "no",
    "Routerless": "no",
    "Lavender": "no",
    "router_guid": "no",
    "Content-Type": "application/json",
}
_ROUTER_ONLY = {"Router-Guid": "r1", "ROUTER-STICKINESS": "s1"}
_LAVA_ONLY = {"lava-guid": "l1", "Lava-Stateful-Api": "true"}


def _jsonrpc():
    provider = Pool(name="eth-sim", chain="eth").add_provider("1", [_HTTP])
    return JsonRpcListener(provider, _HTTP), provider


# ── The split ─────────────────────────────────────────────────────────────────


def test_the_table_names_each_family_once():
    assert [(f.request_prefix, f.row_field, f.filter_prefix, f.reply_prefix) for f in ROUTER_HEADER_FAMILIES] == [
        ("router-", "router_headers", "router_header_", "Router-"),
        ("lava-", "lava_headers", "lava_header_", "Lava-"),
    ]


def test_the_split_puts_each_family_in_its_own_field_and_no_header_of_neither_family():
    assert headers_by_family(_SENT) == {"router_headers": _ROUTER_ONLY, "lava_headers": _LAVA_ONLY}


def test_the_split_of_no_headers_gives_both_fields_empty():
    assert headers_by_family(None) == {"router_headers": {}, "lava_headers": {}}
    assert headers_by_family({"X-Other": "1"}) == {"router_headers": {}, "lava_headers": {}}


# ── History rows ──────────────────────────────────────────────────────────────


def test_an_http_row_keeps_each_family_in_its_own_field_and_every_header_in_headers():
    listener, provider = _jsonrpc()
    listener.serve(RawRequest(body=b'{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber"}', headers=dict(_SENT)))
    row = provider.log.get_history()[0]
    assert row["router_headers"] == _ROUTER_ONLY
    assert row["lava_headers"] == _LAVA_ONLY
    assert row["headers"] == _SENT


@pytest.mark.parametrize(
    "sent",
    [{}, {"X-Other": "1"}, {"Router-Guid": "r1"}, {"lava-guid": "l1"}],
    ids=["none", "neither", "router", "lava"],
)
def test_a_row_always_has_both_fields(sent):
    listener, provider = _jsonrpc()
    listener.arrive(sent)
    row = provider.log.get_history()[0]
    assert row["router_headers"] == headers_by_family(sent)["router_headers"]
    assert row["lava_headers"] == headers_by_family(sent)["lava_headers"]


def test_a_pushed_row_with_no_headers_has_both_fields_empty():
    log = CallLog(pool="eth-sim", pid="1", name="EthProvider1")
    log.push("eth_subscription push", "success", 0, interface="jsonrpc", transport="ws", port=18557)
    row = log.get_history()[0]
    assert (row["router_headers"], row["lava_headers"]) == ({}, {})


def test_the_row_of_a_refused_upgrade_keeps_each_family_in_its_own_field():
    provider = Pool(name="eth-sim", chain="eth").add_provider("1", [_HTTP, _WS])
    listener = JsonRpcWsListener(provider, _WS, WsSubscriptions())
    provider.scenario.update({"mode": "rate_limit"})

    listener.decide_upgrade(dict(_SENT))

    row = provider.log.get_history()[0]
    assert row["method"] == "ws_upgrade"
    assert row["router_headers"] == _ROUTER_ONLY
    assert row["lava_headers"] == _LAVA_ONLY
    assert row["headers"] == _SENT


def test_a_grpc_row_keeps_each_family_and_a_bytes_value_as_base64_text():
    provider = Pool(name="lava-sim-grpc", chain="lava").add_provider("1", [_GRPC])
    listener = GrpcListener(provider, _GRPC)
    sent = {"router-x-bin": b"\x00\x01", "router-guid": "r1", "lava-x-bin": b"\x00\x02", "user-agent": "grpc-go"}

    listener.serve(RawRequest(path="GetLatestBlock", headers=sent, message=None))

    row = provider.log.get_history()[0]
    assert row["router_headers"] == {"router-x-bin": "AAE=", "router-guid": "r1"}
    assert row["lava_headers"] == {"lava-x-bin": "AAI="}
    assert json.loads(json.dumps(provider.log.get_history())) == provider.log.get_history()


@pytest.mark.parametrize("field", ["router_headers", "lava_headers"])
def test_a_reader_that_changes_a_family_field_does_not_change_the_log(field):
    log = CallLog(pool="eth-sim", pid="1", name="EthProvider1")
    log.record_arrival("jsonrpc", "http", 18545, headers=dict(_SENT), **headers_by_family(_SENT))
    log.get_history()[0][field]["Injected"] = "x"
    assert "Injected" not in log.get_history()[0][field]


# ── GET /history ──────────────────────────────────────────────────────────────

_TOPOLOGY = (("eth-sim", "eth", "1", "EthProvider1", False, "", (("jsonrpc", "http", 41101),)),)

# request id -> the request headers of that row
_ROWS = {
    1: {"Router-Guid": "a"},
    2: {"lava-guid": "a"},
    3: {"router-guid": "a", "Lava-Guid": "a"},
    4: {"Router-Guid": "a", "Router-Stickiness": "s1"},
    5: {"X-Router-Guid": "a", "router_guid": "a"},
    6: {},
}


def _filtered(query):
    api = ControlApi(build_registry(_TOPOLOGY), WsSubscriptions())
    provider = api.registry.provider("eth-sim", "1")
    for request_id, sent in _ROWS.items():
        provider.log.push(
            "eth_blockNumber",
            "success",
            0,
            interface="jsonrpc",
            transport="http",
            port=41101,
            request_id=request_id,
            headers=sent,
            **headers_by_family(sent),
        )
    status, body = api.get_history(query)
    assert status == 200, body
    return [row["request_id"] for row in body["history"]]


@pytest.mark.parametrize(
    "query, kept",
    [
        ({"router_header_router-guid": "a"}, [1, 3, 4]),
        ({"router_header_ROUTER-GUID": "a"}, [1, 3, 4]),
        ({"lava_header_lava-guid": "a"}, [2, 3]),
        ({"lava_header_LAVA-GUID": "a"}, [2, 3]),
        # A filter reads the field of its own family only.
        ({"router_header_lava-guid": "a"}, []),
        ({"lava_header_router-guid": "a"}, []),
        # Several filters AND together, within a family and across the two.
        ({"router_header_router-guid": "a", "router_header_router-stickiness": "s1"}, [4]),
        ({"router_header_router-guid": "a", "lava_header_lava-guid": "a"}, [3]),
        # The value is compared as it is written, and the name is the header name
        # as it is written: an underscore is not read as a hyphen.
        ({"router_header_router-guid": "A"}, []),
        ({"router_header_router_guid": "a"}, []),
        # A header of neither family is matched by neither filter.
        ({"router_header_x-router-guid": "a"}, []),
    ],
    ids=lambda value: "&".join(f"{k}={v}" for k, v in value.items()) if isinstance(value, dict) else None,
)
def test_history_filters_by_each_family(query, kept):
    assert _filtered(query) == kept


# ── The 101 reply to an upgrade ───────────────────────────────────────────────


def test_the_101_reply_names_the_provider_in_both_families():
    assert _upgrade_headers("eth-sim:1", {}) == {
        "Router-Provider-Address": "sim-provider-eth-sim:1",
        "Lava-Provider-Address": "sim-provider-eth-sim:1",
    }


@pytest.mark.parametrize(
    "chosen, kept",
    [
        ({"router-provider-address": "chosen"}, "Lava-Provider-Address"),
        ({"LAVA-PROVIDER-ADDRESS": "chosen"}, "Router-Provider-Address"),
    ],
)
def test_a_chosen_provider_address_replaces_the_header_of_that_name_only(chosen, kept):
    assert _upgrade_headers("eth-sim:1", chosen) == {kept: "sim-provider-eth-sim:1", **chosen}
