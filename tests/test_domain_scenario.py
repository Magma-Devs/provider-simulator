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


# ── the GraphQL forms of a rate limit, an error and an unreadable body ────────


def test_the_graphql_fields_default_to_the_status_style_a_generated_error_and_the_default_reply():
    snap = ScenarioConfig().snapshot()
    assert snap["rate_limit_style"] == "status"
    assert snap["error_body"] is None
    assert snap["unreadable_reply"] is None


def test_rate_limit_style_accepts_status_and_body_and_refuses_another_word():
    s = ScenarioConfig()
    s.update({"rate_limit_style": "body"})
    assert s.snapshot()["rate_limit_style"] == "body"
    with pytest.raises(ValueError) as raised:
        s.update({"rate_limit_style": "loud"})
    assert "status" in str(raised.value) and "body" in str(raised.value)
    assert s.snapshot()["rate_limit_style"] == "body"  # a refused update writes nothing


def test_error_body_accepts_any_json_value_or_text():
    s = ScenarioConfig()
    for value in ({"error": "Invalid API key"}, [1, 2], "plain text", 5, None):
        s.update({"error_body": value})
        assert s.snapshot()["error_body"] == value


def test_unreadable_reply_accepts_a_status_with_a_body_and_none():
    s = ScenarioConfig()
    s.update({"unreadable_reply": {"status": 502, "body": "bad gateway"}})
    assert s.snapshot()["unreadable_reply"] == {"status": 502, "body": "bad gateway"}
    s.update({"unreadable_reply": {"status": 200, "body": {"data": None}}})
    s.update({"unreadable_reply": {"status": 100, "body": ""}})
    s.update({"unreadable_reply": {"status": 599}})
    s.update({"unreadable_reply": None})
    assert s.snapshot()["unreadable_reply"] is None


@pytest.mark.parametrize(
    "bad",
    [
        "bad gateway",
        502,
        [502],
        {},
        {"body": "x"},
        {"status": "502", "body": "x"},
        {"status": True, "body": "x"},
        {"status": 502.0, "body": "x"},
        {"status": 99, "body": "x"},
        {"status": 600, "body": "x"},
        {"status": 502, "bodyy": "x"},
    ],
)
def test_unreadable_reply_refuses_anything_but_a_status_from_100_to_599_and_a_body(bad):
    s = ScenarioConfig()
    with pytest.raises(ValueError, match="unreadable_reply"):
        s.update({"unreadable_reply": bad})
    assert s.snapshot()["unreadable_reply"] is None


def test_reset_restores_the_three_graphql_fields():
    s = ScenarioConfig()
    s.update({"rate_limit_style": "body", "error_body": {"x": 1}, "unreadable_reply": {"status": 502, "body": "x"}})
    s.reset()
    snap = s.snapshot()
    assert (snap["rate_limit_style"], snap["error_body"], snap["unreadable_reply"]) == ("status", None, None)
