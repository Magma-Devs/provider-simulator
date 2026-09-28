from provider_simulator import fault_policy
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.domain.scenario import ScenarioConfig

HTTP = Endpoint("jsonrpc", "http", 18545)
WS = Endpoint("jsonrpc", "ws", 18557)

# One provider, the SAME interface and the SAME transport, TWO addresses. This is
# the shape eth-failover-twoaddr-sim has, and the shape the transports filter
# cannot describe — both of these are jsonrpc/http, so no transport list tells
# them apart. Written here rather than reused from the topology so these tests
# keep working if that pool is renamed or retired.
ADDR_A = Endpoint("jsonrpc", "http", 18638)
ADDR_B = Endpoint("jsonrpc", "http", 18639)


def _provider():
    return Pool(name="eth-sim", chain="eth").add_provider("1", [HTTP, WS])


def _two_address_provider():
    return Pool(name="eth-twoaddr-sim", chain="eth").add_provider("1", [ADDR_A, ADDR_B])


def _sc(**kw):
    sc = ScenarioConfig()
    if kw:
        sc.update(kw)
    return sc.snapshot()


def test_success_scenario_is_none():
    v = fault_policy.decide(_sc(), HTTP, _provider())
    assert v.kind == "none"


def test_down_with_no_filter_hits_every_endpoint():
    p = _provider()
    assert fault_policy.decide(_sc(mode="down"), HTTP, p).kind == "down"
    assert fault_policy.decide(_sc(mode="down"), WS, p).kind == "down"


def test_transports_filter_scopes_the_fault():
    p = _provider()
    sc = _sc(mode="down", transports=["ws"])
    assert fault_policy.decide(sc, HTTP, p).kind == "none"  # http not targeted
    assert fault_policy.decide(sc, WS, p).kind == "down"  # ws targeted


def test_error_verdict_carries_fields():
    v = fault_policy.decide(
        _sc(mode="error", error_code=-32050, error_message="boom", http_status=502),
        HTTP,
        _provider(),
    )
    assert v.kind == "error"
    assert v.status == 502
    assert v.error_code == -32050
    assert v.error_message == "boom"


def test_rate_limit_verdict():
    v = fault_policy.decide(_sc(mode="rate_limit"), HTTP, _provider())
    assert (v.kind, v.status, v.error_code) == ("rate_limit", 429, 429)


def test_rate_limit_verdict_default_body_is_prose():
    """error_message stays "Too many requests" (REST/Tendermint/gRPC still
    read it unchanged); rate_limit_body is the separate JSON-RPC-only prose
    field jsonrpc.py's build_fault reads."""
    v = fault_policy.decide(_sc(mode="rate_limit"), HTTP, _provider())
    assert v.error_message == "Too many requests"
    assert v.rate_limit_body == ("Rate limit exceeded. Reduce your request rate, or use an API key for a higher limit.")


def test_rate_limit_verdict_body_overridable():
    v = fault_policy.decide(
        _sc(mode="rate_limit", rate_limit_body="Slow down."),
        HTTP,
        _provider(),
    )
    assert v.rate_limit_body == "Slow down."
    # The override is scoped to rate_limit_body only — error_message (read
    # by REST/Tendermint/gRPC) is untouched.
    assert v.error_message == "Too many requests"


def test_hang_verdict():
    assert fault_policy.decide(_sc(mode="hang"), HTTP, _provider()).kind == "hang"


def test_drop_verdict_carries_drop_at():
    v = fault_policy.decide(_sc(mode="drop_connection", drop_at="mid_body"), HTTP, _provider())
    assert v.kind == "drop"
    assert v.drop_at == "mid_body"


def test_error_probability_one_always_errors():
    v = fault_policy.decide(_sc(error_probability=1.0), HTTP, _provider())
    assert v.kind == "error"


def test_fail_first_n_faults_then_recovers():
    p = _provider()
    sc = _sc(mode="error", fail_first_n=2)  # then_mode defaults to success
    assert fault_policy.decide(sc, HTTP, p).kind == "error"  # 1st
    assert fault_policy.decide(sc, HTTP, p).kind == "error"  # 2nd
    assert fault_policy.decide(sc, HTTP, p).kind == "none"  # 3rd recovers


def test_fail_first_n_only_targeted_endpoints_burn_the_window():
    p = _provider()
    sc = _sc(mode="error", fail_first_n=1, transports=["http"])
    # A ws request is not targeted — it neither faults nor advances the counter.
    assert fault_policy.decide(sc, WS, p).kind == "none"
    assert p.peek_fail() == 0
    # The first http request faults and consumes; the second recovers.
    assert fault_policy.decide(sc, HTTP, p).kind == "error"
    assert fault_policy.decide(sc, HTTP, p).kind == "none"


# ── the ports filter: naming ONE address of two ───────────────────────────────


def test_a_fault_with_no_ports_filter_reaches_both_addresses():
    """The behaviour before this filter existed, kept as the baseline. Without it
    the next test proves nothing: 'one address is silenced' means nothing if the
    unfiltered case had silenced only one too."""
    p = _two_address_provider()
    sc = _sc(mode="down")
    assert fault_policy.decide(sc, ADDR_A, p).kind == "down"
    assert fault_policy.decide(sc, ADDR_B, p).kind == "down"


def test_the_ports_filter_silences_one_address_and_leaves_the_other_serving():
    """The whole reason this field exists.

    A provider with two node-urls, one silenced: the router must move the traffic
    to the other. Before this filter a fault reached both addresses, so that test
    could not be written — and the pool built for it, eth-failover-twoaddr-sim,
    was inert.
    """
    p = _two_address_provider()
    sc = _sc(mode="down", ports=[18638])
    assert fault_policy.decide(sc, ADDR_A, p).kind == "down", "18638 was named and must fault"
    assert fault_policy.decide(sc, ADDR_B, p).kind == "none", "18639 was not named and must serve"


def test_the_ports_filter_works_the_other_way_round_too():
    """The mirror of the test above. A filter that always faulted the FIRST
    endpoint of the list, or always the lowest port, would pass that test and fail
    this one."""
    p = _two_address_provider()
    sc = _sc(mode="down", ports=[18639])
    assert fault_policy.decide(sc, ADDR_A, p).kind == "none"
    assert fault_policy.decide(sc, ADDR_B, p).kind == "down"


def test_a_ports_filter_naming_both_addresses_faults_both():
    p = _two_address_provider()
    sc = _sc(mode="down", ports=[18638, 18639])
    assert fault_policy.decide(sc, ADDR_A, p).kind == "down"
    assert fault_policy.decide(sc, ADDR_B, p).kind == "down"


def test_transports_and_ports_and_together():
    """Two filters, both set. An endpoint has to satisfy BOTH to be targeted. A
    policy that ORed them would fault the ws endpoint here, because its transport
    matches even though its port does not."""
    p = _provider()  # http 18545, ws 18557
    sc = _sc(mode="down", transports=["ws"], ports=[18545])
    # ws matches the transport but not the port; http matches the port but not the
    # transport. Neither satisfies both, so neither faults.
    assert fault_policy.decide(sc, WS, p).kind == "none"
    assert fault_policy.decide(sc, HTTP, p).kind == "none"
    # And the pair that does satisfy both faults.
    sc = _sc(mode="down", transports=["ws"], ports=[18557])
    assert fault_policy.decide(sc, WS, p).kind == "down"
    assert fault_policy.decide(sc, HTTP, p).kind == "none"


def test_only_the_named_address_burns_the_fail_first_n_window():
    """The stateful half has to respect the new filter too. An untargeted address
    must neither fault nor advance the counter — otherwise a request to the
    address a test deliberately left healthy would spend the window, and the
    address under test would recover a request early for no visible reason."""
    p = _two_address_provider()
    sc = _sc(mode="error", fail_first_n=1, ports=[18638])
    assert fault_policy.decide(sc, ADDR_B, p).kind == "none"
    assert p.peek_fail() == 0, "the unnamed address must not consume the window"
    assert fault_policy.decide(sc, ADDR_A, p).kind == "error"
    assert p.peek_fail() == 1
    assert fault_policy.decide(sc, ADDR_A, p).kind == "none", "window spent, recovers"
