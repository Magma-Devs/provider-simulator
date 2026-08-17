"""Solana chain — success-path response building.

Reimplements the Solana success path (today in ``handlers_solana.handle``)
against the new domain shapes. The Solana-specific knobs move to SolanaQuirks:
``slot_offset`` (this provider's slot off the shared base) and ``slot_block_gap``
(the distance between ``result.context.slot`` and
``result.value.lastValidBlockHeight`` that drives the router's consistency
filter), plus ``unknown_method_mode``. ``http_status`` comes from the
ScenarioConfig snapshot.

The chain owns its error catalogue (``SOLANA_ERROR_STUBS``) — the flat
``handlers_solana`` keeps its own copy until it is retired.
"""

import stubs_solana
from provider_simulator.chains.base import Chain
from provider_simulator.domain.quirks import SolanaQuirks

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


class SolanaChain(Chain):
    name = "solana"
    quirks_type = SolanaQuirks

    def error_stub(self, name: str) -> dict:
        return SOLANA_ERROR_STUBS[name]

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

        slot = stubs_solana.SOLANA_BASE_SLOT + quirks.get("slot_offset", 0)

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
