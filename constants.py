"""
constants.py — All project-wide constants, grouped by logical area.

Import from here instead of defining magic values inline.
"""

import os

# ── Server — network / ports ──────────────────────────────────────────────────
# JSON-RPC provider servers. Primaries serve normal traffic; backups are
# consumed by the smart-router only after the primary pool is exhausted
# (PairingListEmptyError → backup fallback in consumer_session_manager.go).
# The simulator process is identical across both pools — tier is a router-side
# concept, not a simulator-side one. The split here keeps intent visible at
# import sites; ALL_PROVIDER_PORTS is the union used by server bootstrap.
# Backups live at 18560-18562 (not contiguous with primaries) because
# 18548-18559 are claimed by gRPC / REST / Tendermint / WebSocket surfaces.
#
# Backup-tier coverage is JSON-RPC-only for now. Tentative follow-up
# allocation (contiguous block above this range):
#   gRPC 18563-18565 / REST 18566-18568 / TM 18569-18571 / WS 18572-18574
#
# BTC and LN JSON-RPC pools sit at 18575-18577 (BTC) and 18578-18580 (LN).
# They each get their own dedicated listeners — handler **dispatch** is
# port-derived, NOT per-provider via ``chain_family``. The ETH JSON-RPC pool
# (18545-18547) handles ETH only. Content fault primitives (error /
# rate_limit / hang / drop_connection / corruption) are still gated per
# listener on the snap's ``chain_family`` matching the listener's own
# ``handler_chain_family``; ``mode="down"`` is the universal exception
# (MAG-2092) and fires on every listener regardless of ``chain_family``.
# See BTC_PRIMARY_PORTS / LN_PRIMARY_PORTS below for the full allocation.
PROVIDER_PORTS        = {"1": 18545, "2": 18546, "3": 18547}
BACKUP_PROVIDER_PORTS = {"4": 18560, "5": 18561, "6": 18562}
ALL_PROVIDER_PORTS    = {**PROVIDER_PORTS, **BACKUP_PROVIDER_PORTS}

# Bitcoin JSON-RPC primary pool (MAG-2089). Each listener routes the
# success branch unconditionally through ``handlers_btc.handle(...)`` —
# success-path dispatch is port-derived, with no ``chain_family`` check.
# Content fault primitives (error / rate_limit / hang / drop_connection
# / corruption) are still chain_family-gated and fire only when the
# snap's ``chain_family == "btc"``. ``mode="down"`` is the universal
# exception (MAG-2092) and fires on every listener regardless of
# ``chain_family``. The 18575-18577 range was picked so it sits above
# the existing JSON-RPC backup block (18560-18562) and the non-JSON-RPC
# surface ranges, and so it doesn't collide with any documented router
# service / chart values. Primary tier only — backup tier (if/when
# needed) would extend contiguously upward.
BTC_PRIMARY_PORTS = {"1": 18575, "2": 18576, "3": 18577}

# Lightning Network JSON-RPC primary pool (MAG-2089). Each listener routes
# the success branch unconditionally through ``handlers_lnd.handle(...)``
# — success-path dispatch is port-derived, with no ``chain_family``
# check. Content fault primitives are still chain_family-gated and fire
# only when the snap's ``chain_family == "ln"``; ``mode="down"`` is the
# universal exception (MAG-2092) and fires on every listener regardless
# of ``chain_family``. Pinned to 18578-18580 immediately above the BTC
# range so the two new JSON-RPC chain pools sit contiguously. Primary
# tier only. No router routes traffic to LN today (MAG-1726 is the
# upstream tracking ticket); allocating dedicated ports here keeps the
# port-allocation pattern symmetric for when the router-side wire-up
# lands.
LN_PRIMARY_PORTS  = {"1": 18578, "2": 18579, "3": 18580}

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
# above the MAG-1841 TM range; 18560-18562 above WS is the backup-tier
# JSON-RPC range (BACKUP_PROVIDER_PORTS, above).
WS_PORTS = {"1": 18557, "2": 18558, "3": 18559}

# ── Backup-tier listeners for the non-JSON-RPC surfaces (MAG-18xx) ────────────
# Same shape contract as BACKUP_PROVIDER_PORTS above — the surface handler is
# identical to its primary, only the router-side `is_backup: true` flag (in
# values_sim.yml) and the dedicated port range differ. The simulator process
# treats every pool the same; tier semantics live in the smart-router.
#
# Provider-id scheme: continue the global integer numbering established by the
# JSON-RPC backup (pids 4-6). Each surface gets its own three contiguous pids
# so a single /scenario POST can target backup providers per-surface without
# accidentally configuring the matching primary or another surface's backup.
# A pid is never shared between two pools; the control API keys by pid string
# globally so distinct pids = distinct ProviderState instances.
#
# Port allocation is a single contiguous block 18563-18574 stacked above the
# JSON-RPC backup at 18560-18562:
#
#   gRPC          : pids  7- 9 → 18563-18565
#   REST          : pids 10-12 → 18566-18568
#   Tendermint-RPC: pids 13-15 → 18569-18571
#   WebSocket     : pids 16-18 → 18572-18574
GRPC_BACKUP_PORTS = {"7":  18563, "8":  18564, "9":  18565}
REST_BACKUP_PORTS = {"10": 18566, "11": 18567, "12": 18568}
TM_BACKUP_PORTS   = {"13": 18569, "14": 18570, "15": 18571}
WS_BACKUP_PORTS   = {"16": 18572, "17": 18573, "18": 18574}

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
#   SIM_HISTORY_MAX=500 python -u server.py     # smaller for memory-constrained dev pods
#   SIM_HISTORY_MAX=5000 python -u server.py    # larger for very long soak tests
#
# Memory is bounded — each entry is a small dict (~250 bytes); 2000 × 3
# providers ≈ 1.5 MB resident, well within the simulator's footprint budget.
HISTORY_MAX = int(os.getenv("SIM_HISTORY_MAX", "2000"))


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


# ── Lightning Network (LND) — chain identity + stub primitives (MAG-1726) ─────
# LN dispatches over its own dedicated JSON-RPC listener pool at
# ``LN_PRIMARY_PORTS`` (18578-18580); handler dispatch is port-derived.
# The LN method-name namespace (``getinfo``, ``listchannels``, ``openchannel``,
# ``decodepayreq``, ``payinvoice``, ``listpeers``) doesn't overlap with ETH
# or BTC, but the dedicated listener pool means we no longer rely on a
# per-provider ``chain_family`` flag to pick the LN handler on JSON-RPC.
# The ``chain_family`` field is still attached to ``/scenario`` payloads
# so REST / gRPC / TM / WS fault-primitive gating keeps working — it just
# stops being load-bearing for BTC / LN JSON-RPC handler selection (MAG-2089).
LN_NETWORK            = "regtest"   # LND's network field — "mainnet" | "testnet" | "regtest" | "signet"
LN_IDENTITY_PUBKEY    = "02" + "ab" * 32      # 33-byte secp256k1 compressed pubkey: 0x02-prefix + 32-byte X coord
LN_PEER_PUBKEY        = "02" + "cd" * 32      # distinct peer pubkey for listpeers / openchannel responses
LN_BLOCK_HEIGHT       = 850_000               # LN nodes track the underlying BTC chain head
LN_NUM_PEERS          = 1
LN_NUM_ACTIVE_CHANNELS = 1
LN_CHAN_POINT          = f"{'ab' * 32}:0"     # funding_txid_str:funding_output_index — LND's channel identifier
LN_PAYMENT_HASH        = "ef" * 32            # 32-byte payment hash, hex-encoded (no 0x prefix)
LN_PAYMENT_PREIMAGE    = "12" * 32            # 32-byte preimage; LND returns this on successful payinvoice
# bech32 invoice prefix is hrp + version, signed and tag-encoded. The stub doesn't
# need to be cryptographically valid — tests assert on shape, not signature.
LN_BOLT11_INVOICE      = "lnbcrt1u1psim000000000000000000000000000000000000000000000000000000000000000"

