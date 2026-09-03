"""Corruption serializer — turns a response body into wire bytes, applying the
configured corruption. Ported from the flat handlers' _reply; these pin the byte
output for each mode."""

import json

from provider_simulator.listeners.wire import serialize


def test_clean_serialize_roundtrips():
    status, raw, emit = serialize(200, {"a": 1})
    assert status == 200 and emit is True
    assert json.loads(raw) == {"a": 1}


def test_missing_field_flat_drops_top_level_key():
    _, raw, _ = serialize(200, {"jsonrpc": "2.0", "result": "0x1"}, "missing_field", "result")
    assert json.loads(raw) == {"jsonrpc": "2.0"}


def test_missing_field_dotted_drops_nested_key():
    body = {"block": {"header": {"height": "5"}, "data": {}}}
    _, raw, _ = serialize(200, body, "missing_field", "block.header.height", dotted=True)
    assert json.loads(raw) == {"block": {"header": {}, "data": {}}}


def test_empty_response_has_no_body():
    status, raw, emit = serialize(200, {"a": 1}, "empty_response")
    assert emit is False
    assert raw == b""


def test_wrong_type_defaults_to_result_for_jsonrpc():
    _, raw, _ = serialize(200, {"result": "0x1"}, "wrong_type")
    assert json.loads(raw)["result"] == 12345  # str -> int sentinel


def test_wrong_type_int_becomes_string():
    _, raw, _ = serialize(200, {"result": 42}, "wrong_type")
    assert json.loads(raw)["result"] == "wrong_type_value"


def test_wrong_type_defaults_to_first_key_for_rest():
    _, raw, _ = serialize(200, {"height": "5", "other": 1}, "wrong_type", dotted=True)
    assert json.loads(raw)["height"] == 12345


def test_truncated_cuts_ten_bytes():
    body = {"a": "x" * 50}
    _, raw, _ = serialize(200, body, "truncated")
    assert len(raw) == len(json.dumps(body).encode()) - 10


def test_invalid_json_is_garbage():
    _, raw, _ = serialize(200, {"a": 1}, "invalid_json")
    assert raw == b"}{ {{ not valid json"


def test_clean_does_not_mutate_the_caller_body():
    body = {"result": "0x1"}
    serialize(200, body, "wrong_type")
    assert body == {"result": "0x1"}  # wrong_type copied before swapping


# ── str bodies (the JSON-RPC rate_limit fault's prose text) ───────────────────
# json.dumps on a string would quote/escape it into a JSON string literal —
# these pin that a str body is written to the wire as-is instead.


def test_str_body_roundtrips_as_plain_text_not_json_quoted():
    status, raw, emit = serialize(429, "Rate limit exceeded.")
    assert status == 429 and emit is True
    assert raw == b"Rate limit exceeded."
    assert not raw.startswith(b'"')  # json.dumps would have quoted it


def test_null_body_replaces_a_dict_body_with_literal_null():
    status, raw, emit = serialize(200, {"jsonrpc": "2.0", "id": 1, "result": "0x1"}, "null_body")
    assert (status, raw, emit) == (200, b"null", True), f"the whole wire body must become the literal null, got {raw!r}"


def test_null_body_replaces_a_str_body_too():
    status, raw, emit = serialize(429, "Rate limit exceeded.", "null_body")
    assert (status, raw, emit) == (
        429,
        b"null",
        True,
    ), f"null_body is a whole-body override for any body type, got {(status, raw, emit)!r}"


def test_str_body_empty_response_has_no_body():
    status, raw, emit = serialize(429, "Rate limit exceeded.", "empty_response")
    assert emit is False
    assert raw == b""


def test_str_body_truncated_cuts_ten_bytes():
    body = "x" * 50
    _, raw, _ = serialize(429, body, "truncated")
    assert len(raw) == len(body.encode()) - 10


def test_str_body_invalid_json_is_garbage():
    _, raw, _ = serialize(429, "Rate limit exceeded.", "invalid_json")
    assert raw == b"}{ {{ not valid json"


def test_str_body_missing_field_is_a_no_op():
    """missing_field targets a dict key; a string has none — the corruption
    silently doesn't apply, same as any other non-dict body today."""
    _, raw, _ = serialize(429, "Rate limit exceeded.", "missing_field", "result")
    assert raw == b"Rate limit exceeded."
