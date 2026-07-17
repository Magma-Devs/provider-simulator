"""Per-provider telemetry: an in-memory ring buffer plus all-time counters.

Kept separate from the scenario config so recording a call never blocks a
config update and vice versa (each has its own lock). Every entry carries the
full identity of the listener that served it — pool, provider id, interface,
transport, port — so a reader never has to guess which listener a call came
from.

record_arrival / finalize exist to survive a cancelled request: the stub is
written the instant the request arrives (before the body is even read), then
filled in when the outcome is known. If the log is cleared between those two
moments, finalize re-appends the entry so the "one counter per row" invariant
holds.
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
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._history: deque = deque(maxlen=HISTORY_MAX)
        self._total_calls = 0
        self._calls_by_status: dict[str, int] = {}
        self._reset_generation = 0

    def record_arrival(
        self,
        pool: str,
        pid: str,
        interface: str,
        transport: str,
        port: int,
        lava_headers: dict | None = None,
    ) -> dict:
        now, stamp = _now_fields()
        entry = {
            "ts": now,
            "time": stamp,
            "request_id": None,
            "method": "*",
            "status": "in_flight",
            "latency_ms": 0,
            "lava_headers": lava_headers or {},
            "pool": pool,
            "pid": pid,
            "interface": interface,
            "transport": transport,
            "port": port,
        }
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
        request_id: object = None,
        lava_headers: dict | None = None,
    ) -> None:
        with self._lock:
            if entry.get("_reset_gen") != self._reset_generation:
                # A clear() landed between arrival and now: the stub was evicted
                # and the counters were reset. Re-append so counts stay consistent.
                entry["method"] = method
                entry["latency_ms"] = latency_ms
                entry["status"] = status
                if request_id is not None:
                    entry["request_id"] = request_id
                if lava_headers is not None:
                    entry["lava_headers"] = lava_headers
                entry["_reset_gen"] = self._reset_generation
                self._history.append(entry)
                self._total_calls += 1
                self._calls_by_status[status] = self._calls_by_status.get(status, 0) + 1
                return
            old_status = entry["status"]
            entry["method"] = method
            entry["latency_ms"] = latency_ms
            if request_id is not None:
                entry["request_id"] = request_id
            if lava_headers is not None:
                entry["lava_headers"] = lava_headers
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
        request_id: object = None,
        lava_headers: dict | None = None,
        pool: str | None = None,
        pid: str | None = None,
        interface: str | None = None,
        transport: str | None = None,
        port: int | None = None,
    ) -> None:
        now, stamp = _now_fields()
        with self._lock:
            self._history.append(
                {
                    "ts": now,
                    "time": stamp,
                    "request_id": request_id,
                    "method": method,
                    "status": status,
                    "latency_ms": latency_ms,
                    "lava_headers": lava_headers or {},
                    "pool": pool,
                    "pid": pid,
                    "interface": interface,
                    "transport": transport,
                    "port": port,
                }
            )
            self._total_calls += 1
            self._calls_by_status[status] = self._calls_by_status.get(status, 0) + 1

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
        with self._lock:
            return list(self._history)
