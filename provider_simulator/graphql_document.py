"""Read a GraphQL request, and shape a reply from a stub.

A GraphQL provider is asked in one language: a JSON body with a ``query`` (a
GraphQL document), and optionally ``variables`` and ``operationName``. This
module is a pure function of that body. It knows no chain and holds no state.
The listener asks it three things:

- ``parse_graphql_request`` finds what the request is: the operation, the root
  fields, the request id, and the variables. The name of a request is its root
  field names, joined with ``&``, in document order. An alias never counts.
  Fragments expand in place. A field that ``@skip`` or ``@include`` removes is
  not a root.
- ``default_error_reply`` gives the reply that the real Sui server gives for a
  body that cannot be read, for each kind of fault.
- ``project`` shapes a Python value by the selection set of a field: only the
  requested fields, under their aliases.

``graphql-core`` is imported inside the functions that need it, and nowhere at
the head of the module. A simulator on a machine that does not have the library
still starts, and it fails only on the request that needs it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# The deepest nesting of selection sets that the real server accepts. A fragment
# that spreads itself is a nesting with no end, so it hits the same limit.
MAX_DEPTH = 32
RECURSION_MESSAGE = f"The recursion depth of the query cannot be greater than `{MAX_DEPTH}`"

_EMPTY_BODY = 'InvalidRequest(Error("EOF while parsing a value", line: 1, column: 0))'
_BAD_SHAPE = 'InvalidRequest(Error("data did not match any variant of untagged enum BatchRequest", line: 0, column: 0))'
_BATCH = "UnsupportedBatch"
_NO_OPERATION = "document does not contain an operation"
_NO_DOCUMENT = " --> 1:1\n  |\n1 | \n  | ^---\n  |\n  = expected executable_definition"
_OPERATION_NAME_REQUIRED = "Operation name required in request."

# The code that the real server gives to each kind of fault of a readable body.
# A fault of the kind ``operation`` has no code.
_CODE_OF_KIND = {"parse": "GRAPHQL_PARSE_FAILED", "validation": "GRAPHQL_VALIDATION_FAILED"}
# The kinds that the real server answers with HTTP 400 and plain text.
_TEXT_KINDS = ("not_json", "batch", "bad_shape")


@dataclass(frozen=True)
class RootField:
    name: str  # the schema field name, never the alias
    response_key: str  # the alias when one is given, else the name
    arguments: dict[str, object]  # evaluated; declared defaults applied; an absent variable leaves its key out
    selection: Any  # the SelectionSetNode of the field; None for a leaf


@dataclass(frozen=True)
class GraphQLRequest:
    operation: str  # "query" | "mutation" | "subscription"
    roots: tuple[RootField, ...]  # document order; fragments expanded; @skip and @include applied
    method: str  # the root names joined with "&"; "unknown" when no root is left
    request_id: str | int | None  # variables.request_id when it is a str or an int (not a bool), else None
    variables: dict[str, object]  # as sent, plus declared defaults
    fragments: dict[str, Any]  # name -> FragmentDefinitionNode


class GraphQLRequestError(Exception):
    """A body that cannot be answered as a request.

    ``kind`` is one of ``not_json``, ``batch``, ``bad_shape``, ``parse``,
    ``validation`` and ``operation``. ``message`` is the text that the real Sui
    server gives for this fault."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _library() -> tuple[Any, Any, Any]:
    """The three things that are read from ``graphql-core``, imported when a
    request is read and not before."""
    import graphql
    from graphql.pyutils import Undefined
    from graphql.utilities import value_from_ast_untyped

    return graphql, Undefined, value_from_ast_untyped


def _invalid_request(reason: str, line: int, column: int) -> str:
    return f'InvalidRequest(Error("{reason}", line: {line}, column: {column}))'


def _read_object(body: bytes) -> dict:
    """The JSON object of the body, or the fault that says why there is none."""
    if not body:
        raise GraphQLRequestError("not_json", _EMPTY_BODY)
    try:
        value = json.loads(body.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise GraphQLRequestError("not_json", _invalid_request(exc.reason, 1, exc.start + 1)) from None
    except json.JSONDecodeError as exc:
        raise GraphQLRequestError("not_json", _invalid_request(exc.msg, exc.lineno, exc.colno)) from None
    except RecursionError:
        raise GraphQLRequestError("not_json", _invalid_request("recursion limit exceeded", 1, 129)) from None
    except ValueError:
        # The one ValueError that ``json.loads`` raises besides the syntax error is the
        # limit of Python on the digits of an integer (4300). The real server reads
        # such a number as a float and refuses it as out of range.
        raise GraphQLRequestError("not_json", _invalid_request("number out of range", 1, 0)) from None
    if isinstance(value, list):
        raise GraphQLRequestError("batch", _BATCH)
    if not isinstance(value, dict):
        raise GraphQLRequestError("bad_shape", _BAD_SHAPE)
    return value


def _check_shape(payload: dict) -> None:
    """``query`` is a text, ``variables`` an object and ``operationName`` a text,
    each when it is there. Null is accepted for the last two."""
    if "query" in payload and not isinstance(payload["query"], str):
        raise GraphQLRequestError("bad_shape", _BAD_SHAPE)
    if payload.get("variables") is not None and not isinstance(payload["variables"], dict):
        raise GraphQLRequestError("bad_shape", _BAD_SHAPE)
    if payload.get("operationName") is not None and not isinstance(payload["operationName"], str):
        raise GraphQLRequestError("bad_shape", _BAD_SHAPE)


def _parse_document(text: str) -> Any:
    graphql, _undefined, _value_of = _library()
    try:
        return graphql.parse(text, no_location=True)
    except graphql.GraphQLSyntaxError as exc:
        raise GraphQLRequestError("parse", exc.message) from None
    except RecursionError:
        # The parser of graphql-core is recursive. A document nested a few hundred
        # levels deep ends here, and it is far past the limit of the real server.
        raise GraphQLRequestError("parse", RECURSION_MESSAGE) from None


def _choose_operation(document: Any, operation_name: str | None) -> Any:
    graphql, _undefined, _value_of = _library()
    operations = [d for d in document.definitions if isinstance(d, graphql.OperationDefinitionNode)]
    if not operations:
        raise GraphQLRequestError("parse", _NO_OPERATION)
    if operation_name is not None:
        for operation in operations:
            if operation.name is not None and operation.name.value == operation_name:
                return operation
        raise GraphQLRequestError("operation", f'Unknown operation named "{operation_name}"')
    if len(operations) > 1:
        raise GraphQLRequestError("operation", _OPERATION_NAME_REQUIRED)
    return operations[0]


def _variables_of(operation: Any, sent: dict) -> dict[str, object]:
    """The variables as sent, then the declared default of each variable that was
    not sent. ``graphql-core`` does not apply a default itself."""
    _graphql, _undefined, value_of = _library()
    variables = dict(sent)
    for definition in operation.variable_definitions or ():
        name = definition.variable.name.value
        if name not in variables and definition.default_value is not None:
            variables[name] = value_of(definition.default_value)
    return variables


def _depth_of(selection_set: Any, fragments: dict, memo: dict, stack: tuple[str, ...]) -> int:
    """How many selection sets deep the document goes below this one, this one
    included. Fragments count where they are spread. A spread of a fragment that
    is on the stack is a cycle, and a spread of a fragment that is not defined is
    a fault of its own. Each fragment is measured once, so a document that spreads
    the same fragment many times is read in one pass."""
    graphql, _undefined, _value_of = _library()
    deepest = 0
    for selection in selection_set.selections:
        below = 0
        if isinstance(selection, graphql.FragmentSpreadNode):
            name = selection.name.value
            if name in stack:
                raise GraphQLRequestError("parse", RECURSION_MESSAGE)
            if name not in fragments:
                raise GraphQLRequestError("validation", f'Unknown fragment: "{name}"')
            if name not in memo:
                memo[name] = _depth_of(fragments[name].selection_set, fragments, memo, stack + (name,))
            below = memo[name]
        elif selection.selection_set is not None:
            below = _depth_of(selection.selection_set, fragments, memo, stack)
        deepest = max(deepest, below)
    return 1 + deepest


def _check_depth(operation: Any, fragments: dict) -> None:
    try:
        depth = _depth_of(operation.selection_set, fragments, {}, ())
    except RecursionError:
        raise GraphQLRequestError("parse", RECURSION_MESSAGE) from None
    if depth > MAX_DEPTH:
        raise GraphQLRequestError("parse", RECURSION_MESSAGE)


def _condition(directive: Any, variables: dict) -> bool | None:
    """The value of ``if`` of ``@skip`` or ``@include``. None when it is not a
    Boolean: a literal of another type, or a variable that has no value."""
    _graphql, _undefined, value_of = _library()
    for argument in directive.arguments or ():
        if argument.name.value == "if":
            value = value_of(argument.value, variables)
            return value if isinstance(value, bool) else None
    return None


def _is_included(selection: Any, variables: dict) -> bool:
    """False when ``@skip(if: true)`` or ``@include(if: false)`` removes the
    selection. A condition that is not a Boolean leaves the selection in."""
    for directive in selection.directives or ():
        name = directive.name.value
        if name not in ("skip", "include"):
            continue
        condition = _condition(directive, variables)
        if condition is None:
            continue
        if (name == "skip" and condition) or (name == "include" and not condition):
            return False
    return True


def _roots_of(selection_set: Any, fragments: dict, variables: dict) -> list[RootField]:
    graphql, undefined, value_of = _library()
    roots: list[RootField] = []
    for selection in selection_set.selections:
        if not _is_included(selection, variables):
            continue
        if isinstance(selection, graphql.FieldNode):
            name = selection.name.value
            arguments: dict[str, object] = {}
            for argument in selection.arguments or ():
                value = value_of(argument.value, variables)
                if value is not undefined:
                    arguments[argument.name.value] = value
            alias = selection.alias.value if selection.alias is not None else name
            roots.append(RootField(name, alias, arguments, selection.selection_set))
        elif isinstance(selection, graphql.InlineFragmentNode):
            roots.extend(_roots_of(selection.selection_set, fragments, variables))
        else:  # a fragment spread; _check_depth already found every undefined one
            roots.extend(_roots_of(fragments[selection.name.value].selection_set, fragments, variables))
    return roots


def parse_graphql_request(body: bytes) -> GraphQLRequest:
    """Read the body of a request. Raises ``GraphQLRequestError`` when the real
    server would not answer it as a request, with the kind of the fault.

    An empty body, or a body that is not JSON or not UTF-8, is ``not_json``. A
    top-level array is ``batch``. Any other JSON that is not an object, and an
    object whose ``query``, ``variables`` or ``operationName`` has the wrong
    type, is ``bad_shape``. An object with no ``query``, an empty query, a syntax
    error, a document with no operation, a fragment cycle, and a nesting deeper
    than 32 levels are ``parse``. A spread of an undefined fragment is
    ``validation``. Two operations with no ``operationName``, and an
    ``operationName`` that matches none, are ``operation``."""
    graphql, _undefined, _value_of = _library()
    payload = _read_object(body)
    _check_shape(payload)
    text = payload.get("query")
    if not text:
        raise GraphQLRequestError("parse", _NO_DOCUMENT)
    document = _parse_document(text)
    operation = _choose_operation(document, payload.get("operationName"))
    fragments = {d.name.value: d for d in document.definitions if isinstance(d, graphql.FragmentDefinitionNode)}
    _check_depth(operation, fragments)
    variables = _variables_of(operation, payload.get("variables") or {})
    roots = tuple(_roots_of(operation.selection_set, fragments, variables))
    candidate = variables.get("request_id")
    request_id = candidate if isinstance(candidate, (str, int)) and not isinstance(candidate, bool) else None
    return GraphQLRequest(
        operation=operation.operation.value,
        roots=roots,
        method="&".join(root.name for root in roots) or "unknown",
        request_id=request_id,
        variables=variables,
        fragments=fragments,
    )


def graphql_error(message: str, code: str | None = None, path: list[str] | None = None) -> dict:
    """One entry of the ``errors`` list of a reply. ``extensions.code`` is there
    only for a code that is not empty."""
    entry: dict = {"message": message}
    if code:
        entry["extensions"] = {"code": code}
    if path is not None:
        entry["path"] = path
    return entry


def default_error_reply(error: GraphQLRequestError) -> tuple[int, object]:
    """The status and the body that the real Sui server answers for this fault.
    A body that is not a request gets HTTP 400 and plain text. A document that
    cannot be answered gets HTTP 200 and ``data`` null beside one error."""
    if error.kind in _TEXT_KINDS:
        return 400, error.message
    return 200, {"data": None, "errors": [graphql_error(error.message, _CODE_OF_KIND.get(error.kind))]}


def project(stub: object, selection: Any, request: GraphQLRequest) -> object:
    """Shape a Python value by the selection set of a field.

    A dict gives only the requested keys, under their aliases, and a requested
    key that the dict lacks gives None. A list is shaped item by item. A scalar,
    None, and any value of a field with no selection set come back as they are.
    Fragment spreads and inline fragments expand with the fragments of the
    request, and ``@skip`` and ``@include`` apply to each nested field."""
    if selection is None:
        return stub
    if isinstance(stub, list):
        return [project(item, selection, request) for item in stub]
    if not isinstance(stub, dict):
        return stub
    shaped: dict[str, object] = {}
    _fill(shaped, stub, selection, request)
    return shaped


def _fill(shaped: dict, stub: dict, selection_set: Any, request: GraphQLRequest) -> None:
    graphql, _undefined, _value_of = _library()
    for selection in selection_set.selections:
        if not _is_included(selection, request.variables):
            continue
        if isinstance(selection, graphql.FieldNode):
            name = selection.name.value
            key = selection.alias.value if selection.alias is not None else name
            shaped[key] = project(stub.get(name), selection.selection_set, request)
        elif isinstance(selection, graphql.InlineFragmentNode):
            _fill(shaped, stub, selection.selection_set, request)
        else:  # a fragment spread
            fragment = request.fragments.get(selection.name.value)
            if fragment is not None:
                _fill(shaped, stub, fragment.selection_set, request)
