"""
stubs_ws.py — WebSocket subscription method routing + event-frame envelopes.

Two flat tables drive the WS handler:

  SUBSCRIBE_METHODS    — JSON-RPC method name → which chain it belongs to and
                         what envelope wraps pushed events. Used by both the
                         reader loop (to recognise subscribe calls) and
                         POST /ws/emit (to build the correctly-shaped push
                         frame from a caller-supplied event payload).

  UNSUBSCRIBE_METHODS  — the matching unsubscribe method names.

  EVENT_DEFAULTS       — canned event payload templates per (chain, event_type).
                         Tests that don't supply a custom event in /ws/emit
                         fall back to these.

The handler imports both. No other modules should depend on stubs_ws.
"""

from typing import Any, Dict, Set

# Maps subscribe method name → {chain, envelope, notification_method}.
#
# envelope: how a pushed event is wrapped for delivery over the WS frame.
#   - "eth_subscription": {"jsonrpc":"2.0","method":"eth_subscription",
#                          "params":{"subscription":<sub_id>,"result":<event>}}
#   - "tendermint_event": {"jsonrpc":"2.0","id":<sub_id>,
#                          "result":{"query":<query>,"data":{"type":<t>,"value":<event>}}}
#   - "solana_account":   {"jsonrpc":"2.0","method":"accountNotification",
#                          "params":{"subscription":int(<sub_id>,16),"result":<event>}}
#   - "solana_logs":      {"jsonrpc":"2.0","method":"logsNotification",
#                          "params":{"subscription":int(<sub_id>,16),"result":<event>}}
SUBSCRIBE_METHODS: Dict[str, Dict[str, str]] = {
    "eth_subscribe": {"chain": "eth", "envelope": "eth_subscription"},
    "subscribe": {"chain": "tendermint", "envelope": "tendermint_event"},
    "accountSubscribe": {"chain": "solana", "envelope": "solana_account"},
    "logsSubscribe": {"chain": "solana", "envelope": "solana_logs"},
}


# Matching unsubscribe method names. Take a single sub_id param.
UNSUBSCRIBE_METHODS: Set[str] = {
    "eth_unsubscribe",
    "unsubscribe",
    "accountUnsubscribe",
    "logsUnsubscribe",
}


# Canned event payload templates per (chain, event_type). Used by /ws/emit
# when the caller doesn't supply a custom event payload, or by tests that
# want a known-good default shape.
EVENT_DEFAULTS: Dict[tuple, Dict[str, Any]] = {
    ("eth", "newHeads"): {
        "number": "0x1312D01",
        "hash": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "parentHash": "0x" + "00" * 32,
        "timestamp": "0x65000000",
    },
    ("tendermint", "NewBlock"): {
        "block": {
            "header": {"height": "850001", "chain_id": "cosmoshub-4"},
            "data": {"txs": []},
        },
    },
    ("solana", "accountChange"): {
        "context": {"slot": 250_000_001},
        "value": {
            "lamports": 1_000_000,
            "owner": "11111111111111111111111111111111",
            "data": ["", "base64"],
            "executable": False,
            "rentEpoch": 0,
        },
    },
    ("solana", "logsChange"): {
        "context": {"slot": 250_000_001},
        "value": {"signature": "5j7s..." + "x" * 80, "err": None, "logs": ["Program log: hello"]},
    },
}


def build_event_frame(envelope: str, sub_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a caller-supplied event payload in the chain-correct envelope.

    Called by POST /ws/emit and by tests that want to construct an event
    frame body without going through the network.
    """
    if envelope == "eth_subscription":
        return {
            "jsonrpc": "2.0",
            "method": "eth_subscription",
            "params": {"subscription": sub_id, "result": event},
        }
    if envelope == "tendermint_event":
        return {
            "jsonrpc": "2.0",
            "id": sub_id,
            "result": {
                "query": "tm.event='NewBlock'",
                "data": {"type": "tendermint/event/NewBlock", "value": event},
            },
        }
    if envelope == "solana_account":
        return {
            "jsonrpc": "2.0",
            "method": "accountNotification",
            "params": {"subscription": int(sub_id, 16), "result": event},
        }
    if envelope == "solana_logs":
        return {
            "jsonrpc": "2.0",
            "method": "logsNotification",
            "params": {"subscription": int(sub_id, 16), "result": event},
        }
    raise ValueError(f"unknown envelope: {envelope}")
