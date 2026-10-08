"""What ``SimulatorServer.start()`` wires: three things that no other test shows.

``start()`` gives each provider endpoint its listener. It also starts parts
that are not provider endpoints: the listener of the cache simulator, the RESP
proxy with the store that it forwards to, and the gRPC half, which it leaves
out when ``grpcio`` is not installed.

Three things about these parts had no test:

- The gRPC port of the cache simulator serves what the control routes stage.
  The other tests of the cache simulator build their piece by hand.
- The RESP proxy forwards a command to its store. ``tests/test_resp_wiring.py``
  shows that ``start()`` binds the two RESP listeners of the shared simulator
  and wires the control listener. That simulator has no store behind it, so
  the file cannot show a command that goes through.
- The simulator starts with no ``grpcio``.

A change of ``start()`` must not take one of the three away, and each test below
fails when it does.

Ports: the cache test uses the shared simulator of the session. The two other
tests start a simulator of their own, on this file's own block of ports. The
block is below the range that the kernel gives to client sockets.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

import server as server_module
from constants import CACHE_SIM_PORTS, CONTROL_PORT
from provider_simulator import topology
from tests.resp_fake_store import FakeRespStore

# This file is two folders below the root of the repository.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The simulator of the RESP test: one REST provider, one RESP proxy, one RESP
# control listener, and no cache simulator.
_RESP_SIM_CONTROL, _RESP_REST = 29731, 28731
_RESP_PROXY, _RESP_CONTROL = 28735, 29735
_RESP_ROWS = (
    ("lava-wiring-sim", "lava", "1", "LavaWiringPrimaryProvider1", False, "", (("rest", "http", _RESP_REST),)),
)

# The simulator of the test with no grpcio: one REST provider, one gRPC
# provider and one cache simulator. It runs in a process of its own.
_NO_GRPCIO_CONTROL, _NO_GRPCIO_REST = 29741, 28741
_NO_GRPCIO_GRPC, _NO_GRPCIO_CACHE = 28742, 28745

_BLOCKS_LATEST = "/cosmos/base/tendermint/v1beta1/blocks/latest"


def _call(request: urllib.request.Request) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _get(url: str) -> tuple[int, dict]:
    return _call(urllib.request.Request(url, method="GET"))


def _post(url: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    request.add_header("Content-Type", "application/json")
    return _call(request)


# ── the cache simulator ───────────────────────────────────────────────────────


def _get_relay(grpc, port: int) -> dict:
    """One lookup against a cache simulator, with raw JSON bytes, as the
    smart-router sends it. Returns the decoded reply.

    The call waits until the channel is ready: ``wait_ready()`` of the
    simulator does not wait for the cache port, and the thread of the cache
    simulator starts last."""
    lookup = {
        "request_hash": base64.b64encode(bytes.fromhex("deadbeef")).decode(),
        "block_hash": None,
        "finalized": False,
        "requested_block": 18_000_000,
        "shared_state_id": "",
        "chain_id": "ETH1",
        "seen_block": 0,
        "blocks_hashes_to_heights": None,
    }
    with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
        rpc = channel.unary_unary(
            "/smartrouter.pairing.RelayerCache/GetRelay",
            request_serializer=lambda raw: raw,
            response_deserializer=lambda raw: raw,
        )
        return json.loads(rpc(json.dumps(lookup).encode(), timeout=5, wait_for_ready=True))


def test_start_serves_the_cache_simulator_that_the_control_routes_stage(sim_server):
    """An entry that is staged over HTTP on the control port comes back from the
    gRPC port of the cache simulator, and the lookup shows in the call log of
    the control port. So ``start()`` bound the port, and it gave the listener
    the same cache simulator that the control routes hold."""
    grpc = pytest.importorskip("grpc", reason="the cache simulator speaks gRPC")
    control = f"http://127.0.0.1:{CONTROL_PORT}"
    assert sim_server.cache_sims_enabled is True
    # The count below is the count of this test alone.
    _post(control + "/cache/secondary/reset", {})
    try:
        status, staged = _post(control + "/cache/secondary/entry", {"mode": "hit", "entry": {"result": "0xfeed"}})
        assert status == 200, staged

        reply = _get_relay(grpc, CACHE_SIM_PORTS["secondary"])
        assert json.loads(base64.b64decode(reply["reply"]["data"]))["result"] == "0xfeed"

        status, calls = _get(control + "/cache/secondary/calls")
        assert (status, calls["count"]) == (200, 1)
    finally:
        _post(control + "/cache/secondary/reset", {})


# ── the RESP proxy ────────────────────────────────────────────────────────────


def _a_simulator(rows: tuple, **settings) -> server_module.SimulatorServer:
    """A second simulator with a topology of its own. The table is swapped only
    while this simulator builds its registry."""
    shipped = topology.TOPOLOGY
    topology.TOPOLOGY = rows
    try:
        return server_module.SimulatorServer(host="127.0.0.1", scenario_ttl_s=0, **settings)
    finally:
        topology.TOPOLOGY = shipped


def test_start_wires_the_resp_proxy_to_the_store_that_it_was_given():
    """A command that is sent to the proxy port reaches the store, and the
    answer of the store comes back. The RESP control listener reads the same
    store. So ``start()`` bound both listeners, and it gave each one its
    target."""
    with FakeRespStore() as store:
        simulator = _a_simulator(
            _RESP_ROWS,
            control_port=_RESP_SIM_CONTROL,
            cache_ports={},
            resp_control_port=_RESP_CONTROL,
            resp_proxy_ports={"primary": _RESP_PROXY},
            resp_store=("127.0.0.1", store.port),
        )
        simulator.start()
        try:
            simulator.wait_ready(20.0)
            seen_before = len(store.commands)
            with socket.create_connection(("127.0.0.1", _RESP_PROXY), timeout=5) as router:
                router.sendall(b"*1\r\n$4\r\nPING\r\n")
                assert router.recv(64) == b"+PONG\r\n"
            assert store.commands[seen_before:] == [["PING"]]

            store.put("sr:wiring:probe", "value-1")
            status, keys = _get(f"http://127.0.0.1:{_RESP_CONTROL}/resp/keys")
            assert status == 200, keys
            assert [(entry["key"], entry["value"]) for entry in keys["entries"]] == [("sr:wiring:probe", "value-1")]
        finally:
            simulator.stop()


# ── a machine with no grpcio ──────────────────────────────────────────────────

# One simulator in a process where ``import grpc`` fails. It prints one JSON
# line with what it saw.
_NO_GRPCIO_SCRIPT = """
import json
import socket
import sys
import urllib.error
import urllib.request

# Make each import of grpc fail, as on a machine with no grpcio.
sys.modules["grpc"] = None

import server
from provider_simulator import topology

control_port, rest_port, grpc_port, cache_port = (int(value) for value in sys.argv[1:5])
topology.TOPOLOGY = [
    ("lava-nogrpcio-sim", "lava", "1", "LavaNogrpcioPrimaryProvider1", False, "", (("rest", "http", rest_port),)),
    ("lava-nogrpcio-sim", "lava", "2", "LavaNogrpcioPrimaryProvider2", False, "", (("grpc", "http2", grpc_port),)),
]


def accepts(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


simulator = server.SimulatorServer(
    host="127.0.0.1",
    control_port=control_port,
    scenario_ttl_s=0,
    cache_ports={"secondary": cache_port},
    resp_proxy_ports={},
)
simulator.start()
for port in (control_port, rest_port):
    for _ in range(100):
        if accepts(port):
            break
    else:
        sys.exit(f"port {port} did not open")

control = f"http://127.0.0.1:{control_port}"
ready_status, ready = get(control + "/ready")
print(
    json.dumps(
        {
            "grpc_enabled": simulator.grpc_enabled,
            "cache_sims_enabled": simulator.cache_sims_enabled,
            "rest_status": get(f"http://127.0.0.1:{rest_port}__BLOCKS_LATEST__")[0],
            "health_status": get(control + "/health")[0],
            "ready_status": ready_status,
            "ready_missing_ports": ready.get("missing_ports"),
            "grpc_port_accepts": accepts(grpc_port),
            "cache_port_accepts": accepts(cache_port),
        }
    ),
    flush=True,
)
simulator.stop()
""".replace("__BLOCKS_LATEST__", _BLOCKS_LATEST)


def test_the_simulator_starts_with_no_grpcio_and_serves_its_http_endpoints():
    """``grpcio`` is an optional dependency. With none, ``start()`` leaves out
    the gRPC endpoints and the cache simulator, and it still serves each HTTP
    endpoint and the control API. /ready then answers 503 and names the gRPC
    port that did not open: the simulator serves, and it does not report ready.

    This test records that answer of /ready as it is today. It does not say
    that 503 is the right answer for a simulator that serves HTTP only.

    The simulator runs in a process of its own, because this process has
    ``grpcio`` loaded already."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _NO_GRPCIO_SCRIPT,
            str(_NO_GRPCIO_CONTROL),
            str(_NO_GRPCIO_REST),
            str(_NO_GRPCIO_GRPC),
            str(_NO_GRPCIO_CACHE),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "grpc_enabled": False,
        "cache_sims_enabled": False,
        "rest_status": 200,
        "health_status": 200,
        "ready_status": 503,
        "ready_missing_ports": [_NO_GRPCIO_GRPC],
        "grpc_port_accepts": False,
        "cache_port_accepts": False,
    }
