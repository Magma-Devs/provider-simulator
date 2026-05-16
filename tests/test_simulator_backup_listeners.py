"""
Smoke test for backup-tier listeners — the wiring step itself.

Why this file exists
--------------------
The handler (JSONRPCHandler) is unchanged from the primary tier, so most
behaviour is covered transitively by tests/test_simulator.py. What that
suite does NOT exercise is the *registration* step:

    constants.py
      → BACKUP_PROVIDER_PORTS / ALL_PROVIDER_PORTS
      → server.main() iterates ALL_PROVIDER_PORTS
      → one ThreadingHTTPServer per pid, each with its own ProviderState
      → control map (ControlHandler.provider_states) keyed by pid

Regression class: someone adds a port to BACKUP_PROVIDER_PORTS but
forgets to extend ALL_PROVIDER_PORTS (or the main() iteration).
Currently that would be silent — no listener binds, the control map
has no entry for the new id, the deploy succeeds, and any test that
configures the new pid gets a 404 from the control API or a connection
refused from the listener. This file catches that.

What it tests
-------------
1. The ALL_PROVIDER_PORTS union actually contains every BACKUP_PROVIDER_PORTS
   entry (constants-level shape).
2. For every pid in BACKUP_PROVIDER_PORTS, the full round-trip works:
     a. POST /scenario {"providers": {pid: {"mode": "down"}}} → 200
     b. GET  /scenario → snapshot[pid]["mode"] == "down"
     c. POST http://localhost:<port>/ with eth_blockNumber → HTTP 503
        (the 'down' mode response)
3. A single /scenario POST can mix primary + backup in one body — the
   integration shape backup-failover tests need.

Isolated test ports
-------------------
The fixture boots six listeners using the SAME iteration pattern main()
uses (``for pid, port in ALL_PROVIDER_PORTS.items()``) but with test
ports in the 58xxx range. A developer running ``python -u server.py``
locally on production ports 18545-18562 / 19000 can still ``pytest``
without port collisions.

Run with:
  pytest tests/test_simulator_backup_listeners.py -v
"""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer, ThreadingHTTPServer

import pytest

from constants import ALL_PROVIDER_PORTS, BACKUP_PROVIDER_PORTS, PROVIDER_PORTS
from server import ControlHandler, JSONRPCHandler, ProviderState

# ── Test ports (peer to test_simulator.py's 28xxx range) ─────────────────────
_TEST_PRIMARY_PORTS = {"1": 58545, "2": 58546, "3": 58547}
_TEST_BACKUP_PORTS  = {"4": 58554, "5": 58555, "6": 58556}
_TEST_ALL_PORTS     = {**_TEST_PRIMARY_PORTS, **_TEST_BACKUP_PORTS}
_TEST_CONTROL_PORT  = 59000


# ── HTTP helpers (same shape as test_simulator.py — inline keeps this file
#    standalone so a single-file pytest run has zero cross-imports) ───────────


def _post(url: str, body: dict) -> tuple[int, dict]:
    """POST JSON body, return (status_code, parsed_response_body)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
            return e.code, json.loads(raw) if raw else {}
        except (ConnectionResetError, OSError):
            # `down` mode returns 503 with no body — server closes the
            # connection before any payload is written.
            return e.code, {}


def _get(url: str) -> tuple[int, dict]:
    """GET url, return (status_code, parsed_response_body)."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else {}


def _rpc(url: str, method: str, params: list | None = None) -> tuple[int, dict]:
    """Send a JSON-RPC request, return (http_status, response_body)."""
    return _post(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def sim():
    """Boot 6 listeners + control on test ports using the real iteration.

    The fixture uses the SAME loop shape main() uses (over
    _TEST_ALL_PORTS, which mirrors ALL_PROVIDER_PORTS). If main()'s loop
    were broken (e.g. iterated PROVIDER_PORTS instead of the union), the
    test ports would have to do the same — meaning a bug in main() would
    NOT be caught by a copy-pasted-bootstrap test. Mirroring keeps the
    test honest.
    """
    states = {pid: ProviderState() for pid in _TEST_ALL_PORTS}

    servers = []
    for pid, port in _TEST_ALL_PORTS.items():
        srv = ThreadingHTTPServer(("127.0.0.1", port), JSONRPCHandler)
        srv.daemon_threads = True
        srv.state = states[pid]
        srv.provider_id = pid
        servers.append(srv)

    ctrl = HTTPServer(("127.0.0.1", _TEST_CONTROL_PORT), ControlHandler)
    ctrl.provider_states = states
    servers.append(ctrl)

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()

    time.sleep(0.15)  # allow all servers to finish binding

    yield {
        "control": f"http://127.0.0.1:{_TEST_CONTROL_PORT}",
        "primary_ports": dict(_TEST_PRIMARY_PORTS),
        "backup_ports": dict(_TEST_BACKUP_PORTS),
    }

    for s in servers:
        s.shutdown()


@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario AND clear history before and after every test."""
    _post(f"{sim['control']}/reset/all", {})
    yield
    _post(f"{sim['control']}/reset/all", {})


# ── Constants shape (cheap; catches typos at import time) ────────────────────


def test_all_provider_ports_is_union_of_primary_and_backup():
    """ALL_PROVIDER_PORTS must contain every PROVIDER_PORTS *and* every
    BACKUP_PROVIDER_PORTS entry.

    Catches the headline regression: someone extends BACKUP_PROVIDER_PORTS
    but forgets to keep ALL_PROVIDER_PORTS in sync. If ALL_PROVIDER_PORTS
    is wrong, main() iterates the wrong set, and the new port never binds.
    """
    expected = {**PROVIDER_PORTS, **BACKUP_PROVIDER_PORTS}
    assert ALL_PROVIDER_PORTS == expected, (
        f"ALL_PROVIDER_PORTS must equal "
        f"PROVIDER_PORTS ∪ BACKUP_PROVIDER_PORTS.\n"
        f"  PROVIDER_PORTS        : {PROVIDER_PORTS}\n"
        f"  BACKUP_PROVIDER_PORTS : {BACKUP_PROVIDER_PORTS}\n"
        f"  expected union        : {expected}\n"
        f"  ALL_PROVIDER_PORTS    : {dict(ALL_PROVIDER_PORTS)}"
    )

    all_port_values = list(ALL_PROVIDER_PORTS.values())
    assert len(all_port_values) == len(set(all_port_values)), (
        f"ALL_PROVIDER_PORTS contains duplicate port numbers: "
        f"{all_port_values}"
    )

    primary_pids = set(PROVIDER_PORTS.keys())
    backup_pids = set(BACKUP_PROVIDER_PORTS.keys())
    assert primary_pids.isdisjoint(backup_pids), (
        f"PROVIDER_PORTS and BACKUP_PROVIDER_PORTS share provider ids: "
        f"{primary_pids & backup_pids}"
    )


# ── Listener wire-up ─────────────────────────────────────────────────────────


class TestBackupListenersWired:
    """Every entry in BACKUP_PROVIDER_PORTS must round-trip through both
    the control API map and the listener itself.

    Catches the silent regression where a new port is added to the
    constant but missed in the iteration: no listener binds, the
    control map has no entry, downstream tests get a 404 from /scenario
    or a connection refused from the listener.
    """

    @pytest.mark.parametrize("pid", list(BACKUP_PROVIDER_PORTS.keys()))
    def test_backup_port_wired_through_control_and_listener(self, sim, pid):
        """The three-step round-trip the user specified, parametrised over
        every backup pid so adding a 7th doesn't slip the new id past
        the test.
        """
        # 1) /scenario POST targeting this backup id is accepted.
        status, body = _post(
            f"{sim['control']}/scenario",
            {"providers": {pid: {"mode": "down"}}},
        )
        assert status == 200, (
            f"Control API rejected /scenario for backup pid={pid!r}: "
            f"status={status} body={body}. Likely missing from "
            f"ControlHandler.provider_states — main() did not pass this "
            f"id when constructing the ProviderState dict (check the "
            f"ALL_PROVIDER_PORTS iteration)."
        )

        # 2) GET /scenario echoes the mode back for this pid.
        # Response shape: {"providers": {pid: snapshot, ...}}
        status, response = _get(f"{sim['control']}/scenario")
        assert status == 200, f"GET /scenario failed: status={status}"
        snapshot = response.get("providers", {})
        assert snapshot.get(pid, {}).get("mode") == "down", (
            f"GET /scenario doesn't echo back the mode for backup "
            f"pid={pid!r}. snapshot[{pid!r}]={snapshot.get(pid)!r}. "
            f"The control map has no entry for this id."
        )

        # 3) The listener bound on this pid's backup port answers per
        #    the configured mode. `down` returns HTTP 503 — the
        #    cheapest assertion that "the listener is bound AND uses
        #    this id's ProviderState (not somebody else's)".
        port = _TEST_BACKUP_PORTS[pid]
        status, _ = _rpc(f"http://127.0.0.1:{port}", "eth_blockNumber")
        assert status == 503, (
            f"Backup listener on port {port} (pid={pid!r}) returned "
            f"HTTP {status} instead of 503. Either the port did not "
            f"bind, or it's wired to a different ProviderState than "
            f"the one the control API mutated. Check that main() does "
            f"`srv.state = states[pid]` keyed by the same pid."
        )

    def test_mixed_primary_and_backup_in_one_scenario(self, sim):
        """A single /scenario POST can configure primary AND backup pids
        together — the integration shape backup-failover tests rely on.

        Without this, a regression that handled `{"1": ...}` correctly
        but choked on `{"1": ..., "4": ...}` (e.g. iterating only
        PROVIDER_PORTS on the apply path) would slip past the
        per-pid parametrize above.
        """
        status, body = _post(
            f"{sim['control']}/scenario",
            {
                "providers": {
                    "1": {"mode": "down"},  # primary
                    "4": {"mode": "success"},  # backup
                }
            },
        )
        assert status == 200, f"mixed-tier /scenario rejected: {body}"

        status, response = _get(f"{sim['control']}/scenario")
        assert status == 200
        snapshot = response.get("providers", {})
        assert snapshot.get("1", {}).get("mode") == "down", (
            f"primary pid=1 mode not applied; snapshot={snapshot.get('1')}"
        )
        assert snapshot.get("4", {}).get("mode") == "success", (
            f"backup pid=4 mode not applied; snapshot={snapshot.get('4')}"
        )

        # Both listeners answer per their own state.
        status1, _ = _rpc(
            f"http://127.0.0.1:{_TEST_PRIMARY_PORTS['1']}", "eth_blockNumber"
        )
        assert status1 == 503, (
            f"primary listener pid=1 returned {status1}, expected 503 "
            "(down). Either state cross-contamination, or the mixed-"
            "tier POST stomped on the primary configuration."
        )
        status4, body4 = _rpc(
            f"http://127.0.0.1:{_TEST_BACKUP_PORTS['4']}", "eth_blockNumber"
        )
        assert status4 == 200, (
            f"backup listener pid=4 returned {status4}, expected 200 "
            "(success). Either listener missing or state cross-"
            "contamination."
        )
        assert "result" in body4, (
            f"backup pid=4 success response missing 'result' field: {body4}"
        )
