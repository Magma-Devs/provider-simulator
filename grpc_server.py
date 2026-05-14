"""
grpc_server.py — gRPC server bootstrap for the provider simulator (MAG-1780).

Three independent gRPC servers (ports 18548 / 18549 / 18550) sit alongside
the existing JSON-RPC ``HTTPServer`` instances. Each gRPC server shares the
same ``ProviderState`` object that backs its sibling JSON-RPC server on
18545 / 18546 / 18547 — so a single ``POST /scenario`` reconfigures both
transports for the same logical provider.

The grpc.aio runtime is asyncio-based, so each server runs inside its own
``asyncio`` event loop on a dedicated daemon thread. This mirrors the
JSON-RPC ``HTTPServer.serve_forever`` pattern (one server, one thread,
daemonised) — see ``server.py::main`` for the JSON-RPC side.

The bind address is ``[::]:<port>`` (IPv4 + IPv6 dual-stack) on the
``insecure_port`` because the simulator is a black-box test fixture, not
production — TLS is the cluster ingress's job, not the simulator's.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import grpc

# Touch the package to splice cosmos_pb2/ onto sys.path so the generated
# stub absolute imports work.
import cosmos_pb2  # noqa: F401

from cosmos.base.tendermint.v1beta1 import query_pb2_grpc

from handlers_grpc import CosmosBaseTendermintServicer

if TYPE_CHECKING:
    from server import ProviderState


_log = logging.getLogger(__name__)


async def serve_grpc(port: int, state: "ProviderState") -> None:
    """Start one gRPC server on ``port`` against ``state``; block until cancelled.

    Uses grpc.aio so the servicer methods can ``await asyncio.sleep`` for
    latency / hang injection without parking a thread per request. The
    server is created with default options — no max-concurrency cap; the
    simulator is single-tenant and the event loop schedules cooperatively.
    """
    server = grpc.aio.server()
    query_pb2_grpc.add_ServiceServicer_to_server(
        CosmosBaseTendermintServicer(state), server
    )
    bind = f"[::]:{port}"
    server.add_insecure_port(bind)
    _log.info("grpc provider bound on %s", bind)
    await server.start()
    await server.wait_for_termination()


def run_grpc_in_thread(port: int, state: "ProviderState") -> None:
    """Entry point for ``threading.Thread(target=run_grpc_in_thread, ...)``.

    Wraps ``asyncio.run(serve_grpc(...))`` so the caller (``server.py::main``)
    can spawn a daemon thread per port without each call site duplicating
    the loop-creation boilerplate. The thread exits naturally when
    ``serve_grpc`` raises or the process tears down (daemon=True).
    """
    asyncio.run(serve_grpc(port, state))
