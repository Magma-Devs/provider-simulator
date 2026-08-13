"""
stubs_rest.py — Default REST response stubs for Cosmos-style HTTP endpoints (MAG-1777).

Companion to ``stubs.py`` (Ethereum JSON-RPC) and ``stubs_btc.py`` (Bitcoin
JSON-RPC). Same goal — a method-name → response-result map — but keyed by the
REST tuple ``(verb, path_template)`` because REST dispatch is verb + URL-path,
not a JSON-RPC ``method`` string.

Method scope (v1)
-----------------
5 seed paths chosen for Cosmos REST parity (Q7 from the MAG-1777 design):

  GET  /cosmos/base/tendermint/v1beta1/blocks/latest
  GET  /cosmos/base/tendermint/v1beta1/blocks/{height}
  GET  /cosmos/base/tendermint/v1beta1/node_info
  GET  /cosmos/bank/v1beta1/balances/{address}
  GET  /cosmos/staking/v1beta1/validators

Adding new path-bundles (staking, governance, Lava-specific) is tracked as
follow-up tickets per the resolved Q7 decision — keep this file under one
bundle's worth of entries until the next ticket lands.

Fixture-shape policy per path (Q7 documentation requirement)
------------------------------------------------------------
"Minimal-valid by default" — same policy as the BTC stubs. Every response is
the smallest body the Cosmos SDK docs declare. The exhaustive shape from a
live node is NOT copied unless the smart router's classifier reads more than
a handful of fields.

| Path | Shape choice | Why |
|------|--------------|-----|
| ``/blocks/latest`` | 1 block_id + 1 block (header, data, evidence, last_commit) | Router's pruning verification reads ``block.header.height`` and ``chain_id`` — keep both. Other fields stubbed minimally. |
| ``/blocks/{height}`` | Same shape as ``/blocks/latest`` | Path param ``{height}`` overrides ``block.header.height`` at request time in handlers_rest. |
| ``/node_info`` | default_node_info + application_version | Two top-level keys most callers read. Inner fields are minimal but well-typed. |
| ``/balances/{address}`` | balances list (1 entry) + pagination | Single ulava balance covers the happy-path classifier check; address is echoed via path param. |
| ``/validators`` | validators list (1 entry) + pagination | One active validator is enough to verify the router decoded the list shape. |

Adding a new path
-----------------
1. Add an entry to ``REST_METHOD_DEFAULTS`` below keyed by ``(verb, path_template)``.
2. Note its shape in the table above so future readers know the policy.
3. If the path needs request-time logic (echo a path param, shift by
   ``blocks_behind``, query-string handling), implement it in
   ``handlers_rest.handle``.
"""

from copy import deepcopy
from typing import Any, Dict, Tuple

from constants import ETH_LATEST_BLOCK as _ETH_LATEST_HEX

# ── Synthetic Cosmos head ──────────────────────────────────────────────────────
# We re-use the ETH simulator's notion of "latest block" so the same
# blocks_behind primitive shifts consistently across chain_family options.
# Cosmos heights are decimal strings (``"20000000"``), unlike ETH's hex.

REST_LATEST_HEIGHT: int = int(_ETH_LATEST_HEX, 16)


def cosmos_height(blocks_behind: int = 0) -> str:
    """Return the effective Cosmos head as a decimal string.

    The Cosmos SDK encodes block heights as JSON strings of decimal digits
    (``"20000000"``) — distinct from ETH's ``"0x1312D00"`` hex convention.
    Tests that pin against a specific shifted head should call this helper
    with the same ``blocks_behind`` they passed to ``/scenario``.
    """
    return str(REST_LATEST_HEIGHT - blocks_behind)


# ── Object factories ──────────────────────────────────────────────────────────


def _block_response(height: int = REST_LATEST_HEIGHT) -> Dict[str, Any]:
    """Minimal valid Cosmos ``/blocks/{height}`` (or ``/blocks/latest``) body.

    ``block.header.height`` is overridden at request time in
    ``handlers_rest.handle`` so a request for any height N gets a response
    with that height echoed back — mirrors the ETH ``eth_getBlockByNumber``
    echo behaviour.
    """
    return {
        "block_id": {
            "hash": "ABABABABABABABABABABABABABABABABABABABABABABABABABABABABABABABAB",
            "part_set_header": {
                "total": 1,
                "hash": "CDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCDCD",
            },
        },
        "block": {
            "header": {
                "version": {"block": "11", "app": "0"},
                "chain_id": "lava-sim",
                "height": str(height),
                "time": "2024-01-01T00:00:00Z",
                "last_block_id": {"hash": "", "part_set_header": {"total": 0, "hash": ""}},
                "last_commit_hash": "",
                "data_hash": "",
                "validators_hash": "",
                "next_validators_hash": "",
                "consensus_hash": "",
                "app_hash": "",
                "last_results_hash": "",
                "evidence_hash": "",
                "proposer_address": "",
            },
            "data": {"txs": []},
            "evidence": {"evidence": []},
            "last_commit": {
                "height": str(max(height - 1, 0)),
                "round": 0,
                "block_id": {"hash": "", "part_set_header": {"total": 0, "hash": ""}},
                "signatures": [],
            },
        },
    }


def _node_info_response() -> Dict[str, Any]:
    """Minimal valid Cosmos ``/node_info`` body."""
    return {
        "default_node_info": {
            "protocol_version": {"p2p": "8", "block": "11", "app": "0"},
            "default_node_id": "sim-node-0000",
            "listen_addr": "tcp://0.0.0.0:26656",
            "network": "lava-sim",
            "version": "0.38.0",
            "channels": "40202122233038606100",
            "moniker": "lava-sim-provider",
            "other": {
                "tx_index": "on",
                "rpc_address": "tcp://0.0.0.0:26657",
            },
        },
        "application_version": {
            "name": "lava",
            "app_name": "lavad",
            "version": "5.0.0",
            "git_commit": "0000000000000000000000000000000000000000",
            "build_tags": "",
            "go_version": "1.21",
            "cosmos_sdk_version": "0.50.7",
        },
    }


def _balances_response(address: str = "lava-sim-address") -> Dict[str, Any]:
    """Minimal valid Cosmos ``/balances/{address}`` body.

    Single ulava balance — enough for the router classifier to confirm it
    decoded the list shape. ``handlers_rest.handle`` echoes the requested
    address in the response so tests can assert the path param round-tripped.
    """
    return {
        "balances": [
            {"denom": "ulava", "amount": "1000000"},
        ],
        "pagination": {
            "next_key": None,
            "total": "1",
        },
    }


def _validators_response() -> Dict[str, Any]:
    """Minimal valid Cosmos ``/validators`` body — single active bonded validator."""
    return {
        "validators": [
            {
                "operator_address": "lavavaloper1simulator00000000000000000000000000",
                "consensus_pubkey": {
                    "@type": "/cosmos.crypto.ed25519.PubKey",
                    "key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                },
                "jailed": False,
                "status": "BOND_STATUS_BONDED",
                "tokens": "1000000",
                "delegator_shares": "1000000.000000000000000000",
                "description": {
                    "moniker": "lava-sim-validator",
                    "identity": "",
                    "website": "",
                    "security_contact": "",
                    "details": "",
                },
                "unbonding_height": "0",
                "unbonding_time": "1970-01-01T00:00:00Z",
                "commission": {
                    "commission_rates": {
                        "rate": "0.100000000000000000",
                        "max_rate": "0.200000000000000000",
                        "max_change_rate": "0.010000000000000000",
                    },
                    "update_time": "1970-01-01T00:00:00Z",
                },
                "min_self_delegation": "1",
            }
        ],
        "pagination": {
            "next_key": None,
            "total": "1",
        },
    }


def _simulate_response() -> Dict[str, Any]:
    """POST /cosmos/tx/v1beta1/simulate — the gas estimate a real Cosmos REST
    node answers with. ``gas_info`` carries the wanted/used pair a caller reads
    to price the transaction; ``result`` is the (empty here) execution echo the
    cosmos-sdk simulate endpoint returns alongside it."""
    return {
        "gas_info": {
            "gas_wanted": "200000",
            "gas_used": "85432",
        },
        "result": {
            "data": "",
            "log": "",
            "events": [],
            "msg_responses": [],
        },
    }


# ── Method (path) defaults ────────────────────────────────────────────────────
#
# Keyed by (verb, template_str). The template carries ``{var}`` placeholders;
# ``handlers_rest`` matches a live URL against the compiled regex and passes
# path params as a dict.
#
# The GET entries are the original v1 catalogue. Write verbs joined later:
# real Cosmos REST nodes accept POST on paths like /cosmos/tx/v1beta1/simulate,
# and the GET-only catalogue made the simulator answer 404 where a real node
# answers 200 — so POST-path router tests could never run against the sim.
#
# This table is also the route table: the listener compiles its routes from
# these keys, so a verb/path absent here matches no route, gets no template,
# and answers 404. A per-(verb, template) ``responses`` override cannot reach
# it either — ``RestListener.method_key`` returns None without a template, so
# only the catch-all ``responses["default"]`` applies to an unknown path.
# Serving a new path therefore means adding it here, not overriding it per
# test.

REST_METHOD_DEFAULTS: Dict[Tuple[str, str], Any] = {
    ("GET", "/cosmos/base/tendermint/v1beta1/blocks/latest"): _block_response(REST_LATEST_HEIGHT),
    ("GET", "/cosmos/base/tendermint/v1beta1/blocks/{height}"): _block_response(REST_LATEST_HEIGHT),
    ("GET", "/cosmos/base/tendermint/v1beta1/node_info"): _node_info_response(),
    ("GET", "/cosmos/bank/v1beta1/balances/{address}"): _balances_response("lava-sim-address"),
    ("GET", "/cosmos/staking/v1beta1/validators"): _validators_response(),
    ("POST", "/cosmos/tx/v1beta1/simulate"): _simulate_response(),
}


def get_default(verb: str, template: str) -> Any:
    """Return a deep-copy of the stub for ``(verb, template)``.

    deepcopy guards the shared dict against per-request mutation (height echo,
    address echo, blocks_behind shifts in ``handlers_rest``).
    """
    return deepcopy(REST_METHOD_DEFAULTS[(verb, template)])


# ── Cosmos REST error stubs ───────────────────────────────────────────────────
#
# REST_ERROR_STUBS mirrors stubs.py::ERROR_STUBS (ETH) and
# stubs_btc.py::BTC_ERROR_STUBS but uses the Cosmos SDK's grpc-gateway error
# body: {"code": <grpc code int>, "message": <str>, "details": []}. Stubs are
# the *inner* error object; ``handlers_rest.handle`` wraps them as
# ``{"error": <stub>}`` at emission time — the same body shape the raw
# ``{"error": {...}}`` override path emits.
#
# Usage via the per-(verb, template) override path:
#     POST /scenario {"providers": {"1": {"chain_family": "rest",
#                                          "responses": [[
#                                              ["GET", "/cosmos/staking/v1beta1/validators"],
#                                              {"error_stub": "not_found", "status": 404}]]}}}

REST_ERROR_STUBS: Dict[str, Dict[str, Any]] = {
    # Resource not found — grpc-gateway NotFound (code 5). Emitted when a
    # query targets a key / height / address the node doesn't have.
    "not_found": {
        "code": 5,
        "message": "rpc error: code = NotFound desc = not found",
        "details": [],
    },
    # Internal server error — grpc-gateway Internal (code 13). The generic
    # "something broke node-side" shape.
    "internal": {
        "code": 13,
        "message": "rpc error: code = Internal desc = internal error",
        "details": [],
    },
}
