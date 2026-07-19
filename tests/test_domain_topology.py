import constants
from provider_simulator.domain.quirks import known_chains
from provider_simulator.topology import TOPOLOGY

# The one deployment pin: this literal set mirrors the router-side
# values_sim.yml ids (plus the two listener-only pools named in the topology
# docstring). Other tests derive their expectations from TOPOLOGY itself so a
# pool addition is a one-literal change here, nowhere else.
EXPECTED_POOLS = {
    "eth-sim",
    "eth-solo-sim",
    "btc-sim",
    "ln-sim",
    "solana-sim",
    "solana-solo-sim",
    "lava-sim-grpc",
    "lava-sim-rest",
    "lava-sim-tm",
}


def test_every_port_is_unique():
    ports = [port for _pool, _chain, _pid, eps in TOPOLOGY for (_i, _t, port) in eps]
    assert len(ports) == len(set(ports)), "duplicate port in TOPOLOGY"


def test_every_pool_pid_is_unique():
    keys = [(pool, pid) for pool, _chain, pid, _eps in TOPOLOGY]
    assert len(keys) == len(set(keys)), "duplicate (pool, pid) in TOPOLOGY"


def test_every_chain_is_known():
    for _pool, chain, _pid, _eps in TOPOLOGY:
        assert chain in known_chains(), f"unknown chain {chain!r}"


def test_expected_pools_present():
    assert {row[0] for row in TOPOLOGY} == EXPECTED_POOLS


def test_rows_are_structurally_immutable():
    # Tuples all the way down — no caller can corrupt the process-global table.
    assert isinstance(TOPOLOGY, tuple)
    for row in TOPOLOGY:
        assert isinstance(row, tuple)
        assert isinstance(row[3], tuple)
        for spec in row[3]:
            assert isinstance(spec, tuple)


def test_eth_sim_provider_1_has_http_and_ws():
    rows = [r for r in TOPOLOGY if r[0] == "eth-sim" and r[2] == "1"]
    assert len(rows) == 1
    eps = rows[0][3]
    assert (("jsonrpc", "http", 18545) in eps) and (("jsonrpc", "ws", 18557) in eps)


def test_lava_rest_and_grpc_provider_1_are_distinct_pools():
    rest1 = [r for r in TOPOLOGY if r[0] == "lava-sim-rest" and r[2] == "1"]
    grpc1 = [r for r in TOPOLOGY if r[0] == "lava-sim-grpc" and r[2] == "1"]
    assert rest1 and grpc1
    assert rest1[0][3] == (("rest", "http", 18551),)
    assert grpc1[0][3] == (("grpc", "http2", 18548),)


def test_topology_ports_match_the_listener_ports_the_server_binds():
    """Cross-validate the table against constants.py — the port truth server.py
    actually binds from. Without this check the two sources drift silently:
    a listener added to constants.py but not here would serve traffic the
    registry can't resolve."""
    constants_ports = set()
    for dct in (
        constants.ETH_ALL_PORTS,  # eth primary + backup + solo
        constants.BTC_PRIMARY_PORTS,
        constants.LN_PRIMARY_PORTS,
        constants.SOLANA_PRIMARY_PORTS,
        constants.SOLANA_SOLO_PORTS,
        constants.GRPC_PRIMARY_PORTS,
        constants.GRPC_BACKUP_PORTS,
        constants.REST_PRIMARY_PORTS,
        constants.REST_BACKUP_PORTS,
        constants.TM_PRIMARY_PORTS,
        constants.TM_BACKUP_PORTS,
        constants.WS_PRIMARY_PORTS,
        constants.WS_BACKUP_PORTS,
    ):
        constants_ports.update(dct.values())
    topology_ports = {port for _p, _c, _pid, eps in TOPOLOGY for (_i, _t, port) in eps}
    assert topology_ports == constants_ports, (
        f"TOPOLOGY and constants.py disagree: only in topology "
        f"{sorted(topology_ports - constants_ports)}, only in constants "
        f"{sorted(constants_ports - topology_ports)}"
    )
