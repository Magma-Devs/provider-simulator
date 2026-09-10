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

    def refuse_handler_for(path: str) -> grpc.RpcMethodHandler:
        """Build the refusing handler for ONE method path.

        The path is closed over here rather than read from the context inside
        the handler. Neither ``grpc.aio.ServicerContext`` nor the synchronous
        ``grpc.ServicerContext`` exposes a ``method()`` -- checked against grpc
        1.81.1 -- so an earlier version of this that asked the context recorded
        an empty string for every refused call. The method name is the one
        field this record exists to carry, and it was the one field that was
        never real. ``service()`` below already holds the path, so it passes it
        in and nothing has to be discovered at call time.

        Only refused calls build a handler here. GetRelay keeps the one built
        once above, so the serving path is untouched.
        """

        async def refuse_and_record(request: bytes, context: grpc.aio.ServicerContext) -> bytes:
            """Record the method, then refuse it exactly as gRPC would have."""
            sim.record_other_method(path)
            await context.abort(
                grpc.StatusCode.UNIMPLEMENTED,
                "this cache serves GetRelay only; the call was recorded",
            )
            return b""

        return grpc.unary_unary_rpc_method_handler(
            refuse_and_record,
            request_deserializer=_identity,
            response_serializer=_identity,
        )

    get_relay_handler = grpc.unary_unary_rpc_method_handler(
        get_relay,
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
            return refuse_handler_for(path)

    return _EveryMethodOnTheService()


class CacheSimUnreachable(RuntimeError):
    """The probe call never reached the cache-sim's handler.

    Nothing listening, a deadline, a dropped connection. It says nothing about
    the call record, which is exactly why it must not be reported as a success.
    """


class CacheSimDidNotRefuse(RuntimeError):
    """The cache-sim ANSWERED a method it must refuse.

    A cache-sim that serves a write is broken, and every test that trusts its
    record is measuring something else.
    """


NON_READ_PROBE_METHOD = f"/{SERVICE_NAME}/SetRelay"
"""The method a router would use if it ever wrote this tier.

Named after the real write so the probe below exercises the case the test
cares about, rather than an invented method no implementation would send.
"""


def send_non_read(
    port: int,
    *,
    method: str = NON_READ_PROBE_METHOD,
    host: str = "127.0.0.1",
    timeout_s: float = 5.0,
) -> str:
    """Send one non-read call to a cache-sim's own listener, and expect refusal.

    This is the positive control for the call record. A test that asserts "the
    router never wrote this tier" is reading an absence, and an absence proves
    nothing unless a write WOULD have shown up. Calling this first puts a real
    non-read call through the real listener, so the record can be checked for
    it before the absence is trusted.

    It goes over a socket rather than invoking the handler in-process, on
    purpose: what it exercises is then the whole path a router's write would
    take -- gRPC's own dispatch, ``_EveryMethodOnTheService.service`` claiming
    the path, and the handler recording it. An in-process call would skip the
    first two, which are exactly where the previous version of this listener
    lost every non-read call.

    Returns ``"UNIMPLEMENTED"``, and only that. Every other outcome raises,
    because every other outcome means the control did NOT establish what it
    claims.

    The distinction that matters is between a call that was REFUSED and a call
    that never arrived. Both raise ``grpc.RpcError``, and an earlier version of
    this returned the status name for either -- so dialling a port with nothing
    listening answered ``"UNAVAILABLE"`` and every caller read it as success.
    The one job of this function is to prove a call reached the handler, so the
    one status that proves it is the only one it accepts.

    Raises:
        CacheSimDidNotRefuse: if the cache-sim ANSWERED the write. That is a
            broken simulator, and it would make every test built on this
            meaningless.
        CacheSimUnreachable: if the call failed any other way -- nothing
            listening, a deadline, a broken connection. The call never reached
            the recorder, so nothing was proven about the recorder.
    """
    with grpc.insecure_channel(f"{host}:{port}") as channel:
        try:
            channel.unary_unary(method)(b"", timeout=timeout_s)
        except grpc.RpcError as exc:
            code = exc.code()
            if code is grpc.StatusCode.UNIMPLEMENTED:
                return code.name
            raise CacheSimUnreachable(
                f"the call to {method!r} on port {port} did not reach the "
                f"cache-sim's handler: it failed with "
                f"{code.name if code is not None else 'an unknown status'}. "
                f"Only UNIMPLEMENTED means the listener saw the call and "
                f"refused it. Anything else means the call never arrived, so "
                f"nothing has been proven about the call record."
            ) from exc
    raise CacheSimDidNotRefuse(
        f"the cache-sim on port {port} ANSWERED {method!r} instead of refusing it. "
        f"This listener must serve {GET_RELAY_METHOD!r} and nothing else."
    )


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
