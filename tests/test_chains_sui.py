"""The Sui chain: what a simulated Sui GraphQL provider answers for each root field.

Each test builds a request with ``parse_graphql_request`` and asks the chain for
the reply, with the scenario and the quirks of one provider. A test that moves
the head uses its own ``SuiChain()``: the chain of the registry is one object
for the whole process.
"""

import base64
import json
from pathlib import Path

import pytest

from provider_simulator.chains import chain_for
from provider_simulator.chains.sui import SuiChain
from provider_simulator.chains.sui_digest import base58_decode, is_valid_transaction_digest
from provider_simulator.domain.quirks import SuiQuirks, quirks_for
from provider_simulator.domain.scenario import ScenarioConfig
from provider_simulator.graphql_document import parse_graphql_request
from stubs_sui_graphql import (
    SUI_BASE_CHECKPOINT,
    SUI_CHAIN_ID,
    SUI_EPOCH_ID,
    SUI_ERROR_CODES,
    SUI_ERROR_STUBS,
    SUI_MAX_CHECKPOINT,
    SUI_TESTNET_CHAIN_ID,
)

_VECTORS = json.loads((Path(__file__).parent / "fixtures" / "sui_transactions.json").read_text())["vectors"]
_HEAD_POLL = '{ serviceConfig { availableRange(type: "Checkpoint") { last { sequenceNumber } } } }'


def _scenario(**kw) -> dict:
    sc = ScenarioConfig()
    if kw:
        sc.update(kw)
    return sc.snapshot()


def _quirks(**kw) -> dict:
    q = SuiQuirks()
    if kw:
        q.update(kw)
    return q.snapshot()


def _ask(chain, query, *, scenario=None, quirks=None, variables=None, extra=None):
    """One query to the chain. Returns the body and the request dict, which the
    chain writes the executed digests into."""
    payload: dict = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    request = {"graphql": parse_graphql_request(json.dumps(payload).encode())}
    request.update(extra or {})
    status, body = chain.build_success(request, _scenario(**(scenario or {})), _quirks(**(quirks or {})))
    assert status == 200
    return body, request


def _head_of(chain, **scenario) -> int:
    body, _ = _ask(chain, _HEAD_POLL, scenario=scenario)
    return body["data"]["serviceConfig"]["availableRange"]["last"]["sequenceNumber"]


def _execute(vector) -> str:
    return f'mutation {{ executeTransaction(transactionDataBcs: "{vector["transaction_data_bcs_base64"]}", signatures: []) {{ effects {{ status digest }} }} }}'


# ── the chain and its settings ────────────────────────────────────────────────


def test_the_registry_resolves_sui():
    chain = chain_for("sui")
    assert isinstance(chain, SuiChain)
    assert chain.quirks_type is SuiQuirks
    assert quirks_for("sui") is SuiQuirks


def test_chain_identifier_is_the_sui_mainnet_id_and_the_chain_id_is_a_setting():
    chain = SuiChain()
    body, _ = _ask(chain, "{ chainIdentifier }")
    assert body == {"data": {"chainIdentifier": SUI_CHAIN_ID}}
    body, _ = _ask(chain, "{ chainIdentifier }", quirks={"chain_id": SUI_TESTNET_CHAIN_ID})
    assert body == {"data": {"chainIdentifier": SUI_TESTNET_CHAIN_ID}}


# ── the head and the tip ──────────────────────────────────────────────────────


def test_the_head_poll_reply_has_the_path_the_router_reads():
    body, _ = _ask(SuiChain(), _HEAD_POLL)
    assert body["data"]["serviceConfig"]["availableRange"]["last"]["sequenceNumber"] == SUI_BASE_CHECKPOINT
    assert body == {"data": {"serviceConfig": {"availableRange": {"last": {"sequenceNumber": SUI_BASE_CHECKPOINT}}}}}


def test_blocks_behind_moves_the_tip_down_and_a_negative_value_moves_it_up():
    chain = SuiChain()
    assert _head_of(chain, blocks_behind=100) == SUI_BASE_CHECKPOINT - 100
    assert _head_of(chain, blocks_behind=-30) == SUI_BASE_CHECKPOINT + 30


def test_a_provider_behind_by_more_than_the_head_has_the_tip_zero():
    chain = SuiChain()
    assert _head_of(chain, blocks_behind=SUI_BASE_CHECKPOINT + 50) == 0
    body, _ = _ask(
        chain,
        "{ checkpoint(sequenceNumber: 0) { sequenceNumber previousCheckpointDigest } }",
        scenario={"blocks_behind": SUI_BASE_CHECKPOINT + 50},
    )
    assert body["data"]["checkpoint"] == {"sequenceNumber": 0, "previousCheckpointDigest": None}


def test_advancing_the_head_moves_the_tip():
    chain = SuiChain()
    chain.head.bump(10)
    assert _head_of(chain) == SUI_BASE_CHECKPOINT + 10


def test_the_service_config_also_gives_the_first_checkpoint_the_server_has():
    body, _ = _ask(SuiChain(), "{ serviceConfig { availableRange { first { sequenceNumber } } } }")
    assert body["data"]["serviceConfig"]["availableRange"]["first"]["sequenceNumber"] == 0


# ── checkpoints ───────────────────────────────────────────────────────────────


def test_a_checkpoint_at_the_tip_is_served_and_one_above_it_is_null():
    chain = SuiChain()
    tip = SUI_BASE_CHECKPOINT
    at_tip, _ = _ask(chain, f"{{ checkpoint(sequenceNumber: {tip}) {{ sequenceNumber digest epoch {{ epochId }} }} }}")
    assert at_tip["data"]["checkpoint"]["sequenceNumber"] == tip
    assert is_valid_transaction_digest(at_tip["data"]["checkpoint"]["digest"])
    assert at_tip["data"]["checkpoint"]["epoch"] == {"epochId": SUI_EPOCH_ID}
    above, _ = _ask(chain, f"{{ checkpoint(sequenceNumber: {tip + 1}) {{ digest }} }}")
    assert above == {"data": {"checkpoint": None}}


def test_a_negative_checkpoint_is_null():
    body, _ = _ask(SuiChain(), "{ checkpoint(sequenceNumber: -1) { digest } }")
    assert body == {"data": {"checkpoint": None}}


def test_a_checkpoint_with_no_number_is_the_latest():
    chain = SuiChain()
    body, _ = _ask(chain, "{ checkpoint { sequenceNumber } }", scenario={"blocks_behind": 7})
    assert body == {"data": {"checkpoint": {"sequenceNumber": SUI_BASE_CHECKPOINT - 7}}}


def test_a_checkpoint_number_may_be_a_variable_or_a_text_of_digits_and_a_word_gives_null():
    chain = SuiChain()
    by_variable, _ = _ask(
        chain,
        "query($n: UInt53!) { checkpoint(sequenceNumber: $n) { sequenceNumber } }",
        variables={"n": 12},
    )
    assert by_variable["data"]["checkpoint"] == {"sequenceNumber": 12}
    as_text, _ = _ask(chain, '{ checkpoint(sequenceNumber: "12") { sequenceNumber } }')
    assert as_text["data"]["checkpoint"] == {"sequenceNumber": 12}
    as_word, _ = _ask(chain, '{ checkpoint(sequenceNumber: "abc") { sequenceNumber } }')
    assert as_word == {"data": {"checkpoint": None}}


def test_a_checkpoint_number_of_thousands_of_digits_is_null_and_not_a_crash():
    chain = SuiChain()
    as_text, _ = _ask(chain, '{ checkpoint(sequenceNumber: "%s") { digest } }' % ("9" * 5000))
    assert as_text == {"data": {"checkpoint": None}}
    as_variable, _ = _ask(
        chain,
        "query($n: UInt53!) { checkpoint(sequenceNumber: $n) { digest } }",
        variables={"n": 10**4000},
    )
    assert as_variable == {"data": {"checkpoint": None}}


@pytest.mark.parametrize(
    "bump, scenario",
    [(2**70, {}), (0, {"blocks_behind": -(2**70)})],
    ids=["head advanced past 2**64", "provider ahead by more than 2**64"],
)
def test_the_tip_never_passes_the_largest_checkpoint_number_of_sui(bump, scenario):
    chain = SuiChain()
    chain.head.bump(bump)
    assert _head_of(chain, **scenario) == SUI_MAX_CHECKPOINT
    body, _ = _ask(chain, "{ checkpoint { sequenceNumber digest } }", scenario=scenario)
    assert body["data"]["checkpoint"]["sequenceNumber"] == SUI_MAX_CHECKPOINT
    assert is_valid_transaction_digest(body["data"]["checkpoint"]["digest"])


def test_the_checkpoint_digest_is_the_same_for_two_providers_at_different_heads():
    chain = SuiChain()
    query = "{ checkpoint(sequenceNumber: 700000) { digest previousCheckpointDigest } }"
    at_head, _ = _ask(chain, query)
    behind, _ = _ask(chain, query, scenario={"blocks_behind": 250})
    assert at_head == behind
    digest = at_head["data"]["checkpoint"]["digest"]
    assert is_valid_transaction_digest(digest)
    assert len(base58_decode(digest)) == 32
    assert at_head["data"]["checkpoint"]["previousCheckpointDigest"] == SuiChain.checkpoint_digest(699_999)
    assert SuiChain.checkpoint_digest(1) != SuiChain.checkpoint_digest(2)


def test_a_digest_salt_changes_the_digest_of_that_provider_only():
    chain = SuiChain()
    query = "{ checkpoint(sequenceNumber: 700000) { digest } }"
    plain, _ = _ask(chain, query)
    salted, _ = _ask(chain, query, quirks={"digest_salt": "x"})
    assert plain["data"]["checkpoint"]["digest"] == SuiChain.checkpoint_digest(700_000)
    assert salted["data"]["checkpoint"]["digest"] == SuiChain.checkpoint_digest(700_000, "x")
    assert salted["data"]["checkpoint"]["digest"] != plain["data"]["checkpoint"]["digest"]


# ── transactions ──────────────────────────────────────────────────────────────


def test_execute_transaction_returns_the_real_digest_of_its_bytes():
    vector = _VECTORS[0]
    body, request = _ask(SuiChain(), _execute(vector))
    assert body == {"data": {"executeTransaction": {"effects": {"status": "SUCCESS", "digest": vector["digest"]}}}}
    assert request["executed_now"] == [vector["digest"]]


def test_execute_transaction_with_a_bad_value_is_a_bad_user_input_error():
    query = 'mutation { executeTransaction(transactionDataBcs: "not base64!", signatures: []) { effects { digest } } }'
    body, request = _ask(SuiChain(), query)
    assert body["data"] == {"executeTransaction": None}
    assert body["errors"][0]["extensions"] == {"code": "BAD_USER_INPUT"}
    assert body["errors"][0]["path"] == ["executeTransaction"]
    assert request.get("executed_now", []) == []


def test_execute_transaction_with_no_value_is_a_bad_user_input_error():
    body, request = _ask(SuiChain(), "mutation { executeTransaction { effects { digest } } }")
    assert body["data"] == {"executeTransaction": None}
    assert body["errors"][0]["extensions"] == {"code": "BAD_USER_INPUT"}
    assert request.get("executed_now", []) == []


def test_a_transaction_is_found_for_any_digest_while_the_lag_is_zero():
    body, _ = _ask(SuiChain(), '{ transaction(digest: "anything") { digest effects { status } } }')
    assert body == {"data": {"transaction": {"digest": "anything", "effects": {"status": "SUCCESS"}}}}


def test_with_a_lag_a_transaction_is_found_only_after_it_ran_and_the_lag_passed():
    chain = SuiChain()
    query = '{ transaction(digest: "d1") { digest effects { status checkpoint { sequenceNumber } } } }'
    now = 1_000.0
    lag = {"transaction_index_lag_ms": 200}

    def ask(executed_at):
        body, _ = _ask(chain, query, quirks=lag, extra={"executed_at": executed_at, "now": now})
        return body["data"]["transaction"]

    assert ask({"d1": now - 0.1}) is None
    found = ask({"d1": now - 0.3})
    assert found == {
        "digest": "d1",
        "effects": {"status": "SUCCESS", "checkpoint": {"sequenceNumber": SUI_BASE_CHECKPOINT}},
    }
    assert ask({"other": now - 5.0}) is None
    assert ask({}) is None


# ── other roots and the shape of the reply ────────────────────────────────────


def test_an_unknown_root_is_null_and_typename_is_query_or_mutation():
    chain = SuiChain()
    body, _ = _ask(chain, "{ nothingHere __typename }")
    assert body == {"data": {"nothingHere": None, "__typename": "Query"}}
    body, _ = _ask(chain, "mutation { __typename }")
    assert body == {"data": {"__typename": "Mutation"}}


def test_the_epoch_has_an_id_and_a_gas_price():
    body, _ = _ask(SuiChain(), "{ epoch { epochId referenceGasPrice } }")
    assert body == {"data": {"epoch": {"epochId": SUI_EPOCH_ID, "referenceGasPrice": "1000"}}}


def test_the_reply_holds_only_the_requested_fields_under_their_aliases():
    body, _ = _ask(SuiChain(), "{ a: checkpoint(sequenceNumber: 5) { digest } }")
    assert body == {"data": {"a": {"digest": SuiChain.checkpoint_digest(5)}}}


def test_a_skipped_root_is_not_in_the_reply():
    body, _ = _ask(SuiChain(), "{ a: chainIdentifier @skip(if: true) b: epoch { epochId } }")
    assert body == {"data": {"b": {"epochId": SUI_EPOCH_ID}}}


def test_a_per_root_result_replaces_the_stub():
    body, _ = _ask(
        SuiChain(),
        "{ checkpoint { digest } }",
        scenario={"responses": {"checkpoint": {"result": {"digest": "X", "other": 1}}}},
    )
    assert body == {"data": {"checkpoint": {"digest": "X"}}}


def test_a_per_root_error_stub_gives_a_partial_reply():
    body, _ = _ask(
        SuiChain(),
        "{ chainIdentifier checkpoint { digest } }",
        scenario={"responses": {"checkpoint": {"error_stub": "resource_exhausted"}}},
    )
    assert body["data"] == {"chainIdentifier": SUI_CHAIN_ID, "checkpoint": None}
    assert body["errors"] == [{**SUI_ERROR_STUBS["resource_exhausted"], "path": ["checkpoint"]}]
    assert body["errors"][0]["extensions"] == {"code": "RESOURCE_EXHAUSTED"}


def test_a_per_root_error_with_no_code_has_no_extensions_and_uses_the_alias_in_its_path():
    body, _ = _ask(
        SuiChain(),
        "{ here: checkpoint { digest } }",
        scenario={"responses": {"checkpoint": {"error": {"message": "boom"}}}},
    )
    assert body == {"data": {"here": None}, "errors": [{"message": "boom", "path": ["here"]}]}


def test_the_error_stubs_are_the_seven_codes_of_suis_server():
    assert set(SUI_ERROR_STUBS) == {code.lower() for code in SUI_ERROR_CODES}
    assert len(SUI_ERROR_CODES) == 7
    for name, stub in SUI_ERROR_STUBS.items():
        assert stub["extensions"]["code"] == name.upper()
        assert stub["message"]
    assert SuiChain().error_stub("internal_server_error") is SUI_ERROR_STUBS["internal_server_error"]


@pytest.mark.parametrize("code", sorted(SUI_ERROR_CODES))
def test_an_error_stub_is_found_by_the_code_in_either_case(code):
    """``error_code`` takes the code in capitals and ``error_stub`` took it in small
    letters, so a caller who gave the code as Sui writes it crashed the request
    thread of the provider and left the history row in flight."""
    assert SuiChain().error_stub(code) is SUI_ERROR_STUBS[code.lower()]
    assert SuiChain().error_stub(code.lower()) is SUI_ERROR_STUBS[code.lower()]


def test_a_per_root_error_stub_given_in_capitals_gives_the_same_partial_reply():
    body, _ = _ask(
        SuiChain(),
        "{ chainIdentifier checkpoint { digest } }",
        scenario={"responses": {"checkpoint": {"error_stub": "REQUEST_TIMEOUT"}}},
    )
    assert body["data"] == {"chainIdentifier": SUI_CHAIN_ID, "checkpoint": None}
    assert body["errors"][0]["extensions"] == {"code": "REQUEST_TIMEOUT"}


def test_an_error_stub_that_names_no_code_is_refused_with_the_names_that_exist():
    with pytest.raises(KeyError, match="resource_exhausted"):
        SuiChain().error_stub("nope")


def test_the_reply_does_not_change_the_stub_that_it_copies():
    chain = SuiChain()
    before = json.dumps(SUI_ERROR_STUBS, sort_keys=True)
    _ask(
        chain,
        "{ checkpoint { digest } }",
        scenario={"responses": {"checkpoint": {"error_stub": "request_timeout"}}},
    )
    assert json.dumps(SUI_ERROR_STUBS, sort_keys=True) == before


@pytest.mark.parametrize("vector", _VECTORS[:3], ids=lambda v: v["digest"][:6])
def test_execute_transaction_gives_the_digest_for_each_of_several_real_transactions(vector):
    raw = base64.b64decode(vector["transaction_data_bcs_base64"])
    assert raw
    body, request = _ask(SuiChain(), _execute(vector))
    assert body["data"]["executeTransaction"]["effects"]["digest"] == vector["digest"]
    assert request["executed_now"] == [vector["digest"]]
