"""RestListener — Cosmos REST over HTTP. Drives serve() with GET requests and
checks the wire plan + history, one behaviour per test."""

import pytest

from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners import RawRequest, RestListener, ServeResult

REST = Endpoint("rest", "http", 18551)
_BLOCKS_LATEST = "/cosmos/base/tendermint/v1beta1/blocks/latest"


def _listener():
    provider = Pool(name="lava-sim-rest", chain="lava").add_provider("1", [REST])
    return RestListener(provider, REST), provider


def _get(path, headers=None):
    return RawRequest(verb="GET", path=path, headers=headers or {})


def test_blocks_latest_success_and_history():
    listener, provider = _listener()
    res = listener.serve(_get(_BLOCKS_LATEST))
    assert res.action == "respond"
    assert res.status == 200
    assert res.body["block"]["header"]["chain_id"] == "lava-sim"
    hist = provider.log.get_history()[0]
    assert hist["status"] == "success"
    assert hist["method"] == f"GET {_BLOCKS_LATEST}"


def test_block_by_height_echoes_and_labels_by_template():
    listener, provider = _listener()
    res = listener.serve(_get("/cosmos/base/tendermint/v1beta1/blocks/98765"))
    assert res.body["block"]["header"]["height"] == "98765"
    # History labels by the TEMPLATE, not the concrete path.
    assert provider.log.get_history()[0]["method"].endswith("/blocks/{height}")


def test_balances_echoes_address():
    listener, _ = _listener()
    res = listener.serve(_get("/cosmos/bank/v1beta1/balances/cosmos1abc"))
    assert res.body["address"] == "cosmos1abc"
    assert res.body["balances"][0]["denom"] == "ulava"


def test_blocks_behind_shifts_head():
    listener, _ = _listener()
    _listener_provider_update(listener, {"blocks_behind": 100})
    res = listener.serve(_get(_BLOCKS_LATEST))
    assert res.body["block"]["header"]["height"] == str(20_000_000 - 100)


def test_unknown_path_is_404_recorded_not_found():
    listener, provider = _listener()
    res = listener.serve(_get("/nope"))
    assert res.status == 404
    assert res.body["code"] == "not_found"
    hist = provider.log.get_history()[0]
    assert hist["status"] == "not_found"
    assert hist["method"] == "GET /nope"


def test_down_is_503_no_body():
    listener, provider = _listener()
    provider.scenario.update({"mode": "down"})
    res = listener.serve(_get(_BLOCKS_LATEST))
    assert res.action == "no_body"
    assert res.status == 503
    assert provider.log.get_history()[0]["status"] == "down"


def test_error_fault_is_bare_code_message():
    listener, provider = _listener()
    provider.scenario.update({"mode": "error", "error_code": -1, "error_message": "boom", "http_status": 502})
    res = listener.serve(_get(_BLOCKS_LATEST))
    assert res.status == 502
    assert res.body == {"code": -1, "message": "boom"}  # no JSON-RPC envelope
    assert provider.log.get_history()[0]["status"] == "error"


def test_rate_limit_is_429():
    listener, provider = _listener()
    provider.scenario.update({"mode": "rate_limit"})
    res = listener.serve(_get(_BLOCKS_LATEST))
    assert res.status == 429
    assert res.body["code"] == 429
    assert provider.log.get_history()[0]["status"] == "rate_limit"


def test_corruption_directive_carried_on_success():
    listener, provider = _listener()
    provider.scenario.update({"corruption_mode": "missing_field", "missing_field": "block.header.height"})
    res = listener.serve(_get(_BLOCKS_LATEST))
    assert res.corruption_mode == "missing_field"
    assert res.missing_field == "block.header.height"


def _listener_provider_update(listener, cfg):
    listener.provider.scenario.update(cfg)


def _post(path, body=b"", headers=None):
    return RawRequest(verb="POST", path=path, body=body, headers=headers or {})


_TX_SIMULATE = "/cosmos/tx/v1beta1/simulate"


def test_post_tx_simulate_success_and_history():
    # Real Cosmos REST nodes accept POST on the simulate path; the GET-only
    # catalogue used to answer 404 here, blocking every POST-path router test.
    listener, provider = _listener()
    res = listener.serve(_post(_TX_SIMULATE, body=b'{"tx_bytes": "AA==", "gas_adjustment": "1.5"}'))
    assert res.action == "respond"
    assert res.status == 200
    assert res.body["gas_info"]["gas_wanted"] == "200000"
    assert res.body["gas_info"]["gas_used"] == "85432"
    assert res.body["result"]["msg_responses"] == []
    hist = provider.log.get_history()[0]
    assert hist["status"] == "success"
    assert hist["method"] == f"POST {_TX_SIMULATE}"


def test_post_unknown_path_still_404():
    # Only catalogued write routes exist — an uncatalogued POST path keeps the
    # no-match contract.
    listener, provider = _listener()
    res = listener.serve(_post("/cosmos/tx/v1beta1/thisdoesnotexist"))
    assert res.status == 404
    assert provider.log.get_history()[0]["status"] == "not_found"


def test_get_on_simulate_path_is_404():
    # Routes are verb-scoped: registering POST /simulate must not create a GET
    # twin. A real node rejects GET here too.
    listener, _ = _listener()
    res = listener.serve(_get(_TX_SIMULATE))
    assert res.status == 404


def _head(path, headers=None):
    return RawRequest(verb="HEAD", path=path, headers=headers or {})


def test_head_borrows_the_get_route_and_withholds_the_body():
    # HEAD has no catalogue entry. It is matched against the GET route, so the
    # status and the body (which sizes Content-Length) are the GET's, and only
    # the bytes are withheld.
    listener, provider = _listener()
    res = listener.serve(_head(_BLOCKS_LATEST))
    assert res.action == "respond"
    assert res.status == 200
    assert res.suppress_body is True
    assert res.body["block"]["header"]["chain_id"] == "lava-sim"
    hist = provider.log.get_history()[0]
    assert hist["status"] == "success"
    # History names the verb the caller sent, not the route it borrowed.
    assert hist["method"] == f"HEAD {_BLOCKS_LATEST}"


def test_get_still_writes_its_body():
    # The boundary for the flag above: a GET is never suppressed.
    listener, _ = _listener()
    res = listener.serve(_get(_BLOCKS_LATEST))
    assert res.suppress_body is False


def test_head_on_uncatalogued_path_is_404():
    # Borrowing the GET route table does not invent routes: a path with no GET
    # entry still answers 404, and the 404 body names HEAD.
    listener, provider = _listener()
    res = listener.serve(_head("/nope"))
    assert res.status == 404
    assert res.body["method"] == "HEAD"
    assert res.suppress_body is True
    hist = provider.log.get_history()[0]
    assert hist["status"] == "not_found"
    assert hist["method"] == "HEAD /nope"


def test_head_on_post_only_path_is_404():
    # /simulate is catalogued for POST only, so it has no GET route to borrow.
    listener, _ = _listener()
    res = listener.serve(_head(_TX_SIMULATE))
    assert res.status == 404


def test_head_inherits_the_get_routes_fault_override():
    # A per-route fault is a property of the route, so a HEAD to a path whose
    # GET is rate-limited is rate-limited too — with the body still withheld.
    listener, provider = _listener()
    provider.scenario.update({"responses": {("GET", _BLOCKS_LATEST): {"mode": "rate_limit"}}})
    res = listener.serve(_head(_BLOCKS_LATEST))
    assert res.status == 429
    assert res.suppress_body is True
    assert provider.log.get_history()[0]["status"] == "rate_limit"


def test_head_on_down_provider_is_503_with_nothing_to_size():
    # A dead node answers no status line's worth of body at all: that is the
    # no_body action, which is not the same as a sized-but-withheld HEAD body.
    listener, provider = _listener()
    provider.scenario.update({"mode": "down"})
    res = listener.serve(_head(_BLOCKS_LATEST))
    assert res.action == "no_body"
    assert res.status == 503
    assert res.suppress_body is False


# --- Trailing slash ---------------------------------------------------------
# The smart-router treats a trailing slash as optional when it matches a request
# against a spec api, and forwards the path as the caller sent it. The route
# table here anchored on "$", so the slashed form matched nothing and answered
# 404 -- a router that had resolved the path correctly still looked broken.


def test_trailing_slash_serves_the_same_resource():
    listener, provider = _listener()
    plain = listener.serve(_get(_BLOCKS_LATEST))
    slashed = listener.serve(_get(_BLOCKS_LATEST + "/"))

    assert plain.status == 200, "control: the unslashed form must already work"
    assert slashed.status == 200, (
        f"{_BLOCKS_LATEST}/ is the same resource as {_BLOCKS_LATEST} and must be "
        f"served, not 404ed. Got {slashed.status}."
    )
    assert slashed.body == plain.body


def test_both_forms_are_recorded_under_one_method_key():
    """History records the matched route, not the caller's raw path.

    So a slashed and an unslashed request to the same resource land under one
    key, and a test filtering history by method sees both. Recording the raw
    path would split them and quietly halve such a filter's results.
    """
    listener, provider = _listener()
    listener.serve(_get(_BLOCKS_LATEST))
    listener.serve(_get(_BLOCKS_LATEST + "/"))

    methods = [row["method"] for row in provider.log.get_history()]
    assert methods == [
        f"GET {_BLOCKS_LATEST}",
        f"GET {_BLOCKS_LATEST}",
    ], f"Both forms must record under the matched route. Got {methods}."


def test_a_double_slash_is_still_not_a_route():
    """One optional slash, never two. Guards `/?` against being loosened to `/*`."""
    listener, _ = _listener()
    assert listener.serve(_get(_BLOCKS_LATEST + "//")).status == 404


def test_an_empty_parameter_is_still_not_a_route():
    """`/blocks/` must not match `/blocks/{height}` with height set to nothing.

    A parametrised segment is ``[^/]+``, which needs at least one character, so
    making the slash optional cannot turn a missing parameter into an empty one.
    """
    listener, _ = _listener()
    assert listener.serve(_get("/cosmos/base/tendermint/v1beta1/blocks/")).status == 404


def test_allowed_verbs_answers_for_the_slashed_form_too():
    """OPTIONS builds its Allow header from the same route table."""
    from provider_simulator.listeners.rest import allowed_verbs

    assert allowed_verbs(_BLOCKS_LATEST + "/") == allowed_verbs(_BLOCKS_LATEST)
    assert allowed_verbs(_BLOCKS_LATEST) != [], "control: the plain form has verbs"


# --- The request id ---------------------------------------------------------
# The caller chooses the request id, and a test reads the rows of one request
# with it. The smart-router passes a query string on to the provider, and it
# passes a header only when the chain spec declares that header. So the query
# parameter ``request_id`` is the place that works through the router. The
# header ``X-Request-Id`` stays for a caller that talks to the simulator
# directly.

_VALIDATORS = "/cosmos/staking/v1beta1/validators"


def _with_query(path, query, headers=None, verb="GET"):
    return RawRequest(verb=verb, path=path, query=query, headers=headers or {})


def _only_row(provider):
    rows = provider.log.get_history()
    assert len(rows) == 1, f"expected one history row, got {len(rows)}"
    return rows[0]


def test_request_id_comes_from_the_query_parameter():
    listener, provider = _listener()
    listener.serve(_with_query(_VALIDATORS, {"request_id": ["probe-a1"]}))
    assert _only_row(provider)["request_id"] == "probe-a1"


def test_the_query_parameter_wins_over_the_header():
    listener, provider = _listener()
    listener.serve(_with_query(_VALIDATORS, {"request_id": ["from-query"]}, headers={"X-Request-Id": "from-header"}))
    assert _only_row(provider)["request_id"] == "from-query"


def test_the_first_value_counts_when_the_parameter_repeats():
    listener, provider = _listener()
    listener.serve(_with_query(_VALIDATORS, {"request_id": ["first", "second"]}))
    assert _only_row(provider)["request_id"] == "first"


def test_a_query_value_that_is_not_in_a_list_is_read_too():
    # A live request goes through parse_qs, which puts each value in a list. A
    # request that a test builds by hand can carry the bare value.
    listener, provider = _listener()
    listener.serve(_with_query(_VALIDATORS, {"request_id": "bare"}))
    assert _only_row(provider)["request_id"] == "bare"


@pytest.mark.parametrize("header_name", ["X-Request-Id", "x-request-id", "X-REQUEST-ID", "X-request-id"])
def test_the_header_is_read_in_any_letter_case(header_name):
    listener, provider = _listener()
    listener.serve(_get(_VALIDATORS, headers={header_name: "trace-7"}))
    assert _only_row(provider)["request_id"] == "trace-7"


def test_an_empty_query_value_is_no_id_so_the_header_applies():
    listener, provider = _listener()
    listener.serve(_with_query(_VALIDATORS, {"request_id": [""]}, headers={"X-Request-Id": "from-header"}))
    assert _only_row(provider)["request_id"] == "from-header"


def test_an_empty_header_value_is_no_id_so_the_counter_applies():
    listener, provider = _listener()
    listener.serve(_get(_VALIDATORS, headers={"x-request-id": ""}))
    assert isinstance(_only_row(provider)["request_id"], int)


def test_with_no_id_the_counter_of_the_simulator_stays():
    listener, provider = _listener()
    listener.serve(_get(_VALIDATORS))
    listener.serve(_get(_VALIDATORS))
    ids = sorted(row["request_id"] for row in provider.log.get_history())
    assert all(isinstance(request_id, int) for request_id in ids)
    assert ids[1] == ids[0] + 1


def test_a_plain_number_from_the_caller_is_kept_as_text():
    # The counter gives numbers and the filter compares text, so a caller must
    # not send a plain number. The simulator does not refuse one: the row holds
    # the text that arrived.
    listener, provider = _listener()
    listener.serve(_with_query(_VALIDATORS, {"request_id": ["7"]}))
    assert _only_row(provider)["request_id"] == "7"


@pytest.mark.parametrize("verb", ["GET", "POST", "PUT", "DELETE", "HEAD"])
def test_every_verb_that_reaches_the_listener_records_the_query_id(verb):
    listener, provider = _listener()
    listener.serve(_with_query(_VALIDATORS, {"request_id": [f"id-{verb}"]}, verb=verb))
    assert _only_row(provider)["request_id"] == f"id-{verb}"


def test_an_unknown_path_keeps_the_id_on_its_not_found_row():
    listener, provider = _listener()
    res = listener.serve(_with_query("/nope", {"request_id": ["lost-1"]}))
    assert res.status == 404
    row = _only_row(provider)
    assert (row["status"], row["request_id"]) == ("not_found", "lost-1")


def test_a_fault_row_keeps_the_id():
    listener, provider = _listener()
    provider.scenario.update({"mode": "error", "error_code": -1, "error_message": "boom"})
    listener.serve(_with_query(_VALIDATORS, {"request_id": ["fault-1"]}))
    row = _only_row(provider)
    assert (row["status"], row["request_id"]) == ("error", "fault-1")


def test_a_provider_wide_down_row_has_no_request_id():
    # A dead node does not read the request, so the row has the method "*" and
    # no request id. A test counts those calls with /stats.
    listener, provider = _listener()
    provider.scenario.update({"mode": "down"})
    listener.serve(_with_query(_VALIDATORS, {"request_id": ["never-read"]}))
    row = _only_row(provider)
    assert (row["status"], row["method"], row["request_id"]) == ("down", "*", None)


@pytest.mark.parametrize("mode", ["down", "hang"])
def test_latency_ms_of_the_row_of_a_call_that_the_provider_did_not_wait_for(mode):
    # A down provider answers at once, and a hung call waits its own 30
    # seconds: the adapter does not wait for latency_ms. So the row records 0,
    # as on gRPC.
    listener, provider = _listener()
    provider.scenario.update({"mode": mode, "latency_ms": 250})
    assert listener.serve(_get(_BLOCKS_LATEST)).latency_ms == 0
    assert [(row["status"], row["latency_ms"]) for row in provider.log.get_history()] == [(mode, 0)]


# ── The hooks of the request flow ────────────────────────────────────────────
# The flow asks two hooks in the places where gRPC differs from the HTTP
# interfaces. Their defaults are what JSON-RPC, REST and Tendermint RPC do, and
# the tests above hold those. These tests show that the flow asks the hooks.


class _ProbeListener(RestListener):
    def build_down(self):
        return ServeResult(action="respond", status=418, body={"probe": "down"})

    def corrupt(self, result, status_label, scenario):
        result.body = {"probe": "corrupted"}
        return "probe-label"


def _probe():
    provider = Pool(name="lava-sim-rest", chain="lava").add_provider("1", [REST])
    return _ProbeListener(provider, REST), provider


def test_the_flow_asks_the_hook_for_the_reply_of_a_down_provider():
    listener, provider = _probe()
    provider.scenario.update({"mode": "down", "latency_ms": 250})
    res = listener.serve(_get(_BLOCKS_LATEST))
    assert (res.action, res.status, res.body, res.latency_ms) == ("respond", 418, {"probe": "down"}, 0)
    row = provider.log.get_history()[0]
    assert (row["method"], row["status"], row["latency_ms"], row["request_id"]) == ("*", "down", 0, None)


def test_the_flow_asks_the_hook_to_corrupt_a_reply_and_takes_its_row_label():
    listener, provider = _probe()
    provider.scenario.update({"corruption_mode": "truncated"})
    res = listener.serve(_get(_BLOCKS_LATEST))
    assert res.body == {"probe": "corrupted"}
    assert res.corruption_mode is None
    assert provider.log.get_history()[0]["status"] == "probe-label"


def test_the_flow_does_not_ask_the_hook_to_corrupt_when_the_filter_does_not_name_the_endpoint():
    listener, provider = _probe()
    provider.scenario.update({"corruption_mode": "truncated", "transports": ["ws"]})
    res = listener.serve(_get(_BLOCKS_LATEST))
    assert res.body != {"probe": "corrupted"}
    assert provider.log.get_history()[0]["status"] == "success"
