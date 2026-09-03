"""Serialize a response body to wire bytes, applying corruption if configured.

The fault ladder decides down / hang / drop / rate_limit / error. A *successful*
response can still be corrupted (corruption composes with mode=success), and this
is the one place that corruption becomes wire bytes — ported verbatim from the
flat handlers' ``_reply`` so the output is byte-identical.

Dict-level corruption (missing_field / wrong_type / empty_response) runs before
serialization; byte-level corruption (truncated / invalid_json) runs after;
``null_body`` replaces the whole wire body with the literal ``null`` before
either, whatever the body type.
``dotted`` selects REST semantics: a dotted missing_field path
(``block.header.height``) and a first-key default for wrong_type, versus
JSON-RPC's flat top-level field and its ``result`` default.

A ``str`` body (the JSON-RPC ``rate_limit`` fault's prose text) is written as
UTF-8 bytes as-is, never through ``json.dumps`` — dumping a string would quote
and escape it into a JSON string literal, which is not a text/plain wire body.
``missing_field`` / ``wrong_type`` have no meaning on a string (no fields to
target) and are no-ops there, matching how they already no-op on any other
non-dict body; ``empty_response`` / ``truncated`` / ``invalid_json`` still
apply — corruption composes with whatever body was actually written.
"""

import json


def serialize(
    status: int,
    body: object,
    corruption_mode: str | None = None,
    missing_field: str | None = None,
    dotted: bool = False,
) -> tuple[int, bytes, bool]:
    """Return ``(status, body_bytes, emit_body)``.

    ``emit_body`` is False only for ``empty_response`` — the caller sends the
    status and a zero-length body.
    """
    # null_body is a WHOLE-BODY override: the wire carries the four bytes
    # ``null`` — valid JSON, no envelope at all. It reproduces the reply shape
    # smart-router v1.4.1 hardened against (a node answering literal null used
    # to end the router process), so it applies whatever the body type was.
    if corruption_mode == "null_body":
        return status, b"null", True

    data = body
    if isinstance(data, dict):
        if corruption_mode == "missing_field" and missing_field:
            data = (
                _remove_dotted(data, missing_field) if dotted else {k: v for k, v in data.items() if k != missing_field}
            )
        elif corruption_mode == "empty_response":
            return status, b"", False
        elif corruption_mode == "wrong_type":
            target = missing_field or ("result" if not dotted else next(iter(data.keys()), None))
            if target and target in data:
                data = dict(data)
                data[target] = _swap_type(data[target])
        raw = json.dumps(data).encode()
    elif isinstance(data, str):
        if corruption_mode == "empty_response":
            return status, b"", False
        raw = data.encode()
    else:
        raw = json.dumps(data).encode()

    if corruption_mode == "truncated" and len(raw) > 10:
        raw = raw[:-10]
    elif corruption_mode == "invalid_json":
        raw = b"}{ {{ not valid json"
    return status, raw, True


def _swap_type(current: object) -> object:
    """Return a value of a different type than ``current`` (bool before int)."""
    if isinstance(current, bool):
        return 1 if current else 0
    if isinstance(current, str):
        return 12345
    if isinstance(current, (int, float)):
        return "wrong_type_value"
    return "wrong_type_value"


def _remove_dotted(data: dict, path: str) -> dict:
    """Return ``data`` with the dotted-path key removed; dicts cloned on descent.

    Missing intermediate keys leave ``data`` unchanged.
    """
    if not path:
        return data
    segments = path.split(".")
    if len(segments) == 1:
        return {k: v for k, v in data.items() if k != segments[0]}
    head, rest = segments[0], ".".join(segments[1:])
    if not isinstance(data.get(head), dict):
        return data
    return {**data, head: _remove_dotted(data[head], rest)}
