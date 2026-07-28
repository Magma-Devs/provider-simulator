"""
Smoke test for backup-tier listeners — the wiring itself.

Why this file exists
--------------------
The response-shape behaviour of every transport is covered by the matching
primary-tier test files (test_simulator.py, test_simulator_rest.py, …). What
those suites do NOT exercise is the backup tier's *registration*: every
backup provider row in the topology must produce a bound listener AND a
control-API address. The bootstrap binds one listener per endpoint straight
from the registry, so the regression class this file guards is a topology row
going missing (or drifting from constants.py): the port silently never binds
and /scenario 400s on the provider.

What it tests
-------------
1. The port-allocation constants keep their shape (distinct pids across the
   legacy dicts, no port collisions, unions consistent) — these mirror the
   values_sim.yml allocation the routers point at.
2. For every backup provider in every pool, the full round-trip works:
     a. POST /scenario {"providers": {"pool:pid": {"mode": "down"}}} → 200
     b. GET  /scenario → snapshot["pool:pid"]["mode"] == "down"
     c. A transport-native request to the provider's port → the down shape
        (503 for HTTP-style, abort UNAVAILABLE for gRPC, upgrade-refused 503
        for WS).
3. A single /scenario POST can mix primary + backup providers across pools.
4. Every fault mode fires on a backup provider (not just down).

Runs against the shared in-process simulator (see conftest.py) on the real
ports.

Run with:
  pytest tests/test_simulator_backup_listeners.py -v
"""

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

from constants import (
    ALL_PROVIDER_PORTS,
    BACKUP_PROVIDER_PORTS,
    BTC_PRIMARY_PORTS,
    ETH_BACKUP_PORTS,
    ETH_DUO_PORTS,
    ETH_SOLO_PORTS,
    GRPC_BACKUP_PORTS,
    GRPC_PROVIDER_PORTS,
    LN_PRIMARY_PORTS,
    PROVIDER_PORTS,
    REST_BACKUP_PORTS,
    REST_PORTS,
    SOLANA_PRIMARY_PORTS,
    SOLO_SOLANA_PROVIDER_PORTS,
    TM_BACKUP_PORTS,
    TM_PORTS,
    WS_BACKUP_PORTS,
    WS_PORTS,
)

# Backup providers under the pool:pid model: local pids 4-6 in each pool.
# The port lists mirror the legacy per-surface backup dicts (constants.py),
# whose values are the deployed allocation.
_ETH_BACKUPS = list(zip(("4", "5", "6"), sorted(ETH_BACKUP_PORTS.values())))
_GRPC_BACKUPS = list(zip(("4", "5", "6"), sorted(GRPC_BACKUP_PORTS.values())))
_REST_BACKUPS = list(zip(("4", "5", "6"), sorted(REST_BACKUP_PORTS.values())))
_TM_BACKUPS = list(zip(("4", "5", "6"), sorted(TM_BACKUP_PORTS.values())))
_WS_BACKUPS = list(zip(("4", "5", "6"), sorted(WS_BACKUP_PORTS.values())))


# ── HTTP helpers (same shape as test_simulator.py — inline keeps this file
#    standalone so a single-file pytest run has zero cross-imports) ───────────


def _post(url: str, body: dict) -> tuple[int, dict]:
    """POST JSON body, return (status_code, parsed_response_body)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
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


@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario AND clear history before and after every test."""
    _post(f"{sim['control']}/reset/all", {})
    yield
    _post(f"{sim['control']}/reset/all", {})


# ── Constants shape (cheap; catches typos at import time) ────────────────────


def test_all_provider_ports_is_union_of_primary_and_backup():
    """ALL_PROVIDER_PORTS must equal the union of PROVIDER_PORTS,
    BACKUP_PROVIDER_PORTS, ETH_SOLO_PORTS, and ETH_DUO_PORTS — the ETH
    pool's port allocation the topology mirrors."""
    expected = {
        **PROVIDER_PORTS,
        **BACKUP_PROVIDER_PORTS,
        **ETH_SOLO_PORTS,
        **ETH_DUO_PORTS,
    }
    assert ALL_PROVIDER_PORTS == expected, (
        f"ALL_PROVIDER_PORTS must equal "
        f"PROVIDER_PORTS ∪ BACKUP_PROVIDER_PORTS ∪ ETH_SOLO_PORTS ∪ ETH_DUO_PORTS.\n"
        f"  PROVIDER_PORTS        : {PROVIDER_PORTS}\n"
        f"  BACKUP_PROVIDER_PORTS : {BACKUP_PROVIDER_PORTS}\n"
        f"  ETH_SOLO_PORTS        : {ETH_SOLO_PORTS}\n"
        f"  ETH_DUO_PORTS         : {ETH_DUO_PORTS}\n"
        f"  expected union        : {expected}\n"
        f"  ALL_PROVIDER_PORTS    : {dict(ALL_PROVIDER_PORTS)}"
    )

    all_port_values = list(ALL_PROVIDER_PORTS.values())
    assert len(all_port_values) == len(set(all_port_values)), (
        f"ALL_PROVIDER_PORTS contains duplicate port numbers: " f"{all_port_values}"
    )

    primary_pids = set(PROVIDER_PORTS.keys())
    backup_pids = set(BACKUP_PROVIDER_PORTS.keys())
    assert primary_pids.isdisjoint(backup_pids), (
        f"PROVIDER_PORTS and BACKUP_PROVIDER_PORTS share provider ids: "
        f"{primary_pids & backup_pids}"
    )


def test_backup_port_dicts_have_no_pid_collisions():
    """The legacy per-surface dicts keep disjoint pid namespaces (their pids
    document the historical global numbering the migration mapped from)."""
    pid_sources = [
        ("PROVIDER_PORTS", PROVIDER_PORTS),
        ("BACKUP_PROVIDER_PORTS", BACKUP_PROVIDER_PORTS),
        ("GRPC_BACKUP_PORTS", GRPC_BACKUP_PORTS),
        ("REST_BACKUP_PORTS", REST_BACKUP_PORTS),
        ("TM_BACKUP_PORTS", TM_BACKUP_PORTS),
        ("WS_BACKUP_PORTS", WS_BACKUP_PORTS),
    ]
    seen: dict[str, str] = {}
    for source_name, port_dict in pid_sources:
        for pid in port_dict:
            if pid in seen and seen[pid] != source_name:
                # Primary-tier pids 1-3 are shared across surface dicts on
                # purpose; only flag collisions that involve a BACKUP dict.
                if source_name.endswith("_BACKUP_PORTS") or seen[pid].endswith("_BACKUP_PORTS"):
                    pytest.fail(
                        f"pid {pid!r} appears in both {seen[pid]} and "
                        f"{source_name} — backup pids must be disjoint "
                        f"from every other pool"
                    )
            seen[pid] = source_name


def test_backup_port_dicts_have_no_port_collisions():
    """Every primary + backup port across every surface must be unique.

    A port collision would make two listeners try to bind the same port at
    startup — the registry refuses such a topology, and this keeps the
    constants that document the allocation in line."""
    pool_sources = [
        ("PROVIDER_PORTS", PROVIDER_PORTS),
        ("BACKUP_PROVIDER_PORTS", BACKUP_PROVIDER_PORTS),
        ("GRPC_PROVIDER_PORTS", GRPC_PROVIDER_PORTS),
        ("GRPC_BACKUP_PORTS", GRPC_BACKUP_PORTS),
        ("REST_PORTS", REST_PORTS),
        ("REST_BACKUP_PORTS", REST_BACKUP_PORTS),
        ("TM_PORTS", TM_PORTS),
        ("TM_BACKUP_PORTS", TM_BACKUP_PORTS),
        ("WS_PORTS", WS_PORTS),
        ("WS_BACKUP_PORTS", WS_BACKUP_PORTS),
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
        ("jsonrpc", PROVIDER_PORTS, BACKUP_PROVIDER_PORTS),
        ("grpc", GRPC_PROVIDER_PORTS, GRPC_BACKUP_PORTS),
        ("rest", REST_PORTS, REST_BACKUP_PORTS),
        ("tendermintrpc", TM_PORTS, TM_BACKUP_PORTS),
        ("ws", WS_PORTS, WS_BACKUP_PORTS),
    ],
)
def test_each_surface_has_three_primary_and_three_backup(
    surface,
    primary_dict,
    backup_dict,
):
    """Every surface allocates a 3+3 pool. Catches an accidental drop of one
    entry (e.g. a 2-node backup pool silently shipping when the router
    expects 3)."""
    assert (
        len(primary_dict) == 3
    ), f"{surface} primary pool has {len(primary_dict)} entries, expected 3"
    assert (
        len(backup_dict) == 3
    ), f"{surface} backup pool has {len(backup_dict)} entries, expected 3"


def test_solo_solana_provider_ports_shape_and_uniqueness():
    """The Solana solo listener constant is exactly port 18585, and neither
    the legacy pid nor the port collides with any other listener pool. The
    topology binds it as solana-solo-sim:1 with its own provider state, so a
    /scenario POST on the solo router can't reconfigure the primary pool."""
    # 1) Exact shape.
    assert SOLO_SOLANA_PROVIDER_PORTS == {"20": 18585}, (
        f"SOLO_SOLANA_PROVIDER_PORTS must be exactly {{'20': 18585}}; "
        f"got {SOLO_SOLANA_PROVIDER_PORTS}"
    )

    other_pid_sources = [
        ("PROVIDER_PORTS", PROVIDER_PORTS),
        ("BACKUP_PROVIDER_PORTS", BACKUP_PROVIDER_PORTS),
        ("ETH_SOLO_PORTS", ETH_SOLO_PORTS),
        ("BTC_PRIMARY_PORTS", BTC_PRIMARY_PORTS),
        ("LN_PRIMARY_PORTS", LN_PRIMARY_PORTS),
        ("SOLANA_PRIMARY_PORTS", SOLANA_PRIMARY_PORTS),
        ("GRPC_PROVIDER_PORTS", GRPC_PROVIDER_PORTS),
        ("GRPC_BACKUP_PORTS", GRPC_BACKUP_PORTS),
        ("REST_PORTS", REST_PORTS),
        ("REST_BACKUP_PORTS", REST_BACKUP_PORTS),
        ("TM_PORTS", TM_PORTS),
        ("TM_BACKUP_PORTS", TM_BACKUP_PORTS),
        ("WS_PORTS", WS_PORTS),
        ("WS_BACKUP_PORTS", WS_BACKUP_PORTS),
    ]
    solo_pid = "20"
    for source_name, port_dict in other_pid_sources:
        assert solo_pid not in port_dict, (
            f"Solana solo legacy pid {solo_pid!r} also appears in {source_name} — "
            f"the solo Solana provider owns a distinct address"
        )

    solo_port = 18585
    for source_name, port_dict in other_pid_sources:
        assert solo_port not in port_dict.values(), (
            f"Solana solo port {solo_port} also bound by {source_name} — "
            f"two listeners cannot share a port"
        )

    assert solo_port not in ALL_PROVIDER_PORTS.values(), (
        f"Port {solo_port} must NOT be in ALL_PROVIDER_PORTS — it belongs to "
        f"the solana-solo-sim pool, not the ETH pool"
    )


# ── Listener wire-up per pool ────────────────────────────────────────────────


class TestBackupListenersWired:
    """Every backup provider must round-trip through both the control API
    and its bound listener. Catches a topology row going missing: no
    listener binds, /scenario has no address, downstream failover tests get
    a 400 from the control API or a connection refused from the listener."""

    @pytest.mark.parametrize("pid,port", _ETH_BACKUPS)
    def test_eth_backup_wired_through_control_and_listener(self, sim, pid, port):
        key = f"eth-sim:{pid}"
        # 1) /scenario POST targeting this backup provider is accepted.
        status, body = _post(f"{sim['control']}/scenario", {"providers": {key: {"mode": "down"}}})
        assert status == 200, (
            f"Control API rejected /scenario for {key}: status={status} "
            f"body={body}. The provider is missing from the registry — "
            f"check its topology row."
        )

        # 2) GET /scenario echoes the mode back.
        status, response = _get(f"{sim['control']}/scenario")
        assert status == 200, f"GET /scenario failed: status={status}"
        snapshot = response.get("providers", {})
        assert snapshot.get(key, {}).get("mode") == "down", (
            f"GET /scenario doesn't echo back the mode for {key}. "
            f"snapshot[{key!r}]={snapshot.get(key)!r}."
        )

        # 3) The listener bound on this provider's backup port answers per
        #    the configured mode. `down` returns HTTP 503 — the cheapest
        #    assertion that the listener is bound AND uses THIS provider's
        #    state (not somebody else's).
        status, _ = _rpc(f"http://127.0.0.1:{port}", "eth_blockNumber")
        assert status == 503, (
            f"Backup listener on port {port} ({key}) returned HTTP {status} "
            f"instead of 503. Either the port did not bind, or it's wired "
            f"to a different provider than the control API mutated."
        )

    def test_mixed_primary_and_backup_in_one_scenario(self, sim):
        """A single /scenario POST can configure primary AND backup
        providers together — the integration shape backup-failover tests
        rely on."""
        status, body = _post(
            f"{sim['control']}/scenario",
            {
                "providers": {
                    "eth-sim:1": {"mode": "down"},  # primary
                    "eth-sim:4": {"mode": "success"},  # backup
                }
            },
        )
        assert status == 200, f"mixed-tier /scenario rejected: {body}"

        status, response = _get(f"{sim['control']}/scenario")
        assert status == 200
        snapshot = response.get("providers", {})
        assert (
            snapshot.get("eth-sim:1", {}).get("mode") == "down"
        ), f"primary eth-sim:1 mode not applied; snapshot={snapshot.get('eth-sim:1')}"
        assert (
            snapshot.get("eth-sim:4", {}).get("mode") == "success"
        ), f"backup eth-sim:4 mode not applied; snapshot={snapshot.get('eth-sim:4')}"

        # Both listeners answer per their own state.
        status1, _ = _rpc(f"http://127.0.0.1:{PROVIDER_PORTS['1']}", "eth_blockNumber")
        assert status1 == 503, (
            f"primary eth-sim:1 returned {status1}, expected 503 (down). "
            "Either state cross-contamination, or the mixed-tier POST "
            "stomped on the primary configuration."
        )
        status4, body4 = _rpc(f"http://127.0.0.1:{ETH_BACKUP_PORTS['4']}", "eth_blockNumber")
        assert status4 == 200, (
            f"backup eth-sim:4 returned {status4}, expected 200 (success). "
            "Either listener missing or state cross-contamination."
        )
        assert "result" in body4, f"backup eth-sim:4 success response missing 'result': {body4}"


class TestRestBackupListenersWired:
    """Every lava-sim-rest backup provider must round-trip through both the
    control API and the REST listener itself."""

    @pytest.mark.parametrize("pid,port", _REST_BACKUPS)
    def test_rest_backup_port_wired(self, sim, pid, port):
        key = f"lava-sim-rest:{pid}"
        status, body = _post(f"{sim['control']}/scenario", {"providers": {key: {"mode": "down"}}})
        assert status == 200, (
            f"Control API rejected REST /scenario for {key}: " f"status={status} body={body}"
        )

        status, response = _get(f"{sim['control']}/scenario")
        assert status == 200
        snapshot = response.get("providers", {})
        assert snapshot.get(key, {}).get("mode") == "down", (
            f"GET /scenario doesn't echo back {key}: " f"{snapshot.get(key)}"
        )

        # The REST listener answers per the configured mode. `down` returns
        # HTTP 503 to any GET request.
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/cosmos/base/tendermint/v1beta1/blocks/latest",
                timeout=5,
            ) as resp:
                pytest.fail(
                    f"REST backup listener on port {port} ({key}) returned "
                    f"{resp.status} instead of 503 — down mode not applied"
                )
        except urllib.error.HTTPError as e:
            assert e.code == 503, (
                f"REST backup listener on port {port} ({key}) "
                f"returned HTTP {e.code} instead of 503"
            )


class TestTmBackupListenersWired:
    """Every lava-sim-tm backup provider must round-trip through both the
    control API and the Tendermint-RPC listener itself."""

    @pytest.mark.parametrize("pid,port", _TM_BACKUPS)
    def test_tm_backup_port_wired(self, sim, pid, port):
        key = f"lava-sim-tm:{pid}"
        status, body = _post(f"{sim['control']}/scenario", {"providers": {key: {"mode": "down"}}})
        assert status == 200, (
            f"Control API rejected TM /scenario for {key}: " f"status={status} body={body}"
        )

        status, response = _get(f"{sim['control']}/scenario")
        assert status == 200
        snapshot = response.get("providers", {})
        assert snapshot.get(key, {}).get("mode") == "down", (
            f"GET /scenario doesn't echo back {key}: " f"{snapshot.get(key)}"
        )

        # The TM listener returns 503 on a GET URI form (the CometBFT-native
        # shape). Any URI works because down is checked before URI parsing.
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5) as resp:
                pytest.fail(
                    f"TM backup listener on port {port} ({key}) returned "
                    f"{resp.status} instead of 503"
                )
        except urllib.error.HTTPError as e:
            assert e.code == 503, (
                f"TM backup listener on port {port} ({key}) "
                f"returned HTTP {e.code} instead of 503"
            )


class TestWsBackupListenersWired:
    """Every eth-sim backup provider's WS endpoint must round-trip through
    both the control API and the WS listener. The scenario scopes the down
    to the ws transport, so this also pins the per-endpoint transports
    filter: the sibling http endpoint stays up."""

    @pytest.mark.parametrize("pid,port", _WS_BACKUPS)
    def test_ws_backup_port_wired(self, sim, pid, port):
        key = f"eth-sim:{pid}"
        status, body = _post(
            f"{sim['control']}/scenario",
            {"providers": {key: {"mode": "down", "transports": ["ws"]}}},
        )
        assert status == 200, (
            f"Control API rejected WS /scenario for {key}: " f"status={status} body={body}"
        )

        status, response = _get(f"{sim['control']}/scenario")
        assert status == 200
        snapshot = response.get("providers", {})
        assert snapshot.get(key, {}).get("mode") == "down", (
            f"GET /scenario doesn't echo back {key}: " f"{snapshot.get(key)}"
        )

        # The WS listener refuses the upgrade with 503 when down. Use a raw
        # socket so the upgrade headers reach the server verbatim —
        # urllib.request normalises Connection: Upgrade in ways the WS
        # handshake validator rejects with 400 before the down check runs.
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
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
            f"WS backup listener on port {port} ({key}) "
            f"returned {status_line!r} instead of 503 — down mode not applied"
        )

        # The transports filter scopes the down to the ws endpoint only:
        # the same provider's http endpoint keeps serving success.
        http_port = ETH_BACKUP_PORTS[pid]
        http_status, http_body = _rpc(f"http://127.0.0.1:{http_port}", "eth_blockNumber")
        assert http_status == 200, (
            f"{key}'s http endpoint (port {http_port}) must stay up when the "
            f"down is scoped to transports=['ws']; got {http_status}"
        )
        assert "result" in http_body


class TestGrpcBackupListenersWired:
    """Every lava-sim-grpc backup provider must round-trip through both the
    control API and the gRPC servicer itself."""

    @pytest.mark.parametrize("pid,port", _GRPC_BACKUPS)
    def test_grpc_backup_port_wired(self, sim, pid, port):
        # Imports here so the file remains importable without grpcio.
        import asyncio

        import grpc as _grpc
        from cosmos.base.tendermint.v1beta1 import query_pb2, query_pb2_grpc

        import cosmos_pb2  # noqa: F401 — splice sys.path

        key = f"lava-sim-grpc:{pid}"
        status, body = _post(f"{sim['control']}/scenario", {"providers": {key: {"mode": "down"}}})
        assert status == 200, (
            f"Control API rejected gRPC /scenario for {key}: " f"status={status} body={body}"
        )

        status, response = _get(f"{sim['control']}/scenario")
        assert status == 200
        snapshot = response.get("providers", {})
        assert snapshot.get(key, {}).get("mode") == "down", (
            f"GET /scenario doesn't echo back {key}: " f"{snapshot.get(key)}"
        )

        # The gRPC servicer aborts UNAVAILABLE under down — verifies the
        # servicer is bound AND wired to THIS provider's state.
        async def _call():
            channel = _grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            try:
                stub = query_pb2_grpc.ServiceStub(channel)
                await stub.GetLatestBlock(
                    query_pb2.GetLatestBlockRequest(),
                    timeout=3.0,
                )
            finally:
                await channel.close()

        with pytest.raises(_grpc.RpcError) as exc_info:
            asyncio.run(_call())
        assert exc_info.value.code() == _grpc.StatusCode.UNAVAILABLE, (
            f"gRPC backup servicer on port {port} ({key}) "
            f"returned status {exc_info.value.code()} instead of "
            "UNAVAILABLE — down mode not applied"
        )


# ── JSON-RPC backup listener fault behaviour ─────────────────────────────────
#
# The parametrised wiring tests above only exercise mode="down" because that
# is the cheapest observable (HTTP 503, no body). The tests below confirm
# that each remaining fault mode fires correctly when applied to a backup
# provider (eth-sim:4, http port 18560).


class TestBackupListenerFaults:
    """Fault modes on a JSON-RPC backup provider produce the correct
    observable HTTP responses and transport behaviours."""

    _KEY = "eth-sim:4"

    def _backup_url(self) -> str:
        return f"http://127.0.0.1:{ETH_BACKUP_PORTS['4']}"

    def _set_scenario(self, sim, scenario: dict) -> None:
        status, body = _post(
            f"{sim['control']}/scenario", {"providers": {self._KEY: dict(scenario)}}
        )
        assert status == 200, (
            f"Control API rejected /scenario for {self._KEY}: " f"status={status} body={body}"
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
        assert status == 200, f"error mode: expected HTTP 200, got {status}. " f"body={body}"
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
        """mode='rate_limit' returns HTTP 429 with error code 429 in body."""
        self._set_scenario(sim, {"mode": "rate_limit"})
        status, body = _rpc(self._backup_url(), "eth_blockNumber")
        assert status == 429, f"rate_limit mode: expected HTTP 429, got {status}. " f"body={body}"
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
        TCP connection before sending any HTTP response."""
        self._set_scenario(sim, {"mode": "drop_connection", "drop_at": "before_headers"})
        url = self._backup_url()
        req = urllib.request.Request(
            url,
            data=b'{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}',
            headers={"Content-Type": "application/json"},
        )
        # The connection is dropped before any HTTP response arrives, so any
        # transport-level exception is the valid observable here.
        with pytest.raises((urllib.error.URLError, ConnectionResetError, OSError)):
            urllib.request.urlopen(req, timeout=3.0)

    # ------------------------------------------------------------------
    # corruption mode
    # ------------------------------------------------------------------

    def test_corruption_invalid_json_is_unparseable(self, sim):
        """corruption_mode='invalid_json' makes the response body
        unparseable as JSON."""
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
            f"Either the latency scenario was not applied to {self._KEY}, "
            f"or the listener is not wired to the correct provider."
        )


# ── Cross-pool mixed /scenario ───────────────────────────────────────────────


def test_scenario_merges_providers_from_every_pool(sim):
    """One /scenario POST can set providers from every pool's backup tier
    plus a primary, and GET /scenario echoes all of them back — the apply
    path must not short-circuit on any address."""
    payload = {
        "providers": {
            "eth-sim:1": {"mode": "down"},  # primary
            "eth-sim:4": {"mode": "success"},  # JSON-RPC backup (http+ws)
            "lava-sim-grpc:4": {"mode": "rate_limit"},  # gRPC backup
            "lava-sim-rest:4": {"mode": "hang"},  # REST backup
            "lava-sim-tm:4": {"mode": "error"},  # TM backup
            "eth-sim:5": {"mode": "drop_connection", "transports": ["ws"]},  # WS-scoped
        }
    }
    status, body = _post(f"{sim['control']}/scenario", payload)
    assert status == 200, f"mixed-pool /scenario rejected: {body}"

    status, response = _get(f"{sim['control']}/scenario")
    assert status == 200
    snapshot = response.get("providers", {})
    for key, cfg in payload["providers"].items():
        assert snapshot.get(key, {}).get("mode") == cfg["mode"], (
            f"mode for {key} not applied; expected " f"{cfg['mode']!r}, got {snapshot.get(key)!r}"
        )
