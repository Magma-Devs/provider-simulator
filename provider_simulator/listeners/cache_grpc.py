"""gRPC listener for the cache-sim.

Serves one method on the service path the smart router calls a cache on::

    /smartrouter.pairing.RelayerCache/GetRelay

No protobuf and no generated stubs are involved. The router's cache messages
are JSON on the wire -- every one of them marshals with ``json.Marshal``
(``smart-router/types/relay/proto_compat.go``) and gRPC's default codec
delegates to that. So this listener registers a generic handler whose
serialisers are the identity, receives the request bytes as they arrived, and
sends the reply bytes unchanged.

That identity codec is also what lets a test send something malformed: the
listener writes exactly the bytes the plan carries, including bytes that are not
a reply at all.

Like the provider gRPC listener beside it, the decision is a pure function
elsewhere (``provider_simulator.cache_sim.CacheSim.plan``) and this module is
only the glue that performs it, so the wire shape stays testable without a
running server.
"""

from __future__ import annotations

import asyncio
import logging

import grpc

from provider_simulator.cache_sim import GET_RELAY_METHOD, SERVICE_NAME, CacheSim

_log = logging.getLogger(__name__)

_STATUS_BY_NAME = {sc.name: sc for sc in grpc.StatusCode}


def _identity(value: bytes) -> bytes:
    """Pass bytes through untouched, in both directions.

    gRPC accepts ``None`` for a serialiser to mean the same thing, but naming
    the function keeps the intent visible at the registration site.
    """
    return value


def build_generic_handler(sim: CacheSim) -> grpc.GenericRpcHandler:
    """Wrap one cache-sim as the RelayerCache service.

    Every method on the service reaches this handler, not only GetRelay. Only
    GetRelay is SERVED; anything else is recorded and then refused as
    UNIMPLEMENTED, which is what the router would have seen before.

    The recording is the point, and it is why this is a generic handler rather
    than a method table. A method table registers GetRelay alone, so gRPC
    answers any other method itself, this module never runs, and the call record
    stays empty whatever the router did. A test asserting "the router never
    wrote the second tier" against that record is asserting something the record
    cannot contradict -- a check that cannot fail.

    Accepting every method and recording the ones we refuse turns the same
    assertion into evidence: zero writes now means none arrived, rather than
    none could have been seen.

    The refusal itself is unchanged. An unexpected call still fails loudly as
    UNIMPLEMENTED rather than answering something invented.
    """

    async def get_relay(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
        plan = sim.plan(request)

        if plan.sleep_s > 0:
            await asyncio.sleep(plan.sleep_s)

        if plan.action == "abort":
            status = _STATUS_BY_NAME.get(plan.status_code, grpc.StatusCode.UNKNOWN)
            await context.abort(status, plan.message)

        if plan.action == "raw":
            return plan.raw_body or b""

        import json

        return json.dumps(plan.body).encode()

    async def refuse_and_record(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
        """Record the method, then refuse it exactly as gRPC would have."""
        method = context.method() if callable(getattr(context, "method", None)) else ""
        sim.record_other_method(str(method))
        await context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            "this cache serves GetRelay only; the call was recorded",
        )
        return b""

    get_relay_handler = grpc.unary_unary_rpc_method_handler(
        get_relay,
        request_deserializer=_identity,
        response_serializer=_identity,
    )
    refuse_handler = grpc.unary_unary_rpc_method_handler(
        refuse_and_record,
        request_deserializer=_identity,
        response_serializer=_identity,
    )

    class _EveryMethodOnTheService(grpc.GenericRpcHandler):
        """Claims the whole service path, so no method bypasses the recorder."""

        def service(self, handler_call_details):
            path = handler_call_details.method or ""
            if not path.startswith(f"/{SERVICE_NAME}/"):
                return None
            if path == GET_RELAY_METHOD:
                return get_relay_handler
            return refuse_handler

    return _EveryMethodOnTheService()


async def serve(
    sim: CacheSim,
    port: int,
    host: str = "0.0.0.0",
    *,
    stop: "asyncio.Event | None" = None,
) -> None:
    """Run one cache-sim's gRPC listener.

    Without ``stop`` it runs until the process ends, which is what the
    simulator's daemon thread wants. With one, it shuts the server down when the
    event is set.

    ``stop`` exists because stopping the event loop is not enough. The gRPC
    server owns its listening socket, and abandoning this coroutine mid-await
    leaves that socket open for the life of the process — a caller that stopped
    the loop and joined the thread would still find the port accepting
    connections. Only ``server.stop()`` closes it.
    """
    server = grpc.aio.server()
    server.add_generic_rpc_handlers((build_generic_handler(sim),))
    bind = f"[::]:{port}" if host == "0.0.0.0" else f"{host}:{port}"
    server.add_insecure_port(bind)
    _log.info("cache-sim %r bound on %s serving %s", sim.name, bind, GET_RELAY_METHOD)
    await server.start()
    if stop is None:
        await server.wait_for_termination()
        return
    await stop.wait()
    await server.stop(grace=None)


def run_in_thread(sim: CacheSim, port: int, host: str = "0.0.0.0") -> None:
    """Entry point for a daemon thread, matching the provider gRPC listener."""
    asyncio.run(serve(sim, port, host))
