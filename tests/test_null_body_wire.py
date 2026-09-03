"""null_body on the real wire: the whole HTTP body is the JSON literal null.

wire-level tests exist (test_wire.py); these lock the SERVER behavior the
wire tests cannot see — the Content-Type header. A rate-limit fault's body
is a str, and the header used to be chosen from that original type, so the
composed reply said text/plain while carrying JSON null.
"""

import json
import urllib.request


def _post(url: str, body: dict):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as err:
        return err.code, err.read(), err.headers.get("Content-Type", "")


def _set_scenario(sim, entry: dict):
    status, _, _ = _post(f"{sim['control']}/scenario", {"providers": {"eth-sim:1": entry}})
    assert status == 200, f"scenario setup must succeed, got HTTP {status}"


def _reset(sim):
    _post(f"{sim['control']}/reset/all", {})


def test_success_with_null_body_is_json_null_on_the_wire(sim):
    _reset(sim)
    try:
        _set_scenario(sim, {"mode": "success", "corruption_mode": "null_body"})
        status, raw, content_type = _post(
            "http://127.0.0.1:18545",
            {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
        )
        assert (status, raw) == (200, b"null"), f"the whole body must be the four bytes null, got {status} {raw!r}"
        assert content_type.startswith("application/json"), f"JSON null must be labeled as JSON, got {content_type!r}"
    finally:
        _reset(sim)


def test_rate_limit_composed_with_null_body_keeps_the_json_label(sim):
    _reset(sim)
    try:
        _set_scenario(sim, {"mode": "rate_limit", "corruption_mode": "null_body"})
        status, raw, content_type = _post(
            "http://127.0.0.1:18545",
            {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
        )
        assert (status, raw) == (429, b"null"), f"corruption composes with the fault body, got {status} {raw!r}"
        assert content_type.startswith("application/json"), (
            f"the original body was a str, but the wire carries JSON null -- "
            f"the label must follow the wire, got {content_type!r}"
        )
    finally:
        _reset(sim)
