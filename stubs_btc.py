"""
stubs_btc.py — Default JSON-RPC response stubs for Bitcoin Core RPC methods.

Companion to ``stubs.py`` (Ethereum). Same shape, different chain:
``BTC_METHOD_DEFAULTS`` is a method-name → response-result map; the JSON-RPC
envelope ``{"jsonrpc": "2.0", "id": <id>, "result": <stub>}`` is wrapped
around the stub by ``handlers_btc.handle``.

Method scope
------------
All 29 JSON-RPC methods from ``cache/specs/btc__BTC.json`` (the canonical LAVA
chain spec — ``proposal.specs[0].api_collections[0].apis[*].name``). Lightning
Network and wallet methods are intentionally out of scope (filed as
MAG-1726 / MAG-1727).

Fixture-shape policy per method (Q7 from the MAG-1716 implementability review)
-------------------------------------------------------------------------------
"Minimal-valid by default" — every stub is the smallest response the bitcoind
docs declare. We do NOT lift real fixtures from a live node unless the smart
router's classifier reads more than a handful of fields. The choice for each
method is documented below; switch to a real bitcoind capture only if you can
point at a specific router-side reader that needs the extra field.

| Method                  | Shape choice          | Why                                                       |
|-------------------------|-----------------------|-----------------------------------------------------------|
| getblockhash            | single hex string     | Bitcoin Core returns a single hash. Synthesised from height. |
| getblock                | dict, 8 fields        | Enough for the router's pruning verification + classifier. |
| decoderawtransaction    | dict, 5 fields        | tx skeleton — id/hash/version/vin/vout placeholders.      |
| decodescript            | dict, 3 fields        | asm/type minimal.                                         |
| estimatesmartfee        | dict, feerate + blocks | Two-field minimum that callers always read.              |
| getbestblockhash        | single hex string     | Mirrors getblockhash output convention.                   |
| getblockchaininfo       | dict, 10 fields       | Q4-resolved list: chain, blocks, headers, bestblockhash, difficulty, mediantime, verificationprogress, initialblockdownload, pruned, chainwork. |
| getblockcount           | int (decimal!)        | Critical: must be JSON number, not hex string.            |
| getblockheader          | dict, 7 fields        | hash/confirmations/height/version/time/nonce/bits.        |
| getblockstats           | dict, 4 fields        | height + 3 stats; full bitcoind shape has 30+ fields.     |
| getchaintips            | list of 1 dict        | Active tip only; reorg branches synthesised on demand.    |
| getchaintxstats         | dict, 3 fields        | window_block_count + txcount + window_tx_count.           |
| getconnectioncount      | int                   | Single scalar.                                            |
| getdifficulty           | float                 | Single scalar (real bitcoind returns a float).            |
| getindexinfo            | dict, 1 inner stub    | txindex placeholder; callers commonly poll just the key.  |
| getmemoryinfo           | dict, 1 field         | Inner "locked" dict — minimal.                            |
| getmempoolancestors     | empty list            | No ancestors by default.                                  |
| getmempooldescendants   | empty list            | No descendants by default.                                |
| getmempoolinfo          | dict, 5 fields        | size/bytes/usage/maxmempool/mempoolminfee.                |
| getrawmempool           | empty list            | No pending txs by default.                                |
| getrawtransaction       | hex-string serialised | bitcoind verbose=false returns hex-string; default form.  |
| gettxoutproof           | hex string            | Merkle proof bytes; empty hex is valid for stub.          |
| gettxoutsetinfo         | dict, 5 fields        | height/bestblock/transactions/txouts/total_amount.        |
| gettxout                | dict, 4 fields        | bestblock/confirmations/value/scriptPubKey.               |
| sendrawtransaction      | tx hash               | Single string return — mirrors bitcoind.                  |
| submitpackage           | dict, 2 fields        | package_msg + tx-results list.                            |
| testmempoolaccept       | list of 1 result      | One element per submitted tx; allowed=true default.       |
| validateaddress         | dict, 2 fields        | isvalid + address echo.                                   |
| verifymessage           | bool                  | Single scalar.                                            |

Adding a new method
-------------------
1. Add an entry to ``BTC_METHOD_DEFAULTS`` below.
2. Note its shape in the table above so future readers know the policy.
3. If the method needs request-time logic (echo a param, shift by
   ``blocks_behind``), implement it in ``handlers_btc.handle``.
"""

from typing import Any, Dict

from constants import (
    BTC_BLOCK_HASH,
    BTC_CHAIN,
    BTC_LATEST_BLOCK,
    BTC_TX_HASH,
)


# ── Block-hash helper ─────────────────────────────────────────────────────────

def btc_block_hash(height: int) -> str:
    """Return a deterministic synthetic Bitcoin block hash for a given height.

    Format: 64 lower-hex chars, NO "0x" prefix (bitcoind convention). Tests
    that pin against a specific height can call this helper directly to build
    the expected hash; ``handlers_btc.handle`` uses it to echo back the hash
    for ``getblockhash(height)`` calls.
    """
    return f"{height:064x}"


# ── Object factories ──────────────────────────────────────────────────────────

def block_stub(height: int = BTC_LATEST_BLOCK) -> Dict[str, Any]:
    """Minimal valid getblock response.

    The ``hash`` and ``height`` fields are overridden at request time in
    ``handlers_btc.handle`` so tests can request any block by hash and see
    the echoed height (mirrors the ETH ``eth_getBlockByNumber`` echo).
    """
    return {
        "hash":          btc_block_hash(height),
        "confirmations": 1,
        "height":        height,
        "version":       0x20000000,
        "merkleroot":    "ab" * 32,
        "time":          1700000000,
        "nonce":         0,
        "previousblockhash": btc_block_hash(height - 1) if height > 0 else "0" * 64,
    }


def blockheader_stub(height: int = BTC_LATEST_BLOCK) -> Dict[str, Any]:
    """Minimal valid getblockheader response (subset of block_stub)."""
    return {
        "hash":          btc_block_hash(height),
        "confirmations": 1,
        "height":        height,
        "version":       0x20000000,
        "time":          1700000000,
        "nonce":         0,
        "bits":          "1d00ffff",
    }


# ── Method defaults ───────────────────────────────────────────────────────────

BTC_METHOD_DEFAULTS: Dict[str, Any] = {

    # ── Block / chain queries ─────────────────────────────────────────────────
    # NOTE: getblockcount and getblockhash are special — handlers_btc rewrites
    # the result from blocks_behind / params at request time. The values here
    # are the at-head defaults.
    "getblockcount":      BTC_LATEST_BLOCK,                # int, decimal — not hex
    "getblockhash":       btc_block_hash(BTC_LATEST_BLOCK),
    "getbestblockhash":   btc_block_hash(BTC_LATEST_BLOCK),
    "getblock":           block_stub(BTC_LATEST_BLOCK),
    "getblockheader":     blockheader_stub(BTC_LATEST_BLOCK),

    "getblockchaininfo": {
        "chain":                  BTC_CHAIN,
        "blocks":                 BTC_LATEST_BLOCK,
        "headers":                BTC_LATEST_BLOCK,
        "bestblockhash":          btc_block_hash(BTC_LATEST_BLOCK),
        "difficulty":             1.0,
        "mediantime":             1700000000,
        "verificationprogress":   1.0,
        "initialblockdownload":   False,
        "pruned":                 False,
        "chainwork":              "00" * 32,
    },

    "getblockstats": {
        "height":       BTC_LATEST_BLOCK,
        "blockhash":    btc_block_hash(BTC_LATEST_BLOCK),
        "txs":          1,
        "total_size":   285,
    },

    "getchaintips": [
        {
            "height":      BTC_LATEST_BLOCK,
            "hash":        btc_block_hash(BTC_LATEST_BLOCK),
            "branchlen":   0,
            "status":      "active",
        }
    ],

    "getchaintxstats": {
        "window_block_count":  144,
        "txcount":             1_000_000,
        "window_tx_count":     150_000,
    },

    "getdifficulty":         1.0,

    # ── Node / network info ───────────────────────────────────────────────────
    "getconnectioncount": 8,

    "getindexinfo": {
        "txindex": {
            "synced":      True,
            "best_block_height": BTC_LATEST_BLOCK,
        }
    },

    "getmemoryinfo": {
        "locked": {
            "used":          0,
            "free":          0,
            "total":         0,
            "locked":        0,
            "chunks_used":   0,
            "chunks_free":   0,
        }
    },

    # ── Mempool ───────────────────────────────────────────────────────────────
    "getmempoolancestors":   [],
    "getmempooldescendants": [],
    "getmempoolinfo": {
        "size":             0,
        "bytes":            0,
        "usage":            0,
        "maxmempool":       300_000_000,
        "mempoolminfee":    0.00001,
    },
    "getrawmempool":         [],

    # ── Transactions ──────────────────────────────────────────────────────────
    # getrawtransaction: default returns the hex-serialised tx (verbose=false).
    # Tests requesting verbose=true receive this string back too — switch via
    # /scenario responses[method] = {"result": {...}} if a richer shape is needed.
    "getrawtransaction":     "0100000001" + "00" * 32 + "00000000" + "00ffffffff",

    "gettxoutproof":         "",  # hex-encoded merkle proof; empty = no proof emitted

    "gettxoutsetinfo": {
        "height":         BTC_LATEST_BLOCK,
        "bestblock":      btc_block_hash(BTC_LATEST_BLOCK),
        "transactions":   1_000_000,
        "txouts":         2_000_000,
        "total_amount":   19_500_000.0,
    },

    "gettxout": {
        "bestblock":      btc_block_hash(BTC_LATEST_BLOCK),
        "confirmations":  1,
        "value":          0.0,
        "scriptPubKey": {
            "asm":     "",
            "hex":     "",
            "type":    "nonstandard",
        },
    },

    # ── Send / submit ─────────────────────────────────────────────────────────
    "sendrawtransaction":    BTC_TX_HASH,

    "submitpackage": {
        "package_msg":    "success",
        "tx-results":     [],
    },

    "testmempoolaccept": [
        {
            "txid":      BTC_TX_HASH,
            "allowed":   True,
        }
    ],

    # ── Decode helpers (deterministic) ────────────────────────────────────────
    "decoderawtransaction": {
        "txid":     BTC_TX_HASH,
        "hash":     BTC_TX_HASH,
        "version":  2,
        "vin":      [],
        "vout":     [],
    },

    "decodescript": {
        "asm":    "",
        "type":   "nonstandard",
        "p2sh":   "",
    },

    # ── Fees / addresses / signing ────────────────────────────────────────────
    "estimatesmartfee": {
        "feerate":  0.00001,
        "blocks":   6,
    },

    "validateaddress": {
        "isvalid":  True,
        "address":  "bc1qexampleaddress00000000000000000000000",
    },

    "verifymessage":  True,
}


# ── Bitcoin error stubs ───────────────────────────────────────────────────────
#
# BTC_ERROR_STUBS mirrors stubs.py::ERROR_STUBS (ETH) but uses bitcoind's error
# code convention (`src/rpc/protocol.h` in the Bitcoin Core source). Stubs are
# the *inner* error object; ``handlers_btc.handle`` wraps them in the JSON-RPC
# envelope at emission time.
#
# Usage via the per-method override path:
#     POST /scenario {"providers": {"1": {"chain_family": "btc",
#                                          "responses": {"getblockhash":
#                                              {"error_stub": "block_not_found"}}}}}

BTC_ERROR_STUBS: Dict[str, Dict[str, Any]] = {

    # Block / tx not found — bitcoind RPC_INVALID_ADDRESS_OR_KEY (-5).
    # Emitted when getblock / getblockhash / getrawtransaction is called for
    # a block hash or txid the node doesn't have.
    "block_not_found": {
        "code":    -5,
        "message": "Block not found",
    },
    "tx_not_found": {
        "code":    -5,
        "message": "No such mempool or blockchain transaction",
    },

    # Invalid parameter — RPC_INVALID_PARAMETER (-8). Used when args are
    # structurally valid JSON but semantically wrong (e.g. negative height).
    "invalid_parameter": {
        "code":    -8,
        "message": "Invalid parameter",
    },

    # Verify failure — RPC_VERIFY_REJECTED (-26). Emitted when
    # sendrawtransaction is rejected (insufficient fee, conflicts, etc.).
    "verify_rejected": {
        "code":    -26,
        "message": "Transaction was rejected",
    },

    # Verify already in chain — RPC_VERIFY_ALREADY_IN_CHAIN (-27). Emitted
    # when sendrawtransaction sees the tx is already mined.
    "already_in_chain": {
        "code":    -27,
        "message": "Transaction already in block chain",
    },

    # In warmup — RPC_IN_WARMUP (-28). Returned during initial node startup
    # before the RPC interface is ready.
    "in_warmup": {
        "code":    -28,
        "message": "Loading block index",
    },

    # JSON-RPC parse error — same as ETH (-32700).
    "parse_error": {
        "code":    -32700,
        "message": "Parse error",
    },

    # JSON-RPC invalid request — same as ETH (-32600).
    "invalid_request": {
        "code":    -32600,
        "message": "Invalid Request",
    },

    # Method not found — same as ETH (-32601). Bitcoin Core uses the same
    # JSON-RPC 2.0 code for missing methods.
    "method_not_found": {
        "code":    -32601,
        "message": "Method not found",
    },
}
