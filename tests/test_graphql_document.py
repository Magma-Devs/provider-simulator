"""Reading a GraphQL request, and shaping a reply from a stub.

The module under test is a pure function of the request body. These tests pin
what it says about the root fields of a request (the name that history and the
fault overrides use), about the faults of a body that cannot be read (the reply
of the real Sui server for each kind), and about the directives ``@skip`` and
``@include``.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from provider_simulator import graphql_document
from provider_simulator.graphql_document import (
    GraphQLRequestError,
    default_error_reply,
    graphql_error,
    parse_graphql_request,
    project,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

_RECURSION = "The recursion depth of the query cannot be greater than `32`"
_EMPTY_BODY_TEXT = 'InvalidRequest(Error("EOF while parsing a value", line: 1, column: 0))'
_UNTAGGED_TEXT = (
    'InvalidRequest(Error("data did not match any variant of untagged enum BatchRequest", line: 0, column: 0))'
)
_WRITE = 'executeTransaction(transactionDataBcs: "AA==", signatures: []) { effects { status } }'


def _body(query, variables=None, operation_name=None) -> bytes:
    payload: dict = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    if operation_name is not None:
        payload["operationName"] = operation_name
    return json.dumps(payload).encode()


def _error_of(body: bytes) -> GraphQLRequestError:
    with pytest.raises(GraphQLRequestError) as raised:
        parse_graphql_request(body)
    return raised.value


# ── the root fields ───────────────────────────────────────────────────────────


def test_a_single_root_field_names_the_request():
    request = parse_graphql_request(_body("{ chainIdentifier }"))
    assert request.operation == "query"
    assert request.method == "chainIdentifier"
    assert [root.name for root in request.roots] == ["chainIdentifier"]


def test_an_alias_is_the_response_key_and_never_the_name():
    request = parse_graphql_request(
        _body('{ cheap: executeTransaction(transactionDataBcs: "AA==", signatures: []) { digest } }')
    )
    assert request.method == "executeTransaction"
    assert request.roots[0].name == "executeTransaction"
    assert request.roots[0].response_key == "cheap"


def test_an_alias_that_is_the_name_of_another_root_does_not_confuse_the_names():
    request = parse_graphql_request(_body("{ epoch: chainIdentifier chainIdentifier: epoch { epochId } }"))
    assert request.method == "chainIdentifier&epoch"
    assert [(root.name, root.response_key) for root in request.roots] == [
        ("chainIdentifier", "epoch"),
        ("epoch", "chainIdentifier"),
    ]


def test_two_root_fields_join_with_an_ampersand_in_document_order():
    request = parse_graphql_request(_body("{ epoch { epochId } chainIdentifier }"))
    assert request.method == "epoch&chainIdentifier"
    assert [root.name for root in request.roots] == ["epoch", "chainIdentifier"]


def test_a_fragment_spread_and_an_inline_fragment_at_the_root_expand_in_place():
    spread = parse_graphql_request(_body("query { ...F epoch { epochId } } fragment F on Query { chainIdentifier }"))
    assert spread.method == "chainIdentifier&epoch"
    inline = parse_graphql_request(_body("query { ... on Query { chainIdentifier } }"))
    assert inline.method == "chainIdentifier"


def test_operation_name_picks_one_of_two_operations():
    two = "query A { epoch { epochId } } query B { chainIdentifier }"
    assert parse_graphql_request(_body(two, operation_name="B")).method == "chainIdentifier"
    assert parse_graphql_request(_body(two, operation_name="A")).method == "epoch"
    mixed = f"query Read {{ chainIdentifier }} mutation Write {{ {_WRITE} }}"
    picked = parse_graphql_request(_body(mixed, operation_name="Read"))
    assert (picked.operation, picked.method) == ("query", "chainIdentifier")
    other = parse_graphql_request(_body(mixed, operation_name="Write"))
    assert (other.operation, other.method) == ("mutation", "executeTransaction")


def test_two_operations_with_no_name_or_an_unknown_name_are_operation_errors():
    two = "query A { chainIdentifier } query B { epoch { epochId } }"
    cases = (
        (_body(two), "Operation name required in request."),
        (_body("query A { chainIdentifier }", operation_name="Z"), 'Unknown operation named "Z"'),
    )
    for body, message in cases:
        error = _error_of(body)
        assert error.kind == "operation"
        assert error.message == message
        assert default_error_reply(error) == (200, {"data": None, "errors": [{"message": message}]})


def test_names_inside_strings_block_strings_and_comments_are_not_roots():
    commented = '{ transaction(digest: "{ checkpoint }") { digest } # { epoch }\n chainIdentifier }'
    assert parse_graphql_request(_body(commented)).method == "transaction&chainIdentifier"
    block = '{ transaction(digest: """{ checkpoint }""") { digest } }'
    assert parse_graphql_request(_body(block)).method == "transaction"


def test_a_mutation_is_marked_as_a_mutation():
    request = parse_graphql_request(_body(f"mutation {{ {_WRITE} }}"))
    assert request.operation == "mutation"
    assert request.method == "executeTransaction"


def test_arguments_come_from_literals_variables_and_declared_defaults():
    query = 'query Q($n: UInt53 = 7, $m: UInt53) { a(x: $n, y: $m, z: 5, s: "t") { b } }'
    without = parse_graphql_request(_body(query))
    assert without.roots[0].arguments == {"x": 7, "z": 5, "s": "t"}
    sent = parse_graphql_request(_body(query, variables={"n": 3, "m": 4}))
    assert sent.roots[0].arguments == {"x": 3, "y": 4, "z": 5, "s": "t"}


def test_the_request_id_is_read_from_the_variables():
    def read(variables):
        return parse_graphql_request(_body("{ chainIdentifier }", variables=variables)).request_id

    assert read({"request_id": "abc"}) == "abc"
    assert read({"request_id": 7}) == 7
    assert read({}) is None
    assert read({"request_id": ["a"]}) is None
    assert read({"request_id": {"a": 1}}) is None
    assert read({"request_id": True}) is None
    assert parse_graphql_request(_body("{ chainIdentifier }")).request_id is None


# ── a body that cannot be read ────────────────────────────────────────────────


def _exponential_fragments(levels: int, below: str = "") -> str:
    """A small document that expands to 2**(levels - 1) fields: each fragment spreads the next one twice.
    ``below`` puts the expansion under a root field, as ``epoch``."""
    spread = "...F1" if not below else f"{below} {{ ...F1 }}"
    on = "Query" if not below else "Epoch"
    leaf = "chainIdentifier" if not below else "epochId"
    fragments = [f"fragment F{i} on {on} {{ ...F{i + 1} ...F{i + 1} }}" for i in range(1, levels)]
    return f"query {{ {spread} }} " + " ".join(fragments) + f" fragment F{levels} on {on} {{ {leaf} }}"


_DEEP = json.dumps({"query": "{ a " * 5000 + "}" * 5000}).encode()
_BODY_NOT_AN_OBJECT = b'{"query": "{ chainIdentifier }", "variables": %s}'

#: Each kind of body that the real Sui server refuses, with the kind that the
#: module gives it and what the default reply must say. ``exact`` is the whole
#: text of a 400 reply, ``prefix`` is the start of one, and ``code`` is the code
#: of the one error of a 200 reply. The listener tests read the bodies from here.
UNREADABLE_BODIES = [
    pytest.param(b"", "not_json", 400, _EMPTY_BODY_TEXT, None, None, None, id="empty body"),
    pytest.param(b"nope", "not_json", 400, None, "InvalidRequest(Error(", None, None, id="not JSON"),
    pytest.param(b"\xff\xfe", "not_json", 400, None, "InvalidRequest(Error(", None, None, id="not UTF-8"),
    pytest.param(b'[{"query": "{ chainIdentifier }"}]', "batch", 400, "UnsupportedBatch", None, None, None, id="batch"),
    pytest.param(b'{"query": 5}', "bad_shape", 400, _UNTAGGED_TEXT, None, None, None, id="query is a number"),
    pytest.param(b'{"query": null}', "bad_shape", 400, _UNTAGGED_TEXT, None, None, None, id="query is null"),
    pytest.param(_BODY_NOT_AN_OBJECT % b"[1]", "bad_shape", 400, _UNTAGGED_TEXT, None, None, None, id="variables list"),
    pytest.param(_BODY_NOT_AN_OBJECT % b'"x"', "bad_shape", 400, _UNTAGGED_TEXT, None, None, None, id="variables text"),
    pytest.param(
        b'{"query": "{ chainIdentifier }", "operationName": 5}',
        "bad_shape",
        400,
        _UNTAGGED_TEXT,
        None,
        None,
        None,
        id="operation name is a number",
    ),
    pytest.param(b"[]", "batch", 400, "UnsupportedBatch", None, None, None, id="empty batch"),
    pytest.param(b"5", "bad_shape", 400, _UNTAGGED_TEXT, None, None, None, id="JSON number"),
    pytest.param(
        b'{"a":' * 100_000 + b"1" + b"}" * 100_000,
        "not_json",
        400,
        None,
        "InvalidRequest(Error(",
        None,
        None,
        id="JSON nested 100000 deep",
    ),
    pytest.param(
        b'{"query": "{ chainIdentifier }", "variables": {"x": ' + b"9" * 5000 + b"}}",
        "not_json",
        400,
        'InvalidRequest(Error("number out of range", line: 1, column: 0))',
        None,
        None,
        None,
        id="JSON integer of 5000 digits",
    ),
    pytest.param(
        json.dumps({"query": "{ " + "chainIdentifier " * 301 + "}"}).encode(),
        "validation",
        200,
        None,
        None,
        "GRAPHQL_VALIDATION_FAILED",
        "Query has over 300 nodes",
        id="301 fields",
    ),
    pytest.param(
        json.dumps({"query": _exponential_fragments(20)}).encode(),
        "validation",
        200,
        None,
        None,
        "GRAPHQL_VALIDATION_FAILED",
        "Query has over 300 nodes",
        id="fragments that expand to half a million fields",
    ),
    pytest.param(b"{}", "parse", 200, None, None, "GRAPHQL_PARSE_FAILED", None, id="no query"),
    pytest.param(b'{"query": ""}', "parse", 200, None, None, "GRAPHQL_PARSE_FAILED", None, id="empty query"),
    pytest.param(
        b'{"query": "{ chainIdentifier"}',
        "parse",
        200,
        None,
        None,
        "GRAPHQL_PARSE_FAILED",
        None,
        id="unclosed selection",
    ),
    pytest.param(
        b'{"query": "fragment F on Query { chainIdentifier }"}',
        "parse",
        200,
        None,
        None,
        "GRAPHQL_PARSE_FAILED",
        "document does not contain an operation",
        id="no operation",
    ),
    pytest.param(_DEEP, "parse", 200, None, None, "GRAPHQL_PARSE_FAILED", _RECURSION, id="5000 nested braces"),
]


@pytest.mark.parametrize("body, kind, status, exact, prefix, code, message", UNREADABLE_BODIES)
def test_each_unreadable_body_has_its_kind_and_the_default_reply_of_sui(
    body, kind, status, exact, prefix, code, message
):
    error = _error_of(body)
    assert error.kind == kind
    reply_status, reply = default_error_reply(error)
    assert reply_status == status
    if status == 400:
        assert isinstance(reply, str)
        if exact is not None:
            assert reply == exact
        else:
            assert reply.startswith(prefix)
        return
    assert reply["data"] is None
    assert len(reply["errors"]) == 1
    entry = reply["errors"][0]
    assert entry["extensions"] == {"code": code}
    if message is not None:
        assert entry["message"] == message
    else:
        assert entry["message"]


def test_a_fragment_cycle_and_an_undefined_fragment():
    own = _error_of(_body("query { ...A } fragment A on Query { ...A }"))
    two = _error_of(_body("query { ...A } fragment A on Query { ...B } fragment B on Query { ...A }"))
    for error in (own, two):
        assert error.kind == "parse"
        assert error.message == _RECURSION
        assert default_error_reply(error)[1]["errors"][0]["extensions"] == {"code": "GRAPHQL_PARSE_FAILED"}
    undefined = _error_of(_body("query { ...Nope }"))
    assert undefined.kind == "validation"
    assert undefined.message == 'Unknown fragment: "Nope"'
    assert default_error_reply(undefined) == (
        200,
        {
            "data": None,
            "errors": [{"message": 'Unknown fragment: "Nope"', "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}],
        },
    )


def test_a_fragment_cycle_is_found_at_the_second_visit_and_not_by_running_out_of_stack(monkeypatch):
    """The walk keeps the names of the fragments that it is inside, so a spread of
    one of them is a cycle. Without that rule the walk recurses until Python's own
    limit stops it, and that gives the same message a thousand calls later."""
    calls: list[int] = []
    original = graphql_document._depth_of

    def counting(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(graphql_document, "_depth_of", counting)
    error = _error_of(_body("query { ...A } fragment A on Query { ...A }"))
    assert error.message == _RECURSION
    assert len(calls) <= 3


# ── the number of nodes ───────────────────────────────────────────────────────

_FIELD = "chainIdentifier "


def _node_error(query: str) -> GraphQLRequestError:
    return _error_of(_body(query))


def test_a_query_of_300_nodes_is_read_and_one_more_is_refused_with_the_message_of_sui():
    assert len(parse_graphql_request(_body("{ " + _FIELD * 300 + "}")).roots) == 300
    error = _node_error("{ " + _FIELD * 301 + "}")
    assert (error.kind, error.message) == ("validation", "Query has over 300 nodes")
    assert default_error_reply(error) == (
        200,
        {
            "data": None,
            "errors": [{"message": "Query has over 300 nodes", "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}],
        },
    )


def test_a_fragment_spread_is_a_node_and_its_fields_count_again_at_each_spread():
    request = parse_graphql_request(_body("{ ...F ...F } fragment F on Query { " + _FIELD * 149 + "}"))
    assert len(request.roots) == 298  # 2 spreads and 298 fields are 300 nodes
    error = _node_error("{ ...F ...F } fragment F on Query { " + _FIELD * 150 + "}")
    assert error.message == "Query has over 300 nodes"


def test_an_inline_fragment_is_a_node():
    def two_inline_fragments(fields: int) -> str:
        return "{ " + ("... on Query { " + _FIELD * fields + "} ") * 2 + "}"

    request = parse_graphql_request(_body(two_inline_fragments(149)))
    assert len(request.roots) == 298  # 2 inline fragments and 298 fields are 300 nodes
    assert _node_error(two_inline_fragments(150)).message == "Query has over 300 nodes"


def test_fields_below_a_root_field_count_as_nodes():
    pair = "epoch { epochId } "
    assert len(parse_graphql_request(_body("{ " + pair * 150 + "}")).roots) == 150
    assert _node_error("{ " + pair * 151 + "}").message == "Query has over 300 nodes"


def test_a_document_that_expands_to_billions_of_nodes_is_refused_before_any_expansion(monkeypatch):
    """Fragments that spread each other twice make 2**29 fields from 30 small fragments. The count
    adds up each fragment once, so the answer costs a few dozen steps, and the expansion of the roots
    never starts."""

    def never(*args):
        raise AssertionError("the roots were expanded")

    monkeypatch.setattr(graphql_document, "_roots_of", never)
    assert _node_error(_exponential_fragments(30)).message == "Query has over 300 nodes"


def test_an_expansion_below_a_root_field_is_refused_too():
    assert _node_error(_exponential_fragments(25, below="epoch")).message == "Query has over 300 nodes"


def test_a_cycle_is_still_a_recursion_error_and_the_count_does_not_run_on_it():
    assert _node_error("query { ...A } fragment A on Query { ...A ...A }").message == _RECURSION


def _nested(levels: int) -> str:
    return "{ a " * levels + "}" * levels


def test_a_selection_nested_deeper_than_32_levels_is_a_parse_error():
    assert parse_graphql_request(_body(_nested(32))).method == "a"
    error = _error_of(_body(_nested(33)))
    assert error.kind == "parse"
    assert error.message == _RECURSION


# ── @skip and @include ────────────────────────────────────────────────────────


def test_skip_and_include_remove_fields_from_the_roots_and_the_reply():
    skipped = parse_graphql_request(_body("{ a: chainIdentifier @skip(if: true) b: epoch { epochId } }"))
    assert [(root.name, root.response_key) for root in skipped.roots] == [("epoch", "b")]
    assert skipped.method == "epoch"

    kept = parse_graphql_request(_body("{ a: chainIdentifier @skip(if: false) b: epoch { epochId } }"))
    assert kept.method == "chainIdentifier&epoch"

    by_variable = "query($s: Boolean!) { chainIdentifier @include(if: $s) epoch { epochId } }"
    assert parse_graphql_request(_body(by_variable, variables={"s": False})).method == "epoch"
    assert parse_graphql_request(_body(by_variable, variables={"s": True})).method == "chainIdentifier&epoch"

    by_default = "query($s: Boolean! = false) { chainIdentifier @include(if: $s) epoch { epochId } }"
    assert parse_graphql_request(_body(by_default)).method == "epoch"

    spread = "query { ...F epoch { epochId } } fragment F on Query { chainIdentifier }"
    assert parse_graphql_request(_body(spread.replace("...F", "...F @skip(if: true)"))).method == "epoch"
    inline = "query { ... on Query @skip(if: true) { chainIdentifier } epoch { epochId } }"
    assert parse_graphql_request(_body(inline)).method == "epoch"

    unresolved = parse_graphql_request(_body("query($s: Boolean) { chainIdentifier @skip(if: $s) }"))
    assert unresolved.method == "chainIdentifier"

    every = parse_graphql_request(_body("{ chainIdentifier @skip(if: true) }"))
    assert every.roots == ()
    assert every.method == "unknown"


def test_a_nested_field_with_skip_is_absent_from_the_projection():
    request = parse_graphql_request(_body("{ checkpoint { digest @skip(if: true) sequenceNumber } }"))
    stub = {"digest": "d", "sequenceNumber": 1}
    assert project(stub, request.roots[0].selection, request) == {"sequenceNumber": 1}


# ── project ───────────────────────────────────────────────────────────────────


def test_project_keeps_only_requested_fields_under_their_aliases():
    request = parse_graphql_request(_body("{ checkpoint { a: digest } }"))
    assert project({"digest": "d", "x": 1}, request.roots[0].selection, request) == {"a": "d"}


def test_project_gives_null_for_a_field_the_stub_lacks():
    request = parse_graphql_request(_body("{ checkpoint { digest missing } }"))
    assert project({"digest": "d"}, request.roots[0].selection, request) == {"digest": "d", "missing": None}


def test_project_follows_fragments_and_lists():
    query = "query { checkpoint { ...F items { id } } } fragment F on Checkpoint { digest }"
    request = parse_graphql_request(_body(query))
    stub = {"digest": "d", "items": [{"id": 1, "x": 2}, {"id": 3}]}
    assert project(stub, request.roots[0].selection, request) == {"digest": "d", "items": [{"id": 1}, {"id": 3}]}


def test_project_reads_typename_from_the_stub():
    request = parse_graphql_request(_body("{ checkpoint { __typename digest } }"))
    stub = {"__typename": "Checkpoint", "digest": "d"}
    assert project(stub, request.roots[0].selection, request) == {"__typename": "Checkpoint", "digest": "d"}


def test_project_returns_a_scalar_and_a_leaf_as_they_are():
    request = parse_graphql_request(_body("{ chainIdentifier }"))
    assert request.roots[0].selection is None
    assert (
        project("4btiuiMPvEENsttpZC7CZ53DruC3MAgfznDbASZ7DR6S", None, request)
        == "4btiuiMPvEENsttpZC7CZ53DruC3MAgfznDbASZ7DR6S"
    )
    assert project(None, None, request) is None


# ── graphql_error ─────────────────────────────────────────────────────────────


def test_graphql_error_builds_the_entry_shape():
    assert graphql_error("m") == {"message": "m"}
    assert graphql_error("m", "X") == {"message": "m", "extensions": {"code": "X"}}
    assert graphql_error("m", path=["a"]) == {"message": "m", "path": ["a"]}
    assert graphql_error("m", "X", ["a"]) == {"message": "m", "extensions": {"code": "X"}, "path": ["a"]}
    assert graphql_error("m", "") == {"message": "m"}


# ── the library is needed only when a request is read ─────────────────────────


def test_the_module_imports_with_no_graphql_core():
    """``graphql-core`` is read inside the functions that need it, so the
    simulator starts on a machine that does not have it. The process below makes
    each ``import graphql`` fail, then imports the server."""
    script = (
        "import sys\n"
        "sys.modules['graphql'] = None\n"
        "import server\n"
        "from provider_simulator import graphql_document\n"
        "try:\n"
        '    graphql_document.parse_graphql_request(b\'{"query": "{ a }"}\')\n'
        "except ImportError:\n"
        "    sys.exit(0)\n"
        "sys.exit('a request was read with no graphql-core')\n"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr or result.stdout
