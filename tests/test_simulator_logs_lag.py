"""
Integration tests for the eth_getLogs stale-indexing primitive.

Runs against the shared in-process simulator (see conftest.py) and covers the
``logs_indexed_up_to`` / ``logs_lag_mode`` Ethereum quirks:

  Default (no lag set)        — eth_getLogs returns its configured response
                                 unchanged.
  logs_lag_mode="empty"        — When the query's ``toBlock`` exceeds
                                 ``logs_indexed_up_to``, the response is forced
                                 to an empty array even when ``responses`` has
                                 logs configured.
  logs_lag_mode="partial"      — Same trigger, but filters the canned response
                                 to entries whose ``blockNumber`` (hex) is
                                 <= ``logs_indexed_up_to``.
  eth_blockNumber unaffected   — Setting logs lag does NOT shift the head;
                                 head-fresh + logs-lagged is the divergence
                                 we want to model.
  Reset clears the lag         — POST /reset returns logs_indexed_up_to to None
                                 and logs_lag_mode to "empty".

Run with:
  pytest tests/test_simulator_logs_lag.py -v
"""

import json
import urllib.error
import urllib.request

import pytest

from constants import ETH_PRIMARY_PORTS

_P1 = f"http://127.0.0.1:{ETH_PRIMARY_PORTS['1']}"


# ── HTTP helpers (kept self-contained, mirrors test_simulator_btc.py) ─────────


def _post(url: str, body: dict) -> tuple[int, dict]:
    """POST JSON body, return (status_code, parsed_response_body)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
            return e.code, json.loads(raw) if raw else {}
        except (ConnectionResetError, OSError):
            return e.code, {}


def _get(url: str) -> tuple[int, dict]:
    """GET url, return (status_code, parsed_response_body)."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else {}


def _rpc(url: str, method: str, params: list | None = None) -> tuple[int, dict]:
    """Send a JSON-RPC request, return (http_status, response_body)."""
    return _post(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})


def _ctrl(sim: dict, path: str) -> str:
    return sim["control"] + path


# ── Function-scoped autouse: clean slate before/after every test ──────────────


@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario config AND clear history before and after every test."""
    _post(_ctrl(sim, "/reset/all"), {})
    yield
    _post(_ctrl(sim, "/reset/all"), {})


# Canonical canned log entries for partial-mode tests. Block numbers chosen so
# the partial-filter assertion is unambiguous: head=20_000_000 (0x1312D00),
# indexed-up-to=19_999_950 (50 blocks lagged), entries straddle the boundary.
HEAD_BLOCK = 0x1312D00  # 20_000_000
INDEXED_UP_TO_DEFAULT = HEAD_BLOCK - 50

_LOG_ENTRY_OLD = {
    "address": "0x" + "a" * 40,
    "topics": ["0x" + "1" * 64],
    "data": "0x",
    "blockNumber": hex(HEAD_BLOCK - 100),  # well below indexed-up-to
    "transactionHash": "0x" + "b" * 64,
    "transactionIndex": "0x0",
    "blockHash": "0x" + "c" * 64,
    "logIndex": "0x0",
    "removed": False,
}

_LOG_ENTRY_NEAR_INDEXED = {
    "address": "0x" + "a" * 40,
    "topics": ["0x" + "2" * 64],
    "data": "0x",
    "blockNumber": hex(HEAD_BLOCK - 60),  # below indexed-up-to (HEAD - 50)
    "transactionHash": "0x" + "d" * 64,
    "transactionIndex": "0x0",
    "blockHash": "0x" + "e" * 64,
    "logIndex": "0x0",
    "removed": False,
}

_LOG_ENTRY_PAST_INDEXED = {
    "address": "0x" + "a" * 40,
    "topics": ["0x" + "3" * 64],
    "data": "0x",
    "blockNumber": hex(HEAD_BLOCK - 30),  # above indexed-up-to → filtered out
    "transactionHash": "0x" + "f" * 64,
    "transactionIndex": "0x0",
    "blockHash": "0x" + "0" * 64,
    "logIndex": "0x0",
    "removed": False,
}

_LOG_ENTRY_AT_HEAD = {
    "address": "0x" + "a" * 40,
    "topics": ["0x" + "4" * 64],
    "data": "0x",
    "blockNumber": hex(HEAD_BLOCK),  # at head → filtered out
    "transactionHash": "0x" + "9" * 64,
    "transactionIndex": "0x0",
    "blockHash": "0x" + "8" * 64,
    "logIndex": "0x0",
    "removed": False,
}

_CANNED_LOGS = [
    _LOG_ENTRY_OLD,
    _LOG_ENTRY_NEAR_INDEXED,
    _LOG_ENTRY_PAST_INDEXED,
    _LOG_ENTRY_AT_HEAD,
]


# ─────────────────────────────────────────────────────────────────────────────
# Defaults — primitive is off until set
# ─────────────────────────────────────────────────────────────────────────────


class TestDefaults:

    def test_logs_indexed_up_to_defaults_to_none(self, sim):
        """A freshly-reset eth provider has logs_indexed_up_to=None (off)."""
        _, body = _get(_ctrl(sim, "/scenario"))
        for pid in ("1", "2", "3"):
            assert body["providers"][f"eth-sim:{pid}"]["logs_indexed_up_to"] is None
            assert body["providers"][f"eth-sim:{pid}"]["logs_lag_mode"] == "empty"

    def test_eth_getLogs_unaffected_when_primitive_unset(self, sim):
        """Without logs_indexed_up_to, eth_getLogs returns configured response.

        Kraken-CCIP risk control: the primitive must not change behaviour for
        callers that don't opt in — existing baseline tests stay green.
        """
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "responses": {"eth_getLogs": {"result": _CANNED_LOGS}},
                    }
                }
            },
        )
        params = [{"fromBlock": hex(HEAD_BLOCK - 30), "toBlock": "latest"}]
        status, body = _rpc(_P1, "eth_getLogs", params)
        assert status == 200
        assert "error" not in body
        # Full list returned — no filtering applied because primitive is off.
        assert body["result"] == _CANNED_LOGS


# ─────────────────────────────────────────────────────────────────────────────
# Empty mode — toBlock past indexed → []
# ─────────────────────────────────────────────────────────────────────────────


class TestEmptyMode:

    def test_empty_mode_returns_empty_array_when_range_exceeds_indexed(self, sim):
        """Query touches a block above logs_indexed_up_to → response forced to [].

        Kraken-CCIP risk: a stale-indexed provider claiming 'no events here'
        is the silent-failure case Kraken-CCIP polls would hit without CV.
        """
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "logs_indexed_up_to": INDEXED_UP_TO_DEFAULT,
                        "logs_lag_mode": "empty",
                        "responses": {"eth_getLogs": {"result": _CANNED_LOGS}},
                    }
                }
            },
        )
        params = [{"fromBlock": hex(HEAD_BLOCK - 30), "toBlock": "latest"}]
        status, body = _rpc(_P1, "eth_getLogs", params)
        assert status == 200
        assert body["result"] == []

    def test_empty_mode_no_op_when_toBlock_within_indexed(self, sim):
        """Query stays at or below logs_indexed_up_to → no lag triggered."""
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "logs_indexed_up_to": INDEXED_UP_TO_DEFAULT,
                        "logs_lag_mode": "empty",
                        "responses": {"eth_getLogs": {"result": _CANNED_LOGS}},
                    }
                }
            },
        )
        params = [
            {
                "fromBlock": hex(HEAD_BLOCK - 200),
                "toBlock": hex(HEAD_BLOCK - 100),
            }
        ]
        status, body = _rpc(_P1, "eth_getLogs", params)
        assert status == 200
        # toBlock (HEAD-100) <= indexed-up-to (HEAD-50) → full payload returned.
        assert body["result"] == _CANNED_LOGS

    def test_empty_mode_is_default_when_logs_indexed_set_without_explicit_mode(self, sim):
        """Set only logs_indexed_up_to → mode defaults to 'empty'."""
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "logs_indexed_up_to": INDEXED_UP_TO_DEFAULT,
                        "responses": {"eth_getLogs": {"result": _CANNED_LOGS}},
                    }
                }
            },
        )
        params = [{"fromBlock": hex(HEAD_BLOCK - 30), "toBlock": "latest"}]
        _, body = _rpc(_P1, "eth_getLogs", params)
        assert body["result"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Partial mode — keep entries with blockNumber <= indexed
# ─────────────────────────────────────────────────────────────────────────────


class TestPartialMode:

    def test_partial_mode_filters_entries_past_indexed(self, sim):
        """logs_lag_mode='partial' keeps only entries with blockNumber <= indexed.

        Kraken-CCIP risk: some providers index partially during a lag window —
        a CCIPMessageReceived event in the lagged range would be silently
        omitted from the response. CV catches the divergence with other
        providers that have caught up.
        """
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "logs_indexed_up_to": INDEXED_UP_TO_DEFAULT,
                        "logs_lag_mode": "partial",
                        "responses": {"eth_getLogs": {"result": _CANNED_LOGS}},
                    }
                }
            },
        )
        params = [{"fromBlock": hex(HEAD_BLOCK - 200), "toBlock": "latest"}]
        status, body = _rpc(_P1, "eth_getLogs", params)
        assert status == 200
        # Only entries with blockNumber <= HEAD - 50 stay in result.
        retained = body["result"]
        assert len(retained) == 2
        assert retained[0]["blockNumber"] == hex(HEAD_BLOCK - 100)
        assert retained[1]["blockNumber"] == hex(HEAD_BLOCK - 60)

    def test_partial_mode_returns_empty_when_all_entries_past_indexed(self, sim):
        """If every canned entry is above logs_indexed_up_to → empty array."""
        all_post_index = [_LOG_ENTRY_PAST_INDEXED, _LOG_ENTRY_AT_HEAD]
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "logs_indexed_up_to": INDEXED_UP_TO_DEFAULT,
                        "logs_lag_mode": "partial",
                        "responses": {"eth_getLogs": {"result": all_post_index}},
                    }
                }
            },
        )
        params = [{"fromBlock": hex(HEAD_BLOCK - 40), "toBlock": "latest"}]
        _, body = _rpc(_P1, "eth_getLogs", params)
        assert body["result"] == []

    def test_partial_mode_unfiltered_when_toBlock_within_indexed(self, sim):
        """toBlock <= indexed → no filter (query doesn't touch lagged range)."""
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "logs_indexed_up_to": INDEXED_UP_TO_DEFAULT,
                        "logs_lag_mode": "partial",
                        "responses": {"eth_getLogs": {"result": _CANNED_LOGS}},
                    }
                }
            },
        )
        params = [
            {
                "fromBlock": hex(HEAD_BLOCK - 200),
                "toBlock": hex(HEAD_BLOCK - 100),
            }
        ]
        _, body = _rpc(_P1, "eth_getLogs", params)
        assert body["result"] == _CANNED_LOGS


# ─────────────────────────────────────────────────────────────────────────────
# Method isolation — only eth_getLogs is affected
# ─────────────────────────────────────────────────────────────────────────────


class TestMethodIsolation:

    def test_eth_blockNumber_unaffected_by_logs_lag(self, sim):
        """logs_indexed_up_to does NOT shift eth_blockNumber.

        Kraken-CCIP risk: the WHOLE POINT of the primitive is head-fresh +
        logs-lagged. If eth_blockNumber were also shifted, this would
        collapse into the existing blocks_behind primitive and we'd lose
        the divergence-vs-consensus contrast the test bundle relies on.
        """
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "logs_indexed_up_to": INDEXED_UP_TO_DEFAULT,
                        "logs_lag_mode": "empty",
                    }
                }
            },
        )
        _, body = _rpc(_P1, "eth_blockNumber")
        # Default head is METHOD_DEFAULTS["eth_blockNumber"] = "0x1312D00"
        assert body["result"].lower() == "0x1312d00"

    def test_eth_getBlockByNumber_unaffected_by_logs_lag(self, sim):
        """logs_indexed_up_to does NOT alter eth_getBlockByNumber."""
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "logs_indexed_up_to": INDEXED_UP_TO_DEFAULT,
                        "logs_lag_mode": "partial",
                    }
                }
            },
        )
        _, body = _rpc(_P1, "eth_getBlockByNumber", ["latest", False])
        # Should still be a dict (the canonical block stub) — not [], not error.
        assert isinstance(body["result"], dict)
        assert "number" in body["result"]


# ─────────────────────────────────────────────────────────────────────────────
# Reset clears the lag
# ─────────────────────────────────────────────────────────────────────────────


class TestResetClearsLag:

    def test_reset_returns_logs_indexed_up_to_to_none(self, sim):
        """POST /reset clears both logs_indexed_up_to and logs_lag_mode."""
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "logs_indexed_up_to": INDEXED_UP_TO_DEFAULT,
                        "logs_lag_mode": "partial",
                    }
                }
            },
        )
        # Verify lag was set.
        _, body = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["eth-sim:1"]["logs_indexed_up_to"] == INDEXED_UP_TO_DEFAULT
        assert body["providers"]["eth-sim:1"]["logs_lag_mode"] == "partial"

        # Reset and verify both are back to defaults.
        _post(_ctrl(sim, "/reset"), {})
        _, body = _get(_ctrl(sim, "/scenario"))
        assert body["providers"]["eth-sim:1"]["logs_indexed_up_to"] is None
        assert body["providers"]["eth-sim:1"]["logs_lag_mode"] == "empty"

    def test_eth_getLogs_returns_full_response_after_reset(self, sim):
        """After /reset, subsequent eth_getLogs ignore the previously-set lag."""
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "logs_indexed_up_to": INDEXED_UP_TO_DEFAULT,
                        "logs_lag_mode": "empty",
                        "responses": {"eth_getLogs": {"result": _CANNED_LOGS}},
                    }
                }
            },
        )
        # Confirm lag is active.
        params = [{"fromBlock": hex(HEAD_BLOCK - 30), "toBlock": "latest"}]
        _, body = _rpc(_P1, "eth_getLogs", params)
        assert body["result"] == []

        # Reset — wipes scenario config including responses + lag.
        _post(_ctrl(sim, "/reset"), {})

        # Default eth_getLogs is [] (METHOD_DEFAULTS) so we re-arm a payload
        # to prove the lag is cleared (otherwise we'd see [] and not be able
        # to distinguish "lag still applied" from "no payload set").
        _post(
            _ctrl(sim, "/scenario"),
            {
                "providers": {
                    "eth-sim:1": {
                        "responses": {"eth_getLogs": {"result": _CANNED_LOGS}},
                    }
                }
            },
        )
        _, body = _rpc(_P1, "eth_getLogs", params)
        assert body["result"] == _CANNED_LOGS
