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
  mutating them never affects the log.
"""

import datetime
import threading
import time
from collections import deque

from constants import HISTORY_MAX


def _now_fields() -> tuple[float, str]:
    now = time.time()
    stamp = (
        datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.")
        + f"{int(now % 1 * 1000):03d} UTC"
    )
    return now, stamp


class CallLog:
    def __init__(self, pool: str, pid: str, history_max: int = HISTORY_MAX) -> None:
        self._pool = pool
        self._pid = pid
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
    ) -> dict:
        now, stamp = _now_fields()
        return {
            "ts": now,
            "time": stamp,
            "request_id": request_id,
            "method": method,
            "status": status,
            "latency_ms": latency_ms,
            "lava_headers": lava_headers or {},
            "pool": self._pool,
            "pid": self._pid,
            "interface": interface,
            "transport": transport,
            "port": port,
        }

    def record_arrival(
        self,
        interface: str,
        transport: str,
        port: int,
        lava_headers: dict | None = None,
    ) -> dict:
        """Append an in_flight stub the moment a request arrives; return it for
        finalize(). The returned dict is the log's own bookkeeping object —
        treat it as opaque, pass it back to finalize, never edit it directly."""
        entry = self._new_entry(interface, transport, port, "*", "in_flight", 0, None, lava_headers)
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
    ) -> dict:
        """Append a fully-known record directly (paths with no arrival stub).
        Endpoint identity is required — every row names its listener. Returns
        the entry so a caller may still finalize() it later (e.g. to upgrade
        a provisional status); it carries the generation stamp, so finalize
        updates in place instead of double-counting."""
        entry = self._new_entry(
            interface, transport, port, method, status, latency_ms, request_id, lava_headers
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

    def get_history(self) -> list[dict]:
        """Sanitized copies of the buffered rows: internal bookkeeping keys
        (underscore-prefixed) are stripped and each row is an independent dict,
        so readers can hold, serialize, or edit the result freely."""
        with self._lock:
            return [{k: v for k, v in e.items() if not k.startswith("_")} for e in self._history]
