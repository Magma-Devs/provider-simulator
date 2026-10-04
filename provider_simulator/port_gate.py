"""One gRPC port that a scenario can close, and a way to wait until it has.

``mode="port_closed"`` is the one fault that is not a reply. Every other mode
decides what to answer a request that arrived; this one stops the gRPC server
of the endpoint, so the listening socket is gone, every open connection is
closed, and a new TCP connection is refused. Nothing arrives, so nothing is
recorded in the provider's history.

Three parties meet here, and each owns one thing:

- **The scenario says what is wanted.** ``wants_closed()`` reads it through
  ``fault_policy.port_closed``. Nothing else stores the wish, so a change that
  never passed through the control API (the scenario time-to-live sweep) is
  seen like any other.
- **The serve loop owns the server.** It runs on the port's own thread and
  event loop (``_run_grpc_in_thread`` in server.py) and is the only code that
  stops or starts the server. It works in passes: read the wish, act, report.
  It runs a pass when it is woken and also on a short poll, which is what
  reopens the port after the time-to-live sweep.
- **The control API waits.** ``settle()`` asks for a pass and blocks until a
  pass that STARTED after the ask has finished. A report from an older pass is
  not accepted: it describes the port before the scenario changed, and a
  caller that trusted it would be told the port had closed while it was still
  open.

No grpc import here on purpose: the control API imports this module and has to
stay importable on a machine without grpcio.
"""

import socket
import threading
from collections.abc import Callable

from provider_simulator import fault_policy
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Provider


class PortGate:
    def __init__(self, provider: Provider, endpoint: Endpoint, probe_host: str = "127.0.0.1") -> None:
        self.provider = provider
        self.endpoint = endpoint
        self.probe_host = probe_host
        self._cond = threading.Condition()
        self._asked = 0  # passes asked for so far
        self._done = 0  # the newest ask a FINISHED pass had already seen
        self._open = False  # no server until the first pass starts one
        self._wake: Callable[[], object] | None = None

    def wants_closed(self) -> bool:
        """Does the provider's scenario close this port, at the time of the call?"""
        return fault_policy.port_closed(self.provider.scenario.snapshot(), self.endpoint)

    # ── the serve loop's side ────────────────────────────────────────────────
    def attach(self, wake: Callable[[], object]) -> None:
        """Register the thread-safe call that wakes the serve loop."""
        with self._cond:
            self._wake = wake

    def begin_pass(self) -> int:
        """Call BEFORE reading the scenario; hand the result to ``end_pass``.

        The order is the whole guarantee. A caller writes the scenario and then
        asks, so a pass that read the ask counter first and the scenario second
        has seen every write made before that ask.
        """
        with self._cond:
            return self._asked

    def end_pass(self, seen: int, is_open: bool) -> None:
        with self._cond:
            self._done = max(self._done, seen)
            self._open = is_open
            self._cond.notify_all()

    # ── the control API's side ───────────────────────────────────────────────
    def ask(self) -> int:
        """Ask the serve loop for a pass at once. Returns the ticket to wait on."""
        with self._cond:
            self._asked += 1
            ticket = self._asked
            wake = self._wake
        if wake is not None:
            try:
                wake()
            except RuntimeError:
                # The serve loop is gone. The wait below times out and the
                # caller is told the port did not change, which is the truth.
                pass
        return ticket

    def wait(self, ticket: int, want_open: bool, timeout_s: float) -> bool:
        """Block until a pass newer than ``ticket`` left the port as wanted.

        True only when the port is MEASURED in that state: after the serve loop
        reports it, one TCP connection is tried, and it has to be accepted for
        an open port and refused for a closed one. A report is what the loop
        believes; the connection is what a router would meet.
        """
        with self._cond:
            reached = self._cond.wait_for(lambda: self._done >= ticket and self._open == want_open, timeout_s)
        return reached and self.accepts() == want_open

    def accepts(self) -> bool:
        """Does a new TCP connection to the port succeed?"""
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        try:
            return probe.connect_ex((self.probe_host, self.endpoint.port)) == 0
        finally:
            probe.close()
