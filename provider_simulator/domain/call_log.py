"""Per-provider telemetry: an in-memory ring buffer plus all-time counters.

Kept separate from the scenario config so recording a call never blocks a
config update and vice versa (each has its own lock). The log knows its own
provider identity (pool, pid) from construction; each record names the
endpoint that served it (interface, transport, port) — so a reader never has
to guess which listener a call came from.

record_arrival / finalize exist to survive a cancelled request: the stub is
written the instant the request arrives (before the body is even read), then
filled in when the outcome is known. Callers must invoke finalize from a
``finally`` block — a handler that dies without finalizing leaves the entry
counted as in_flight until the next clear().

Honest boundaries of the counters:
- The ring buffer holds the last ``history_max`` entries; the all-time
  counters are never capped, so under rollover ``total_calls`` exceeds
  ``len(history)`` by design.
- If an in-flight entry is evicted by rollover before finalize, its final
  status still lands in the counters but the row itself is gone from the
  buffer (same trade-off as the legacy simulator).
- get_history() returns sanitized copies (internal bookkeeping keys removed);
  mutating them never affects the log. Given a ``where`` test it copies only
  the rows that pass it.

What a row says about the request itself:
- ``lava_headers`` holds the request headers whose name starts with ``lava-``.
- ``headers`` holds every request header. The log stores them as one compact
  JSON text and decodes the text for a reader: a full log holds up to
  ``history_max`` rows for each provider, and a text costs less memory than a
  dict of small strings.
- ``deadline_remaining_ms`` and ``grpc_status`` are for gRPC rows. They are
  None on a row of any other interface.
"""

import base64
import datetime
import json
import threading
import time
from collections import deque
from collections.abc import Callable

from constants import HISTORY_MAX


def _header_value_as_text(value: object) -> str:
    """A header value that JSON cannot hold, as text. gRPC gives the value of a
    ``-bin`` metadata key as bytes, and the row holds it as base64 text."""
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    return str(value)


def _headers_as_json(headers: dict | None) -> str:
    """Every request header as one compact JSON text."""
    if not headers:
        return "{}"
    return json.dumps(dict(headers), separators=(",", ":"), default=_header_value_as_text)


def decode_row_headers(row: dict) -> None:
    """Give a row of ``get_history(decode_headers=False)`` its ``headers`` as an
    object. Such a row holds the stored JSON text under ``headers``. A row that
    holds the object already stays as it is."""
    text = row.get("headers")
    if isinstance(text, str):
        row["headers"] = json.loads(text)


def _now_fields() -> tuple[float, str]:
    now = time.time()
    stamp = (
        datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.")
        + f"{int(now % 1 * 1000):03d} UTC"
    )
    return now, stamp


class CallLog:
    def __init__(self, pool: str, pid: str, history_max: int = HISTORY_MAX, name: str = "") -> None:
        self._pool = pool
        self._pid = pid
        # What the router calls this provider — EthBackupProvider4. Carried onto
        # every entry so a history record saved into a test report can be read
        # months later without a live simulator to ask what eth-sim:4 was called.
        #
        # Empty when the caller supplied no name, which in practice means a test
        # built this log directly; every provider describing a real deployment
        # comes through build_registry, which always passes it.
        #
        # Empty on purpose rather than a stand-in like "eth-sim:4". The row
        # already carries pool and pid in their own fields, so a stand-in would
        # only repeat them — and it would repeat them in the exact shape of
        # Provider.key, the address the control API accepts, so it would read
        # like a name the router reported. Tests learn which provider served a
        # request by matching the Lava-Provider-Address header against these
        # names, and no real name is ever empty. An empty name therefore cannot
        # be mistaken for a real one; "eth-sim:4" could.
        self._name = name
        self._lock = threading.Lock()
        self._history: deque = deque(maxlen=history_max)
        self._total_calls = 0
        self._calls_by_status: dict[str, int] = {}
        self._reset_generation = 0

    def _new_entry(
        self,
        interface: str,
        transport: str,
        port: int,
        method: str,
        status: str,
        latency_ms: int,
        request_id: int | str | None,
        lava_headers: dict | None,
        headers: dict | None = None,
        deadline_remaining_ms: int | None = None,
    ) -> dict:
        now, stamp = _now_fields()
        return {
            "ts": now,
            "time": stamp,
            "request_id": request_id,
            "method": method,
            "status": status,
            "latency_ms": latency_ms,
            "lava_headers": dict(lava_headers) if lava_headers else {},
            "deadline_remaining_ms": deadline_remaining_ms,
            "grpc_status": None,
            "pool": self._pool,
            "pid": self._pid,
            "name": self._name,
            "interface": interface,
            "transport": transport,
            "port": port,
            "_headers_json": _headers_as_json(headers),
        }

    def record_arrival(
        self,
        interface: str,
        transport: str,
        port: int,
        lava_headers: dict | None = None,
        headers: dict | None = None,
        deadline_remaining_ms: int | None = None,
    ) -> dict:
        """Append an in_flight stub the moment a request arrives; return it for
        finalize(). The returned dict is the log's own bookkeeping object —
        treat it as opaque, pass it back to finalize, never edit it directly.

        ``headers`` is every request header. ``deadline_remaining_ms`` is the
        time that the deadline of a gRPC caller had left at the arrival."""
        entry = self._new_entry(
            interface,
            transport,
            port,
            "*",
            "in_flight",
            0,
            None,
            lava_headers,
            headers=headers,
            deadline_remaining_ms=deadline_remaining_ms,
        )
        with self._lock:
            self._history.append(entry)
            self._total_calls += 1
            self._calls_by_status["in_flight"] = self._calls_by_status.get("in_flight", 0) + 1
            entry["_reset_gen"] = self._reset_generation
        return entry

    def finalize(
        self,
        entry: dict,
        method: str,
        status: str,
        latency_ms: int,
        request_id: int | str | None = None,
    ) -> None:
        """Fill in a stub from record_arrival (or a record from push). Call from
        a ``finally`` block so a dying handler can't leak an in_flight row.
        If the log was cleared in between, the entry is re-appended with a
        fresh timestamp so counters stay consistent and post-clear history
        never carries pre-clear timestamps."""
        with self._lock:
            entry["method"] = method
            entry["latency_ms"] = latency_ms
            if request_id is not None:
                entry["request_id"] = request_id
            if entry.get("_reset_gen") != self._reset_generation:
                # A clear() landed between arrival and now: the stub was evicted
                # and the counters were reset. Re-append (fresh timestamp) and
                # re-count so total_calls and the buffer stay consistent.
                now, stamp = _now_fields()
                entry["ts"] = now
                entry["time"] = stamp
                entry["status"] = status
                entry["_reset_gen"] = self._reset_generation
                self._history.append(entry)
                self._total_calls += 1
                self._calls_by_status[status] = self._calls_by_status.get(status, 0) + 1
                return
            old_status = entry["status"]
            if old_status != status:
                # Drop the old status key when its count hits zero so /stats
                # never shows a lingering "in_flight": 0 after a request finishes.
                remaining = self._calls_by_status.get(old_status, 0) - 1
                if remaining > 0:
                    self._calls_by_status[old_status] = remaining
                else:
                    self._calls_by_status.pop(old_status, None)
                self._calls_by_status[status] = self._calls_by_status.get(status, 0) + 1
                entry["status"] = status

    def set_grpc_status(self, entry: dict, grpc_status: str) -> None:
        """Write the name of the gRPC status on a row of record_arrival.

        It is not a part of finalize(): a row is finalized before the provider
        waits, and the status is handed to the gRPC library after the wait. So
        ``grpc_status`` is None until then, and it stays None for a call whose
        caller went away during the wait."""
        with self._lock:
            entry["grpc_status"] = grpc_status

    def push(
        self,
        method: str,
        status: str,
        latency_ms: int,
        *,
        interface: str,
        transport: str,
        port: int,
        request_id: int | str | None = None,
        lava_headers: dict | None = None,
        headers: dict | None = None,
    ) -> dict:
        """Append a fully-known record directly (paths with no arrival stub).
        Endpoint identity is required — every row names its listener. Returns
        the entry so a caller may still finalize() it later (e.g. to upgrade
        a provisional status); it carries the generation stamp, so finalize
        updates in place instead of double-counting."""
        entry = self._new_entry(
            interface, transport, port, method, status, latency_ms, request_id, lava_headers, headers=headers
        )
        with self._lock:
            entry["_reset_gen"] = self._reset_generation
            self._history.append(entry)
            self._total_calls += 1
            self._calls_by_status[status] = self._calls_by_status.get(status, 0) + 1
        return entry

    def clear(self) -> None:
        with self._lock:
            self._history.clear()
            self._total_calls = 0
            self._calls_by_status = {}
            self._reset_generation += 1

    def stats(self) -> dict:
        with self._lock:
            return {
                "total_calls": self._total_calls,
                "calls_by_status": dict(self._calls_by_status),
                "history_entries": len(self._history),
            }

    def get_history(self, where: Callable[[dict], bool] | None = None, decode_headers: bool = True) -> list[dict]:
        """Sanitized copies of the buffered rows: internal bookkeeping keys
        (underscore-prefixed) are stripped and each row is an independent dict,
        so readers can hold, serialize, or edit the result freely.

        ``where`` keeps a read from copying rows it does not need. It is called
        once for each stored row, under the lock of this log, and only a row for
        which it answers true is copied. It is given the stored row itself,
        bookkeeping keys included, so it must only read the row, must be quick,
        and must not call back into this log (the lock is not reentrant).

        Each copy gets ``headers``, decoded from the stored text. The decode
        runs after the lock is released, so it does not hold up a provider that
        records a call.

        A reader that cuts the list before it uses the rows passes
        ``decode_headers=False``. Each copy then holds the stored JSON text
        under ``headers``, and the reader calls ``decode_row_headers`` on each
        row that it keeps."""
        with self._lock:
            rows = []
            for e in self._history:
                if where is None or where(e):
                    row = {
                        k: (dict(v) if k == "lava_headers" and isinstance(v, dict) else v)
                        for k, v in e.items()
                        if not k.startswith("_")
                    }
                    row["headers"] = e.get("_headers_json", "{}")
                    rows.append(row)
        if decode_headers:
            for row in rows:
                decode_row_headers(row)
        return rows
