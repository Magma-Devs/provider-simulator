"""WebSocket subscription registry.

Per-frame JSON-RPC over WebSocket reuses JsonRpcListener: a WS endpoint is still
``(jsonrpc, ws, port)``, so its chain and fault handling are identical to the
http endpoint. What is WS-specific is the subscription lifecycle —
``eth_subscribe`` registers a subscription with an outbound queue, ``POST
/ws/emit`` pushes an event onto that queue for the connection's writer to send,
and ``eth_unsubscribe`` / close removes it. This registry owns that map, moved
out of server.py's module globals so one running simulator has exactly one place
tracking subscriptions.

The connection handshake, frame codec, and reader/writer threads are the WS
socket adapter's job and land with the server cut-over (the same split as gRPC:
this module owns the decision/state, the adapter owns the wire).
"""

import queue
import threading
from dataclasses import dataclass, field


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
