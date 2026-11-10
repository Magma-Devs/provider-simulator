"""
stubs_solana.py — Default Solana stub constants for the provider simulator.

Companion to ``stubs.py`` (Ethereum), ``stubs_btc.py`` (Bitcoin),
``stubs_lnd.py`` (Lightning), ``stubs_rest.py`` (Cosmos REST), and
``stubs_tendermintrpc.py`` (CometBFT). Different shape from those files:
Solana's five stubbed methods (``getLatestBlockhash`` / ``getSlot`` /
``getBalance`` / ``getHealth`` / ``getVersion``) are computed per request in
``provider_simulator.chains.solana`` — the reported slot depends on the head of
the provider, the lag of the commitment that the request names, and the
per-provider ``slot_offset``, and the slot ↔ lastValidBlockHeight distance on
``slot_block_gap`` — so there is no static method-name → response map
to hold here. What lives here instead are the fixed chain values those
computed responses are built from.

Adding a new Solana method: implement its branch in ``SolanaChain.build_success``
(``provider_simulator.chains.solana``) and put any new fixed values here so the
chain stays free of inline magic numbers.
"""

# Base mainnet slot — a realistic post-2024 Solana slot number. It is the HEAD of
# an unmoved chain. A provider with no offset, no ``blocks_behind``, no
# ``frozen_head`` and no commitment lag reports this head at every commitment, so
# with the default lags every provider reports exactly this value and tests can
# pin exact equality on the default getSlot / getLatestBlockhash slot. A non-zero
# ``slot_offset`` shifts a single provider off this base for multi-slot
# divergence tests; the slot stays fixed per request either way (the simulator
# does not step it off the wall clock), so the head, the offset, the commitment
# lags and the slot ↔ lastValidBlockHeight gap are the only moving parts.
SOLANA_BASE_SLOT = 419_709_627

# The commitment that a request is answered at when it names none, and when it
# names one that is none of ``processed``, ``confirmed`` and ``finalized``.
#
# MEASURED on 2026-11-10 on Solana mainnet, in one JSON-RPC batch to
# api.mainnet-beta.solana.com, so that one node state answered every request:
# getSlot with no commitment answered 467685357, with commitment finalized
# 467685357, confirmed 467685357 and processed 467685358, and with
# {"commitment": "bogus"} 467685357, with no error. So no commitment and an
# unknown commitment are answered alike, at the finalized slot. (In that batch
# confirmed and finalized answered the same slot, so the batch matches finalized
# and confirmed equally well.)
SOLANA_DEFAULT_COMMITMENT = "finalized"

# How far below its head a provider answers at each commitment, in slots. No lag by
# default: processed, confirmed and finalized all answer the head, as the simulator
# answered before commitments had a slot of their own.
#
# A test that wants the shape of a real network sets the lags
# (``processed_lag_slots``, ``confirmed_lag_slots``, ``finalized_lag_slots``), and
# a test that needs a node whose confirmed and finalized slots differ sets them
# apart. What a real network showed:
#
# MEASURED on 2026-11-10: 30 rounds on each network, and each round
# asked the three commitments in one JSON-RPC batch, so that one node state
# answered all three.
#   Solana mainnet (a public RPC): processed - confirmed had a median of 1 slot
#     (minimum 1, maximum 6). confirmed - finalized was 0 in all 30 rounds.
#   Solana testnet (a QuickNode node): processed - confirmed had a median of 1
#     (0 to 1). confirmed - finalized had a median of 0 (0 to 1).
#   The slot rate on both networks was 4.64 slots per second.
# So a real network has processed at the head, and confirmed and finalized one
# slot below it: the lags 0, 1 and 1. A commitment lag on a real network can
# change, which is the other reason that each lag is a setting.
SOLANA_DEFAULT_PROCESSED_LAG_SLOTS = 0
SOLANA_DEFAULT_CONFIRMED_LAG_SLOTS = 0
SOLANA_DEFAULT_FINALIZED_LAG_SLOTS = 0

# What getBalance reports for an account: the lamports of an address that holds
# none. A per-method ``responses`` entry replaces it.
SOLANA_DEFAULT_BALANCE_LAMPORTS = 0

# Default distance between context.slot and value.lastValidBlockHeight.
# Mirrors the ~22M real-mainnet gap and exceeds the router's 50-block
# consistency threshold so the default scenario reproduces MAG-1591. Overridable
# per provider via the /scenario field ``solana_slot_block_gap``.
SOLANA_DEFAULT_SLOT_BLOCK_GAP = 21_900_000

# A blockhash is base58, 32 bytes → 43-44 chars. The stub doesn't need to be a
# real hash — the router reads the numeric fields, not the hash bytes — so a
# fixed 44-char base58-alphabet string is enough for shape verification.
SOLANA_BLOCKHASH = "SiMu1atorBLockhash1111111111111111111111111"  # 44 base58 chars

# Reported Solana core version for getVersion. Shape mirrors a real
# getVersion reply: {"solana-core": "<semver>", "feature-set": <u32>}.
SOLANA_CORE_VERSION = "1.18.22"
SOLANA_FEATURE_SET = 3469865029
