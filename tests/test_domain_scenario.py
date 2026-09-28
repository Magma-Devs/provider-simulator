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


# ── the ports filter's shape ──────────────────────────────────────────────────
#
# This class holds no endpoints, so it can only judge the SHAPE of the list. A
# port the provider does not serve is the control API's to refuse, and
# tests/test_control_api.py covers that half.


def test_an_empty_ports_list_is_rejected():
    """The two readings of [] are opposite — "target nothing" and "target
    everything" — so it is refused rather than guessed at. A caller that built
    the list from a filter which matched nothing would get "everything" when it
    meant "nothing", and a fault would land on every address it was scoping away
    from."""
    s = ScenarioConfig()
    with pytest.raises(ValueError, match="must not be empty"):
        s.update({"mode": "down", "ports": []})
    assert s.snapshot()["mode"] == "success", "the whole update aborts"


def test_a_ports_list_of_the_wrong_type_is_rejected():
    s = ScenarioConfig()
    with pytest.raises(ValueError, match="integers"):
        s.update({"mode": "down", "ports": ["18638"]})
    assert s.snapshot()["mode"] == "success"


def test_a_ports_value_outside_the_tcp_range_is_rejected():
    s = ScenarioConfig()
    with pytest.raises(ValueError, match=r"\[1, 65535\]"):
        s.update({"mode": "down", "ports": [70000]})


def test_a_boolean_is_not_a_port():
    """bool subclasses int, so True would otherwise pass as port 1 — a real port
    somebody could plausibly be serving on."""
    s = ScenarioConfig()
    with pytest.raises(ValueError, match="integers"):
        s.update({"mode": "down", "ports": [True]})


def test_ports_not_given_is_not_the_same_as_ports_empty():
    """None means every endpoint and is the default; [] is refused above. Pinned
    so a later change cannot quietly make the default an empty list."""
    s = ScenarioConfig()
    s.update({"mode": "down"})
    assert s.snapshot()["ports"] is None
