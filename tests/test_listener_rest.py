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
    provider.scenario.update(
        {"mode": "error", "error_code": -1, "error_message": "boom", "http_status": 502}
    )
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
    provider.scenario.update(
        {"corruption_mode": "missing_field", "missing_field": "block.header.height"}
    )
    res = listener.serve(_get(_BLOCKS_LATEST))
    assert res.corruption_mode == "missing_field"
    assert res.missing_field == "block.header.height"


def _listener_provider_update(listener, cfg):
    listener.provider.scenario.update(cfg)
