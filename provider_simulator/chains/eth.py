"""Ethereum chain — success-path response building plus the advancing head.

Reimplements the ETH success path (today in ``handlers_eth.handle``) against the
redesigned domain shapes: the per-method overrides, ``blocks_behind`` and
``http_status`` come from the ScenarioConfig snapshot; ``logs_indexed_up_to`` and
``logs_lag_mode`` come from the EthQuirks snapshot; the head lives on this chain
instance instead of a module global.

Method-specific behaviour:
- ``eth_blockNumber`` — reports the head shifted by ``blocks_behind`` (unless a
  response override pins a result). Static head + blocks_behind 0 = ``0x1312D00``.
- ``eth_getBlockByNumber`` — echoes the requested block number so the router's
  pruning verification sees it; the named tags resolve to shifted heights. A
  provider whose ``blocks_behind`` is not zero answers ``null`` for a numeric
  block above its own effective head, which is the chain head minus its
  ``blocks_behind``. A real node that has not reached a block does not fail: it
  answers HTTP 200 with a null result and no error field, the router does not
  classify that as a failure and does not retry, and the caller records a gap in
  the chain that is not there. Reproducing that symptom is why this exists.
  ``blocks_behind`` is signed, so this covers both directions: a provider that
  is behind, and one set AHEAD with a negative value, which is how a provider
  that claims a head it cannot back up is modelled. Both have a head, and a
  block above either one does not exist.
  ``blocks_behind`` of zero is left exactly as it was — the guard below is the
  first thing tested, so a provider at rest never takes the new path, because
  the router's pruning verification asks it for block 0 and must get a block.
- ``eth_getLogs`` — models head-fresh-but-logs-lagged: when the query's upper
  bound exceeds ``logs_indexed_up_to``, return no logs (``empty``) or only the
  indexed ones (``partial``).
"""

from provider_simulator.chains.base import AdvancingHead, Chain
from provider_simulator.domain.quirks import EthQuirks
from stubs import ETH_ERROR_STUBS, ETH_METHOD_DEFAULTS


def _hex_upper(n: int) -> str:
    """Upper-case hex ("0x" + uppercase digits), matching the legacy stub values."""
    return "0x" + format(n, "X")


def _resolve_block_tag(params: list, key: str, head_int: int):
    """Resolve an eth_getLogs fromBlock/toBlock to an int, or None if unresolvable.
    Tags (latest/safe/finalized/pending) resolve to the current head."""
    if not params or not isinstance(params[0], dict):
        return None
    raw = params[0].get(key, "latest")
    if isinstance(raw, str):
        if raw in ("latest", "safe", "finalized", "pending"):
            return head_int
        try:
            return int(raw, 16) if raw.startswith("0x") else int(raw)
        except (ValueError, TypeError):
            return None
    if isinstance(raw, int):
        return raw
    return None


def _parse_block_number(raw: object) -> int | None:
    """Parse an eth_getBlockByNumber block parameter to an int, or None when it is
    not a number at all (a named tag, or anything unparseable). Same parsing as
    ``_resolve_block_tag`` above, on a bare parameter instead of a filter dict."""
    if isinstance(raw, bool):  # bool is an int subclass; a bool is not a height
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw, 16) if raw.startswith("0x") else int(raw)
        except (ValueError, TypeError):
            return None
    return None


def _entry_blocknum_le(entry: dict, threshold: int) -> bool:
    """True if entry["blockNumber"] parses to an int <= threshold (defensive)."""
    if not isinstance(entry, dict):
        return False
    raw = entry.get("blockNumber")
    if isinstance(raw, int):
        return raw <= threshold
    if isinstance(raw, str):
        try:
            return int(raw, 16) <= threshold
        except (ValueError, TypeError):
            return False
    return False


class EthChain(Chain):
    name = "eth"
    quirks_type = EthQuirks

    def __init__(self) -> None:
        self.head = AdvancingHead(int(ETH_METHOD_DEFAULTS["eth_blockNumber"], 16))

    def error_stub(self, name: str) -> dict:
        return ETH_ERROR_STUBS[name]

    def build_success(self, request: dict, scenario: dict, quirks: dict, interface: str = "") -> tuple[int, dict]:
        req_id = request.get("id", 1)
        method = request.get("method", "unknown")
        params = request.get("params", [])
        responses = scenario.get("responses") or {}
        method_cfg = responses.get(method) or responses.get("default", {})

        # Per-method error override (named catalogue or raw envelope).
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

        result = method_cfg.get("result", ETH_METHOD_DEFAULTS.get(method, "0x1"))
        blocks_behind = scenario.get("blocks_behind", 0)

        if method == "eth_blockNumber" and "result" not in method_cfg:
            result = _hex_upper(self.head.current() - blocks_behind)

        if method == "eth_getBlockByNumber" and isinstance(result, dict) and params:
            head = self.head.current()
            effective_head = head - blocks_behind
            effective_latest = _hex_upper(effective_head)
            named = {
                "latest": effective_latest,
                "earliest": "0x0",
                "pending": _hex_upper(effective_head + 1),
                "safe": effective_latest,
                "finalized": _hex_upper(effective_head - 1),
            }
            # Only a NUMERIC parameter can be above the head. Every named tag is
            # already resolved against the shifted height, so "latest" on a
            # provider that is behind means that provider's latest, not the
            # chain's, and asking for it must still return a block. No tag needs
            # excluding by name: none of the five parses as a number, so
            # _parse_block_number returns None for each. That is what keeps them
            # out, so a test covers every tag rather than trusting this comment.
            # A provider that claims to be AHEAD still has a head, and a block
            # above it does not exist either. ``blocks_behind`` is signed: a
            # negative value moves the provider's head UP, so effective_head
            # already carries the right boundary for both directions and the
            # comparison below needs no sign of its own.
            #
            # Zero is the one value left alone, and that is deliberate. A
            # provider at the canonical head keeps answering for any height,
            # because the router's pruning verification asks for block 0 and
            # must receive a block.
            above_head = False
            if blocks_behind != 0 and isinstance(params[0], (str, int)):
                requested = _parse_block_number(params[0])
                above_head = requested is not None and requested > effective_head
            if above_head:
                result = None  # HTTP 200, no error field — the false-gap symptom
            else:
                result = dict(result)
                result["number"] = named.get(params[0], params[0])

        if method == "eth_getLogs":
            logs_indexed = quirks.get("logs_indexed_up_to")
            if logs_indexed is not None:
                head_int = self.head.current() - blocks_behind
                to_block = _resolve_block_tag(params, "toBlock", head_int)
                if to_block is not None and to_block > logs_indexed:
                    mode = quirks.get("logs_lag_mode", "empty")
                    if mode == "partial" and isinstance(result, list):
                        result = [e for e in result if _entry_blocknum_le(e, logs_indexed)]
                    else:
                        result = []

        return scenario.get("http_status", 200), {"jsonrpc": "2.0", "id": req_id, "result": result}
