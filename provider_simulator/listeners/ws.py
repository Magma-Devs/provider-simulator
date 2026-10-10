"""The WebSocket listener, the connection object and the subscription registry.

``JsonRpcWsListener`` is the listener of a ``(jsonrpc, ws, port)`` endpoint. It
is a ``JsonRpcListener``: each JSON frame goes through ``serve()``, with the
same chain and the same fault handling as a request of the http endpoint. Two
things are its own. It answers a subscribe frame and an unsubscribe frame from
the registry, and it asks no chain for them (``build_content``). It also
decides the upgrade request that opens a connection (``decide_upgrade``), and
it writes the history row of a refused upgrade.

``WsConnection`` is one connection, as the listener needs it: the queue that
the writer thread of the connection drains, and the ids of the subscriptions
that the connection owns. The socket adapter makes one for each connection,
and it gives it to ``serve()`` in ``RawRequest.connection``.

``WsSubscriptions`` is the registry of the subscriptions. A subscribe frame
registers a subscription with the queue of its connection. ``POST /ws/emit``
pushes an event onto that queue, and the writer thread of the connection sends
it. The registry writes the history row of each pushed event. An unsubscribe
frame of the same connection, or the end of the connection, removes the
subscription. One running simulator has one registry.

The socket adapter in server.py owns the wire: the handshake bytes, the frame
codec, and the reader thread and the writer thread of each connection. This is
the same split as gRPC: this module owns the decision and the state.
"""

import queue
import secrets
import threading
from dataclasses import dataclass, field

import stubs_ws
from provider_simulator import fault_policy
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Provider
from provider_simulator.domain.registry import Registry
from provider_simulator.listeners.base import RawRequest, ServeResult
from provider_simulator.listeners.jsonrpc import JsonRpcListener


@dataclass
class Subscription:
    sub_id: str
    pool: str
    pid: str
    method: str
    out_queue: queue.Queue = field(default_factory=queue.Queue)
    closed: bool = False


@dataclass
class WsConnection:
    """One WebSocket connection, as the listener needs it. One reader thread
    owns the object."""

    # The queue that the writer thread of the connection drains.
    out_queue: queue.Queue
    # The ids of the subscriptions that this connection registered and still owns.
    subscription_ids: set[str] = field(default_factory=set)


def _envelope(method: str) -> str:
    """The envelope of the events of one subscribe method. A method that the
    table does not hold gets the eth envelope."""
    return stubs_ws.SUBSCRIBE_METHODS.get(method, {}).get("envelope", "eth_subscription")


def event_message(sub: Subscription, event: object) -> dict:
    """The JSON message of one pushed event, in the envelope of its subscribe
    method. An event that is not an object is pushed as an empty object."""
    payload = event if isinstance(event, dict) else {}
    return stubs_ws.build_event_frame(_envelope(sub.method), sub.sub_id, payload)


class WsSubscriptions:
    """Thread-safe registry of live WS subscriptions, keyed by subscription id.

    With a ``Registry`` of providers, ``emit`` also writes the history row of
    each pushed event. With none, it writes no row.
    """

    def __init__(self, registry: Registry | None = None) -> None:
        self._registry = registry
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
        ``"unknown"`` (no such / closed subscription), or ``"full"``.

        The queue gets ``frame_of(sub, event)``. With a registry of providers,
        a push that reached the queue then gets one ``success`` row. The row is
        in the history of the provider of the subscription, so a /history read
        shows the push next to the served calls. A push asks no fault policy. A
        full queue writes no row.
        """
        sub = self.get(sub_id)
        if sub is None or sub.closed:
            return "unknown"
        try:
            sub.out_queue.put_nowait(self.frame_of(sub, event))
        except queue.Full:
            return "full"
        if self._registry is None:
            return "emitted"
        try:
            provider = self._registry.provider(sub.pool, sub.pid)
        except KeyError:
            return "emitted"
        ws_endpoint = next((ep for ep in provider.endpoints if ep.transport == "ws"), None)
        provider.log.push(
            f"{_envelope(sub.method)} push",
            "success",
            0,
            interface=ws_endpoint.interface if ws_endpoint else "jsonrpc",
            transport="ws",
            port=ws_endpoint.port if ws_endpoint else 0,
            request_id=sub_id,
            lava_headers={},
        )
        return "emitted"

    def frame_of(self, sub: Subscription, event: object) -> object:
        """What ``emit`` puts on the queue for one event. Default: the event as
        it is. The registry of the socket adapter gives the bytes of a frame."""
        return event

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
    answers a subscribe frame and an unsubscribe frame, and that decides the
    upgrade request."""

    def __init__(self, provider: Provider, endpoint: Endpoint, subscriptions: WsSubscriptions) -> None:
        super().__init__(provider, endpoint)
        self.subscriptions = subscriptions

    def build_content(self, parsed: dict, scenario: dict, request: RawRequest) -> tuple[int, object]:
        """The success content of one frame. A subscribe frame registers a
        subscription on the connection of the request, and an unsubscribe
        frame removes one. The listener asks no chain for these two, so a
        content key of ``responses`` does not reach them. Each other frame
        gets the content of the chain."""
        method = parsed.get("method")
        if method not in stubs_ws.SUBSCRIBE_METHODS and method not in stubs_ws.UNSUBSCRIBE_METHODS:
            return super().build_content(parsed, scenario, request)

        connection = request.connection
        if not isinstance(connection, WsConnection):
            raise ValueError(
                f"the frame {method!r} needs a WsConnection in RawRequest.connection, "
                f"and the request has {connection!r}"
            )
        # The reply has the id of the frame, and null for a frame with no id.
        if method in stubs_ws.SUBSCRIBE_METHODS:
            sub_id = "0x" + secrets.token_hex(16)
            self.subscriptions.register(
                sub_id, self.provider.pool.name, self.provider.pid, method, out_queue=connection.out_queue
            )
            connection.subscription_ids.add(sub_id)
            return 200, {"jsonrpc": "2.0", "id": parsed.get("id"), "result": sub_id}

        # A connection removes only a subscription that it owns. A ``params``
        # of a wrong shape stops the flow here with an error, and the row of
        # the frame stays in_flight. The recorded test
        # test_an_unsubscribe_frame_with_params_of_a_wrong_shape_closes_the_connection_and_leaves_its_row_in_flight
        # holds that defect as it is.
        params = parsed.get("params") or []
        target_id = params[0] if params else None
        removed = False
        if target_id and target_id in connection.subscription_ids and self.subscriptions.unregister(target_id):
            connection.subscription_ids.discard(target_id)
            removed = True
        return 200, {"jsonrpc": "2.0", "id": parsed.get("id"), "result": removed}

    def release(self, connection: WsConnection) -> None:
        """Remove each subscription of a connection that ended."""
        for sub_id in list(connection.subscription_ids):
            self.subscriptions.unregister(sub_id)

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
