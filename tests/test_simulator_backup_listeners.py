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
going missing: the port silently never binds and /scenario 400s on the
provider.

What it tests
-------------
1. Every surface with a backup tier allocates 3 primary + 3 backup providers,
   as pool-local pids 1-6 — the shape the routers' values_sim.yml points at.
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

from provider_simulator.topology import TOPOLOGY, port_of

# Backup providers are pool-local pids 4-6. The pid is the address the control
# API takes; port_of turns it into the port this test connects to.
_BACKUP_PIDS = ("4", "5", "6")
_ETH_BACKUPS = [(pid, port_of("eth-sim", pid)) for pid in _BACKUP_PIDS]
_GRPC_BACKUPS = [(pid, port_of("lava-sim-grpc", pid, "grpc", "http2")) for pid in _BACKUP_PIDS]
_REST_BACKUPS = [(pid, port_of("lava-sim-rest", pid, "rest")) for pid in _BACKUP_PIDS]
_TM_BACKUPS = [(pid, port_of("lava-sim-tm", pid, "tendermintrpc")) for pid in _BACKUP_PIDS]
_WS_BACKUPS = [(pid, port_of("eth-sim", pid, transport="ws")) for pid in _BACKUP_PIDS]


# ── HTTP helpers (same shape as test_simulator.py — inline keeps this file
#    standalone so a single-file pytest run has zero cross-imports) ───────────


def _parse_body(raw: bytes) -> dict | str:
    """JSON-decode ``raw``, falling back to the decoded text when it isn't
    JSON — the rate_limit fault's prose body is not, by design (see
    provider_simulator/listeners/jsonrpc.py)."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode()


def _post(url: str, body: dict) -> tuple[int, dict | str]:
    """POST JSON body, return (status_code, parsed_response_body)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, _parse_body(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, _parse_body(e.read())
        except (ConnectionResetError, OSError):
            # `down` mode returns 503 with no body — server closes the
            # connection before any payload is written.
            return e.code, {}


def _get(url: str) -> tuple[int, dict | str]:
    """GET url, return (status_code, parsed_response_body)."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, _parse_body(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse_body(e.read())


def _rpc(url: str, method: str, params: list | None = None) -> tuple[int, dict | str]:
    """Send a JSON-RPC request, return (http_status, response_body)."""
    return _post(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})


@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario AND clear history before and after every test."""
    _post(f"{sim['control']}/reset/all", {})
    yield
    _post(f"{sim['control']}/reset/all", {})


# ── Topology shape (cheap; catches an allocation mistake at import time) ─────
#
# These used to compare constants.py's port dicts against each other. They now
# read the topology, the table the server binds from, so they measure the thing
# that actually decides what exists. Port uniqueness and pool:pid uniqueness are
# covered by test_domain_topology.py and enforced at startup by build_registry.


@pytest.mark.parametrize(
    "surface,pool,interface,transport",
    [
        ("jsonrpc-http", "eth-sim", "jsonrpc", "http"),
        ("jsonrpc-ws", "eth-sim", "jsonrpc", "ws"),
        ("grpc", "lava-sim-grpc", "grpc", "http2"),
        ("rest", "lava-sim-rest", "rest", "http"),
        ("tendermintrpc", "lava-sim-tm", "tendermintrpc", "http"),
    ],
)
def test_each_surface_allocates_three_primary_and_three_backup(
    surface,
    pool,
    interface,
    transport,
):
    """Every surface that has a backup tier allocates 3 primary + 3 backup, as
    pool-local pids 1-3 and 4-6.

    Catches an accidental drop of one entry. A 2-provider backup pool shipping
    while the router's values file still lists three fails much later, at relay
    time, as an endpoint the router cannot reach — and that failure reads like a
    router bug rather than a missing listener.
    """
    pids = [
        pid
        for row_pool, _chain, pid, _name, _backup, _group, endpoints in TOPOLOGY
        if row_pool == pool
        for row_interface, row_transport, _port in endpoints
        if row_interface == interface and row_transport == transport
    ]
    # Sorted: which slots exist is the contract, the order of the rows in the
    # table is not. A reorder that changes no allocation must not fail here.
    # Duplicates still fail, because a repeated slot makes the list too long.
    assert sorted(pids, key=int) == ["1", "2", "3", "4", "5", "6"], (
        f"{surface}: expected pool-local pids 1-6 (3 primary + 3 backup) on "
        f"{pool}/{interface}/{transport}, got {pids}"
    )


def test_solana_solo_pool_is_isolated_from_the_solana_primary_pool():
    """The Solana solo router owns its own pool, its own provider and its own
    port, so a /scenario POST aimed at it cannot reconfigure solana-sim.

    Isolation is the point. A shared pool or a shared port would let one
    router's fault injection change another router's traffic, and the failure
    that followed would look like a router bug.

    The port literal is deliberate: 18585 is a deployed contract, named in the
    routers' values files, so a change to it must be a conscious edit rather
    than something a derived expected value absorbs silently.
    """
    solo_rows = [row for row in TOPOLOGY if row[0] == "solana-solo-sim"]
    assert len(solo_rows) == 1, f"solana-solo-sim must hold exactly one provider, got {len(solo_rows)}"

    _pool, chain, pid, name, is_backup, _group, endpoints = solo_rows[0]
    assert chain == "solana", f"solana-solo-sim must serve the solana chain, got {chain!r}"
    assert pid == "1", f"the solo provider is its pool's slot 1, got {pid!r}"
    assert name == "SolanaSoloProvider1", f"unexpected name: {name!r}"
    assert is_backup is False, "the solo provider is not a backup tier"
    assert endpoints == (("jsonrpc", "http", 18585),), f"unexpected endpoints: {endpoints}"

    primary_ports = {
        port for row_pool, _c, _p, _n, _b, _group, eps in TOPOLOGY if row_pool == "solana-sim" for (_i, _t, port) in eps
    }
    assert 18585 not in primary_ports, (
        f"the solo listener shares port 18585 with solana-sim ({sorted(primary_ports)}) — "
        f"a scenario on one router would reach the other"
    )


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
            f"GET /scenario doesn't echo back the mode for {key}. " f"snapshot[{key!r}]={snapshot.get(key)!r}."
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
        status1, _ = _rpc(f"http://127.0.0.1:{port_of('eth-sim', '1')}", "eth_blockNumber")
        assert status1 == 503, (
            f"primary eth-sim:1 returned {status1}, expected 503 (down). "
            "Either state cross-contamination, or the mixed-tier POST "
            "stomped on the primary configuration."
        )
        status4, body4 = _rpc(f"http://127.0.0.1:{port_of('eth-sim', '4')}", "eth_blockNumber")
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
        assert status == 200, f"Control API rejected REST /scenario for {key}: " f"status={status} body={body}"

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
                f"REST backup listener on port {port} ({key}) " f"returned HTTP {e.code} instead of 503"
            )


class TestTmBackupListenersWired:
    """Every lava-sim-tm backup provider must round-trip through both the
    control API and the Tendermint-RPC listener itself."""

    @pytest.mark.parametrize("pid,port", _TM_BACKUPS)
    def test_tm_backup_port_wired(self, sim, pid, port):
        key = f"lava-sim-tm:{pid}"
        status, body = _post(f"{sim['control']}/scenario", {"providers": {key: {"mode": "down"}}})
        assert status == 200, f"Control API rejected TM /scenario for {key}: " f"status={status} body={body}"

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
                pytest.fail(f"TM backup listener on port {port} ({key}) returned " f"{resp.status} instead of 503")
        except urllib.error.HTTPError as e:
            assert e.code == 503, f"TM backup listener on port {port} ({key}) " f"returned HTTP {e.code} instead of 503"


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
        assert status == 200, f"Control API rejected WS /scenario for {key}: " f"status={status} body={body}"

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
        http_port = port_of("eth-sim", pid)
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
        assert status == 200, f"Control API rejected gRPC /scenario for {key}: " f"status={status} body={body}"

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
        return f"http://127.0.0.1:{port_of('eth-sim', '4')}"

    def _set_scenario(self, sim, scenario: dict) -> None:
        status, body = _post(f"{sim['control']}/scenario", {"providers": {self._KEY: dict(scenario)}})
        assert status == 200, f"Control API rejected /scenario for {self._KEY}: " f"status={status} body={body}"

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
            f"error mode: expected error code -32000, " f"got {body['error']['code']!r}. full body={body}"
        )

    # ------------------------------------------------------------------
    # rate_limit mode
    # ------------------------------------------------------------------

    def test_rate_limit_returns_429(self, sim):
        """mode='rate_limit' returns HTTP 429 with a prose body — not a
        JSON-RPC error envelope; a backup provider follows the same shape
        as a primary (see tests/test_simulator.py::test_rate_limit_returns_429)."""
        self._set_scenario(sim, {"mode": "rate_limit"})
        status, body = _rpc(self._backup_url(), "eth_blockNumber")
        assert status == 429, f"rate_limit mode: expected HTTP 429, got {status}. " f"body={body}"
        assert isinstance(body, str), f"rate_limit mode: expected a prose body, got {body!r}"
        assert not body.lstrip().startswith("{"), f"rate_limit mode: body must not look like JSON, got {body!r}"

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
