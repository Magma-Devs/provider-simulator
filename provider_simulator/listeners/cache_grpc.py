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
    """Wrap one cache-sim as the RelayerCache service's GetRelay method.

    Only GetRelay is registered. The other five methods the service declares are
    unreachable on the secondary path -- the router holds a secondary behind a
    read-only interface -- so leaving them out makes an unexpected call fail
    loudly as UNIMPLEMENTED rather than answer something invented.
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

    handler = grpc.unary_unary_rpc_method_handler(
        get_relay,
        request_deserializer=_identity,
        response_serializer=_identity,
    )
    return grpc.method_handlers_generic_handler(SERVICE_NAME, {"GetRelay": handler})


async def serve(sim: CacheSim, port: int, host: str = "0.0.0.0") -> None:
    """Run one cache-sim's gRPC listener until the process ends."""
    server = grpc.aio.server()
    server.add_generic_rpc_handlers((build_generic_handler(sim),))
    bind = f"[::]:{port}" if host == "0.0.0.0" else f"{host}:{port}"
    server.add_insecure_port(bind)
    _log.info("cache-sim %r bound on %s serving %s", sim.name, bind, GET_RELAY_METHOD)
    await server.start()
    await server.wait_for_termination()


def run_in_thread(sim: CacheSim, port: int, host: str = "0.0.0.0") -> None:
    """Entry point for a daemon thread, matching the provider gRPC listener."""
    asyncio.run(serve(sim, port, host))
