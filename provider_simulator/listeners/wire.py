"""Serialize a response body to JSON bytes, applying corruption if configured.

The fault ladder decides down / hang / drop / rate_limit / error. A *successful*
response can still be corrupted (corruption composes with mode=success), and this
is the one place that corruption becomes wire bytes — ported verbatim from the
flat handlers' ``_reply`` so the output is byte-identical.

Dict-level corruption (missing_field / wrong_type / empty_response) runs before
serialization; byte-level corruption (truncated / invalid_json) runs after.
``dotted`` selects REST semantics: a dotted missing_field path
(``block.header.height``) and a first-key default for wrong_type, versus
JSON-RPC's flat top-level field and its ``result`` default.
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
