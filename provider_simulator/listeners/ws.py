"""The WebSocket listener and the subscription registry.

``JsonRpcWsListener`` is the listener of a ``(jsonrpc, ws, port)`` endpoint. It
is a ``JsonRpcListener``: a frame gets the same chain and the same fault
handling as a request of the http endpoint. It also decides the upgrade request
that opens a connection (``decide_upgrade``), and it writes the history row of
a refused upgrade.

``WsSubscriptions`` is the registry of the subscriptions. ``eth_subscribe``
registers a subscription with an outbound queue. ``POST /ws/emit`` pushes an
event onto that queue, and the writer thread of the connection sends it.
``eth_unsubscribe`` or the end of the connection removes the subscription. One
running simulator has one registry.

The socket adapter in server.py owns the wire: the handshake bytes, the frame
codec, and the reader thread and the writer thread of each connection. This is
the same split as gRPC: this module owns the decision and the state.
"""

import queue
import threading
from dataclasses import dataclass, field

from provider_simulator import fault_policy
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Provider
from provider_simulator.listeners.base import ServeResult
from provider_simulator.listeners.jsonrpc import JsonRpcListener


@dataclass
class Subscription:
    sub_id: str
    pool: str
    pid: str
    method: str
    out_queue: queue.Queue = field(default_factory=queue.Queue)
    closed: bool = False


class WsSubscriptions:
    """Thread-safe registry of live WS subscriptions, keyed by subscription id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, Subscription] = {}

    def register(
        self,
        sub_id: str,
        pool: str,
        pid: str,
        method: str,
        out_queue: queue.Queue | None = None,
    ) -> Subscription:
        sub = Subscription(
            sub_id=sub_id,
            pool=pool,
            pid=pid,
            method=method,
            out_queue=out_queue if out_queue is not None else queue.Queue(),
        )
        with self._lock:
            self._subs[sub_id] = sub
        return sub

    def get(self, sub_id: str) -> Subscription | None:
        with self._lock:
            return self._subs.get(sub_id)

    def unregister(self, sub_id: str) -> bool:
        with self._lock:
            sub = self._subs.pop(sub_id, None)
        if sub is None:
            return False
        sub.closed = True
        return True

    def emit(self, sub_id: str, event: object) -> str:
        """Push an event to the subscription's queue. Returns ``"emitted"``,
        ``"unknown"`` (no such / closed subscription), or ``"full"``."""
        sub = self.get(sub_id)
        if sub is None or sub.closed:
            return "unknown"
        try:
            sub.out_queue.put_nowait(event)
        except queue.Full:
            return "full"
        return "emitted"

    def list(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "subscription_id": s.sub_id,
                    "pool": s.pool,
                    "pid": s.pid,
                    "method": s.method,
                    "queue_depth": s.out_queue.qsize(),
                }
                for s in self._subs.values()
            ]

    def clear(self) -> None:
        with self._lock:
            for s in self._subs.values():
                s.closed = True
            self._subs.clear()


class JsonRpcWsListener(JsonRpcListener):
    """The listener of a ``ws`` endpoint: a ``JsonRpcListener`` that also
    decides the upgrade request."""

    def __init__(self, provider: Provider, endpoint: Endpoint, subscriptions: WsSubscriptions) -> None:
        super().__init__(provider, endpoint)
        self.subscriptions = subscriptions

    def decide_upgrade(self, headers: dict) -> ServeResult:
        """Decide the upgrade request that opens a WebSocket.

        The status 101 tells the adapter to complete the handshake. Each other
        result is a refusal: a JSON body with its HTTP status, a hang, or a
        drop with its drop point. A refusal writes one complete history row
        before this method returns, with the ``lava-`` headers of ``headers``.
        An upgrade that succeeds writes no row.

        The upgrade is not a request of ``serve()``. It applies no latency, no
        corruption and no per-method override, and it has its own reply
        bodies. Each upgrade uses one count of the ``fail_first_n`` window.
        """
        scenario = self.provider.scenario.snapshot()
        verdict = fault_policy.decide(scenario, self.endpoint, self.provider)
        if verdict.kind == "none":
            return ServeResult(action="respond", status=101)

        lava = {k: v for k, v in headers.items() if k.lower().startswith("lava-")}

        def _record(method: str, status: str) -> None:
            self.provider.log.push(
                method,
                status,
                0,
                interface=self.endpoint.interface,
                transport=self.endpoint.transport,
                port=self.endpoint.port,
                lava_headers=lava,
            )

        if verdict.kind == "down":
            # The provider is dead before it reads anything: the method is "*".
            _record("*", "down")
            return ServeResult(action="respond", status=503, body={"error": "provider down"})
        if verdict.kind == "rate_limit":
            _record("ws_upgrade", "rate_limit")
            return ServeResult(action="respond", status=429, body={"error": "rate limited"})
        if verdict.kind == "error":
            # 200-without-101 is non-spec for WS upgrades; 4xx is the cleanest
            # "upgrade refused" a client can read.
            _record("ws_upgrade", "error")
            message = scenario.get("error_message", "Internal error")
            return ServeResult(action="respond", status=400, body={"error": message})
        if verdict.kind == "hang":
            _record("ws_upgrade", "hang")
            return ServeResult(action="hang")
        _record("ws_upgrade", "drop_connection")
        return ServeResult(action="drop", drop_at=verdict.drop_at)
