"""GraphQLListener — GraphQL over HTTP. Drives serve() with POST requests and
checks the wire plan and the history row, one behaviour per test.

The chain is Sui: the listener holds no chain name, and the pool of these tests
is a Sui pool so that the chain gives the success content."""

import hashlib
import json
import time
from pathlib import Path

import pytest

from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.graphql_document import GraphQLRequestError, default_error_reply, parse_graphql_request
from provider_simulator.listeners import GraphQLListener, RawRequest
from stubs_sui_graphql import SUI_CHAIN_ID, SUI_ERROR_CODES
from tests.test_graphql_document import UNREADABLE_BODIES

GRAPHQL = Endpoint("graphql", "http", 18999)
_VECTORS = json.loads((Path(__file__).parent / "fixtures" / "sui_transactions.json").read_text())["vectors"]


def _listener():
    provider = Pool(name="sui-graphql-test", chain="sui").add_provider("1", [GRAPHQL])
    return GraphQLListener(provider, GRAPHQL), provider


def _body(query: str, variables=None) -> bytes:
    payload: dict = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    return json.dumps(payload).encode()


def _post(query: str, variables=None) -> RawRequest:
    return RawRequest(verb="POST", path="/graphql", body=_body(query, variables))


def _raw(body: bytes) -> RawRequest:
    return RawRequest(verb="POST", path="/graphql", body=body)


def _row(provider, index: int = 0) -> dict:
    return provider.log.get_history()[index]


def _scenario(provider, **fields) -> None:
    provider.scenario.update(fields)


# ── success and the history row ───────────────────────────────────────────────


def test_chain_identifier_success_and_history_row():
    listener, provider = _listener()
    body = _body("{ chainIdentifier }")
    result = listener.serve(RawRequest(verb="POST", path="/graphql", body=body))
    assert (result.action, result.status) == ("respond", 200)
    assert result.body == {"data": {"chainIdentifier": SUI_CHAIN_ID}}
    row = _row(provider)
    assert (row["method"], row["status"], row["path"]) == ("chainIdentifier", "success", "/graphql")
    assert (row["body_len"], row["body_sha256"]) == (len(body), hashlib.sha256(body).hexdigest())
    assert (row["interface"], row["transport"], row["port"]) == ("graphql", "http", 18999)


def test_the_path_that_the_request_was_sent_to_is_in_the_row():
    listener, provider = _listener()
    listener.serve(RawRequest(verb="POST", path="/x/y", body=_body("{ chainIdentifier }")))
    assert _row(provider)["path"] == "/x/y"


def test_the_request_id_comes_from_the_variables():
    listener, provider = _listener()
    listener.serve(_post("{ chainIdentifier }", {"request_id": "abc"}))
    assert _row(provider)["request_id"] == "abc"
    listener.serve(_post("{ chainIdentifier }", {"request_id": 41}))
    assert _row(provider, 1)["request_id"] == 41
    listener.serve(_post("{ chainIdentifier }"))
    assert _row(provider, 2)["request_id"] is None


def test_two_roots_and_an_alias_are_named_by_field_names():
    listener, provider = _listener()
    result = listener.serve(_post("{ a: epoch { epochId } chainIdentifier }"))
    assert _row(provider)["method"] == "epoch&chainIdentifier"
    assert set(result.body["data"]) == {"a", "chainIdentifier"}


def test_a_skipped_root_field_is_not_in_the_reply_or_in_the_history_method():
    listener, provider = _listener()
    result = listener.serve(_post("{ a: chainIdentifier @skip(if: true) b: epoch { epochId } }"))
    assert set(result.body["data"]) == {"b"}
    assert _row(provider)["method"] == "epoch"


# ── a body that cannot be read ────────────────────────────────────────────────

_UNREADABLE = [pytest.param(p.values[0], id=p.id) for p in UNREADABLE_BODIES]


def _expected_default(body: bytes) -> tuple:
    with pytest.raises(GraphQLRequestError) as raised:
        parse_graphql_request(body)
    return default_error_reply(raised.value)


@pytest.mark.parametrize("body", _UNREADABLE)
def test_each_unreadable_body_gets_one_history_row_and_the_default_reply_of_sui(body):
    listener, provider = _listener()
    result = listener.serve(_raw(body))
    status, reply = _expected_default(body)
    assert (result.action, result.status, result.body) == ("respond", status, reply)
    rows = provider.log.get_history()
    assert len(rows) == 1
    assert (rows[0]["method"], rows[0]["status"]) == ("unknown", "error")
    assert (rows[0]["body_len"], rows[0]["body_sha256"]) == (len(body), hashlib.sha256(body).hexdigest())
    assert rows[0]["path"] == "/graphql"


@pytest.mark.parametrize("body", _UNREADABLE)
def test_a_reply_that_a_test_chose_goes_to_every_unreadable_body(body):
    listener, provider = _listener()
    _scenario(provider, unreadable_reply={"status": 502, "body": "bad gateway"})
    result = listener.serve(_raw(body))
    assert (result.action, result.status, result.body) == ("respond", 502, "bad gateway")
    rows = provider.log.get_history()
    assert len(rows) == 1
    assert (rows[0]["method"], rows[0]["status"]) == ("unknown", "error")
    assert (rows[0]["body_len"], rows[0]["body_sha256"]) == (len(body), hashlib.sha256(body).hexdigest())


def test_a_chosen_reply_may_be_a_json_value_and_a_missing_body_is_empty_text():
    listener, provider = _listener()
    _scenario(provider, unreadable_reply={"status": 200, "body": {"data": None, "errors": []}})
    assert listener.serve(_raw(b"nope")).body == {"data": None, "errors": []}
    _scenario(provider, unreadable_reply={"status": 418})
    result = listener.serve(_raw(b"nope"))
    assert (result.status, result.body) == (418, "")


def test_a_chosen_reply_reaches_only_the_endpoints_that_the_transports_filter_names():
    listener, provider = _listener()
    _scenario(provider, unreadable_reply={"status": 502, "body": "bad gateway"}, transports=["ws"])
    result = listener.serve(_raw(b"nope"))
    status, reply = _expected_default(b"nope")
    assert (result.status, result.body) == (status, reply)


def test_an_unreadable_body_is_still_answered_when_the_mode_is_error():
    # The body is read first. A body that cannot be read has a reply of its own
    # and it is not a fault of the provider.
    listener, provider = _listener()
    _scenario(provider, mode="error", error_code="REQUEST_TIMEOUT")
    result = listener.serve(_raw(b"nope"))
    assert result.status == 400
    assert isinstance(result.body, str)


# ── the fault ladder ──────────────────────────────────────────────────────────


def test_down_hang_and_drop_are_the_transport_plans():
    listener, provider = _listener()
    _scenario(provider, mode="down")
    down = listener.serve(_post("{ chainIdentifier }"))
    assert (down.action, down.status) == ("no_body", 503)
    row = _row(provider)
    assert (row["status"], row["method"]) == ("down", "*")

    _scenario(provider, mode="hang")
    assert listener.serve(_post("{ chainIdentifier }")).action == "hang"

    _scenario(provider, mode="drop_connection", drop_at="mid_body")
    drop = listener.serve(_post("{ chainIdentifier }"))
    assert (drop.action, drop.drop_at) == ("drop", "mid_body")


def test_rate_limit_is_a_429_with_plain_text_by_default():
    listener, provider = _listener()
    _scenario(provider, mode="rate_limit")
    result = listener.serve(_post("{ chainIdentifier }"))
    assert (result.action, result.status) == ("respond", 429)
    assert isinstance(result.body, str) and result.body
    assert _row(provider)["status"] == "rate_limit"


def test_rate_limit_style_body_is_a_200_with_resource_exhausted():
    listener, provider = _listener()
    _scenario(provider, mode="rate_limit", rate_limit_style="body")
    result = listener.serve(_post("{ chainIdentifier }"))
    assert result.status == 200
    assert result.body["data"] is None
    assert result.body["errors"][0]["extensions"] == {"code": "RESOURCE_EXHAUSTED"}
    assert _row(provider)["status"] == "rate_limit"


def test_error_mode_sends_a_string_code_inside_a_200():
    listener, provider = _listener()
    _scenario(provider, mode="error", error_code="REQUEST_TIMEOUT", error_message="too slow")
    result = listener.serve(_post("{ chainIdentifier }"))
    assert result.status == 200
    assert result.body == {"data": None, "errors": [{"message": "too slow", "extensions": {"code": "REQUEST_TIMEOUT"}}]}
    assert _row(provider)["status"] == "error"


def test_error_mode_with_an_empty_code_has_no_extensions():
    listener, provider = _listener()
    _scenario(provider, mode="error", error_code="", error_message="bad")
    result = listener.serve(_post("{ chainIdentifier }"))
    assert result.body == {"data": None, "errors": [{"message": "bad"}]}


def test_error_mode_with_the_default_integer_code_uses_internal_server_error():
    listener, provider = _listener()
    _scenario(provider, mode="error")
    result = listener.serve(_post("{ chainIdentifier }"))
    assert result.body["errors"][0]["extensions"] == {"code": "INTERNAL_SERVER_ERROR"}
    assert result.body["errors"][0]["message"] == "Internal error"


def test_error_mode_honours_http_status():
    listener, provider = _listener()
    _scenario(provider, mode="error", error_code="INTERNAL_SERVER_ERROR", http_status=502)
    assert listener.serve(_post("{ chainIdentifier }")).status == 502


@pytest.mark.parametrize("code", SUI_ERROR_CODES)
def test_error_mode_with_a_code_for_each_of_the_seven_codes(code):
    listener, provider = _listener()
    _scenario(provider, mode="error", error_code=code)
    result = listener.serve(_post("{ chainIdentifier }"))
    assert result.body["errors"][0]["extensions"] == {"code": code}


def test_error_body_replaces_the_generated_error():
    listener, provider = _listener()
    _scenario(provider, mode="error", error_body={"error": "Invalid API key"}, http_status=400)
    result = listener.serve(_post("{ chainIdentifier }"))
    assert (result.status, result.body) == (400, {"error": "Invalid API key"})
    _scenario(provider, error_body="plain refusal")
    result = listener.serve(_post("{ chainIdentifier }"))
    assert (result.status, result.body) == (400, "plain refusal")
    assert _row(provider, 1)["status"] == "error"


def test_a_per_method_canned_body_and_fault_keys_follow_the_root_field_name():
    listener, provider = _listener()
    _scenario(provider, responses={"checkpoint": {"body": {"data": {"checkpoint": None}}}})
    canned = listener.serve(_post("{ checkpoint { digest } }"))
    assert canned.body == {"data": {"checkpoint": None}}
    assert listener.serve(_post("{ chainIdentifier }")).body == {"data": {"chainIdentifier": SUI_CHAIN_ID}}

    _scenario(provider, responses={"checkpoint": {"mode": "rate_limit"}})
    assert listener.serve(_post("{ checkpoint { digest } }")).status == 429
    assert listener.serve(_post("{ chainIdentifier }")).status == 200

    _scenario(provider, responses={"epoch&chainIdentifier": {"mode": "rate_limit"}})
    assert listener.serve(_post("{ epoch { epochId } chainIdentifier }")).status == 429
    assert listener.serve(_post("{ epoch { epochId } }")).status == 200


def test_a_partial_reply_keeps_data_beside_errors_and_is_labelled_error():
    listener, provider = _listener()
    _scenario(provider, responses={"checkpoint": {"error_stub": "internal_server_error"}})
    result = listener.serve(_post("{ chainIdentifier checkpoint { digest } }"))
    assert result.status == 200
    assert result.body["data"] == {"chainIdentifier": SUI_CHAIN_ID, "checkpoint": None}
    assert len(result.body["errors"]) == 1
    assert result.body["errors"][0]["path"] == ["checkpoint"]
    assert _row(provider)["status"] == "error"


def test_corruption_marks_the_reply():
    listener, provider = _listener()
    _scenario(provider, corruption_mode="truncated")
    result = listener.serve(_post("{ chainIdentifier }"))
    assert result.corruption_mode == "truncated"


def test_fail_first_n_then_success():
    listener, provider = _listener()
    _scenario(provider, mode="error", fail_first_n=1)
    first = listener.serve(_post("{ chainIdentifier }"))
    second = listener.serve(_post("{ chainIdentifier }"))
    assert "errors" in first.body
    assert second.body == {"data": {"chainIdentifier": SUI_CHAIN_ID}}
    assert [row["status"] for row in provider.log.get_history()] == ["error", "success"]


def test_latency_is_planned_and_not_waited():
    listener, provider = _listener()
    _scenario(provider, latency_ms=150)
    result = listener.serve(_post("{ chainIdentifier }"))
    assert result.latency_ms == 150
    assert _row(provider)["latency_ms"] == 150


# ── a transaction that has not reached the index yet ──────────────────────────


def _execute(vector) -> str:
    return (
        f'mutation {{ executeTransaction(transactionDataBcs: "{vector["transaction_data_bcs_base64"]}", '
        "signatures: []) { effects { digest } } }"
    )


def _lookup(digest: str) -> str:
    return f'{{ transaction(digest: "{digest}") {{ digest effects {{ status }} }} }}'


def test_a_transaction_is_not_found_until_the_lag_has_passed_after_it_ran(monkeypatch):
    listener, provider = _listener()
    provider.quirks.update({"transaction_index_lag_ms": 200})
    vector = _VECTORS[0]
    clock = [5_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    executed = listener.serve(_post(_execute(vector)))
    assert executed.body["data"]["executeTransaction"]["effects"]["digest"] == vector["digest"]
    assert provider.executed_snapshot() == {vector["digest"]: 5_000.0}

    clock[0] = 5_000.1
    assert listener.serve(_post(_lookup(vector["digest"]))).body["data"]["transaction"] is None
    clock[0] = 5_000.3
    found = listener.serve(_post(_lookup(vector["digest"]))).body["data"]["transaction"]
    assert found == {"digest": vector["digest"], "effects": {"status": "SUCCESS"}}
    assert listener.serve(_post(_lookup("never-ran"))).body["data"]["transaction"] is None

    provider.forget_executed()
    assert listener.serve(_post(_lookup(vector["digest"]))).body["data"]["transaction"] is None


def test_with_no_lag_any_digest_is_found():
    listener, provider = _listener()
    found = listener.serve(_post(_lookup("never-ran"))).body["data"]["transaction"]
    assert found["digest"] == "never-ran"


def test_a_transaction_that_ran_through_a_fault_is_not_remembered():
    # A reply that is a fault never reaches the chain, so nothing ran.
    listener, provider = _listener()
    _scenario(provider, mode="error")
    listener.serve(_post(_execute(_VECTORS[0])))
    assert provider.executed_snapshot() == {}
