"""Cache-sim — a simulated read-only secondary cache for the smart router.

The router can consult a second cache when its own produces no answer, before it
falls through to upstream nodes. It treats that second cache as untrusted: it
strips the writer's signature, keeps only two of the entry's headers, and refuses
the entry's view of the chain head.

None of those rules can be exercised against a real cache, because a real cache
holds only what a real router wrote into it. This one holds whatever a test
chooses, which is the entire reason it exists. Faithful storage is not a goal and
is not attempted -- the router repository already ships a two-zone lab that runs
a real cache for that.

What the router sees
--------------------
One gRPC call, on the same service path a real cache serves::

    /smartrouter.pairing.RelayerCache/GetRelay

The message bodies are JSON rather than protobuf. That is the router's own
choice, recorded in ``smart-router/types/relay/proto_compat.go``: every cache
message's ``Marshal`` is ``json.Marshal``, and gRPC's default codec delegates to
it. So this module needs no ``.proto`` file and no generated stubs -- it reads
and writes JSON, and the field names below are the Go structs' JSON tags.

Only ``GetRelay`` is served. The service declares six methods, but the router
holds a secondary behind a read-only interface offering "is it up?" and "get an
entry", and "is it up?" is answered by the connection having been established
(``protocol/performance/cache.go``, ``CacheActive``) rather than by a call. The
other five are unreachable on this path.

How a test drives it
--------------------
A test never names the key an entry is filed under. A real key carries a hash the
router mints from the request data, which Python cannot recompute, so a test that
had to name it would get a clean miss whenever it named it wrongly -- and a clean
miss is indistinguishable from a passing test. Instead a test stages a *rule*
("the next lookup gets this entry") and reads back the key the router actually
asked under, which is also how a key-mismatch fault becomes visible.

Every incoming call is recorded. That record matters more than it looks: both
cache tiers answer with the same headers, so nothing in the response says which
one served. A test proving the secondary was reached needs this log, or the
router's own per-tier counter, or both.
"""

from __future__ import annotations

import base64
import binascii
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from constants import HISTORY_MAX

# The full method path the router calls. Anything else is unimplemented.
GET_RELAY_METHOD = "/smartrouter.pairing.RelayerCache/GetRelay"
SERVICE_NAME = "smartrouter.pairing.RelayerCache"

# Key prefixes the real cache files entries under, so a recorded key can be
# compared with one. Source: smart-router/ecosystem/cache/core/keys.go.
RELAY_FINALIZED_PREFIX = "rel:f:"
RELAY_TEMP_PREFIX = "rel:t:"

# What the cache-sim does with the next lookup. Mutually exclusive, the same way
# a simulated provider's mode is.
MODES = ("hit", "miss", "error", "hang", "malformed")

DEFAULT_MODE = "miss"

# A hang must outlast the router's secondary-cache-timeout, whose default is
# 50ms. Long enough to exceed a raised one, short enough not to stall a suite.
HANG_SECONDS = 2.0


_UNSET = object()


class UnknownStatus(ValueError):
    """Raised for an error status that is not a gRPC code, or is ``OK``.

    Two failures this closes, both of which produce a green test that measured
    nothing:

    ``OK`` is a valid gRPC code and aborting with it neither answers the call
    nor fails it -- the router waits out its whole budget on every lookup. That
    is a hang wearing the name of an error.

    A misspelt code (``UNAVAILABEL``) would otherwise fall through to
    ``UNKNOWN``. The router treats every gRPC error as a miss, so a test meaning
    to prove "an unavailable cache degrades to a miss" would pass while actually
    exercising a different code. This module already refuses a misspelt mode for
    exactly that reason; a status deserves the same.
    """

    def __init__(self, status: object, allowed: list) -> None:
        super().__init__(
            f"unknown error_status {status!r}; expected a gRPC status code such as "
            f"{', '.join(allowed[:4])} (OK is rejected: aborting with it hangs the call)"
        )


class UnknownMode(ValueError):
    """Raised for a mode outside MODES, naming the ones that exist.

    A typo must fail loudly here. Left to fall through to a default it would
    silently become a miss, and the test built on it would pass having proved
    nothing.
    """

    def __init__(self, mode: object) -> None:
        super().__init__(f"unknown cache-sim mode {mode!r}; expected one of {', '.join(MODES)}")


def grpc_status_names() -> list:
    """Every gRPC status name, or an empty list when grpcio is absent.

    Imported lazily so the offline module keeps working without grpcio, which is
    an optional dependency here. With no grpcio there is nothing to validate
    against, so a status is accepted and the listener -- which needs grpcio to
    run at all -- is never reached.
    """
    try:
        import grpc
    except ImportError:  # pragma: no cover - grpcio is installed in CI
        return []
    return [sc.name for sc in grpc.StatusCode]


def _check_status(status: object) -> None:
    """Refuse a status the router could never act on. See UnknownStatus."""
    names = grpc_status_names()
    if not names:
        return
    if status == "OK" or status not in names:
        raise UnknownStatus(status, [n for n in names if n != "OK"])


def _b64(raw: bytes | str | None) -> str | None:
    """Encode bytes the way Go's encoding/json does: base64, or null when unset.

    Go marshals a nil []byte as null and a present one as a base64 string, so a
    reader on the other side must see exactly that.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.encode()
    return base64.b64encode(raw).decode()


def _list_or_null(items: list[Any]) -> list[Any] | None:
    """Render a list the way Go's encoding/json does: null when it holds nothing.

    The same rule ``_b64`` follows for bytes. Go writes a nil slice as null and
    an empty one as ``[]``, and a cache that stored nothing under a field holds
    a nil slice, so the wire shows null. Every reply a real cache sent in
    ``recorded-2026-09-10.jsonl`` agrees: ``optional_metadata`` and
    ``blocks_hashes_to_heights`` are null in all 307 of them, hits and misses
    alike. We used to send ``[]``, which says "present and empty" -- a different
    statement, and one no real cache made.
    """
    return list(items) if items else None


def _unb64(value: object) -> bytes:
    """Decode a base64 field back to bytes, tolerating null and malformed input.

    A request the router sent is always well formed; this stays forgiving so a
    hand-made probe cannot crash the listener.
    """
    if not isinstance(value, str) or not value:
        return b""
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return b""


@dataclass
class CacheEntry:
    """One entry the cache-sim can hand back, including entries no real writer
    would produce.

    Every field a test needs in order to prove one of the router's trust rules
    is settable here:

    ``sig`` / ``sig_blocks``
        The writer's signatures. The router drops both. A test sets them to
        prove the drop happened.

        Their defaults differ on purpose, and the difference is measured rather
        than chosen. A real cache stores an empty signature and no signature
        blocks, so every hit it sent in ``recorded-2026-09-10.jsonl`` carried
        ``sig: ""`` and ``sig_blocks: null`` -- 26 replies out of 26. Empty is
        not the same statement as absent, and one real message makes both, so
        ``sig`` defaults to empty bytes and ``sig_blocks`` to None. Pass
        ``sig=None`` when a test needs the absent case.
    ``metadata``
        The upstream's response headers, as ``[{"name": ..., "value": ...}]``.
        The router keeps only ``Content-Type`` and ``Content-Encoding`` and
        discards the rest, so a test sets a header outside that pair.
    ``latest_block``
        The other zone's view of the chain head. The router drops it and stamps
        its own tip in its place, so a test sets it far above the local one.
    ``blocks_hashes_to_heights``
        Block-hash-to-height mappings. The router never asks for these, so a
        test sets them to prove the answer is ignored.
    ``is_node_error``
        Marks the payload as a stored node error. The router serves it, labels
        it, and refuses to re-file it as a success.
    ``status_code``
        The status the entry's writer recorded. Zero means the writer recorded
        none, which is a distinct case from 200 and is preserved as such.
    """

    data: bytes | str = b""
    sig: bytes | str | None = b""
    sig_blocks: bytes | str | None = None
    finalized_blocks_hashes: bytes | str | None = None
    latest_block: int = 0
    metadata: list[dict[str, str]] = field(default_factory=list)
    optional_metadata: list[dict[str, str]] = field(default_factory=list)
    seen_block: int = 0
    blocks_hashes_to_heights: list[dict[str, Any]] = field(default_factory=list)
    is_node_error: bool = False
    status_code: int = 0

    def validate(self) -> None:
        """Refuse a field whose type the router cannot read.

        Go decodes the whole reply in one step, so one wrongly-typed field does
        not degrade -- it fails the entire unmarshal, the router logs a miss,
        and the test reads a cold cache instead of the entry it staged. A number
        where a string belongs is caught here rather than three layers later.
        """
        for name in ("data", "sig", "sig_blocks", "finalized_blocks_hashes"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, (bytes, str)):
                raise TypeError(f"entry.{name} must be bytes or str, got {type(value).__name__}")
        for name in ("latest_block", "seen_block", "status_code"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"entry.{name} must be an int, got {type(value).__name__}")
        if not isinstance(self.is_node_error, bool):
            raise TypeError(f"entry.is_node_error must be a bool, got {type(self.is_node_error).__name__}")
        for name in ("metadata", "optional_metadata", "blocks_hashes_to_heights"):
            value = getattr(self, name)
            if not isinstance(value, list):
                raise TypeError(f"entry.{name} must be a list, got {type(value).__name__}")

    @classmethod
    def from_dict(cls, body: dict[str, Any]) -> CacheEntry:
        """Build an entry from a control-API body. Unknown keys REFUSE, loudly.

        A test writes the field names this class uses, not the wire names, so
        the wire shape stays one module's business.

        Two conveniences, because every test that stages a hit thinks in
        JSON-RPC and not in payload bytes:

        ``result``
            Becomes ``data`` carrying the JSON-RPC envelope
            ``{"jsonrpc": "2.0", "id": 0, "result": <value>}``. The router
            rewrites the id on serve, so the staged id does not matter.
        ``error``
            Becomes ``data`` carrying an error envelope, and marks the entry
            ``is_node_error`` unless the body says otherwise. This is the
            stored-node-error shape no real router can write (MAG-2662's
            P2.2), staged in one key.

        Refusals, each naming what to do instead: ``result`` together with
        ``data`` (two payloads, one entry), ``result`` together with
        ``error`` (a reply is one or the other), and any unknown key. The
        silent unknown-key filter this replaces is how MAG-3562 lived:
        every caller staged ``result``, the filter dropped it, ``data``
        stayed empty, and the router served hits with no body while every
        header said otherwise.
        """
        body = dict(body)
        result = body.pop("result", _UNSET)
        error = body.pop("error", _UNSET)
        if result is not _UNSET and error is not _UNSET:
            raise ValueError(
                "entry carries both 'result' and 'error' - a JSON-RPC reply "
                "is one or the other. Stage two entries for two shapes."
            )
        if (result is not _UNSET or error is not _UNSET) and "data" in body:
            raise ValueError(
                "entry carries 'data' beside 'result'/'error' - two payloads "
                "for one entry. Pass raw bytes in 'data' alone, or the "
                "JSON-RPC value alone."
            )
        known = {f for f in cls.__dataclass_fields__}
        unknown = sorted(set(body) - known)
        if unknown:
            raise ValueError(
                f"unknown entry field(s) {unknown!r}; known fields are "
                f"{sorted(known)!r} plus the 'result'/'error' conveniences. "
                f"A silently dropped field is how a staged payload vanishes "
                f"(MAG-3562), so nothing here is ignored."
            )
        entry = cls(**body)
        if result is not _UNSET:
            entry.data = json.dumps({"jsonrpc": "2.0", "id": 0, "result": result})
        elif error is not _UNSET:
            entry.data = json.dumps({"jsonrpc": "2.0", "id": 0, "error": error})
            if "is_node_error" not in body:
                entry.is_node_error = True
        return entry

    def to_cache_relay_reply(self) -> dict[str, Any]:
        """Render this entry as the ``CacheRelayReply`` JSON the router decodes.

        Field names and nesting follow the Go structs' JSON tags in
        ``smart-router/types/relay/cache.go`` and ``relay.go``. Byte fields are
        base64 because that is how Go's encoding/json writes them.
        """
        return {
            "reply": {
                "data": _b64(self.data),
                "sig": _b64(self.sig),
                "latest_block": self.latest_block,
                "finalized_blocks_hashes": _b64(self.finalized_blocks_hashes),
                "sig_blocks": _b64(self.sig_blocks),
                "metadata": list(self.metadata),
            },
            "optional_metadata": _list_or_null(self.optional_metadata),
            "seen_block": self.seen_block,
            "blocks_hashes_to_heights": _list_or_null(self.blocks_hashes_to_heights),
            "is_node_error": self.is_node_error,
            "status_code": self.status_code,
        }


def miss_reply(seen_block: int = 0) -> dict[str, Any]:
    """The answer a real cache gives when it holds nothing for the key.

    The real server does not fail the call on a not-found: its GetRelay handler
    swallows the lookup error and returns a reply whose ``reply`` is absent
    (``ecosystem/cache/handlers.go``). The router reads a hit as "no transport
    error AND a non-nil reply", so this is a miss and not a failure.

    All six fields are sent, because a real cache sends all six. Every one of
    the 281 misses in ``recorded-2026-09-10.jsonl`` carried the full set; this
    used to send ``reply`` alone and nothing else. Go fills a missing field with
    its zero value, so the router decodes either shape the same way today -- but
    the cache-sim is what every test reasons about as a cache, and a shape no
    real cache produces is a fiction a test can come to depend on.

    ``seen_block`` is the CACHE's own view of the chain head, never anything
    taken from the request. The recording shows it is cache-level state: it
    rises over time and hits and misses answered in the same moment carry the
    same value. It stays 0 until the cache has a head, which is what 249 of
    those 281 misses reported.
    """
    return {
        "reply": None,
        "optional_metadata": None,
        "seen_block": int(seen_block),
        "blocks_hashes_to_heights": None,
        "is_node_error": False,
        "status_code": 0,
    }


def relay_key(*, finalized: bool, chain_id: str, request_hash: bytes, block: int) -> str:
    """The key a real cache would file this lookup under.

    Mirrors ``RelayKey`` in ``smart-router/ecosystem/cache/core/keys.go`` so a
    key recorded here can be compared with a real one. The cache-sim does not
    look entries up by it -- it records it, so a test can assert on what the
    router actually asked for.
    """
    prefix = RELAY_FINALIZED_PREFIX if finalized else RELAY_TEMP_PREFIX
    return f"{prefix}{chain_id}:{request_hash.hex()}:{block}"


@dataclass
class CachePlan:
    """What the listener glue should do for one lookup.

    ``action`` is one of ``respond`` (send ``body``), ``abort`` (fail the call
    with ``status_code``), or ``raw`` (send ``raw_body`` unchanged, which is how
    a malformed answer is produced).
    """

    action: str
    body: dict[str, Any] | None = None
    raw_body: bytes | None = None
    status_code: str = "OK"
    message: str = ""
    sleep_s: float = 0.0


@dataclass
class RecordedCall:
    """One lookup the router made, as the cache-sim saw it."""

    method: str
    chain_id: str
    request_hash_hex: str
    requested_block: int
    finalized: bool
    seen_block: int
    shared_state_id: str
    blocks_hashes_to_heights: list[dict[str, Any]]
    key: str
    mode: str
    ts: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "chain_id": self.chain_id,
            "request_hash": self.request_hash_hex,
            "requested_block": self.requested_block,
            "finalized": self.finalized,
            "seen_block": self.seen_block,
            "shared_state_id": self.shared_state_id,
            "blocks_hashes_to_heights": self.blocks_hashes_to_heights,
            "key": self.key,
            "mode": self.mode,
            "ts": self.ts,
        }


class CacheSim:
    """One simulated secondary cache: a staged answer and a log of every call.

    Holds a lock for the same reason a provider's state does -- the gRPC
    listener and the control API run on different threads and a test may stage
    an entry while a lookup is in flight.

    A cache is not a provider. It has no pool and takes no provider slot, so it
    is addressed by its own name and a fault set on a chain's providers can
    never reach it.
    """

    def __init__(self, name: str = "secondary") -> None:
        self.name = name
        self._lock = threading.Lock()
        self._mode: str = DEFAULT_MODE
        self._entry: CacheEntry | None = None
        self._latency_ms: int = 0
        self._error_status: str = "UNAVAILABLE"
        self._error_message: str = "cache-sim: injected error"
        self._malformed_body: bytes = b"{not json"
        # This cache's own view of the chain head, reported on a miss. Cache
        # state, not request state: a real cache answers every lookup with the
        # head IT knows, and 0 until it knows one. Nothing derives it from the
        # request, because a real cache does not either.
        self._seen_block: int = 0
        # Bounded for the same reason every provider's log is: the router
        # queries the secondary on every primary miss, and an unbounded list
        # grows until the pod restarts. Same cap, same env override.
        self._calls: deque = deque(maxlen=HISTORY_MAX)
        # Counts every call ever recorded, and is never evicted. ``_calls`` is
        # capped, so once it is full an append drops the oldest entry and its
        # LENGTH stops changing -- which makes "did the record grow" unanswerable
        # from len() alone on exactly the busy cache-sim where the question
        # matters. This counter answers it. Cleared by reset(), like the log.
        self._records_total = 0

    # ── staging ───────────────────────────────────────────────────────────

    def stage(
        self,
        *,
        mode: str = "hit",
        entry: CacheEntry | dict[str, Any] | None = None,
        latency_ms: int = 0,
        error_status: str | None = None,
        error_message: str | None = None,
        malformed_body: bytes | str | None = None,
        seen_block: int | None = None,
    ) -> None:
        """Decide what the next lookups get. Raises on an unknown mode.

        ``mode="hit"`` with no entry is a caller error rather than an empty
        answer: an entry-less hit would serve a reply with no data and read as a
        cache that answered, which is never what a test means.

        ``seen_block`` sets this cache's own view of the chain head, which a
        miss reports. It persists until it is set again or the cache is reset,
        because it describes the cache rather than the next answer.
        """
        if mode not in MODES:
            raise UnknownMode(mode)
        if mode == "hit" and entry is None:
            raise ValueError("mode='hit' needs an entry; pass entry=... or use mode='miss'")
        if error_status is not None:
            _check_status(error_status)
        if isinstance(entry, dict):
            entry = CacheEntry.from_dict(entry)
        if entry is not None:
            entry.validate()
        if isinstance(malformed_body, str):
            malformed_body = malformed_body.encode()
        with self._lock:
            self._mode = mode
            if entry is not None:
                self._entry = entry
            self._latency_ms = int(latency_ms)
            if error_status is not None:
                self._error_status = error_status
            if error_message is not None:
                self._error_message = error_message
            if malformed_body is not None:
                self._malformed_body = malformed_body
            if seen_block is not None:
                self._seen_block = int(seen_block)

    def reset(self) -> None:
        """Return to answering misses, drop the staged entry and the call log.

        The chain head goes back to 0 with everything else, because a cache that
        has just come up has not seen a head yet, and that is the state a reset
        cache should describe.
        """
        with self._lock:
            self._mode = DEFAULT_MODE
            self._entry = None
            self._latency_ms = 0
            self._calls.clear()
            self._records_total = 0
            self._seen_block = 0

    def record_other_method(self, method: str) -> None:
        """Record a call to a method this cache does not serve.

        The router holds its second tier behind a read-only interface, so a
        write should never arrive. "Should never" is the claim a test wants to
        make, and it can only be made by something that WOULD have seen one.

        A cache that registers GetRelay alone cannot make it: gRPC answers any
        other method itself, the handler never runs, and the record stays empty
        whatever the router did -- so a count of zero there proves nothing. The
        listener therefore accepts every method on the service and calls this
        for the ones it does not serve, before refusing them as UNIMPLEMENTED.

        Only the method name and the time are real here. The other fields
        describe a cache lookup, and this was not one.
        """
        with self._lock:
            self._records_total += 1
            self._calls.append(
                RecordedCall(
                    method=method,
                    chain_id="",
                    request_hash_hex="",
                    requested_block=0,
                    finalized=False,
                    seen_block=0,
                    shared_state_id="",
                    blocks_hashes_to_heights=[],
                    key="",
                    mode=self._mode,
                    ts=time.time(),
                )
            )

    def clear_calls(self) -> None:
        """Drop the call log, keeping whatever is staged."""
        with self._lock:
            self._calls.clear()

    # ── reading ───────────────────────────────────────────────────────────

    def calls(self) -> list[dict[str, Any]]:
        """Every lookup so far, oldest first."""
        with self._lock:
            return [c.as_dict() for c in self._calls]

    def records_total(self) -> int:
        """How many calls have EVER been recorded, lost ones included.

        ``calls()`` is capped at ``HISTORY_MAX`` and drops its oldest entry when
        full, so its length stops growing on a busy cache-sim while calls keep
        arriving. A caller asking "did my call get recorded" by comparing the
        length before and after gets "no" on every cache-sim that has been doing
        real work. This is the number that answers that question.

        Reset by ``reset()`` along with the log, so a test that resets first
        reads both from zero.
        """
        with self._lock:
            return self._records_total

    def call_count(self) -> int:
        """How many LOOKUPS the router has made.

        The number a test asserts on to prove the secondary was asked exactly
        once, or never -- the router must reach it only after its own cache
        misses.

        Counts GetRelay alone. The record also holds calls to methods this
        cache refuses, and those are not lookups: a refused write means the
        router tried to write, which is a different fault from asking twice, and
        adding it here would make both numbers unreadable. Ask ``calls()`` and
        filter on ``method`` for that question.
        """
        with self._lock:
            return sum(1 for c in self._calls if c.method == GET_RELAY_METHOD)

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "mode": self._mode,
                "latency_ms": self._latency_ms,
                "has_entry": self._entry is not None,
                "seen_block": self._seen_block,
                "calls": sum(1 for c in self._calls if c.method == GET_RELAY_METHOD),
                "other_method_calls": sum(1 for c in self._calls if c.method != GET_RELAY_METHOD),
            }

    # ── the decision ──────────────────────────────────────────────────────

    def plan(self, request_body: bytes) -> CachePlan:
        """Record one lookup and decide the answer. Pure apart from the log.

        ``request_body`` is the JSON the router sent. A body that will not parse
        is still recorded -- a lookup that reached us is a lookup, however it
        was shaped -- and answered with a miss, because inventing a hit from a
        request we could not read would be a fiction a test might believe.
        """
        try:
            decoded = json.loads(request_body or b"{}")
            if not isinstance(decoded, dict):
                decoded = {}
        except (ValueError, TypeError):
            decoded = {}

        request_hash = _unb64(decoded.get("request_hash"))
        chain_id = str(decoded.get("chain_id") or "")
        requested_block = int(decoded.get("requested_block") or 0)
        finalized = bool(decoded.get("finalized"))

        with self._lock:
            mode = self._mode
            entry = self._entry
            latency_ms = self._latency_ms
            error_status = self._error_status
            error_message = self._error_message
            malformed_body = self._malformed_body
            seen_block = self._seen_block
            self._records_total += 1
            self._calls.append(
                RecordedCall(
                    method=GET_RELAY_METHOD,
                    chain_id=chain_id,
                    request_hash_hex=request_hash.hex(),
                    requested_block=requested_block,
                    finalized=finalized,
                    seen_block=int(decoded.get("seen_block") or 0),
                    shared_state_id=str(decoded.get("shared_state_id") or ""),
                    blocks_hashes_to_heights=list(decoded.get("blocks_hashes_to_heights") or []),
                    key=relay_key(
                        finalized=finalized,
                        chain_id=chain_id,
                        request_hash=request_hash,
                        block=requested_block,
                    ),
                    mode=mode,
                    ts=time.time(),
                )
            )

        sleep_s = latency_ms / 1000.0

        if mode == "hang":
            # Answer later than the router's budget allows. The router counts
            # the overrun as a miss and goes to its providers, so the answer
            # itself is never read -- a miss body keeps that honest.
            #
            # A staged latency longer than the default wins, so a test that asks
            # for a specific overrun gets it rather than silently getting 2s.
            return CachePlan(
                action="respond",
                body=miss_reply(seen_block),
                sleep_s=max(HANG_SECONDS, sleep_s),
            )
        if mode == "error":
            return CachePlan(
                action="abort",
                status_code=error_status,
                message=error_message,
                sleep_s=sleep_s,
            )
        if mode == "malformed":
            return CachePlan(action="raw", raw_body=malformed_body, sleep_s=sleep_s)
        if mode == "hit" and entry is not None:
            return CachePlan(action="respond", body=entry.to_cache_relay_reply(), sleep_s=sleep_s)
        return CachePlan(action="respond", body=miss_reply(seen_block), sleep_s=sleep_s)


class CacheSimRegistry:
    """The cache-sims this simulator is running, addressed by name.

    Named rather than numbered because a zone-segregated topology has one cache
    per zone, and because a cache has no pool slot to be numbered within.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sims: dict[str, CacheSim] = {}

    def get_or_create(self, name: str) -> CacheSim:
        with self._lock:
            sim = self._sims.get(name)
            if sim is None:
                sim = CacheSim(name)
                self._sims[name] = sim
            return sim

    def get(self, name: str) -> CacheSim | None:
        with self._lock:
            return self._sims.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._sims)

    def reset_all(self) -> None:
        with self._lock:
            sims = list(self._sims.values())
        for sim in sims:
            sim.reset()
