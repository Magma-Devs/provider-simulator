"""RestListener — Cosmos REST over HTTP. Drives serve() with GET requests and
checks the wire plan + history, one behaviour per test."""

from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners import RawRequest, RestListener

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
