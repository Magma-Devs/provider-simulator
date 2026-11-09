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


def test_reply_headers_default_to_none_and_take_text_headers_and_an_empty_object():
    s = ScenarioConfig()
    assert s.snapshot()["reply_headers"] is None
    s.update({"reply_headers": {"Retry-After": "5", "X-Quota-Note": "a b c"}})
    assert s.snapshot()["reply_headers"] == {"Retry-After": "5", "X-Quota-Note": "a b c"}
    s.update({"reply_headers": {}})
    assert s.snapshot()["reply_headers"] == {}


@pytest.mark.parametrize(
    "headers, words",
    [
        pytest.param(["Retry-After", "5"], "must be an object", id="a-list"),
        pytest.param({"Retry After": "5"}, "not an HTTP header name", id="a-space-in-the-name"),
        pytest.param({"Retry-After:": "5"}, "not an HTTP header name", id="a-colon-in-the-name"),
        pytest.param({"": "5"}, "not an HTTP header name", id="an-empty-name"),
        pytest.param({"Retry-After": 5}, "Write a number as text", id="a-number-for-a-value"),
        pytest.param({"Retry-After": "5\r\nX-Added: 1"}, "visible ASCII", id="a-line-break-in-the-value"),
        pytest.param({"X-Note": "café"}, "visible ASCII", id="a-character-outside-ascii"),
        pytest.param({"Retry-After": "5", "retry-after": "6"}, "twice", id="one-name-in-two-letter-cases"),
        pytest.param({"Content-Length": "0"}, "writes it itself", id="content-length"),
        pytest.param({"content-type": "text/html"}, "writes it itself", id="content-type"),
        pytest.param({"Transfer-Encoding": "chunked"}, "writes it itself", id="transfer-encoding"),
        pytest.param({"Connection": "close"}, "writes it itself", id="connection"),
        pytest.param({"Upgrade": "h2c"}, "writes it itself", id="upgrade"),
        pytest.param({"Sec-WebSocket-Accept": "x"}, "writes it itself", id="sec-websocket-accept"),
    ],
)
def test_reply_headers_that_could_not_be_sent_as_written_are_rejected(headers, words):
    s = ScenarioConfig()
    with pytest.raises(ValueError, match=words):
        s.update({"mode": "rate_limit", "reply_headers": headers})
    # the whole update aborted
    assert s.snapshot()["mode"] == "success"
    assert s.snapshot()["reply_headers"] is None


def test_reply_headers_are_accepted_with_any_transports_filter():
    """Every adapter writes the chosen headers: as headers on 'http' and 'ws', and
    as the header metadata of a gRPC reply on 'http2'."""
    s = ScenarioConfig()
    for transports in (["http"], ["ws"], ["http2"], ["http2", "ws"]):
        s.update({"reply_headers": {"Retry-After": "5"}, "transports": transports})
        assert s.snapshot()["transports"] == transports
        assert s.snapshot()["reply_headers"] == {"Retry-After": "5"}


def test_reset_clears_the_reply_headers():
    s = ScenarioConfig()
    s.update({"reply_headers": {"Retry-After": "5"}})
    s.reset()
    assert s.snapshot()["reply_headers"] is None


def test_reply_trailers_default_to_none_and_take_text_values_and_an_empty_object():
    s = ScenarioConfig()
    assert s.snapshot()["reply_trailers"] is None
    s.update({"reply_trailers": {"grpc-retry-pushback-ms": "5000", "Retry-After": "7"}})
    assert s.snapshot()["reply_trailers"] == {"grpc-retry-pushback-ms": "5000", "Retry-After": "7"}
    s.update({"reply_trailers": {}})
    assert s.snapshot()["reply_trailers"] == {}


@pytest.mark.parametrize(
    "trailers, words",
    [
        pytest.param(["grpc-retry-pushback-ms", "5000"], "must be an object", id="a-list"),
        pytest.param({"retry after": "5"}, "not an HTTP header name", id="a-space-in-the-name"),
        pytest.param({"": "5"}, "not an HTTP header name", id="an-empty-name"),
        pytest.param({"retry-after": 5}, "Write a number as text", id="a-number-for-a-value"),
        pytest.param({"retry-after": "5\r\nx-added: 1"}, "visible ASCII", id="a-line-break-in-the-value"),
        pytest.param({"Retry-After": "5", "retry-after": "6"}, "twice", id="one-name-in-two-letter-cases"),
        pytest.param({"content-type": "text/plain"}, "gRPC library", id="content-type"),
        pytest.param({"content-length": "0"}, "gRPC library", id="content-length"),
    ],
)
def test_reply_trailers_that_could_not_be_sent_as_written_are_rejected(trailers, words):
    s = ScenarioConfig()
    with pytest.raises(ValueError, match=words):
        s.update({"mode": "rate_limit", "reply_trailers": trailers})
    # the whole update aborted
    assert s.snapshot()["mode"] == "success"
    assert s.snapshot()["reply_trailers"] is None


# What a gRPC reply cannot carry as written. Measured with the gRPC library of the
# simulator: a tab in a value, a capital letter or a character outside letters,
# digits, '-', '_' and '.' in a name, and a name ending in '-bin' with a text value
# each end the call; grpc-timeout stops the simulator process; the other names are
# dropped or go out with the library's own value. reply_headers reaches the header
# metadata of a gRPC reply, so both fields refuse them.
_REFUSED_ON_THE_GRPC_WIRE = [
    pytest.param({"X-Quota!": "5"}, "not an HTTP header name", id="a-character-a-grpc-name-cannot-hold"),
    pytest.param({"X-Note": "a\tb"}, "visible ASCII", id="a-tab-in-the-value"),
    pytest.param({"te": "trailers"}, "gRPC library", id="te"),
    pytest.param({"grpc-status": "0"}, "gRPC library", id="grpc-status"),
    pytest.param({"grpc-message": "fine"}, "gRPC library", id="grpc-message"),
    pytest.param({"grpc-encoding": "gzip"}, "gRPC library", id="grpc-encoding"),
    pytest.param({"grpc-accept-encoding": "gzip"}, "gRPC library", id="grpc-accept-encoding"),
    pytest.param({"grpc-internal-encoding-request": "gzip"}, "gRPC library", id="grpc-internal-encoding-request"),
    pytest.param({"grpc-previous-rpc-attempts": "2"}, "gRPC library", id="grpc-previous-rpc-attempts"),
    pytest.param({"Grpc-Timeout": "1S"}, "gRPC library", id="grpc-timeout"),
    pytest.param({"X-Trace-Bin": "abc"}, "-bin", id="a-binary-name"),
    pytest.param({"grpc-retry-pushback-ms": "1.5"}, "whole number of milliseconds", id="a-pushback-of-a-fraction"),
    pytest.param({"grpc-retry-pushback-ms": "soon"}, "whole number of milliseconds", id="a-pushback-of-words"),
    pytest.param(
        {"grpc-retry-pushback-ms": "9223372036854775808"},
        "whole number of milliseconds",
        id="a-pushback-above-the-signed-64-bit-range",
    ),
    pytest.param(
        {"grpc-retry-pushback-ms": "-9223372036854775809"},
        "whole number of milliseconds",
        id="a-pushback-below-the-signed-64-bit-range",
    ),
    pytest.param(
        {"grpc-retry-pushback-ms": "0007"}, "whole number of milliseconds", id="a-pushback-with-a-leading-zero"
    ),
]


@pytest.mark.parametrize("field_name", ["reply_headers", "reply_trailers"])
@pytest.mark.parametrize("chosen, words", _REFUSED_ON_THE_GRPC_WIRE)
def test_chosen_metadata_that_a_grpc_reply_cannot_carry_is_rejected(field_name, chosen, words):
    s = ScenarioConfig()
    with pytest.raises(ValueError, match=words):
        s.update({"mode": "rate_limit", field_name: chosen})
    # the whole update aborted
    assert s.snapshot()["mode"] == "success"
    assert s.snapshot()[field_name] is None


def test_a_pushback_of_whole_milliseconds_is_taken_by_both_fields():
    """The positive control of the pushback rows above: a whole number, a zero, a
    negative number and both ends of the signed 64-bit range each reach the caller
    as written."""
    for field_name in ("reply_headers", "reply_trailers"):
        for value in ("5000", "0", "-1", "9223372036854775807", "-9223372036854775808"):
            s = ScenarioConfig()
            s.update({field_name: {"grpc-retry-pushback-ms": value}})
            assert s.snapshot()[field_name] == {"grpc-retry-pushback-ms": value}


def test_reply_trailers_with_a_transports_filter_without_grpc_are_rejected():
    s = ScenarioConfig()
    with pytest.raises(ValueError, match="reply_trailers cannot apply"):
        s.update({"reply_trailers": {"grpc-retry-pushback-ms": "5000"}, "transports": ["http", "ws"]})
    # The positive control: only the gRPC adapter ('http2') writes trailing metadata.
    for transports in (["http2"], ["http", "http2"]):
        s.update({"reply_trailers": {"grpc-retry-pushback-ms": "5000"}, "transports": transports})
        assert s.snapshot()["transports"] == transports


def test_reset_clears_the_reply_trailers():
    s = ScenarioConfig()
    s.update({"reply_trailers": {"grpc-retry-pushback-ms": "5000"}})
    s.reset()
    assert s.snapshot()["reply_trailers"] is None


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


# ── frozen_head ───────────────────────────────────────────────────────────────


def test_a_frozen_head_is_stored_and_a_reset_clears_it():
    """None means the provider reports the chain head. A block number freezes it
    there, and a reset puts the provider back on the chain head."""
    s = ScenarioConfig()
    assert s.snapshot()["frozen_head"] is None
    s.update({"frozen_head": 20_000_123})
    assert s.snapshot()["frozen_head"] == 20_000_123
    s.reset()
    assert s.snapshot()["frozen_head"] is None


@pytest.mark.parametrize("bad", ["ws", {"ws": 1}])
def test_a_transports_value_that_is_not_a_list_is_rejected(bad):
    """A text would be read letter by letter and an object by its keys, so an object
    whose keys name transports passed the check and scoped the block to nothing a
    caller meant."""
    s = ScenarioConfig()
    with pytest.raises(ValueError, match="transports must be a list of transport names or None"):
        s.update({"mode": "down", "transports": bad})
    assert s.snapshot()["mode"] == "success", "the whole update aborts"


@pytest.mark.parametrize("bad", ["now", "20000123", -1, True, 1.5, [20_000_123]])
def test_a_frozen_head_that_is_not_a_block_number_is_rejected(bad):
    """A block number is a non-negative integer. ``True`` is refused although it
    subclasses int, and a text is refused although it can name a number: a value
    the chains cannot compare with a height would freeze nothing."""
    s = ScenarioConfig()
    with pytest.raises(ValueError, match="frozen_head must be a block number"):
        s.update({"mode": "down", "frozen_head": bad})
    assert s.snapshot()["mode"] == "success", "the whole update aborts"
    assert s.snapshot()["frozen_head"] is None
