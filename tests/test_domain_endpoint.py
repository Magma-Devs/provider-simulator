import dataclasses

import pytest

from provider_simulator.domain.endpoint import INTERFACES, TRANSPORTS, Endpoint


def test_endpoint_holds_interface_transport_port():
    ep = Endpoint(interface="jsonrpc", transport="http", port=18545)
    assert ep.interface == "jsonrpc"
    assert ep.transport == "http"
    assert ep.port == 18545


def test_endpoint_is_frozen():
    ep = Endpoint(interface="jsonrpc", transport="ws", port=18557)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ep.port = 9999  # type: ignore[misc]


def test_endpoint_is_hashable_and_value_equal():
    a = Endpoint("jsonrpc", "http", 18545)
    b = Endpoint("jsonrpc", "http", 18545)
    assert a == b
    assert len({a, b}) == 1


def test_vocabularies_are_disjoint():
    # An interface name must never double as a transport name — the scenario
    # transports filter relies on the two vocabularies not overlapping.
    assert not set(INTERFACES) & set(TRANSPORTS)
