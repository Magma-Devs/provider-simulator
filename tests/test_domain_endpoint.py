from provider_simulator.domain.endpoint import Endpoint


def test_endpoint_holds_interface_transport_port():
    ep = Endpoint(interface="jsonrpc", transport="http", port=18545)
    assert ep.interface == "jsonrpc"
    assert ep.transport == "http"
    assert ep.port == 18545


def test_endpoint_is_frozen():
    ep = Endpoint(interface="jsonrpc", transport="ws", port=18557)
    try:
        ep.port = 9999
    except Exception as exc:  # FrozenInstanceError is an AttributeError subclass
        assert "assign" in str(exc).lower() or "frozen" in type(exc).__name__.lower()
    else:
        raise AssertionError("Endpoint should be immutable")


def test_endpoint_is_hashable_and_value_equal():
    a = Endpoint("jsonrpc", "http", 18545)
    b = Endpoint("jsonrpc", "http", 18545)
    assert a == b
    assert len({a, b}) == 1
