"""
constants.py — All project-wide constants, grouped by logical area.

Import from here instead of defining magic values inline.
"""

# ── Server — network / ports ──────────────────────────────────────────────────
# JSON-RPC provider servers (one per simulated provider)
PROVIDER_PORTS = {"1": 18545, "2": 18546, "3": 18547}

# Control API (scenario config, reset, stats, history)
CONTROL_PORT = 19000

# gRPC provider servers (MAG-1780). One per simulated provider, sharing the
# same ProviderState dict the JSON-RPC servers use — a /scenario call with
# chain_family="grpc" reconfigures the matching gRPC servicer just like an
# eth/btc payload reconfigures the JSON-RPC handler. REST sim (MAG-1777)
# takes 18551 / 18552 / 18553 so the gRPC range stays compact below it.
GRPC_PROVIDER_PORTS = {"1": 18548, "2": 18549, "3": 18550}

# REST provider servers (MAG-1777). One per simulated provider, sharing the
# same ProviderState dict the JSON-RPC servers use — a /scenario call with
# chain_family="rest" reconfigures the matching REST handler just like an
# eth/btc payload reconfigures the JSON-RPC handler. Pinned to 18551-18553
# leaving 18548-18550 reserved for MAG-1780's gRPC sims.
REST_PORTS = {"1": 18551, "2": 18552, "3": 18553}

# Tendermint-RPC (CometBFT) provider servers (MAG-1841). One per simulated
# provider, sharing the same ProviderState dict the other handlers use — a
# /scenario call with chain_family="tendermintrpc" reconfigures the matching
# TendermintHandler. Pinned to 18554-18556 leaving the earlier ranges intact
# (ETH 18545-7, gRPC 18548-50, REST 18551-3).
TM_PORTS = {"1": 18554, "2": 18555, "3": 18556}

# WebSocket provider servers (MAG-1801). One per simulated provider, sharing
# the same ProviderState dict that backs the JSON-RPC / REST / gRPC / TM
# handlers. A /scenario call with chain_family="ws" reconfigures the matching
# WS handler (latency, fault primitives, corruption). Pinned to 18557-18559
# above the MAG-1841 TM range so port allocations stay contiguous.
WS_PORTS = {"1": 18557, "2": 18558, "3": 18559}

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


# ── Bitcoin — chain identity (MAG-1716) ───────────────────────────────────────
# Block height chosen as a realistic post-2024 mainnet number. Tests that
# assert exact equality on the default getblockcount response can pin against
# this value.
BTC_LATEST_BLOCK = 850_000            # decimal int; bitcoind returns block heights as JSON numbers, not hex
BTC_CHAIN        = "main"             # one of bitcoind's chain names: "main" | "test" | "signet" | "regtest"


# ── Bitcoin — stub primitives ─────────────────────────────────────────────────
# bitcoind serialises block hashes and tx ids as 64-char lower-hex strings
# (no "0x" prefix, the chain identifier "00..." prefix is part of the value).
# We synthesise them deterministically from BTC_LATEST_BLOCK so tests can pin
# exact equality without lifting real blockchain data.

BTC_BLOCK_HASH = f"{BTC_LATEST_BLOCK:064x}"       # 64 lower-hex chars, no 0x prefix
BTC_TX_HASH    = "ab" * 32                         # 64 hex chars, distinct from block hash


# ── Tendermint RPC — chain identity + stub primitives (MAG-1841) ──────────────
# The sim returns ``lava-sim-tm`` as its network id so a router with the
# strict chain-id verification on rejects the sim (which is what
# ``skip_verifications: chain-id`` in values_sim.yml is for). Keeping the
# network name distinct from ``lava-mainnet-1`` prevents accidental cross-talk
# in tests that compare sim vs live envelopes.
TM_NETWORK_ID    = "lava-sim-tm"
TM_LATEST_HEIGHT = 5_000_000           # decimal int; Tendermint serialises heights as string-ints

# CometBFT serialises hashes as upper-hex 64-char strings (no 0x prefix).
# Synthesised from the latest-height so tests can pin exact equality without
# pulling real chain bytes.
TM_BLOCK_HASH    = f"{TM_LATEST_HEIGHT:064X}"
TM_APP_HASH      = "AB" * 32                          # 64-char hex, distinct from block hash
TM_VALIDATOR_ADDR = "C" * 40                          # 40-char hex, validator addr shape
TM_PROPOSER_ADDR = "D" * 40                           # 40-char hex, proposer addr shape

