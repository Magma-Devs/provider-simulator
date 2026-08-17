"""
constants.py — What a simulated chain answers, plus two process-wide settings.

Import from here instead of defining magic values inline.

**Port numbers are not here.** They live in ``provider_simulator/topology.py``,
the table the server binds from. This file used to carry a parallel set of port
dicts whose keys were a second, older provider numbering; the keys disagreed
with the topology pids for six pools and nothing read them as identity. Ask
``topology.port_of(pool, pid, interface, transport)`` for a port.
"""

import os

# ── Control API (scenario config, reset, stats, history) ──────────────────────
# One fixed service port. No provider owns it, so it is not in the topology.
CONTROL_PORT = 19000


# ── Provider history — call-log ring-buffer ───────────────────────────────────
# Each provider keeps the last N calls in memory.
# When full, the oldest entry is dropped to make room for the newest.
# Affects /history responses. Does NOT affect /stats (all-time counters).
#
# Default raised to 2000 (MAG-1822) so long simulator scenarios — sustained
# retry storms, soak tests, cross-validation sweeps — don't silently roll the
# oldest entries off the buffer before a test can assert on them. The previous
# 200 cap left ~67 calls per provider headroom, which a single eth_blockNumber
# burst could exhaust. Override via env at pod startup:
#
#   SIM_HISTORY_MAX=500 python -u run.py     # smaller for memory-constrained dev pods
#   SIM_HISTORY_MAX=5000 python -u run.py    # larger for very long soak tests
#
# Memory is bounded — each entry is a small dict (~250 bytes); 2000 × 3
# providers ≈ 1.5 MB resident, well within the simulator's footprint budget.
HISTORY_MAX = int(os.getenv("SIM_HISTORY_MAX", "2000"))


# ── Ethereum — chain identity ─────────────────────────────────────────────────
ETH_CHAIN_ID = "0x1"  # Ethereum mainnet chain ID
ETH_LATEST_BLOCK = "0x1312D00"  # 20 000 000 — realistic mainnet block height used in stubs


# ── Ethereum — stub primitives (fake but correctly-formatted on-chain values) ──
# Used by stubs.py to build valid-shaped RPC responses without real chain data.

ETH_ZERO_ADDR = "0x" + "0" * 40  # zero address   — 20 bytes, e.g. 0x0000...0000
ETH_ZERO_HASH = "0x" + "0" * 64  # zero hash       — 32 bytes, e.g. 0x0000...0000
ETH_BLOCK_HASH = "0xaaaa" + "a" * 60  # fake block hash — 32 bytes, distinct from zero
ETH_TX_HASH = "0xbbbb" + "b" * 60  # fake tx hash    — 32 bytes, distinct from block hash
ETH_BLOOM = "0x" + "0" * 512  # empty logs bloom filter — 256 bytes


# ── Bitcoin — chain identity (MAG-1716) ───────────────────────────────────────
# Block height chosen as a realistic post-2024 mainnet number. Tests that
# assert exact equality on the default getblockcount response can pin against
# this value.
BTC_LATEST_BLOCK = 850_000  # decimal int; bitcoind returns block heights as JSON numbers, not hex
BTC_CHAIN = "main"  # one of bitcoind's chain names: "main" | "test" | "signet" | "regtest"


# ── Bitcoin — stub primitives ─────────────────────────────────────────────────
# bitcoind serialises block hashes and tx ids as 64-char lower-hex strings
# (no "0x" prefix, the chain identifier "00..." prefix is part of the value).
# We synthesise them deterministically from BTC_LATEST_BLOCK so tests can pin
# exact equality without lifting real blockchain data.

BTC_BLOCK_HASH = f"{BTC_LATEST_BLOCK:064x}"  # 64 lower-hex chars, no 0x prefix
BTC_TX_HASH = "ab" * 32  # 64 hex chars, distinct from block hash


# ── Tendermint RPC — chain identity + stub primitives (MAG-1841) ──────────────
# The sim returns ``lava-sim-tm`` as its network id so a router with the
# strict chain-id verification on rejects the sim (which is what
# ``skip_verifications: chain-id`` in values_sim.yml is for). Keeping the
# network name distinct from ``lava-mainnet-1`` prevents accidental cross-talk
# in tests that compare sim vs live envelopes.
TM_NETWORK_ID = "lava-sim-tm"
TM_LATEST_HEIGHT = 5_000_000  # decimal int; Tendermint serialises heights as string-ints

# CometBFT serialises hashes as upper-hex 64-char strings (no 0x prefix).
# Synthesised from the latest-height so tests can pin exact equality without
# pulling real chain bytes.
TM_BLOCK_HASH = f"{TM_LATEST_HEIGHT:064X}"
TM_APP_HASH = "AB" * 32  # 64-char hex, distinct from block hash
TM_VALIDATOR_ADDR = "C" * 40  # 40-char hex, validator addr shape
TM_PROPOSER_ADDR = "D" * 40  # 40-char hex, proposer addr shape


# ── Lightning Network (LND) — chain identity + stub primitives (MAG-1726) ─────
# LN dispatches over its own dedicated JSON-RPC listener pool (the ln-sim rows
# in topology.py); handler dispatch is port-derived.
# The LN method-name namespace (``getinfo``, ``listchannels``, ``openchannel``,
# ``decodepayreq``, ``payinvoice``, ``listpeers``) doesn't overlap with ETH
# or BTC, but the dedicated listener pool means we no longer rely on a
# per-provider ``chain_family`` flag to pick the LN handler on JSON-RPC.
# The ``chain_family`` field is still attached to ``/scenario`` payloads
# so REST / gRPC / TM / WS fault-primitive gating keeps working — it just
# stops being decisive for BTC / LN JSON-RPC handler selection (MAG-2089).
LN_NETWORK = "regtest"  # LND's network field — "mainnet" | "testnet" | "regtest" | "signet"
LN_IDENTITY_PUBKEY = "02" + "ab" * 32  # 33-byte secp256k1 compressed pubkey: 0x02-prefix + 32-byte X coord
LN_PEER_PUBKEY = "02" + "cd" * 32  # distinct peer pubkey for listpeers / openchannel responses
LN_BLOCK_HEIGHT = 850_000  # LN nodes track the underlying BTC chain head
LN_NUM_PEERS = 1
LN_NUM_ACTIVE_CHANNELS = 1
LN_CHAN_POINT = f"{'ab' * 32}:0"  # funding_txid_str:funding_output_index — LND's channel identifier
LN_PAYMENT_HASH = "ef" * 32  # 32-byte payment hash, hex-encoded (no 0x prefix)
LN_PAYMENT_PREIMAGE = "12" * 32  # 32-byte preimage; LND returns this on successful payinvoice
# bech32 invoice prefix is hrp + version, signed and tag-encoded. The stub doesn't
# need to be cryptographically valid — tests assert on shape, not signature.
LN_BOLT11_INVOICE = "lnbcrt1u1psim000000000000000000000000000000000000000000000000000000000000000"
