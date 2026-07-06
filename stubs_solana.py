"""
stubs_solana.py — Default Solana stub constants for the provider simulator.

Companion to ``stubs.py`` (Ethereum), ``stubs_btc.py`` (Bitcoin),
``stubs_lnd.py`` (Lightning), ``stubs_rest.py`` (Cosmos REST), and
``stubs_tendermintrpc.py`` (CometBFT). Different shape from those files:
Solana's four stubbed methods (``getLatestBlockhash`` / ``getSlot`` /
``getHealth`` / ``getVersion``) are computed per request in
``handlers_solana.handle`` — the reported slot depends on the per-provider
``solana_slot_offset`` and the slot ↔ lastValidBlockHeight distance on
``solana_slot_block_gap`` — so there is no static method-name → response map
to hold here. What lives here instead are the fixed chain values those
computed responses are built from.

Adding a new Solana method: implement the per-request branch in
``handlers_solana.handle`` and put any new fixed values here so the handler
stays free of inline magic numbers.
"""

# Base mainnet slot — a realistic post-2024 Solana slot number. _solana_slot()
# returns this plus the provider's solana_slot_offset (default 0), so with no
# offset every provider reports exactly this value and tests can pin exact
# equality on the default getSlot / getLatestBlockhash slot. A non-zero offset
# shifts a single provider off this base for multi-slot divergence tests; the
# slot stays fixed per request either way (the simulator does not step it off
# the wall clock), so the offset and the slot ↔ lastValidBlockHeight gap are the
# only moving parts.
SOLANA_BASE_SLOT = 419_709_627

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
