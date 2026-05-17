"""
stubs_lnd.py — Default JSON-RPC response stubs for Lightning Network (LND) methods.

Companion to ``stubs_btc.py`` (BTC L1) and ``stubs.py`` (ETH). Same shape,
different protocol: ``LND_METHOD_DEFAULTS`` is a method-name → response-result
map; the JSON-RPC envelope ``{"jsonrpc": "2.0", "id": <id>, "result": <stub>}``
is wrapped around the stub by ``handlers_lnd.handle``.

Method scope (MAG-1726)
-----------------------
6 happy-path methods covering the most-exercised LND surface (``getinfo``,
``listchannels``, ``openchannel``, ``decodepayreq``, ``payinvoice``,
``listpeers``). Real payment-channel state simulation, gossip layer
(``channel_announcement`` / ``node_announcement``), and onion routing are
explicitly out of scope — every stub here is a canned response, not a
simulation of LN protocol semantics. Tests that need richer state should
override per-method via ``responses[method] = {"result": ...}``.

Why a single ``handlers_lnd`` module covers LND only (no c-lightning / eclair):
the three implementations expose roughly the same JSON-RPC surface for the
methods in scope, so a single canned response set per method is enough until
a router-side classifier reads implementation-divergent fields. If lnd vs
c-lightning behaviour ever needs to diverge here, factor out a per-impl module
the same way handlers_btc / handlers_eth are factored.

Fixture-shape policy per method
-------------------------------
Same "minimal-valid by default" rule as ``stubs_btc.py``: every stub is the
smallest response the LND docs declare. We do NOT lift real fixtures from a
live node unless the smart router's classifier reads more than a handful of
fields.

| Method        | Shape choice         | Why                                                                            |
|---------------|----------------------|--------------------------------------------------------------------------------|
| getinfo       | dict, 8 fields       | identity_pubkey + sync flags + height — what any LN dashboard / health probe reads. |
| listchannels  | dict {channels:list} | LND wraps the channel list in a top-level ``channels`` key; one stub channel.  |
| openchannel   | dict, 1 field        | Just ``funding_txid_str`` — the channel-point handle the caller uses next.     |
| decodepayreq  | dict, 4 fields       | destination + payment_hash + num_msat + expiry — minimum tag-decode shape.    |
| payinvoice    | dict, 2 fields       | payment_preimage + payment_hash on success; ``payment_error`` is empty.       |
| listpeers     | dict {peers:list}    | LND wraps the peer list in a top-level ``peers`` key; one stub peer.          |

Adding a new method
-------------------
1. Add an entry to ``LND_METHOD_DEFAULTS`` below.
2. Note its shape in the table above so future readers know the policy.
3. If the method needs request-time logic (echo a param, shift by
   ``blocks_behind``), implement it in ``handlers_lnd.handle``.
"""

from typing import Any, Dict

from constants import (
    LN_BLOCK_HEIGHT,
    LN_BOLT11_INVOICE,
    LN_CHAN_POINT,
    LN_IDENTITY_PUBKEY,
    LN_NETWORK,
    LN_NUM_ACTIVE_CHANNELS,
    LN_NUM_PEERS,
    LN_PAYMENT_HASH,
    LN_PAYMENT_PREIMAGE,
    LN_PEER_PUBKEY,
)


# ── Method defaults ───────────────────────────────────────────────────────────

LND_METHOD_DEFAULTS: Dict[str, Any] = {

    # ── Node info ─────────────────────────────────────────────────────────────
    # Mirrors `lncli getinfo`. block_height is shifted by handlers_lnd when
    # blocks_behind != 0 (the LN node tracks the underlying BTC chain head).
    "getinfo": {
        "identity_pubkey":      LN_IDENTITY_PUBKEY,
        "alias":                "sim-lnd",
        "num_peers":            LN_NUM_PEERS,
        "num_active_channels":  LN_NUM_ACTIVE_CHANNELS,
        "block_height":         LN_BLOCK_HEIGHT,
        "synced_to_chain":      True,
        "synced_to_graph":      True,
        "chains": [
            {"chain": "bitcoin", "network": LN_NETWORK},
        ],
    },

    # ── Channel queries ───────────────────────────────────────────────────────
    # LND wraps the list in {"channels": [...]} — preserved here so callers
    # that walk body["result"]["channels"] don't need a wrapper-mode flag.
    "listchannels": {
        "channels": [
            {
                "active":            True,
                "remote_pubkey":     LN_PEER_PUBKEY,
                "channel_point":     LN_CHAN_POINT,
                "chan_id":           "0",
                "capacity":          "1000000",          # satoshis as string — LND convention
                "local_balance":     "500000",
                "remote_balance":    "500000",
                "private":           False,
            }
        ]
    },

    # ── Channel open ──────────────────────────────────────────────────────────
    # `openchannel` returns the funding tx handle. Real LND emits a streaming
    # response with `chan_pending` → `chan_open` updates; the sim collapses to
    # a single final-state dict so /scenario consumers see one response.
    "openchannel": {
        "funding_txid_str":    LN_CHAN_POINT.split(":")[0],
        "output_index":        0,
    },

    # ── Invoice decode ────────────────────────────────────────────────────────
    # Echoes back the destination + payment hash for the caller's invoice. The
    # default is at-rest; handlers_lnd echoes back the requested invoice string
    # when present so tests can pin against round-trip behaviour.
    "decodepayreq": {
        "destination":     LN_IDENTITY_PUBKEY,
        "payment_hash":    LN_PAYMENT_HASH,
        "num_msat":        "100000",                # 100 sats in millisats
        "expiry":          "3600",
    },

    # ── Pay ───────────────────────────────────────────────────────────────────
    # On success: preimage + hash; payment_error empty string. To simulate a
    # payment failure, override via responses[payinvoice] = {"result": {
    # "payment_preimage": "", "payment_error": "no_route"}} — the simulator
    # doesn't model routing, so a "successful" payment is always returned by
    # default.
    "payinvoice": {
        "payment_preimage":   LN_PAYMENT_PREIMAGE,
        "payment_hash":       LN_PAYMENT_HASH,
        "payment_error":      "",
    },

    # ── Peer list ─────────────────────────────────────────────────────────────
    # LND wraps in {"peers": [...]} like listchannels. One stub peer is enough
    # for tests that assert presence/shape; richer scenarios override.
    "listpeers": {
        "peers": [
            {
                "pub_key":   LN_PEER_PUBKEY,
                "address":   "127.0.0.1:9735",
                "inbound":   False,
                "sat_sent":  "0",
                "sat_recv":  "0",
            }
        ]
    },
}


# ── Lightning error stubs ─────────────────────────────────────────────────────
#
# LND_ERROR_STUBS mirrors ``stubs_btc.py::BTC_ERROR_STUBS`` and ``stubs.py::ERROR_STUBS``.
# LND surfaces gRPC status codes natively, but the JSON-RPC shim it ships with
# (used by lncli's REST proxy in test deployments) wraps them in the standard
# JSON-RPC error envelope. The codes here are the JSON-RPC numerics — tests
# that want gRPC status semantics should override via the raw escape hatch.
#
# Usage via the per-method override path:
#     POST /scenario {"providers": {"1": {"chain_family": "ln",
#                                          "responses": {"payinvoice":
#                                              {"error_stub": "no_route"}}}}}

LND_ERROR_STUBS: Dict[str, Dict[str, Any]] = {

    # No route — LND returns this when the routing graph can't find a path
    # to the destination. Common in invoice-pay flows where the peer is offline
    # or capacity is insufficient.
    "no_route": {
        "code":    -32000,
        "message": "unable to find a path to destination",
    },

    # Invalid invoice — bolt11 decode failed (bad bech32, missing fields,
    # signature mismatch). Tests for decodepayreq error paths use this stub.
    "invalid_invoice": {
        "code":    -32602,
        "message": "invalid bolt11 invoice",
    },

    # Channel not found — listchannels / openchannel error when the channel
    # point doesn't exist on this node.
    "channel_not_found": {
        "code":    -32000,
        "message": "channel not found",
    },

    # Peer not connected — openchannel rejects when the remote pubkey isn't in
    # the peer list. Real LND emits a gRPC FailedPrecondition; we map to the
    # JSON-RPC -32000 server-error range so the router's classifier sees a
    # canonical "node refused" signal.
    "peer_not_connected": {
        "code":    -32000,
        "message": "peer is not connected",
    },

    # Insufficient funds — openchannel / payinvoice failure when the local
    # balance is below the requested amount + fees.
    "insufficient_funds": {
        "code":    -32000,
        "message": "insufficient local balance to open channel",
    },

    # Method not found — same JSON-RPC standard as ETH/BTC.
    "method_not_found": {
        "code":    -32601,
        "message": "Method not found",
    },

    # JSON-RPC parse error — same as ETH/BTC.
    "parse_error": {
        "code":    -32700,
        "message": "Parse error",
    },
}


# Re-export the canonical invoice string so test files have one import path
# for "the invoice the sim hands back" without having to thread constants
# through. Mirrors how ``btc_block_hash`` is exported from stubs_btc.
LND_BOLT11_INVOICE = LN_BOLT11_INVOICE
