"""
handlers_solana.py — Solana success-branch dispatch for the provider simulator.

Sibling of ``handlers_eth`` / ``handlers_btc`` / ``handlers_lnd``. Same public
surface:

    handle(state, request, snap, lava_headers) -> tuple[int, dict]

Why Solana needs its own handler
--------------------------------
The smart-router's Solana consistency filter compares two numbers that come
from two different places in a single relay reply:

  * the per-user ``seenBlock`` — parsed from ``result.context.slot``.
  * the endpoint chain-tracker value — parsed from
    ``result.value.lastValidBlockHeight``.

On real Solana mainnet these two differ by roughly 22 million: the slot is the
monotonic ledger position (~419M and climbing) while ``lastValidBlockHeight``
is the smaller block-height counter (~397M). The router treats the gap as a
sync problem: when it exceeds the 50-block consistency threshold every endpoint
is filtered out and the client gets "No pairings available".

To reproduce that deterministically the ``getLatestBlockhash`` stub here emits
BOTH numbers from one response with a configurable gap. ``solana_slot_block_gap``
(read off the provider snapshot, default 21_900_000) sets the distance:

    slot                 = S
    lastValidBlockHeight = S - gap

so a test can dial the gap above or below the router's threshold and watch the
filter fire or pass.

Method scope (MAG-2231)
-----------------------
Four methods — just enough for the router's Solana spec verification plus the
``getLatestBlockhash`` divergence that drives the bug:

  * ``getLatestBlockhash`` — the divergence carrier (context.slot vs
    value.lastValidBlockHeight).
  * ``getSlot``            — current slot scalar.
  * ``getHealth``          — "ok".
  * ``getVersion``         — small version object.

Real Solana account/transaction/program state simulation is out of scope —
every response is a canned stub. Tests that need richer state override
per-method via ``responses[method] = {"result": ...}``.

Fault handling
--------------
``mode="down"`` (provider-wide unreachable) is honored upstream in
``server.py`` BEFORE this handler is reached — the JSON-RPC listener emits the
transport failure itself, identical to the BTC / LN / ETH handlers, so this
module never sees a down request. The success path below is the only thing
this module owns; per-method error overrides (``responses[method] =
{"error": ...}``) are honored the same way the sibling handlers honor them.
"""

from typing import Any, Dict, Tuple

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


def handle(state, request: dict, snap: dict, lava_headers: dict) -> Tuple[int, Dict[str, Any]]:
    """Resolve the Solana success-path response for one JSON-RPC request.

    Args:
        state:         The live ``ProviderState`` — read for ``state.responses``
                       under ``state.lock``. Same contract as
                       ``handlers_btc.handle``.
        request:       Parsed JSON-RPC body (``method``, ``params`` optional).
        snap:          ``ProviderState.snapshot()`` dict — read for
                       ``solana_slot_block_gap`` and ``http_status``.
        lava_headers:  Captured ``lava-*`` headers, threaded through for symmetry.

    Returns:
        ``(http_status, response_body)``. Either the success envelope with the
        method's stub or the error envelope when the test override emits one via
        ``responses[method] = {"error": ...}``.
    """
    req_id = request.get("id", 1)
    method = request.get("method", "unknown")

    # Look up method-specific override (named or default) — mirrors the BTC / LN
    # handlers. Read under the lock because state.responses is mutated by
    # /scenario from another thread.
    with state.lock:
        method_cfg = state.responses.get(method) or state.responses.get("default", {})

    # Per-method error override path — mirrors handlers_btc.handle. Solana has no
    # named error-stub registry yet, so only a raw ``error`` envelope is honored.
    if "error" in method_cfg:
        http_st = method_cfg.get("http_status", 200)
        return http_st, {"jsonrpc": "2.0", "id": req_id, "error": method_cfg["error"]}

    # Explicit per-method result override wins over the computed stub.
    if "result" in method_cfg:
        return snap.get("http_status", 200), {
            "jsonrpc": "2.0", "id": req_id, "result": method_cfg["result"]
        }

    slot = _solana_slot(snap)

    if method == "getLatestBlockhash":
        # The divergence carrier. context.slot and value.lastValidBlockHeight are
        # separated by the configured gap so the router's consistency filter sees
        # a per-user seenBlock (slot) far ahead of the chain-tracker value
        # (lastValidBlockHeight). gap defaults to SOLANA_DEFAULT_SLOT_BLOCK_GAP.
        gap = snap.get("solana_slot_block_gap", SOLANA_DEFAULT_SLOT_BLOCK_GAP)
        result = {
            "context": {"slot": slot},
            "value": {
                "blockhash": SOLANA_BLOCKHASH,
                "lastValidBlockHeight": slot - gap,
            },
        }
    elif method == "getSlot":
        result = slot
    elif method == "getHealth":
        result = "ok"
    elif method == "getVersion":
        result = {
            "solana-core": SOLANA_CORE_VERSION,
            "feature-set": SOLANA_FEATURE_SET,
        }
    else:
        # Unknown method — return a parse-friendly null result, mirroring the
        # BTC / LN fallback. The router sees a well-formed but empty response
        # instead of an error.
        result = None

    return snap.get("http_status", 200), {"jsonrpc": "2.0", "id": req_id, "result": result}


def _solana_slot(snap: dict) -> int:
    """Return the effective Solana slot for this provider's request.

    ``SOLANA_BASE_SLOT + solana_slot_offset`` (offset read off the provider
    snapshot, default 0). The base is a fixed realistic mainnet slot; the
    per-provider offset shifts THIS provider's reported slot off that base so a
    test can stand up multiple providers at different slots (one current, others
    stale-behind) and watch the router's Solana consistency filter keep the
    current provider and drop the stale ones. Offset 0 ⇒ the base verbatim, so
    every provider sits at the same slot (no divergence). Negative = behind the
    base, positive = ahead.

    The slot stays fixed per request (the simulator does not advance it off the
    wall clock) so tests can pin exact equality; the offset and the
    slot ↔ lastValidBlockHeight gap are the only moving parts under test.
    """
    return SOLANA_BASE_SLOT + snap.get("solana_slot_offset", 0)
