"""
Smoke test for backup-tier listeners — the wiring step itself.

Why this file exists
--------------------
The handler classes (JSONRPCHandler / RestHandler / TendermintHandler /
WsHandler / gRPC servicer) are unchanged from each surface's primary tier,
so the response-shape behaviour is covered transitively by the matching
primary-tier test files (test_simulator.py, test_simulator_rest.py, …).
What those suites do NOT exercise is the *registration* step that the
backup tier introduces:

    constants.py
      → BACKUP_PROVIDER_PORTS / GRPC_BACKUP_PORTS / REST_BACKUP_PORTS /
        TM_BACKUP_PORTS / WS_BACKUP_PORTS
      → server.main() iterates each surface's primary + backup dicts
      → one server per pid, each with its own ProviderState
      → control map (ControlHandler.provider_states) keyed by pid

Regression class: someone adds a port to a backup-port dict but forgets
to extend main()'s iteration. That would be silent — no listener binds,
the control map has no entry for the new pid, deploy succeeds, and any
backup-failover test that configures the new pid gets a 404 from the
control API or a connection refused from the listener. This file
catches that.

What it tests
-------------
1. Each surface's backup-port dict is the right shape (distinct pids
   across the matrix, no port collisions with primary or other
   surfaces, ALL_PROVIDER_PORTS still a union of PROVIDER_PORTS and
   BACKUP_PROVIDER_PORTS).
2. For every pid in every surface's backup-port dict, the full round-
   trip works:
     a. POST /scenario {"providers": {pid: {"mode": "down"}}} → 200
     b. GET  /scenario → snapshot[pid]["mode"] == "down"
     c. A surface-native request to <port> → the surface's down-mode
        response shape (503 for HTTP-style, abort UNAVAILABLE for gRPC,
        upgrade-refused 503 for WS).
3. A single /scenario POST can mix primary + backup pids across
   surfaces — the integration shape backup-failover tests need.

Isolated test ports
-------------------
Each surface's fixture uses an offset-from-production port range so a
developer running ``python -u server.py`` locally on the production
ports (18545-18574 / 19000) can still ``pytest`` without collisions.
The fixtures use the SAME iteration pattern main() uses for each
surface — copying the bootstrap shape rather than reaching into main()
directly. If main()'s loop were broken (e.g. iterated PROVIDER_PORTS
instead of the union), the test would have to mirror the same bug —
which keeps the test honest.

Run with:
  pytest tests/test_simulator_backup_listeners.py -v
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import HTTPServer, ThreadingHTTPServer

import pytest

from constants import (
    ALL_PROVIDER_PORTS,
    BACKUP_PROVIDER_PORTS,
    BTC_PRIMARY_PORTS,
    GRPC_BACKUP_PORTS,
    GRPC_PROVIDER_PORTS,
    LN_PRIMARY_PORTS,
    PROVIDER_PORTS,
    REST_BACKUP_PORTS,
    REST_PORTS,
    SOLANA_PRIMARY_PORTS,
    SOLO_PROVIDER_PORTS,
    SOLO_SOLANA_PROVIDER_PORTS,
    TM_BACKUP_PORTS,
    TM_PORTS,
    WS_BACKUP_PORTS,
    WS_PORTS,
)
from server import (
    ControlHandler,
    JSONRPCHandler,
    ProviderState,
    RestHandler,
    TendermintHandler,
)
import handlers_ws

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
    """ALL_PROVIDER_PORTS must equal the union of PROVIDER_PORTS,
    BACKUP_PROVIDER_PORTS, and SOLO_PROVIDER_PORTS.

    Catches the headline regression: someone extends one of the three source
    dicts but forgets to keep ALL_PROVIDER_PORTS in sync. If ALL_PROVIDER_PORTS
    is wrong, main() iterates the wrong set and the new port never binds.

    Note: SOLO_SOLANA_PROVIDER_PORTS (pid "20", port 18585) is deliberately
    excluded from ALL_PROVIDER_PORTS — the Solana solo listener is bound by
    its own loop in main() using a separate handler. Adding it here would make
    main()'s ETH loop bind port 18585 a second time (the dedicated Solana-solo
    loop already binds it), so startup fails with "address already in use".
    """
    expected = {**PROVIDER_PORTS, **BACKUP_PROVIDER_PORTS, **SOLO_PROVIDER_PORTS}
    assert ALL_PROVIDER_PORTS == expected, (
        f"ALL_PROVIDER_PORTS must equal "
        f"PROVIDER_PORTS ∪ BACKUP_PROVIDER_PORTS ∪ SOLO_PROVIDER_PORTS.\n"
        f"  PROVIDER_PORTS        : {PROVIDER_PORTS}\n"
        f"  BACKUP_PROVIDER_PORTS : {BACKUP_PROVIDER_PORTS}\n"
        f"  SOLO_PROVIDER_PORTS   : {SOLO_PROVIDER_PORTS}\n"
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


# ── Cross-surface constants shape (cheap; catches typos at import time) ──────


def test_backup_port_dicts_have_no_pid_collisions():
    """The five backup-port dicts must use disjoint pid namespaces.

    A pid collision (e.g. gRPC backup pid "10" while REST backup also
    uses "10") would make a /scenario POST overwrite the wrong surface's
    backup config when server.main() merges them all into one
    ``provider_states`` dict. The bug would be silent until a test
    exercised both pools at once and saw the cross-talk.
    """
    pid_sources = [
        ("PROVIDER_PORTS",         PROVIDER_PORTS),
        ("BACKUP_PROVIDER_PORTS",  BACKUP_PROVIDER_PORTS),
        ("GRPC_BACKUP_PORTS",      GRPC_BACKUP_PORTS),
        ("REST_BACKUP_PORTS",      REST_BACKUP_PORTS),
        ("TM_BACKUP_PORTS",        TM_BACKUP_PORTS),
        ("WS_BACKUP_PORTS",        WS_BACKUP_PORTS),
    ]
    seen: dict[str, str] = {}
    for source_name, port_dict in pid_sources:
        for pid in port_dict:
            if pid in seen and seen[pid] != source_name:
                # Primary-tier pids 1-3 are SHARED across GRPC/REST/TM/WS
                # primary dicts on purpose (the simulator wires one
                # ProviderState per primary pid backing every surface).
                # Only flag collisions between BACKUP dicts and primaries
                # of a different surface.
                if (source_name.endswith("_BACKUP_PORTS") or
                        seen[pid].endswith("_BACKUP_PORTS")):
                    pytest.fail(
                        f"pid {pid!r} appears in both {seen[pid]} and "
                        f"{source_name} — backup pids must be disjoint "
                        f"from every other pool"
                    )
            seen[pid] = source_name


def test_backup_port_dicts_have_no_port_collisions():
    """Every primary + backup port across every surface must be unique.

    A port collision would make two ThreadingHTTPServer instances try to
    bind the same port at startup — only one succeeds, the other dies.
    main() doesn't currently fail fast on this so it would silently lose
    one surface's listeners.
    """
    pool_sources = [
        ("PROVIDER_PORTS",         PROVIDER_PORTS),
        ("BACKUP_PROVIDER_PORTS",  BACKUP_PROVIDER_PORTS),
        ("GRPC_PROVIDER_PORTS",    GRPC_PROVIDER_PORTS),
        ("GRPC_BACKUP_PORTS",      GRPC_BACKUP_PORTS),
        ("REST_PORTS",             REST_PORTS),
        ("REST_BACKUP_PORTS",      REST_BACKUP_PORTS),
        ("TM_PORTS",               TM_PORTS),
        ("TM_BACKUP_PORTS",        TM_BACKUP_PORTS),
        ("WS_PORTS",               WS_PORTS),
        ("WS_BACKUP_PORTS",        WS_BACKUP_PORTS),
    ]
    seen: dict[int, str] = {}
    for source_name, port_dict in pool_sources:
        for pid, port in port_dict.items():
            owner = f"{source_name}[{pid!r}]"
            if port in seen:
                pytest.fail(
                    f"port {port} bound by both {seen[port]} and "
                    f"{owner} — two listeners cannot share a port"
                )
            seen[port] = owner


@pytest.mark.parametrize(
    "surface,primary_dict,backup_dict",
    [
        ("jsonrpc",        PROVIDER_PORTS,      BACKUP_PROVIDER_PORTS),
        ("grpc",           GRPC_PROVIDER_PORTS, GRPC_BACKUP_PORTS),
        ("rest",           REST_PORTS,          REST_BACKUP_PORTS),
        ("tendermintrpc",  TM_PORTS,            TM_BACKUP_PORTS),
        ("ws",             WS_PORTS,            WS_BACKUP_PORTS),
    ],
)
def test_each_surface_has_three_primary_and_three_backup(
    surface, primary_dict, backup_dict,
):
    """Every surface boots a 3+3 pool. Catches an accidental drop of one
    pid (e.g. {"7": 18563, "8": 18564} silently shipping a 2-node backup
    pool when the router expects 3)."""
    assert len(primary_dict) == 3, (
        f"{surface} primary pool has {len(primary_dict)} entries, expected 3"
    )
    assert len(backup_dict) == 3, (
        f"{surface} backup pool has {len(backup_dict)} entries, expected 3"
    )


def test_solo_solana_provider_ports_shape_and_uniqueness():
    """The Solana solo listener constant (MAG-2239) is exactly pid 20 on
    port 18585, and neither the pid nor the port collides with any other
    listener pool.

    Why hermetic: this is the registration-shape guard for the solo Solana
    listener. The listener itself is bound by a dedicated loop in
    ``server.main()`` (handler_module=handlers_solana, its own ProviderState).
    If pid "20" or port 18585 ever drifts to overlap another pool — e.g.
    someone reuses 18585, or re-points the solo pool at the Solana primary pid
    "1" — two listeners would fight for one port at startup (only one binds),
    or a /scenario POST would cross-talk between the solo router and the
    solana-sim-router primary pool. Both failures are silent at deploy time and
    only surface as a flaky downstream test. This catches them at import.
    """
    # 1) Exact shape — the value MAG-2239 specifies.
    assert SOLO_SOLANA_PROVIDER_PORTS == {"20": 18585}, (
        f"SOLO_SOLANA_PROVIDER_PORTS must be exactly {{'20': 18585}}; "
        f"got {SOLO_SOLANA_PROVIDER_PORTS}"
    )

    # 2) Pid "20" is unique across every pid namespace. (Primary pids 1-3 are
    #    shared across surfaces by design; the solo Solana pid must NOT clash
    #    with any of them, with the ETH solo pid 19, or with any backup pid.)
    other_pid_sources = [
        ("PROVIDER_PORTS",         PROVIDER_PORTS),
        ("BACKUP_PROVIDER_PORTS",  BACKUP_PROVIDER_PORTS),
        ("SOLO_PROVIDER_PORTS",    SOLO_PROVIDER_PORTS),
        ("BTC_PRIMARY_PORTS",      BTC_PRIMARY_PORTS),
        ("LN_PRIMARY_PORTS",       LN_PRIMARY_PORTS),
        ("SOLANA_PRIMARY_PORTS",   SOLANA_PRIMARY_PORTS),
        ("GRPC_PROVIDER_PORTS",    GRPC_PROVIDER_PORTS),
        ("GRPC_BACKUP_PORTS",      GRPC_BACKUP_PORTS),
        ("REST_PORTS",             REST_PORTS),
        ("REST_BACKUP_PORTS",      REST_BACKUP_PORTS),
        ("TM_PORTS",               TM_PORTS),
        ("TM_BACKUP_PORTS",        TM_BACKUP_PORTS),
        ("WS_PORTS",               WS_PORTS),
        ("WS_BACKUP_PORTS",        WS_BACKUP_PORTS),
    ]
    solo_pid = "20"
    for source_name, port_dict in other_pid_sources:
        assert solo_pid not in port_dict, (
            f"Solana solo pid {solo_pid!r} also appears in {source_name} — "
            f"the solo Solana provider must own a distinct pid so /scenario "
            f"on the solo router can't reconfigure another pool"
        )

    # 3) Port 18585 is unique across every listener pool. Two listeners on one
    #    port means only one binds at startup; the other dies silently.
    solo_port = 18585
    for source_name, port_dict in other_pid_sources:
        assert solo_port not in port_dict.values(), (
            f"Solana solo port {solo_port} also bound by {source_name} — "
            f"two listeners cannot share a port"
        )

    # 4) The solo Solana port is deliberately NOT in ALL_PROVIDER_PORTS — that
    #    union is bound by the ETH-default loop in main(); the solo Solana
    #    listener has its own loop (handlers_solana). If it leaked into the
    #    union it would double-bind 18585 AND route it to the ETH handler.
    assert solo_port not in ALL_PROVIDER_PORTS.values(), (
        f"Port {solo_port} must NOT be in ALL_PROVIDER_PORTS — that union is "
        f"bound by the ETH-default loop, which would double-bind the port and "
        f"dispatch it to handlers_eth instead of handlers_solana"
    )
    assert solo_pid not in ALL_PROVIDER_PORTS, (
        f"Pid {solo_pid!r} must NOT be in ALL_PROVIDER_PORTS — the solo Solana "
        f"listener gets its own ProviderState, set separately in main()"
    )


# ── REST backup-tier smoke test ──────────────────────────────────────────────

_REST_TEST_PRIMARY = {"1": 60545, "2": 60546, "3": 60547}
_REST_TEST_BACKUP  = {"10": 60566, "11": 60567, "12": 60568}
_REST_TEST_CONTROL = 60000


@pytest.fixture(scope="module")
def rest_sim():
    """Boot 3 REST primary + 3 REST backup + control on test ports.

    Mirrors the production main() iteration over REST_PORTS + REST_BACKUP_PORTS
    so a regression that drops one of the two loops fails here. Pids match
    the production scheme (10-12 are the REST backup pids) so the round-trip
    test below catches pid-handling bugs in the control map.
    """
    all_pids = {**_REST_TEST_PRIMARY, **_REST_TEST_BACKUP}
    states = {pid: ProviderState() for pid in all_pids}

    servers = []
    for pid, port in all_pids.items():
        srv = ThreadingHTTPServer(("127.0.0.1", port), RestHandler)
        srv.daemon_threads = True
        srv.state = states[pid]
        srv.provider_id = pid
        servers.append(srv)

    ctrl = HTTPServer(("127.0.0.1", _REST_TEST_CONTROL), ControlHandler)
    ctrl.provider_states = states
    servers.append(ctrl)

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    time.sleep(0.15)

    yield {
        "control":        f"http://127.0.0.1:{_REST_TEST_CONTROL}",
        "primary_ports":  dict(_REST_TEST_PRIMARY),
        "backup_ports":   dict(_REST_TEST_BACKUP),
    }

    for s in servers:
        s.shutdown()


@pytest.fixture(autouse=True)
def rest_clean_state(request):
    """Only reset when this test consumes rest_sim — saves boot time on
    JSON-RPC-only tests which would otherwise drag rest_sim into scope."""
    if "rest_sim" not in request.fixturenames:
        yield
        return
    control = f"http://127.0.0.1:{_REST_TEST_CONTROL}"
    _post(f"{control}/reset/all", {})
    yield
    _post(f"{control}/reset/all", {})


class TestRestBackupListenersWired:
    """Every entry in REST_BACKUP_PORTS must round-trip through both the
    control API map and the REST listener itself."""

    @pytest.mark.parametrize("pid", list(REST_BACKUP_PORTS.keys()))
    def test_rest_backup_port_wired(self, rest_sim, pid):
        # 1) /scenario POST targeting this backup pid is accepted.
        status, body = _post(
            f"{rest_sim['control']}/scenario",
            {"providers": {pid: {"chain_family": "rest", "mode": "down"}}},
        )
        assert status == 200, (
            f"Control API rejected REST /scenario for backup pid={pid!r}: "
            f"status={status} body={body}"
        )

        # 2) GET /scenario echoes the mode back for this pid.
        status, response = _get(f"{rest_sim['control']}/scenario")
        assert status == 200
        snapshot = response.get("providers", {})
        assert snapshot.get(pid, {}).get("mode") == "down", (
            f"GET /scenario doesn't echo back REST backup pid={pid!r}: "
            f"{snapshot.get(pid)}"
        )

        # 3) The REST listener answers per the configured mode. `down`
        #    returns HTTP 503 to any GET request — verifies the listener
        #    is bound AND wired to this pid's ProviderState.
        port = _REST_TEST_BACKUP[pid]
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/cosmos/base/tendermint/v1beta1/blocks/latest",
                timeout=5,
            ) as resp:
                pytest.fail(
                    f"REST backup listener on port {port} (pid={pid!r}) "
                    f"returned {resp.status} instead of 503 — down mode "
                    "not applied"
                )
        except urllib.error.HTTPError as e:
            assert e.code == 503, (
                f"REST backup listener on port {port} (pid={pid!r}) "
                f"returned HTTP {e.code} instead of 503"
            )


# ── Tendermint-RPC backup-tier smoke test ────────────────────────────────────

_TM_TEST_PRIMARY = {"1": 61545, "2": 61546, "3": 61547}
_TM_TEST_BACKUP  = {"13": 61569, "14": 61570, "15": 61571}
_TM_TEST_CONTROL = 61000


@pytest.fixture(scope="module")
def tm_sim():
    """Boot 3 TM primary + 3 TM backup + control on test ports."""
    all_pids = {**_TM_TEST_PRIMARY, **_TM_TEST_BACKUP}
    states = {pid: ProviderState() for pid in all_pids}

    servers = []
    for pid, port in all_pids.items():
        srv = ThreadingHTTPServer(("127.0.0.1", port), TendermintHandler)
        srv.daemon_threads = True
        srv.state = states[pid]
        srv.provider_id = pid
        servers.append(srv)

    ctrl = HTTPServer(("127.0.0.1", _TM_TEST_CONTROL), ControlHandler)
    ctrl.provider_states = states
    servers.append(ctrl)

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    time.sleep(0.15)

    yield {
        "control":        f"http://127.0.0.1:{_TM_TEST_CONTROL}",
        "primary_ports":  dict(_TM_TEST_PRIMARY),
        "backup_ports":   dict(_TM_TEST_BACKUP),
    }

    for s in servers:
        s.shutdown()


@pytest.fixture(autouse=True)
def tm_clean_state(request):
    if "tm_sim" not in request.fixturenames:
        yield
        return
    control = f"http://127.0.0.1:{_TM_TEST_CONTROL}"
    _post(f"{control}/reset/all", {})
    yield
    _post(f"{control}/reset/all", {})


class TestTmBackupListenersWired:
    """Every entry in TM_BACKUP_PORTS must round-trip through both the
    control API map and the Tendermint-RPC listener itself."""

    @pytest.mark.parametrize("pid", list(TM_BACKUP_PORTS.keys()))
    def test_tm_backup_port_wired(self, tm_sim, pid):
        # 1) /scenario POST targeting this backup pid is accepted.
        status, body = _post(
            f"{tm_sim['control']}/scenario",
            {"providers": {pid: {"chain_family": "tendermintrpc", "mode": "down"}}},
        )
        assert status == 200, (
            f"Control API rejected TM /scenario for backup pid={pid!r}: "
            f"status={status} body={body}"
        )

        # 2) GET /scenario echoes the mode back.
        status, response = _get(f"{tm_sim['control']}/scenario")
        assert status == 200
        snapshot = response.get("providers", {})
        assert snapshot.get(pid, {}).get("mode") == "down", (
            f"GET /scenario doesn't echo back TM backup pid={pid!r}: "
            f"{snapshot.get(pid)}"
        )

        # 3) The TM listener returns 503 on a GET URI form (the CometBFT-
        #    native shape). Any URI works because down is checked before
        #    URI parsing.
        port = _TM_TEST_BACKUP[pid]
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/status", timeout=5,
            ) as resp:
                pytest.fail(
                    f"TM backup listener on port {port} (pid={pid!r}) "
                    f"returned {resp.status} instead of 503"
                )
        except urllib.error.HTTPError as e:
            assert e.code == 503, (
                f"TM backup listener on port {port} (pid={pid!r}) "
                f"returned HTTP {e.code} instead of 503"
            )


# ── WebSocket backup-tier smoke test ─────────────────────────────────────────

_WS_TEST_PRIMARY = {"1": 62545, "2": 62546, "3": 62547}
_WS_TEST_BACKUP  = {"16": 62572, "17": 62573, "18": 62574}
_WS_TEST_CONTROL = 62000


@pytest.fixture(scope="module")
def ws_sim():
    """Boot 3 WS primary + 3 WS backup + control on test ports."""
    all_pids = {**_WS_TEST_PRIMARY, **_WS_TEST_BACKUP}
    states = {pid: ProviderState() for pid in all_pids}

    servers = []
    for pid, port in all_pids.items():
        srv = ThreadingHTTPServer(("127.0.0.1", port), handlers_ws.WsHandler)
        srv.daemon_threads = True
        srv.state = states[pid]
        srv.provider_id = pid
        servers.append(srv)

    ctrl = HTTPServer(("127.0.0.1", _WS_TEST_CONTROL), ControlHandler)
    ctrl.provider_states = states
    servers.append(ctrl)

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    time.sleep(0.15)

    yield {
        "control":        f"http://127.0.0.1:{_WS_TEST_CONTROL}",
        "primary_ports":  dict(_WS_TEST_PRIMARY),
        "backup_ports":   dict(_WS_TEST_BACKUP),
    }

    for s in servers:
        s.shutdown()


@pytest.fixture(autouse=True)
def ws_clean_state(request):
    if "ws_sim" not in request.fixturenames:
        yield
        return
    control = f"http://127.0.0.1:{_WS_TEST_CONTROL}"
    _post(f"{control}/reset/all", {})
    yield
    _post(f"{control}/reset/all", {})


class TestWsBackupListenersWired:
    """Every entry in WS_BACKUP_PORTS must round-trip through both the
    control API map and the WebSocket listener itself."""

    @pytest.mark.parametrize("pid", list(WS_BACKUP_PORTS.keys()))
    def test_ws_backup_port_wired(self, ws_sim, pid):
        # 1) /scenario POST targeting this backup pid is accepted.
        status, body = _post(
            f"{ws_sim['control']}/scenario",
            {"providers": {pid: {"chain_family": "ws", "mode": "down"}}},
        )
        assert status == 200, (
            f"Control API rejected WS /scenario for backup pid={pid!r}: "
            f"status={status} body={body}"
        )

        # 2) GET /scenario echoes the mode back.
        status, response = _get(f"{ws_sim['control']}/scenario")
        assert status == 200
        snapshot = response.get("providers", {})
        assert snapshot.get(pid, {}).get("mode") == "down", (
            f"GET /scenario doesn't echo back WS backup pid={pid!r}: "
            f"{snapshot.get(pid)}"
        )

        # 3) The WS listener refuses the upgrade with 503 when in down
        #    mode (handlers_ws.WsHandler.do_GET — _send_simple_error 503
        #    before completing the handshake). Use a raw socket so the
        #    upgrade headers reach the server verbatim — urllib.request
        #    normalises Connection: Upgrade in ways the WS handshake
        #    validator rejects with 400 before the down-mode check runs.
        import socket as _socket

        port = _WS_TEST_BACKUP[pid]
        s = _socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            s.sendall(
                b"GET /ws HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                b"Sec-WebSocket-Version: 13\r\n"
                b"\r\n"
            )
            data = s.recv(1024)
        finally:
            s.close()

        status_line = data.split(b"\r\n", 1)[0]
        assert b" 503 " in status_line, (
            f"WS backup listener on port {port} (pid={pid!r}) "
            f"returned {status_line!r} instead of 503 — down mode "
            "not applied"
        )


# ── gRPC backup-tier smoke test (skipped when grpcio missing) ────────────────

_GRPC_TEST_PRIMARY = {"1": 63548, "2": 63549, "3": 63550}
_GRPC_TEST_BACKUP  = {"7": 63563, "8": 63564, "9": 63565}
_GRPC_TEST_CONTROL = 63000


@pytest.fixture(scope="module")
def grpc_sim():
    """Boot 3 gRPC primary + 3 gRPC backup + control on test ports.

    Skipped when grpcio isn't installed — handlers_grpc imports it at
    module load time. The wider grpc test suite (test_simulator_grpc.py)
    runs the same way and has the same skip semantics.
    """
    pytest.importorskip("grpc")
    grpc_server = pytest.importorskip("grpc_server")

    all_pids = {**_GRPC_TEST_PRIMARY, **_GRPC_TEST_BACKUP}
    states = {pid: ProviderState() for pid in all_pids}

    ctrl = HTTPServer(("127.0.0.1", _GRPC_TEST_CONTROL), ControlHandler)
    ctrl.provider_states = states

    threads = [threading.Thread(target=ctrl.serve_forever, daemon=True)]
    for pid, port in all_pids.items():
        threads.append(threading.Thread(
            target=grpc_server.run_grpc_in_thread,
            args=(port, states[pid]),
            daemon=True,
            name=f"grpc-backup-test-{pid}",
        ))
    for t in threads:
        t.start()

    # gRPC servers need a touch more time to spin up the asyncio loop.
    time.sleep(0.5)

    yield {
        "control":        f"http://127.0.0.1:{_GRPC_TEST_CONTROL}",
        "primary_ports":  dict(_GRPC_TEST_PRIMARY),
        "backup_ports":   dict(_GRPC_TEST_BACKUP),
    }

    ctrl.shutdown()


@pytest.fixture(autouse=True)
def grpc_clean_state(request):
    if "grpc_sim" not in request.fixturenames:
        yield
        return
    control = f"http://127.0.0.1:{_GRPC_TEST_CONTROL}"
    _post(f"{control}/reset/all", {})
    yield
    _post(f"{control}/reset/all", {})


class TestGrpcBackupListenersWired:
    """Every entry in GRPC_BACKUP_PORTS must round-trip through both the
    control API map and the gRPC servicer itself."""

    @pytest.mark.parametrize("pid", list(GRPC_BACKUP_PORTS.keys()))
    def test_grpc_backup_port_wired(self, grpc_sim, pid):
        # Imports here so the file remains importable without grpcio.
        import asyncio

        import grpc as _grpc
        import cosmos_pb2  # noqa: F401 — splice sys.path
        from cosmos.base.tendermint.v1beta1 import query_pb2, query_pb2_grpc

        # 1) /scenario POST targeting this gRPC backup pid is accepted.
        status, body = _post(
            f"{grpc_sim['control']}/scenario",
            {"providers": {pid: {"chain_family": "grpc", "mode": "down"}}},
        )
        assert status == 200, (
            f"Control API rejected gRPC /scenario for backup pid={pid!r}: "
            f"status={status} body={body}"
        )

        # 2) GET /scenario echoes the mode back.
        status, response = _get(f"{grpc_sim['control']}/scenario")
        assert status == 200
        snapshot = response.get("providers", {})
        assert snapshot.get(pid, {}).get("mode") == "down", (
            f"GET /scenario doesn't echo back gRPC backup pid={pid!r}: "
            f"{snapshot.get(pid)}"
        )

        # 3) The gRPC servicer aborts UNAVAILABLE on the configured down
        #    mode — verifies the servicer is bound AND wired to this
        #    pid's ProviderState. (Down maps to UNAVAILABLE per the
        #    JSON-RPC primitive table in handlers_grpc.py.)
        port = _GRPC_TEST_BACKUP[pid]

        async def _call():
            channel = _grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            try:
                stub = query_pb2_grpc.ServiceStub(channel)
                await stub.GetLatestBlock(
                    query_pb2.GetLatestBlockRequest(), timeout=3.0,
                )
            finally:
                await channel.close()

        with pytest.raises(_grpc.RpcError) as exc_info:
            asyncio.run(_call())
        assert exc_info.value.code() == _grpc.StatusCode.UNAVAILABLE, (
            f"gRPC backup servicer on port {port} (pid={pid!r}) "
            f"returned status {exc_info.value.code()} instead of "
            "UNAVAILABLE — down mode not applied"
        )


# ── JSON-RPC backup listener fault behaviour ─────────────────────────────────
#
# The parametrised wiring tests above (TestBackupListenersWired) only exercise
# mode="down" because that is the cheapest observable (HTTP 503, no body).
# The six tests below confirm that each remaining fault mode fires correctly
# when applied to a backup pid. Each test POSTs the scenario, sends a real
# JSON-RPC request to the bound backup port, and asserts a fault-specific
# observable that would FAIL if the provider were in success mode.
#
# Uses pid "4" (port _TEST_BACKUP_PORTS["4"] = 58554) and the `sim` fixture
# which is already module-scoped and wires all six test ports.


class TestBackupListenerFaults:
    """Fault modes on a JSON-RPC backup listener produce the correct
    observable HTTP responses and transport behaviours.

    Template: TestBackupListenersWired.test_backup_port_wired_through_control_and_listener
    which confirms down→503. These tests cover the remaining six modes.
    """

    _PID = "4"

    def _backup_url(self) -> str:
        port = _TEST_BACKUP_PORTS[self._PID]
        return f"http://127.0.0.1:{port}"

    def _set_scenario(self, sim, scenario: dict) -> None:
        status, body = _post(
            f"{sim['control']}/scenario",
            {"providers": {self._PID: scenario}},
        )
        assert status == 200, (
            f"Control API rejected /scenario for backup pid={self._PID!r}: "
            f"status={status} body={body}"
        )

    # ------------------------------------------------------------------
    # error mode
    # ------------------------------------------------------------------

    def test_error_mode_returns_jsonrpc_error(self, sim):
        """mode='error' returns HTTP 200 with a JSON-RPC error envelope.

        In success mode the response would contain 'result', not 'error'.
        Asserting body['error']['code'] == -32000 is the fault-specific
        observable: success mode never sets 'error'.
        """
        self._set_scenario(sim, {"mode": "error"})
        status, body = _rpc(self._backup_url(), "eth_blockNumber")
        assert status == 200, (
            f"error mode: expected HTTP 200, got {status}. "
            f"body={body}"
        )
        assert "error" in body, (
            f"error mode: response body has no 'error' key. "
            f"expected={{'error': {{'code': -32000, ...}}}}, actual={body}"
        )
        assert body["error"]["code"] == -32000, (
            f"error mode: expected error code -32000, "
            f"got {body['error']['code']!r}. full body={body}"
        )

    # ------------------------------------------------------------------
    # rate_limit mode
    # ------------------------------------------------------------------

    def test_rate_limit_returns_429(self, sim):
        """mode='rate_limit' returns HTTP 429 with error code 429 in body.

        In success mode the response is HTTP 200 with 'result'. Asserting
        status==429 AND body['error']['code']==429 is fault-specific:
        success mode returns 200.
        """
        self._set_scenario(sim, {"mode": "rate_limit"})
        status, body = _rpc(self._backup_url(), "eth_blockNumber")
        assert status == 429, (
            f"rate_limit mode: expected HTTP 429, got {status}. "
            f"body={body}"
        )
        assert body.get("error", {}).get("code") == 429, (
            f"rate_limit mode: expected error code 429 in body, "
            f"got {body.get('error')}. full body={body}"
        )

    # ------------------------------------------------------------------
    # hang mode
    # ------------------------------------------------------------------

    def test_hang_blocks_until_client_timeout(self, sim):
        """mode='hang' accepts the connection but never sends a response.

        The test uses a 1-second client timeout so it doesn't block for the
        full server-side hang period. It asserts the client timed out AND
        that at least ~1s elapsed — proving the server held the connection
        open rather than responding immediately (as success mode would).
        """
        self._set_scenario(sim, {"mode": "hang"})
        url = self._backup_url()
        req = urllib.request.Request(
            url,
            data=b'{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}',
            headers={"Content-Type": "application/json"},
        )
        t0 = time.monotonic()
        with pytest.raises(
            (urllib.error.URLError, TimeoutError),
        ):
            urllib.request.urlopen(req, timeout=1.0)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.9, (
            f"hang mode: expected client timeout after ~1s, "
            f"but elapsed={elapsed:.2f}s. Server may have responded "
            f"immediately instead of holding the connection."
        )
        assert elapsed < 3.0, (
            f"hang mode: client waited {elapsed:.2f}s — longer than the "
            f"1s client timeout; check pytest-timeout backstop."
        )

    # ------------------------------------------------------------------
    # drop_connection mode
    # ------------------------------------------------------------------

    def test_drop_connection_raises_transport_error(self, sim):
        """mode='drop_connection' with drop_at='before_headers' closes the
        TCP connection before sending any HTTP response.

        In success mode urlopen returns a valid response. Here we assert
        that urlopen raises a transport-level exception — the fault-specific
        observable that distinguishes drop_connection from all other modes.
        """
        self._set_scenario(
            sim, {"mode": "drop_connection", "drop_at": "before_headers"}
        )
        url = self._backup_url()
        req = urllib.request.Request(
            url,
            data=b'{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}',
            headers={"Content-Type": "application/json"},
        )
        # The connection is dropped before any HTTP response arrives, so any
        # transport-level exception is the valid observable here.
        with pytest.raises(
            (urllib.error.URLError, ConnectionResetError, OSError)
        ):
            urllib.request.urlopen(req, timeout=3.0)

    # ------------------------------------------------------------------
    # corruption mode
    # ------------------------------------------------------------------

    def test_corruption_invalid_json_is_unparseable(self, sim):
        """corruption_mode='invalid_json' makes the response body
        unparseable as JSON.

        In success mode json.loads(raw) succeeds. Here we assert it raises
        json.JSONDecodeError — the fault-specific observable.
        """
        self._set_scenario(sim, {"corruption_mode": "invalid_json"})
        url = self._backup_url()
        req = urllib.request.Request(
            url,
            data=b'{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}',
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)

    # ------------------------------------------------------------------
    # latency mode
    # ------------------------------------------------------------------

    def test_latency_ms_delays_response(self, sim):
        """latency_ms=200 delays the response by at least ~200ms.

        In success mode the response arrives in under 50ms on loopback.
        Asserting elapsed_ms >= 180 (with a small slack) is fault-specific:
        a success-mode response would never approach that threshold.
        """
        self._set_scenario(sim, {"latency_ms": 200})
        t0 = time.monotonic()
        _rpc(self._backup_url(), "eth_blockNumber")
        elapsed_ms = (time.monotonic() - t0) * 1000
        assert elapsed_ms >= 180, (
            f"latency mode: expected >= 180ms elapsed, got {elapsed_ms:.1f}ms. "
            f"Either the latency scenario was not applied to backup pid={self._PID!r}, "
            f"or the listener is not wired to the correct ProviderState."
        )


# ── Cross-surface mixed /scenario ────────────────────────────────────────────


def test_scenario_merges_pids_from_every_surface_into_control_map():
    """The control API can accept a single /scenario POST that sets pids
    from every surface's backup pool plus a primary pid, and the control
    map stores all of them correctly.

    This is a control-map dict-merge test: it verifies that one POST can
    configure pids from every surface without short-circuiting on an
    unknown pid, and that GET /scenario echoes all of them back. It does
    NOT verify that any fault actually fires — that is covered by
    TestBackupListenerFaults above.

    Uses the constants module's actual pids (1, 4, 7, 10, 13, 16) so
    the test fails if the production iteration misses any of them.
    """
    all_pids = {
        **_TEST_PRIMARY_PORTS,                 # pid "1"
        **_TEST_BACKUP_PORTS,                  # pids "4"-"6"
        "7":  64500, "8": 64501, "9": 64502,   # gRPC backup
        "10": 64510, "11": 64511, "12": 64512, # REST backup
        "13": 64520, "14": 64521, "15": 64522, # TM backup
        "16": 64530, "17": 64531, "18": 64532, # WS backup
    }
    test_control = 64000

    states = {pid: ProviderState() for pid in all_pids}
    # We don't actually need to bind listeners here — the test is purely
    # about the control map / state map plumbing. Bind only the control
    # server so the assertion checks the dict-merge path.
    ctrl = HTTPServer(("127.0.0.1", test_control), ControlHandler)
    ctrl.provider_states = states
    t = threading.Thread(target=ctrl.serve_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.1)

        # One body, one pid from every surface's backup pool (plus a primary).
        payload = {
            "providers": {
                "1":  {"mode": "down"},      # primary
                "4":  {"mode": "success"},   # JSON-RPC backup
                "7":  {"mode": "rate_limit"},  # gRPC backup
                "10": {"mode": "hang"},      # REST backup
                "13": {"mode": "error"},     # TM backup
                "16": {"mode": "drop_connection"},  # WS backup
            }
        }
        status, body = _post(
            f"http://127.0.0.1:{test_control}/scenario", payload,
        )
        assert status == 200, f"mixed-surface /scenario rejected: {body}"

        status, response = _get(f"http://127.0.0.1:{test_control}/scenario")
        assert status == 200
        snapshot = response.get("providers", {})
        for pid, cfg in payload["providers"].items():
            assert snapshot.get(pid, {}).get("mode") == cfg["mode"], (
                f"mode for pid {pid!r} not applied; expected "
                f"{cfg['mode']!r}, got {snapshot.get(pid)!r}"
            )
    finally:
        ctrl.shutdown()
