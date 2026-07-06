"""Default Tendermint-RPC response stubs (MAG-1841).

Companion to ``stubs.py`` (Ethereum JSON-RPC), ``stubs_btc.py`` (Bitcoin
JSON-RPC), and ``stubs_rest.py`` (Cosmos REST). Keyed by Tendermint method
name (``status`` / ``health`` / ``block`` / ...), value is the ``result``
field of the JSON-RPC envelope (the handler module adds the envelope).

Method scope (v1) — 7 methods
-----------------------------

The minimum-viable set the smart-router's failure-mode + routing-behavior
tests exercise against the simulator (MAG-1741 residual scope):

* ``status``       — node identity, sync info, validator info.
* ``health``       — bare ``{}`` (liveness probe).
* ``abci_info``    — app version + last-block fields.
* ``block``        — block_id + header at the requested height.
* ``validators``   — paginated validator list (``page`` / ``per_page``).
* ``abci_query``   — ABCI response envelope (height echo, code).
* ``net_info``     — peer connectivity (listeners / n_peers / peers).

The 24 remaining CometBFT methods (block_results, block_search, commit,
consensus_state, dump_consensus_state, num_unconfirmed_txs, tx, tx_search,
genesis, blockchain, broadcast_tx_*, header, header_by_hash, …) are
out of scope for this ticket. Each one is a separate envelope shape to
maintain; add them in follow-up tickets when a specific test needs them.

Fixture-shape policy
--------------------

"Minimal-valid by default" — same policy as ``stubs_rest.py``. Every
response is the smallest body the CometBFT docs declare. Real-node fields
that no router-side classifier reads are stubbed minimally or omitted.

| Method      | Shape choice                                         | Why                                                                                          |
|-------------|------------------------------------------------------|----------------------------------------------------------------------------------------------|
| status      | node_info + sync_info + validator_info                | These three sub-dicts are what every CometBFT-aware tool reads first.                        |
| health      | ``{}``                                                | Bare empty dict; the only success shape CometBFT defines.                                    |
| abci_info   | response.{data, version, app_version, …}              | Pruning verification on the router checks ``response.last_block_height``.                    |
| block       | block_id + block.header + block.data + block.last_commit | Header.height is overridden per request in handlers_tendermintrpc; commit kept for shape.    |
| validators  | block_height + validators[] + count + total            | Pagination contract (count == per_page mid-page) is the contract MAG-1741 tests exercise.    |
| abci_query  | response.{code, log, info, index, key, value, proofOps, height, codespace} | Height echo is verified by MAG-1741 test_tm_abci_query_echoes_height_param.                  |
| net_info    | listening + listeners + n_peers + peers               | n_peers is a string-encoded int per CometBFT (verified against live gateway 2026-05-16).     |

Adding a new method
-------------------

1. Add an entry to ``TENDERMINT_METHOD_DEFAULTS`` keyed by the method name.
2. Note the shape choice in the table above.
3. If the method needs request-time logic (echo a param, slice a list,
   shift by ``blocks_behind``), implement it in ``handlers_tendermintrpc.handle``.
"""

from copy import deepcopy
from typing import Any, Dict, List

from constants import (
    TM_APP_HASH,
    TM_BLOCK_HASH,
    TM_LATEST_HEIGHT,
    TM_NETWORK_ID,
    TM_PROPOSER_ADDR,
    TM_VALIDATOR_ADDR,
)


def tm_height(blocks_behind: int = 0) -> str:
    """Return the effective Tendermint head as a string-encoded integer.

    CometBFT serialises block heights as JSON strings of decimal digits
    (``"5000000"``) — distinct from Cosmos REST's string-int shape it
    inherits (same convention) and from ETH's hex prefix. Tests that pin
    against a specific shifted head should call this helper with the same
    ``blocks_behind`` they passed to ``/scenario``.
    """
    return str(TM_LATEST_HEIGHT - blocks_behind)


# ── Synthesised validator-set ──────────────────────────────────────────────
# A pool large enough that paginating with per_page=2 gives non-overlapping
# pages 1 vs 2 vs 3. Tests that need a specific pool size can override via
# state.responses["validators"] = {...}.

_VALIDATOR_POOL: List[Dict[str, Any]] = [
    {
        "address": f"{i:040X}",
        "pub_key": {"type": "tendermint/PubKeyEd25519", "value": f"pubkey_{i}_base64=="},
        "voting_power": str(1_000_000 - i * 1000),
        "proposer_priority": str(-1 * i * 100),
    }
    for i in range(1, 13)
]


def _status_response() -> Dict[str, Any]:
    """status — node identity + sync info + local validator."""
    return {
        "node_info": {
            "protocol_version": {"p2p": "8", "block": "11", "app": "0"},
            "id": "0" * 40,
            "listen_addr": "tcp://0.0.0.0:26656",
            "network": TM_NETWORK_ID,
            "version": "0.34.27",
            "channels": "40202122233038606100",
            "moniker": "sim-node",
            "other": {"tx_index": "on", "rpc_address": "tcp://0.0.0.0:26657"},
        },
        "sync_info": {
            "latest_block_hash": TM_BLOCK_HASH,
            "latest_app_hash": TM_APP_HASH,
            "latest_block_height": str(TM_LATEST_HEIGHT),
            "latest_block_time": "2026-05-16T00:00:00Z",
            "earliest_block_hash": TM_BLOCK_HASH,
            "earliest_app_hash": TM_APP_HASH,
            "earliest_block_height": "1",
            "earliest_block_time": "2024-01-01T00:00:00Z",
            "catching_up": False,
        },
        "validator_info": {
            "address": TM_VALIDATOR_ADDR,
            "pub_key": {
                "type": "tendermint/PubKeyEd25519",
                "value": "sim_validator_pubkey_base64==",
            },
            "voting_power": "1000000",
        },
    }


def _abci_info_response() -> Dict[str, Any]:
    """abci_info — app identity + last-block fields. Pruning verifier reads this."""
    return {
        "response": {
            "data": "sim-app",
            "version": "1.0.0",
            "app_version": "1",
            "last_block_height": str(TM_LATEST_HEIGHT),
            "last_block_app_hash": TM_APP_HASH,
        }
    }


def _block_response(height: int = TM_LATEST_HEIGHT) -> Dict[str, Any]:
    """block — block_id + block envelope.

    ``block.header.height`` is overridden per-request in
    ``handlers_tendermintrpc.handle`` so a request for any height N gets a
    response with that height echoed back (mirrors the ETH
    ``eth_getBlockByNumber`` and REST ``/blocks/{height}`` shifts).
    """
    return {
        "block_id": {
            "hash": TM_BLOCK_HASH,
            "parts": {"total": 1, "hash": TM_BLOCK_HASH},
        },
        "block": {
            "header": {
                "version": {"block": "11", "app": "0"},
                "chain_id": TM_NETWORK_ID,
                "height": str(height),
                "time": "2026-05-16T00:00:00Z",
                "last_block_id": {
                    "hash": TM_BLOCK_HASH,
                    "parts": {"total": 1, "hash": TM_BLOCK_HASH},
                },
                "last_commit_hash": TM_BLOCK_HASH,
                "data_hash": TM_BLOCK_HASH,
                "validators_hash": TM_BLOCK_HASH,
                "next_validators_hash": TM_BLOCK_HASH,
                "consensus_hash": TM_BLOCK_HASH,
                "app_hash": TM_APP_HASH,
                "last_results_hash": TM_BLOCK_HASH,
                "evidence_hash": TM_BLOCK_HASH,
                "proposer_address": TM_PROPOSER_ADDR,
            },
            "data": {"txs": []},
            "evidence": {"evidence": []},
            "last_commit": {
                "height": str(max(height - 1, 0)),
                "round": 0,
                "block_id": {
                    "hash": TM_BLOCK_HASH,
                    "parts": {"total": 1, "hash": TM_BLOCK_HASH},
                },
                "signatures": [],
            },
        },
    }


def _validators_response(
    height: int = TM_LATEST_HEIGHT, page: int = 1, per_page: int = 30
) -> Dict[str, Any]:
    """validators — paginated slice of the validator pool.

    ``page`` and ``per_page`` slice the validator pool. CometBFT contract:
    ``count == per_page`` mid-page (last page may return fewer); ``total``
    is the full pool size independent of paging.
    """
    total = len(_VALIDATOR_POOL)
    start = max((page - 1) * per_page, 0)
    end = min(start + per_page, total)
    slice_ = deepcopy(_VALIDATOR_POOL[start:end])
    return {
        "block_height": str(height),
        "validators": slice_,
        "count": str(len(slice_)),
        "total": str(total),
    }


def _abci_query_response(
    path: str = "",
    data: str = "",
    height: int = 0,
) -> Dict[str, Any]:
    """abci_query — ABCI response envelope.

    The 9-key envelope (``code``, ``log``, ``info``, ``index``, ``key``,
    ``value``, ``proofOps``, ``height``, ``codespace``) is pinned exactly
    because MAG-1741's test asserts on the set-equality of these keys.
    ``height`` is echoed from the caller; defaults to 0 when the caller
    didn't supply one.
    """
    return {
        "response": {
            "code": 0,
            "log": "",
            "info": "",
            "index": "0",
            "key": "",
            "value": "",
            "proofOps": None,
            "height": str(height),
            "codespace": "",
        }
    }


def _net_info_response() -> Dict[str, Any]:
    """net_info — peer connectivity envelope.

    ``n_peers`` is a string-encoded integer per the CometBFT contract
    (verified against the live Lava gateway 2026-05-16). A sim with three
    upstream peers is a reasonable default; tests can override via
    ``state.responses["net_info"] = {"body": {...}}``.
    """
    return {
        "listening": True,
        "listeners": ["Listener(@)"],
        "n_peers": "3",
        "peers": [
            {
                "node_info": {
                    "id": f"peer{i:039d}",
                    "listen_addr": f"tcp://10.0.0.{i}:26656",
                    "network": TM_NETWORK_ID,
                    "moniker": f"sim-peer-{i}",
                },
                "is_outbound": True,
                "connection_status": {
                    "Duration": str(60_000_000_000),  # 1 minute in ns
                    "SendMonitor": {},
                    "RecvMonitor": {},
                    "Channels": [],
                },
                "remote_ip": f"10.0.0.{i}",
            }
            for i in (1, 2, 3)
        ],
    }


# Method → result-body map. The handler module reads from this, applies
# request-time overrides (height echo, pagination slicing), and wraps the
# result in a JSON-RPC envelope before returning.
TENDERMINT_METHOD_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "status": _status_response(),
    "health": {},  # Bare empty result — only success shape CometBFT defines.
    "abci_info": _abci_info_response(),
    "block": _block_response(),
    "validators": _validators_response(),
    "abci_query": _abci_query_response(),
    "net_info": _net_info_response(),
}


# ── Tendermint-RPC error stubs ─────────────────────────────────────────────
#
# TENDERMINT_ERROR_STUBS mirrors stubs.py::ERROR_STUBS (ETH) and
# stubs_btc.py::BTC_ERROR_STUBS with CometBFT's JSON-RPC error convention.
# Stubs are the *inner* error object; ``handlers_tendermintrpc.handle``
# returns ``{"error": <stub>}`` and the caller (``TendermintHandler`` in
# ``server.py``) wraps it into the JSON-RPC envelope at emission time —
# the same flow as the raw ``{"error": {...}}`` override path.
#
# Usage via the per-method override path:
#     POST /scenario {"providers": {"1": {"chain_family": "tendermintrpc",
#                                          "responses": {"status":
#                                              {"error_stub": "internal"}}}}}

TENDERMINT_ERROR_STUBS: Dict[str, Dict[str, Any]] = {

    # Method not found — JSON-RPC 2.0 -32601, the code CometBFT emits for
    # an unknown RPC endpoint. Same code the handler's own unknown-method
    # fallback uses.
    "method_not_found": {
        "code":    -32601,
        "message": "Method not found",
    },

    # Internal error — JSON-RPC 2.0 -32603. CometBFT's generic node-side
    # failure shape (e.g. an ABCI query that panicked).
    "internal": {
        "code":    -32603,
        "message": "Internal error",
    },
}
