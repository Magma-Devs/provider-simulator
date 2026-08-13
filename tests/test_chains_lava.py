"""LavaChain — success-path content for REST, Tendermint-RPC, and gRPC.

One Lava pool speaks one interface, so build_success branches on the interface
string. These tests call it directly (no socket) the same way the other chain
tests do, one interface at a time.
"""

import pytest

from provider_simulator.chains import chain_for
from provider_simulator.chains.lava import GRPC_LATEST_BLOCK, LAVA_SIM_CHAIN_ID, LavaChain
from provider_simulator.domain.scenario import ScenarioConfig

REST_HEIGHT = 20_000_000  # int(ETH_LATEST_BLOCK, 16) == 0x1312D00
TM_HEIGHT = 5_000_000  # TM_LATEST_HEIGHT

_BLOCKS_LATEST = "/cosmos/base/tendermint/v1beta1/blocks/latest"
_BLOCKS_HEIGHT = "/cosmos/base/tendermint/v1beta1/blocks/{height}"
_NODE_INFO = "/cosmos/base/tendermint/v1beta1/node_info"
_BALANCES = "/cosmos/bank/v1beta1/balances/{address}"
_VALIDATORS = "/cosmos/staking/v1beta1/validators"


def _sc(**kw):
    sc = ScenarioConfig()
    if kw:
        sc.update(kw)
    return sc.snapshot()


def _rest(template, verb="GET", path_params=None, query=None, body=None, route_verb=None):
    """A parsed REST request. ``route_verb`` is the verb the route table was
    searched under — it differs from ``verb`` only for HEAD, which borrows GET's
    route; leaving it None mirrors a caller that never sets it."""
    request = {
        "verb": verb,
        "template": template,
        "path_params": path_params or {},
        "query": query or {},
        "body": body,
    }
    if route_verb is not None:
        request["route_verb"] = route_verb
    return request


def _tm(method, params=None, req_id=1):
    return {"method": method, "params": params or {}, "id": req_id}


def _chain():
    return LavaChain()


def test_registry_resolves_lava():
    assert isinstance(chain_for("lava"), LavaChain)


def test_missing_or_wrong_interface_raises():
    with pytest.raises(ValueError):
        _chain().build_success(_tm("status"), _sc(), {})  # interface defaults ""
    with pytest.raises(ValueError):
        _chain().build_success(_tm("status"), _sc(), {}, "jsonrpc")


# ── REST ────────────────────────────────────────────────────────────────────
def test_rest_blocks_latest_static():
    st, body = _chain().build_success(_rest(_BLOCKS_LATEST), _sc(), {}, "rest")
    assert st == 200
    assert body["block"]["header"]["height"] == str(REST_HEIGHT)
    assert body["block"]["header"]["chain_id"] == "lava-sim"


def test_rest_blocks_latest_blocks_behind_shifts_head():
    st, body = _chain().build_success(_rest(_BLOCKS_LATEST), _sc(blocks_behind=100), {}, "rest")
    assert body["block"]["header"]["height"] == str(REST_HEIGHT - 100)
    assert body["block"]["last_commit"]["height"] == str(REST_HEIGHT - 101)


def test_rest_block_by_height_echoes_requested_height():
    st, body = _chain().build_success(
        _rest(_BLOCKS_HEIGHT, path_params={"height": "12345"}), _sc(), {}, "rest"
    )
    assert body["block"]["header"]["height"] == "12345"


def test_rest_balances_echoes_address():
    st, body = _chain().build_success(
        _rest(_BALANCES, path_params={"address": "cosmos1abc"}), _sc(), {}, "rest"
    )
    assert body["address"] == "cosmos1abc"
    assert body["balances"][0]["denom"] == "ulava"


def test_rest_node_info_static():
    st, body = _chain().build_success(_rest(_NODE_INFO), _sc(), {}, "rest")
    assert body["default_node_info"]["network"] == "lava-sim"


def test_rest_unknown_path_is_404():
    st, body = _chain().build_success(_rest("/nope"), _sc(), {}, "rest")
    assert st == 404
    assert body["code"] == "not_found"


def test_rest_error_stub_override():
    st, body = _chain().build_success(
        _rest(_VALIDATORS),
        _sc(responses={("GET", _VALIDATORS): {"error_stub": "not_found", "http_status": 404}}),
        {},
        "rest",
    )
    assert st == 404
    assert body["error"]["code"] == 5


def test_rest_body_override_http_status_wins_over_status():
    st, body = _chain().build_success(
        _rest(_NODE_INFO),
        _sc(responses={("GET", _NODE_INFO): {"http_status": 201, "status": 500, "body": {"x": 1}}}),
        {},
        "rest",
    )
    assert st == 201  # REST: http_status wins over the deprecated status fallback
    assert body == {"x": 1}


def test_rest_head_reads_the_get_stub():
    # A HEAD carries route_verb GET, so the catalogue lookup finds the GET stub
    # even though the request's own verb is HEAD.
    st, body = _chain().build_success(
        _rest(_BLOCKS_LATEST, verb="HEAD", route_verb="GET"), _sc(), {}, "rest"
    )
    assert st == 200
    assert body["block"]["header"]["height"] == str(REST_HEIGHT)


def test_rest_head_404_names_the_verb_the_caller_sent():
    st, body = _chain().build_success(
        _rest("/nope", verb="HEAD", route_verb="GET"), _sc(), {}, "rest"
    )
    assert st == 404
    assert body["method"] == "HEAD"  # not the GET route it tried to borrow


def test_rest_validators_echoes_inbound_page_key():
    st, body = _chain().build_success(
        _rest(_VALIDATORS, query={"pagination.key": ["sentinel-cursor-abc"]}), _sc(), {}, "rest"
    )
    assert st == 200
    assert body["pagination"]["inbound_key"] == "sentinel-cursor-abc"


def test_rest_validators_echoes_bare_page_key_value():
    # parse_qs wraps values in lists; a hand-built request may not.
    _st, body = _chain().build_success(
        _rest(_VALIDATORS, query={"pagination.key": "bare-cursor"}), _sc(), {}, "rest"
    )
    assert body["pagination"]["inbound_key"] == "bare-cursor"


def test_rest_validators_without_page_key_echoes_none():
    _st, body = _chain().build_success(_rest(_VALIDATORS), _sc(), {}, "rest")
    assert body["pagination"]["inbound_key"] is None
    assert body["pagination"]["next_key"] is None  # single-page stub: no next page
    assert body["validators"][0]["status"] == "BOND_STATUS_BONDED"


def test_rest_balances_echoes_inbound_page_key_too():
    # The other paginated Cosmos path gets the same echo — the cursor is a
    # property of pagination, not of one endpoint.
    _st, body = _chain().build_success(
        _rest(
            _BALANCES,
            path_params={"address": "cosmos1abc"},
            query={"pagination.key": ["cursor-9"]},
        ),
        _sc(),
        {},
        "rest",
    )
    assert body["pagination"]["inbound_key"] == "cursor-9"
    assert body["address"] == "cosmos1abc"


def test_rest_unpaginated_path_gains_no_pagination_block():
    # /blocks/latest has no pagination block; a cursor on the query string must
    # not conjure one.
    _st, body = _chain().build_success(
        _rest(_BLOCKS_LATEST, query={"pagination.key": ["cursor-9"]}), _sc(), {}, "rest"
    )
    assert "pagination" not in body


def test_rest_page_key_echo_does_not_leak_into_the_shared_stub():
    # The catalogue entry is deep-copied per request; a cursor from one call
    # must not show up on the next.
    _st, first = _chain().build_success(
        _rest(_VALIDATORS, query={"pagination.key": ["cursor-1"]}), _sc(), {}, "rest"
    )
    _st, second = _chain().build_success(_rest(_VALIDATORS), _sc(), {}, "rest")
    assert first["pagination"]["inbound_key"] == "cursor-1"
    assert second["pagination"]["inbound_key"] is None


# ── Tendermint-RPC ───────────────────────────────────────────────────────────
def test_tm_status_wrapped_in_jsonrpc_envelope():
    st, body = _chain().build_success(_tm("status", req_id=7), _sc(), {}, "tendermintrpc")
    assert st == 200
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 7
    assert body["result"]["sync_info"]["latest_block_height"] == str(TM_HEIGHT)


def test_tm_block_echoes_requested_height():
    st, body = _chain().build_success(
        _tm("block", params={"height": "99"}), _sc(), {}, "tendermintrpc"
    )
    assert body["result"]["block"]["header"]["height"] == "99"


def test_tm_block_default_head_shifts_by_blocks_behind():
    st, body = _chain().build_success(_tm("block"), _sc(blocks_behind=10), {}, "tendermintrpc")
    assert body["result"]["block"]["header"]["height"] == str(TM_HEIGHT - 10)


def test_tm_validators_pagination():
    st, body = _chain().build_success(
        _tm("validators", params={"page": 1, "per_page": 2}), _sc(), {}, "tendermintrpc"
    )
    assert body["result"]["count"] == "2"
    assert body["result"]["total"] == "12"


def test_tm_abci_query_echoes_height():
    st, body = _chain().build_success(
        _tm("abci_query", params={"height": "77"}), _sc(), {}, "tendermintrpc"
    )
    assert body["result"]["response"]["height"] == "77"


def test_tm_unknown_method_is_minus_32601():
    st, body = _chain().build_success(_tm("bogus"), _sc(), {}, "tendermintrpc")
    assert body["error"]["code"] == -32601


def test_tm_error_stub_status_wins_over_http_status():
    st, body = _chain().build_success(
        _tm("status"),
        _sc(responses={"status": {"error_stub": "internal", "status": 500, "http_status": 200}}),
        {},
        "tendermintrpc",
    )
    assert st == 500  # TM: status wins over http_status (the reverse of REST)
    assert body["error"]["code"] == -32603


# ── gRPC (success-data the gRPC listener serializes) ─────────────────────────
def test_grpc_latest_block_reports_head_and_chain_id():
    st, body = _chain().build_success({"method": "GetLatestBlock"}, _sc(), {}, "grpc")
    assert body["grpc_method"] == "GetLatestBlock"
    assert body["height"] == GRPC_LATEST_BLOCK
    assert body["chain_id"] == LAVA_SIM_CHAIN_ID


def test_grpc_latest_block_blocks_behind_shifts_head():
    st, body = _chain().build_success(
        {"method": "GetLatestBlock"}, _sc(blocks_behind=5), {}, "grpc"
    )
    assert body["height"] == GRPC_LATEST_BLOCK - 5


def test_grpc_node_info_fields():
    st, body = _chain().build_success({"method": "GetNodeInfo"}, _sc(), {}, "grpc")
    assert body["network"] == LAVA_SIM_CHAIN_ID
    assert body["moniker"] == "lava-sim-grpc-provider"


def test_grpc_per_method_result_override():
    st, body = _chain().build_success(
        {"method": "GetLatestBlock"},
        _sc(responses={"GetLatestBlock": {"result": {"custom": 1}}}),
        {},
        "grpc",
    )
    assert body["result"] == {"custom": 1}
