from provider_simulator.domain.scenario import ScenarioConfig


def test_defaults_are_a_healthy_provider():
    s = ScenarioConfig()
    snap = s.snapshot()
    assert snap["mode"] == "success"
    assert snap["latency_ms"] == 0
    assert snap["error_code"] == -32000
    assert snap["http_status"] == 200
    assert snap["responses"] == {}
    assert snap["transports"] is None


def test_update_sets_a_fault():
    s = ScenarioConfig()
    s.update({"mode": "down"})
    assert s.snapshot()["mode"] == "down"


def test_update_sets_transport_filter():
    s = ScenarioConfig()
    s.update({"mode": "error", "transports": ["ws"]})
    snap = s.snapshot()
    assert snap["mode"] == "error"
    assert snap["transports"] == ["ws"]


def test_unknown_field_is_rejected_with_clear_message():
    s = ScenarioConfig()
    try:
        s.update({"slot_offset": 3})  # a Solana quirk, not a universal field
    except ValueError as exc:
        assert "slot_offset" in str(exc)
    else:
        raise AssertionError("a quirk key must not be accepted by ScenarioConfig")


def test_reset_clears_a_prior_fault():
    s = ScenarioConfig()
    s.update({"mode": "hang", "latency_ms": 500, "responses": {"eth_call": {"result": "0x1"}}})
    s.reset()
    snap = s.snapshot()
    assert snap["mode"] == "success"
    assert snap["latency_ms"] == 0
    assert snap["responses"] == {}


def test_two_instances_do_not_share_responses_dict():
    a = ScenarioConfig()
    b = ScenarioConfig()
    a.update({"responses": {"eth_call": {"result": "0x1"}}})
    assert b.snapshot()["responses"] == {}
