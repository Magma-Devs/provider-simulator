from provider_simulator import fault_policy
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.domain.scenario import ScenarioConfig

HTTP = Endpoint("jsonrpc", "http", 18545)
WS = Endpoint("jsonrpc", "ws", 18557)


def _provider():
    return Pool(name="eth-sim", chain="eth").add_provider("1", [HTTP, WS])


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
