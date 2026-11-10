"""Solana chain — success-path response building.

Reimplements the Solana success path (today in ``handlers_solana.handle``)
against the new domain shapes. The Solana-specific knobs move to SolanaQuirks:
``slot_offset`` (this provider's slot off the shared base) and ``slot_block_gap``
(the distance between ``result.context.slot`` and
``result.value.lastValidBlockHeight`` that drives the router's consistency
filter), plus ``unknown_method_mode`` and the lag of each commitment.
``http_status``, ``blocks_behind`` and ``frozen_head`` come from the
ScenarioConfig snapshot.

A reply that reports a slot reports the slot of a COMMITMENT. The provider has one
head, which is ``reported_head`` of the chain head shifted by ``slot_offset``: the
block of ``frozen_head`` when the scenario sets one, otherwise the chain head shifted
by ``blocks_behind``. Each commitment then answers below that head by its lag
(``processed_lag_slots``, ``confirmed_lag_slots``, ``finalized_lag_slots``), and the
request chooses the commitment in the config object of its params. A request that
names none gets the finalized slot, and so does a request that names a commitment
that is none of the three: a real node answered both alike (see
``stubs_solana.SOLANA_DEFAULT_COMMITMENT``). The lags start at 0, so at the defaults
every commitment answers the head.

The chain owns its error catalogue (``SOLANA_ERROR_STUBS``) — the flat
``handlers_solana`` keeps its own copy until it is retired.
"""

import stubs_solana
from provider_simulator.chains.base import AdvancingHead, Chain, reported_head
from provider_simulator.domain.quirks import SolanaQuirks

# The commitments of Solana, from the most recent slot to the least recent one, each with
# the lag below the head that it starts at (none).
COMMITMENTS = ("processed", "confirmed", "finalized")
_DEFAULT_LAG_SLOTS = {
    "processed": stubs_solana.SOLANA_DEFAULT_PROCESSED_LAG_SLOTS,
    "confirmed": stubs_solana.SOLANA_DEFAULT_CONFIRMED_LAG_SLOTS,
    "finalized": stubs_solana.SOLANA_DEFAULT_FINALIZED_LAG_SLOTS,
}

# The methods whose reply reports a slot, as a bare number or as ``result.context.slot``.
# Only these read the commitment of their request.
SLOT_METHODS = frozenset({"getSlot", "getLatestBlockhash", "getBalance"})

SOLANA_ERROR_STUBS: dict[str, dict] = {
    "method_not_found": {"code": -32601, "message": "Method not found"},
    "invalid_params": {"code": -32602, "message": "Invalid params"},
    "node_behind": {
        "code": -32005,
        "message": "Node is behind by 100 slots",
        "data": {"numSlotsBehind": 100},
    },
    "slot_skipped": {
        "code": -32007,
        "message": "Slot 123456789 was skipped, or missing due to ledger jump to recent snapshot",
    },
    "long_term_storage_slot_skipped": {
        "code": -32009,
        "message": "Slot 123456789 was skipped, or missing in long-term storage",
    },
    "min_context_slot_not_reached": {
        "code": -32016,
        "message": "Minimum context slot has not been reached",
    },
    "transaction_simulation_failed": {
        "code": -32002,
        "message": "Transaction simulation failed",
    },
    "blockhash_not_found": {
        "code": -32002,
        "message": "Transaction simulation failed: Blockhash not found",
        "data": {"err": "BlockhashNotFound"},
    },
}


def named_commitment(params: object) -> object:
    """The commitment that a request names, or None when it names none.

    A commitment is the ``commitment`` of the config object in the params. That object
    is not at one position: getSlot has it first, getBalance after the address, and a
    method with two objects holds it in the second. So the first object that names a
    commitment is the one. A ``commitment`` of null names none, as it does for a node.
    The value is returned as it is, whatever its type: the caller answers a value that
    is none of the three commitments like a request that names none.
    """
    if not isinstance(params, list):
        return None
    for param in params:
        if isinstance(param, dict) and param.get("commitment") is not None:
            return param["commitment"]
    return None


class SolanaChain(Chain):
    name = "solana"
    quirks_type = SolanaQuirks
    reads_frozen_head = True

    def __init__(self) -> None:
        self.head = AdvancingHead(stubs_solana.SOLANA_BASE_SLOT)

    def error_stub(self, name: str) -> dict:
        return SOLANA_ERROR_STUBS[name]

    def slot_at(self, commitment: str, scenario: dict, quirks: dict) -> int:
        """The slot that this provider answers at ``commitment``, never below 0.

        The head is the chain head shifted by this ONE provider's ``slot_offset``.
        ``reported_head`` then applies ``frozen_head`` or ``blocks_behind``, so a frozen
        provider reports the slot that was named and no offset moves it. The lag of the
        commitment applies below that head.
        """
        head = reported_head(self.head.current() + quirks.get("slot_offset", 0), scenario)
        return max(0, head - quirks.get(f"{commitment}_lag_slots", _DEFAULT_LAG_SLOTS[commitment]))

    def build_success(self, request: dict, scenario: dict, quirks: dict, interface: str = "") -> tuple[int, dict]:
        req_id = request.get("id", 1)
        method = request.get("method", "unknown")
        responses = scenario.get("responses") or {}
        method_cfg = responses.get(method) or responses.get("default", {})
        http_status = scenario.get("http_status", 200)

        err = None
        if "error_stub" in method_cfg:
            err = self.error_stub(method_cfg["error_stub"])
        elif "error" in method_cfg:
            err = method_cfg["error"]
        if err is not None:
            return method_cfg.get("http_status", 200), {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": err,
            }

        if "result" in method_cfg:
            return http_status, {"jsonrpc": "2.0", "id": req_id, "result": method_cfg["result"]}

        # The slot of a reply is the slot of a commitment, below the head of this
        # provider (see ``slot_at``). Moving the chain head moves every provider
        # together and leaves the offsets between them intact.
        slot = 0
        if method in SLOT_METHODS:
            named = named_commitment(request.get("params"))
            # A commitment that is none of the three is no error: a real node answered
            # {"commitment": "bogus"} with the slot of a request that names none, and no
            # error (measured on 2026-11-10, see ``stubs_solana.SOLANA_DEFAULT_COMMITMENT``).
            # So it is answered like a request that names none, at the finalized slot.
            commitment = named if isinstance(named, str) and named in COMMITMENTS else None
            slot = self.slot_at(commitment or stubs_solana.SOLANA_DEFAULT_COMMITMENT, scenario, quirks)

        if method == "getLatestBlockhash":
            gap = quirks.get("slot_block_gap", stubs_solana.SOLANA_DEFAULT_SLOT_BLOCK_GAP)
            result: object = {
                "context": {"slot": slot},
                "value": {
                    "blockhash": stubs_solana.SOLANA_BLOCKHASH,
                    "lastValidBlockHeight": slot - gap,
                },
            }
        elif method == "getSlot":
            result = slot
        elif method == "getBalance":
            result = {"context": {"slot": slot}, "value": stubs_solana.SOLANA_DEFAULT_BALANCE_LAMPORTS}
        elif method == "getHealth":
            result = "ok"
        elif method == "getVersion":
            result = {
                "solana-core": stubs_solana.SOLANA_CORE_VERSION,
                "feature-set": stubs_solana.SOLANA_FEATURE_SET,
            }
        else:
            if quirks.get("unknown_method_mode") == "error":
                return http_status, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": SOLANA_ERROR_STUBS["method_not_found"],
                }
            result = None

        return http_status, {"jsonrpc": "2.0", "id": req_id, "result": result}
