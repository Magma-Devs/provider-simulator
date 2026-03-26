"""
stubs.py — Default JSON-RPC response stubs for every supported method.

Usage
-----
from stubs import METHOD_DEFAULTS

# At runtime the router can override any method via POST /scenario:
#   {"providers": {"1": {"responses": {"eth_blockNumber": {"result": "0xff"}}}}}

Adding a new method
-------------------
1. Add a constant / factory call below.
2. Use the existing helpers (_block, _tx, _receipt, _trace_frame) for object types.
3. Simple scalar returns (hex string, bool, list) can be inlined directly.
"""

from typing import Any, Dict

# ── Shared constants ──────────────────────────────────────────────────────────

LATEST     = "0x1312D00"           # 20 000 000 — realistic ETH mainnet height
CHAIN_ID   = "0x1"                 # Ethereum mainnet
ZERO_ADDR  = "0x" + "0" * 40
ZERO_HASH  = "0x" + "0" * 64
BLK_HASH   = "0xaaaa" + "a" * 60  # fake but correctly-formatted block hash
TX_HASH    = "0xbbbb" + "b" * 60  # fake but correctly-formatted tx hash
BLOOM      = "0x" + "0" * 512


# ── Object factories ──────────────────────────────────────────────────────────

def block(number: str = LATEST) -> dict:
    """
    Minimal valid block object.

    The ``number`` field is intentionally overridden at request time in
    server.py so that ``eth_getBlockByNumber(["0x0", false])`` returns
    ``number: "0x0"`` — required by the router's pruning verification.
    """
    return {
        "number":           number,
        "hash":             BLK_HASH,
        "parentHash":       ZERO_HASH,
        "nonce":            "0x0000000000000000",
        "sha3Uncles":       "0x1dcc4de8dec75d7aab85b567b6ccd41ad312451b948a7413f0a142fd40d49347",
        "logsBloom":        BLOOM,
        "transactionsRoot": "0x56e81f171bcc55a6ff8345e692c0f86e5b48e01b996cadc001622fb5e363b421",
        "stateRoot":        "0xd7f8974fb5ac78d9ac099b9ad5018bedc2ce0a72dad1827a1709da30580f0544",
        "receiptsRoot":     "0x56e81f171bcc55a6ff8345e692c0f86e5b48e01b996cadc001622fb5e363b421",
        "miner":            ZERO_ADDR,
        "difficulty":       "0x0",
        "totalDifficulty":  "0x0",
        "extraData":        "0x",
        "size":             "0x1f4",
        "gasLimit":         "0x1c9c380",
        "gasUsed":          "0x0",
        "timestamp":        "0x65f3d4c0",
        "baseFeePerGas":    "0x3b9aca00",
        "withdrawalsRoot":  ZERO_HASH,
        "transactions":     [],
        "uncles":           [],
        "withdrawals":      [],
    }


def tx() -> dict:
    """Minimal valid transaction object."""
    return {
        "hash":                 TX_HASH,
        "nonce":                "0x0",
        "blockHash":            BLK_HASH,
        "blockNumber":          LATEST,
        "transactionIndex":     "0x0",
        "from":                 ZERO_ADDR,
        "to":                   ZERO_ADDR,
        "value":                "0x0",
        "gas":                  "0x5208",
        "gasPrice":             "0x3b9aca00",
        "maxFeePerGas":         "0x3b9aca00",
        "maxPriorityFeePerGas": "0x0",
        "input":                "0x",
        "v":                    "0x1",
        "r":                    ZERO_HASH,
        "s":                    ZERO_HASH,
        "type":                 "0x2",
        "accessList":           [],
        "chainId":              CHAIN_ID,
    }


def receipt() -> dict:
    """Minimal valid transaction receipt."""
    return {
        "transactionHash":   TX_HASH,
        "transactionIndex":  "0x0",
        "blockHash":         BLK_HASH,
        "blockNumber":       LATEST,
        "from":              ZERO_ADDR,
        "to":                ZERO_ADDR,
        "cumulativeGasUsed": "0x5208",
        "gasUsed":           "0x5208",
        "effectiveGasPrice": "0x3b9aca00",
        "contractAddress":   None,
        "logs":              [],
        "logsBloom":         BLOOM,
        "status":            "0x1",
        "type":              "0x2",
    }


def trace_frame() -> dict:
    """Minimal call trace frame (used by debug/trace methods)."""
    return {
        "type":    "CALL",
        "from":    ZERO_ADDR,
        "to":      ZERO_ADDR,
        "value":   "0x0",
        "gas":     "0x5208",
        "gasUsed": "0x0",
        "input":   "0x",
        "output":  "0x",
        "calls":   [],
    }


# ── Method defaults ───────────────────────────────────────────────────────────

METHOD_DEFAULTS: Dict[str, Any] = {

    # ── eth — chain state ─────────────────────────────────────────────────────
    "eth_blockNumber":              LATEST,
    "eth_chainId":                  CHAIN_ID,
    "eth_protocolVersion":          "0x41",
    "eth_syncing":                  False,
    "eth_coinbase":                 ZERO_ADDR,
    "eth_mining":                   False,
    "eth_hashrate":                 "0x0",
    "eth_accounts":                 [],

    # ── eth — gas / fees ──────────────────────────────────────────────────────
    "eth_gasPrice":                 "0x3b9aca00",   # 1 gwei
    "eth_maxPriorityFeePerGas":     "0x3b9aca00",
    "eth_feeHistory": {
        "oldestBlock":   "0x1312CF0",
        "baseFeePerGas": ["0x3b9aca00", "0x3b9aca00"],
        "gasUsedRatio":  [0.0],
        "reward":        [["0x0"]],
    },

    # ── eth — state queries ───────────────────────────────────────────────────
    "eth_getBalance":               "0x0",
    "eth_getCode":                  "0x",
    "eth_getStorageAt":             ZERO_HASH,
    "eth_getTransactionCount":      "0x0",
    "eth_call":                     "0x",
    "eth_estimateGas":              "0x5208",

    # ── eth — blocks ──────────────────────────────────────────────────────────
    # NOTE: "number" is overridden from params[0] at request time — see server.py
    "eth_getBlockByNumber":                     block(LATEST),
    "eth_getBlockByHash":                       block(LATEST),
    "eth_getBlockTransactionCountByNumber":     "0x0",
    "eth_getBlockTransactionCountByHash":       "0x0",
    "eth_getUncleCountByBlockNumber":           "0x0",
    "eth_getUncleCountByBlockHash":             "0x0",
    "eth_getUncleByBlockNumberAndIndex":        None,
    "eth_getUncleByBlockHashAndIndex":          None,

    # ── eth — transactions ────────────────────────────────────────────────────
    "eth_getTransactionByHash":                 tx(),
    "eth_getTransactionByBlockNumberAndIndex":  tx(),
    "eth_getTransactionByBlockHashAndIndex":    tx(),
    "eth_getTransactionReceipt":                receipt(),
    "eth_sendRawTransaction":                   TX_HASH,

    # ── eth — logs / filters ──────────────────────────────────────────────────
    "eth_getLogs":                      [],
    "eth_newFilter":                    "0x1",
    "eth_newBlockFilter":               "0x2",
    "eth_newPendingTransactionFilter":  "0x3",
    "eth_getFilterChanges":             [],
    "eth_getFilterLogs":                [],
    "eth_uninstallFilter":              True,

    # ── net ───────────────────────────────────────────────────────────────────
    "net_version":      "1",
    "net_listening":    True,
    "net_peerCount":    "0x10",

    # ── web3 ──────────────────────────────────────────────────────────────────
    "web3_clientVersion":   "simulator/v1.0.0",
    "web3_sha3":            "0x" + "c" * 64,

    # ── trace (addon: trace) ──────────────────────────────────────────────────
    "trace_block":                   [],
    "trace_transaction":             [trace_frame()],
    "trace_get":                     trace_frame(),
    "trace_call":                    {"output": "0x", "stateDiff": None, "trace": [trace_frame()], "vmTrace": None},
    "trace_callMany":                [],
    "trace_rawTransaction":          {"output": "0x", "stateDiff": None, "trace": [trace_frame()], "vmTrace": None},
    "trace_replayTransaction":       {"output": "0x", "stateDiff": None, "trace": [trace_frame()], "vmTrace": None},
    "trace_replayBlockTransactions": [],
    "trace_filter":                  [],

    # ── debug (addon: debug) ──────────────────────────────────────────────────
    "debug_traceTransaction":   trace_frame(),
    "debug_traceBlockByNumber": [],
    "debug_traceBlockByHash":   [],
    "debug_traceCall":          trace_frame(),
    "debug_getRawBlock":        "0x",
    "debug_getRawHeader":       "0x",
    "debug_getRawReceipts":     "0x",
    "debug_getRawTransaction":  "0x",
}

