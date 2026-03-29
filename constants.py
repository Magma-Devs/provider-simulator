"""
constants.py — All project-wide constants, grouped by logical area.

Import from here instead of defining magic values inline.
"""

# ── Server — network / ports ──────────────────────────────────────────────────
# JSON-RPC provider servers (one per simulated provider)
PROVIDER_PORTS = {"1": 18545, "2": 18546, "3": 18547}

# Control API (scenario config, reset, stats, history)
CONTROL_PORT = 19000


# ── Provider history — call-log ring-buffer ───────────────────────────────────
# Each provider keeps the last N calls in memory.
# When full, the oldest entry is dropped to make room for the newest.
# Affects /history responses. Does NOT affect /stats (all-time counters).
HISTORY_MAX = 200


# ── Ethereum — chain identity ─────────────────────────────────────────────────
ETH_CHAIN_ID     = "0x1"        # Ethereum mainnet chain ID
ETH_LATEST_BLOCK = "0x1312D00"  # 20 000 000 — realistic mainnet block height used in stubs


# ── Ethereum — stub primitives (fake but correctly-formatted on-chain values) ──
# Used by stubs.py to build valid-shaped RPC responses without real chain data.

ETH_ZERO_ADDR  = "0x" + "0" * 40    # zero address   — 20 bytes, e.g. 0x0000...0000
ETH_ZERO_HASH  = "0x" + "0" * 64    # zero hash       — 32 bytes, e.g. 0x0000...0000
ETH_BLOCK_HASH = "0xaaaa" + "a" * 60  # fake block hash — 32 bytes, distinct from zero
ETH_TX_HASH    = "0xbbbb" + "b" * 60  # fake tx hash    — 32 bytes, distinct from block hash
ETH_BLOOM      = "0x" + "0" * 512     # empty logs bloom filter — 256 bytes

