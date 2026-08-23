import pytest

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
    assert snap["rate_limit_body"] == (
        "Rate limit exceeded. Reduce your request rate, or use an API key for a higher limit."
    )


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
    with pytest.raises(ValueError, match="slot_offset"):
        s.update({"slot_offset": 3})  # a Solana quirk, not a universal field


def test_unknown_transport_value_is_rejected():
    s = ScenarioConfig()
    # "grpc" is an interface, not a transport — accepting it would make the
    # fault silently match zero endpoints.
    with pytest.raises(ValueError, match="grpc"):
        s.update({"mode": "error", "transports": ["grpc"]})
    # the whole update aborted — mode unchanged
    assert s.snapshot()["mode"] == "success"


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


def test_snapshot_responses_edits_do_not_touch_live_config():
    s = ScenarioConfig()
    s.update({"responses": {"eth_call": {"result": "0x1"}}})
    snap = s.snapshot()
    snap["responses"]["eth_call"] = "CORRUPTED"
    assert s.snapshot()["responses"] == {"eth_call": {"result": "0x1"}}
