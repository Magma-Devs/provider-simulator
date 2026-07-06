"""
handlers_grpc.py — gRPC dispatch for the provider simulator (MAG-1780).

Sibling of ``handlers_eth`` / ``handlers_btc``. The key shape difference: the
JSON-RPC handlers return ``(http_status, response_body_dict)`` because their
caller writes the HTTP response. The gRPC path is different — grpc.aio drives
the return path from inside the servicer method, so the fault primitives
must call ``await context.abort(...)`` directly. There is no integer status
code to thread back; aborting raises ``grpc.aio.AbortError`` so any caller
code after the ``await`` is unreachable.

Why a separate ``_apply_grpc_fault`` helper
-------------------------------------------
The JSON-RPC ``_apply_fault`` extracted by MAG-1777 returns
``Optional[Tuple[int, dict]]`` — an HTTP status code plus a body dict that
the caller writes. The gRPC variant can't share that shape:

  - There is no HTTP status code; errors carry a ``grpc.StatusCode`` enum.
  - The response is a serialised protobuf, not a JSON dict.
  - Aborting requires ``await context.abort(...)``; the helper must be
    ``async`` and run in the same event loop as the servicer method.

So MAG-1780 keeps its own helper. If MAG-1777 lands first, the two helpers
sit side-by-side in this directory without sharing code; if MAG-1780 lands
first, the inline copy stays put and MAG-1777 still ships its own helper.

Public surface
--------------

  ``CosmosBaseTendermintServicer(state)`` — gRPC servicer for the
    ``cosmos.base.tendermint.v1beta1.Service``. v1 scope is the two unary
    methods used by retry / failover / cross-validation tests:

       * ``GetLatestBlock`` — primary head-tracking call.
       * ``GetNodeInfo`` — version probe used during provider warmup.

    The remaining cosmos services (bank, auth, staking, tx) are out of
    scope for MAG-1780 and tracked as follow-ups.

  ``_apply_grpc_fault(state, snap, context, method, lava_headers, t_start)``
    returns ``True`` if a fault path aborted the request (caller should
    ``return`` immediately); ``False`` to proceed with the success branch.

Fault → gRPC mapping
--------------------

JSON-RPC primitive          gRPC translation
---------------------------  ----------------------------------------------------
mode="down"                 abort(UNAVAILABLE, "provider down")
mode="hang"                 ``await asyncio.sleep(30)`` then abort with
                             CANCELLED — analogous to the JSON-RPC 30s sleep
mode="drop_connection"      drop_at="before_headers" → abort UNAVAILABLE without
                             sending metadata; drop_at="after_headers" /
                             "mid_body" → ``await context.send_initial_metadata([])``
                             then abort CANCELLED. Unary RPCs can't legally
                             stream mid-body, so the two non-default values
                             collapse to the same shape until streaming is
                             added in a follow-up ticket.
mode="rate_limit"           abort(RESOURCE_EXHAUSTED, "Too many requests")
mode="error"                abort with the gRPC status named in
                             ``state.error_message`` (symbolic name), or use
                             ``error_code`` as the integer status when no name
                             match. Falls back to UNKNOWN.
corruption_mode="invalid_proto"      abort(UNKNOWN, "corruption: invalid_proto")
corruption_mode="empty_response"     abort(UNKNOWN, "corruption: empty_response")
corruption_mode="truncated"          abort(UNKNOWN, "corruption: truncated")
corruption_mode="missing_field"      build the response and ``ClearField``
                             the snap["missing_field"] field before returning.
                             Proto3 makes most fields optional, so omitting is
                             legal-on-wire but signals incompleteness.
corruption_mode="wrong_type"         abort(INTERNAL, "wrong_type ...") — the
                             proto runtime won't let us assign a mismatched
                             field type, so we surface the corruption as a
                             status-code failure rather than a wire-format one.
blocks_behind > 0           decrement ``block.header.height`` on the
                             GetLatestBlockResponse before returning.
"""

from __future__ import annotations

import asyncio
import datetime
import random
import time
from typing import Any, Optional

import grpc

# cosmos_pb2/__init__.py splices its own path onto sys.path on first import,
# so the absolute imports inside the generated stubs resolve without a
# package rewrite. Importing the package is enough to trigger the splice;
# the subsequent ``from cosmos.base...`` lines then succeed.
import cosmos_pb2  # noqa: F401 — side-effect import to splice sys.path

from cosmos.base.tendermint.v1beta1 import query_pb2, query_pb2_grpc
from tendermint.types import block_pb2, types_pb2


# Canonical chain id used in the GetLatestBlockResponse stub. Lava router
# treats this as the chain identifier reported by the provider, so tests can
# pin a known value.
SIM_CHAIN_ID = "lava-sim"

# Realistic Tendermint-style block height for the simulator's default head.
# Tests that assert exact equality on the default GetLatestBlockResponse
# pin against this constant.
GRPC_LATEST_BLOCK = 25_000_000

# Name → grpc.StatusCode lookup table. Symbolic names are the primary
# interface for tests (mode="error" + error_message="RESOURCE_EXHAUSTED");
# integer codes are also accepted as a fallback so legacy payloads using
# error_code=8 keep working.
_GRPC_STATUS_BY_NAME = {sc.name: sc for sc in grpc.StatusCode}
_GRPC_STATUS_BY_VALUE = {sc.value[0]: sc for sc in grpc.StatusCode}


def _capture_grpc_metadata(context: grpc.aio.ServicerContext) -> dict:
    """Return a dict of all ``lava-*`` request-metadata keys.

    The gRPC servicer context carries client metadata as an iterable of
    (key, value) tuples — gRPC normalises keys to lowercase per the spec.
    We mirror the JSON-RPC handler's behaviour of keeping only the
    ``lava-*`` namespace so /history entries stay comparable across
    chain families.
    """
    md = context.invocation_metadata() or []
    return {k: v for (k, v) in md if k.lower().startswith("lava-")}


def _record_history(state, method: str, status: str, latency_ms: int,
                    lava_headers: dict, request_id: Any = None,
                    chain: Optional[str] = None,
                    port: Optional[int] = None) -> None:
    """Append one entry to the ProviderState ring-buffer.

    Same shape as ``ProviderState.push_call_to_buffer`` so /history queries
    don't have to special-case the chain family. ``request_id`` is always
    ``None`` for gRPC since the protocol carries no equivalent — the field
    is optional in the history entry already.

    ``chain`` / ``port`` (MAG-2236) carry the serving gRPC listener's chain
    family and bound port so /history can be filtered per listener instead
    of per shared provider pid.
    """
    state.push_call_to_buffer(
        method=method,
        status=status,
        latency_ms=latency_ms,
        request_id=request_id,
        lava_headers=lava_headers,
        chain=chain,
        port=port,
    )


async def _apply_grpc_fault(state, snap: dict, context: grpc.aio.ServicerContext,
                             method: str, lava_headers: dict, t_start: float,
                             chain: Optional[str] = None,
                             port: Optional[int] = None) -> bool:
    """Run the fault-injection ladder against an incoming gRPC request.

    Evaluation order mirrors ``JSONRPCHandler.do_POST`` so the two chain
    families behave consistently. Returns ``True`` once an abort has been
    raised; the caller should treat the return as a fall-through. Returns
    ``False`` to proceed with the success branch.

    The history entry is appended before the abort so failed requests still
    show up in /history with the matching status — same contract as the
    JSON-RPC side.

    MAG-1832 — write history BEFORE any latency sleep so a deadline cancel
    mid-sleep still leaves a trace in /history. Each fault branch follows
    the record-before-sleep pattern Avi rolled out for JSON-RPC / REST /
    Tendermint-RPC / WebSocket: ``_record_history(snap["latency_ms"])`` →
    ``await asyncio.sleep(snap["latency_ms"] / 1000)`` → ``await
    context.abort(...)``. The recorded latency is the *configured* value
    (what the call would have taken had it run to completion) — measuring
    elapsed time after the sleep moves to inside the branch would be ~0ms
    on a canceled-mid-sleep path, which is exactly the case we're fixing.

    ``down`` and ``hang`` stay as-is — ``down`` has no latency at all and
    ``hang`` already had its 30s sleep AFTER recording.

    Cross-transport isolation (MAG-1836)
    ------------------------------------
    ``ProviderState`` is shared across all transports (JSON-RPC, REST, gRPC,
    WS) for the same provider id. The fault primitives (``down`` / ``hang`` /
    ``drop_connection`` / ``rate_limit`` / ``error``) are chain-agnostic
    fields on the snap, so without an explicit gate a fault authored for
    JSON-RPC (chain_family="eth") would also kill the gRPC port. Gate the
    whole ladder on ``chain_family == "grpc"`` and fall through to the
    normal success stub when the fault was set for another transport.

    Exception (MAG-2092): ``mode="down"`` is honored on every transport
    because reachability is provider-wide; per-transport isolation only
    applies to content modes (error / corrupt / hang / rate_limit /
    latency / drop_connection). Without this exemption, an ETH provider
    in mode=down would still serve gRPC requests, hiding router-side
    bugs whose reproduction depends on the provider being unreachable
    across every node-url (e.g. MAG-2061).

    Sequenced fault (fail_first_n / then_mode)
    ------------------------------------------
    The fail_first_n window is measured in OWNING-listener requests — only
    the JSON-RPC listener whose chain_family matches the snap consumes the
    counter. This surface never advances the window; it observes it through
    the effective mode below, so a sequenced provider-wide down clears here
    once the owning listener has consumed the window. For snaps authored
    for the gRPC transport no listener consumes the counter, so the
    effective mode always equals the raw ``snap["mode"]`` and the ladder
    behaves exactly as before.
    """
    # Lazy import: server.py owns the shared cross-transport helpers and
    # imports the sibling handler modules at load time — resolving at call
    # time keeps this module free of any load-order coupling with it.
    from server import _effective_mode
    mode = _effective_mode(state, snap)
    # MAG-1836: only apply faults that were authored for the gRPC transport.
    # MAG-2092: but always honor mode="down" regardless of chain_family.
    if snap.get("chain_family") != "grpc" and mode != "down":
        return False
    # 1. Outage — no latency on the down path (provider drops the request
    #    immediately, mirroring JSON-RPC ``down`` semantics).
    if mode == "down":
        _record_history(state, method, "down", 0, lava_headers,
                        chain=chain, port=port)
        await context.abort(grpc.StatusCode.UNAVAILABLE, "provider down")
        return True  # unreachable — abort raises, but keep type-checkers happy

    # 2. Hang — accept request, sleep ~forever so the client hits its
    #    deadline. Same 30s cap as the JSON-RPC handler so a stuck request
    #    eventually unwinds and the asyncio task doesn't leak. Already
    #    write-before-sleep; left untouched per Avi's review.
    if mode == "hang":
        _record_history(state, method, "hang", 0, lava_headers,
                        chain=chain, port=port)
        await asyncio.sleep(30)
        await context.abort(grpc.StatusCode.CANCELLED, "hang timeout")
        return True

    # 3. Connection drop — record first, then pay the configured latency on
    #    the wire, then abort. Order matters: history must be durable before
    #    the abort surface so a router-side deadline cancel doesn't drop the
    #    entry.
    if mode == "drop_connection":
        drop_at = snap.get("drop_at", "before_headers")
        _record_history(state, method, "drop_connection", snap["latency_ms"],
                        lava_headers, chain=chain, port=port)
        if snap["latency_ms"] > 0:
            await asyncio.sleep(snap["latency_ms"] / 1000.0)
        if drop_at in ("after_headers", "mid_body"):
            # Send empty initial metadata so the client sees a half-open
            # response, then abort. Unary RPCs collapse the "mid_body"
            # variant to this same shape until streaming support lands.
            try:
                await context.send_initial_metadata([])
            except Exception:
                pass
        await context.abort(grpc.StatusCode.UNAVAILABLE, "connection dropped")
        return True

    # 4. Rate limit
    if mode == "rate_limit":
        _record_history(state, method, "rate_limit", snap["latency_ms"],
                        lava_headers, chain=chain, port=port)
        if snap["latency_ms"] > 0:
            await asyncio.sleep(snap["latency_ms"] / 1000.0)
        await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Too many requests")
        return True

    # 5. Forced or probabilistic error — translate error_message / error_code
    #    to a grpc.StatusCode. Symbolic name wins; integer code is the
    #    fallback for legacy payloads. UNKNOWN is the last-resort default.
    if mode == "error" or random.random() < snap["error_probability"]:
        err_msg = snap.get("error_message", "Internal error")
        err_code = snap.get("error_code", -32000)
        status_code = (
            _GRPC_STATUS_BY_NAME.get(err_msg)
            or _GRPC_STATUS_BY_VALUE.get(err_code)
            or grpc.StatusCode.UNKNOWN
        )
        _record_history(state, method, "error", snap["latency_ms"], lava_headers,
                        chain=chain, port=port)
        if snap["latency_ms"] > 0:
            await asyncio.sleep(snap["latency_ms"] / 1000.0)
        await context.abort(status_code, err_msg)
        return True

    return False


def _build_get_latest_block_response(snap: dict) -> query_pb2.GetLatestBlockResponse:
    """Build the canonical GetLatestBlockResponse, applying ``blocks_behind``.

    The height lives at ``block.header.height`` (int64). The response carries
    both legacy ``block`` and modern ``sdk_block`` slots; we fill the legacy
    ``block`` because that's what every cosmos-sdk release ≤ v0.50 reads
    from.
    """
    head = GRPC_LATEST_BLOCK - snap.get("blocks_behind", 0)

    # Header timestamp: pinned to the current UTC for proximity to wall
    # clock without taking a hard dep on a fixed value. Tests that need
    # determinism can override via state.responses[method] = {...}.
    now = datetime.datetime.now(datetime.timezone.utc)

    header = types_pb2.Header(chain_id=SIM_CHAIN_ID, height=head)
    header.time.seconds = int(now.timestamp())
    header.time.nanos = now.microsecond * 1000

    block = block_pb2.Block(header=header)
    block_id = types_pb2.BlockID(hash=b"\xab" * 32)

    return query_pb2.GetLatestBlockResponse(block_id=block_id, block=block)


def _build_get_node_info_response(snap: dict) -> query_pb2.GetNodeInfoResponse:
    """Build the canonical GetNodeInfoResponse stub.

    Cosmos-SDK uses this for chain identification on provider warmup; the
    interesting fields are ``default_node_info.network`` (chain id) and
    ``application_version.version`` (app build). ``snap`` is accepted for
    signature parity with ``_build_get_latest_block_response`` but ignored
    here — GetNodeInfo doesn't honour ``blocks_behind`` (it's a version
    probe, not a chain-head call).
    """
    resp = query_pb2.GetNodeInfoResponse()
    resp.default_node_info.network = SIM_CHAIN_ID
    resp.default_node_info.moniker = "lava-sim-grpc-provider"
    resp.default_node_info.version = "sim-1.0"
    resp.application_version.name = "lava-sim"
    resp.application_version.app_name = "lava-sim-app"
    resp.application_version.version = "sim-1.0"
    return resp


# ── Servicer ─────────────────────────────────────────────────────────────────

class CosmosBaseTendermintServicer(query_pb2_grpc.ServiceServicer):
    """gRPC servicer for ``cosmos.base.tendermint.v1beta1.Service``.

    The servicer carries a reference to the ``ProviderState`` instance that
    matches its provider id. State is shared with the JSON-RPC ``HTTPServer``
    on the sibling port, so a single ``POST /scenario`` call reconfigures
    both transports for the same logical provider.

    Why two methods only
    --------------------
    ``GetLatestBlock`` and ``GetNodeInfo`` cover the retry / failover /
    cross-validation paths the smart-router exercises during provider
    warmup and steady-state head-tracking. The remaining cosmos.tendermint
    methods (GetBlockByHeight, GetSyncing, GetLatestValidatorSet, etc.)
    are out of scope for v1 — they can be added in follow-ups once retry
    coverage is stable.
    """

    def __init__(self, state, port: Optional[int] = None, chain: str = "grpc"):
        self._state = state
        # Listener identity for /history stamping (MAG-2236). A gRPC servicer
        # has no ``self.server`` like the HTTP handlers, so the bound port is
        # passed in from ``serve_grpc``; the chain family is fixed to "grpc"
        # (the value the fault ladder gates on).
        self._port = port
        self._chain = chain

    async def GetLatestBlock(self, request: query_pb2.GetLatestBlockRequest,
                              context: grpc.aio.ServicerContext) -> query_pb2.GetLatestBlockResponse:
        return await self._handle("GetLatestBlock", request, context,
                                   _build_get_latest_block_response)

    async def GetNodeInfo(self, request: query_pb2.GetNodeInfoRequest,
                           context: grpc.aio.ServicerContext) -> query_pb2.GetNodeInfoResponse:
        return await self._handle("GetNodeInfo", request, context,
                                   _build_get_node_info_response)

    async def _handle(self, method: str, request, context, build_response_fn):
        """Common pipeline for every unary servicer method.

        Captures metadata, runs the fault ladder, builds the success
        response, applies corruption / per-method overrides, and either
        returns the proto message or aborts. ``build_response_fn(snap)``
        produces the method-specific success proto so we can share the
        rest of the pipeline.

        MAG-1832 — every downstream branch (override / corruption /
        success) follows the record-before-sleep pattern: record history
        with the configured ``snap["latency_ms"]`` FIRST, sleep, then emit
        the wire response. Mirrors what landed for JSON-RPC / REST /
        Tendermint-RPC / WebSocket so a deadline cancel mid-sleep still
        leaves a /history entry.
        """
        t_start = time.monotonic()
        snap = self._state.snapshot()
        lava_headers = _capture_grpc_metadata(context)

        aborted = await _apply_grpc_fault(
            self._state, snap, context, method, lava_headers, t_start,
            chain=self._chain, port=self._port,
        )
        if aborted:
            return  # unreachable — abort always raises

        # Per-method override paths. Two shapes supported (mirrors ETH/BTC):
        #
        #   responses[method] = {"error_stub": "<grpc.StatusCode name>"}
        #   responses[method] = {"error": {"code": <name-or-int>, "message": "..."}}
        with self._state.lock:
            method_cfg = self._state.responses.get(method) or self._state.responses.get("default", {})

        if "error_stub" in method_cfg:
            status_code = _GRPC_STATUS_BY_NAME.get(method_cfg["error_stub"],
                                                    grpc.StatusCode.UNKNOWN)
            _record_history(self._state, method, "error", snap["latency_ms"],
                            lava_headers, chain=self._chain, port=self._port)
            if snap["latency_ms"] > 0:
                await asyncio.sleep(snap["latency_ms"] / 1000.0)
            await context.abort(status_code, method_cfg.get("message", method_cfg["error_stub"]))
            return
        if "error" in method_cfg:
            err = method_cfg["error"]
            code = err.get("code", "")
            status_code = (
                _GRPC_STATUS_BY_NAME.get(code if isinstance(code, str) else "")
                or _GRPC_STATUS_BY_VALUE.get(code if isinstance(code, int) else -1)
                or grpc.StatusCode.UNKNOWN
            )
            _record_history(self._state, method, "error", snap["latency_ms"],
                            lava_headers, chain=self._chain, port=self._port)
            if snap["latency_ms"] > 0:
                await asyncio.sleep(snap["latency_ms"] / 1000.0)
            await context.abort(status_code, err.get("message", "override"))
            return

        # Build the success proto.
        response = build_response_fn(snap)

        # Structural corruption (missing / wrong-type fields). Applied
        # before byte-level corruption so a "missing_field" response with
        # truncation still has the field cleared on the cleared bytes.
        #
        # Cross-transport isolation (MAG-1837): ``corruption_mode`` is a
        # chain-agnostic field on the snap, so without an explicit gate a
        # corruption authored for JSON-RPC (chain_family="eth") would also
        # break the gRPC port. Mirrors MAG-1836's _apply_grpc_fault gate —
        # fall through to the clean success proto unless the corruption was
        # authored for the gRPC transport.
        if snap.get("chain_family") != "grpc":
            cm = None
        else:
            cm = snap.get("corruption_mode")
        missing_field = snap.get("missing_field")
        if cm == "missing_field" and missing_field:
            # proto3 fields are clearable; ClearField is the canonical
            # API for the receiver to see the field unset.
            if response.DESCRIPTOR.fields_by_name.get(missing_field):
                response.ClearField(missing_field)
        elif cm == "wrong_type":
            # Wrong-type corruption requires bypassing the framework
            # serializer because the proto runtime won't let us assign a
            # mismatched field type at the Python layer. We surface the
            # corruption as an INTERNAL status so the client sees a
            # well-defined failure rather than a partial parse.
            _record_history(self._state, method, "error", snap["latency_ms"],
                            lava_headers, chain=self._chain, port=self._port)
            if snap["latency_ms"] > 0:
                await asyncio.sleep(snap["latency_ms"] / 1000.0)
            await context.abort(grpc.StatusCode.INTERNAL,
                                 f"wrong_type corruption on {missing_field or 'response'}")
            return
        elif cm in ("invalid_proto", "empty_response", "truncated"):
            # Byte-level corruption — invalid_proto, empty_response, and
            # truncated all break the wire contract the same way as a
            # parse error on the client. Surface as UNKNOWN so the client
            # sees a generic parse-failure surface.
            _record_history(self._state, method, "error", snap["latency_ms"],
                            lava_headers, chain=self._chain, port=self._port)
            if snap["latency_ms"] > 0:
                await asyncio.sleep(snap["latency_ms"] / 1000.0)
            await context.abort(grpc.StatusCode.UNKNOWN, f"corruption: {cm}")
            return

        # Success — record history with the configured latency BEFORE the
        # sleep so a router-side cancel mid-sleep still leaves a trace.
        _record_history(self._state, method, "success", snap["latency_ms"],
                        lava_headers, chain=self._chain, port=self._port)
        if snap["latency_ms"] > 0:
            await asyncio.sleep(snap["latency_ms"] / 1000.0)
        return response
