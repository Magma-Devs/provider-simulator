"""Shared fixtures for the simulator integration tests.

One in-process ``SimulatorServer`` boots ONCE per pytest session, serving the
REAL topology on the REAL ports (18545-18585 + control 19000) — the same
registry, listeners, and control API a deployed pod runs, so the tests cannot
drift from production wiring. Every integration file talks to it through
``sim`` and isolates itself with its own autouse reset fixture (POST
/reset/all before and after each test).

The engine unit-test files (test_domain_*, test_chains_*, test_listener_*,
test_fault_policy, test_control_api, test_wire, test_ws_*) never request these
fixtures, so collecting or running them alone starts no sockets.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from constants import CONTROL_PORT  # noqa: E402
from server import SimulatorServer  # noqa: E402

CONTROL_URL = f"http://127.0.0.1:{CONTROL_PORT}"


def provider_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


@pytest.fixture(scope="session")
def sim_server():
    """The real simulator, in-process, on the real ports. TTL sweep disabled —
    tests own their scenario lifecycle explicitly."""
    server = SimulatorServer(host="127.0.0.1", scenario_ttl_s=0)
    server.start()
    server.wait_ready(20.0)
    yield server
    server.stop()


@pytest.fixture(scope="session")
def sim(sim_server):
    """URL map + registry handle for the shared simulator.

    ``registry`` is the live object behind the running listeners — tests that
    must reach real provider internals (e.g. swapping a CallLog for a smaller
    ring buffer) go through it; everything else should use the control API.
    """
    yield {
        "control": CONTROL_URL,
        "registry": sim_server.registry,
        "server": sim_server,
    }
