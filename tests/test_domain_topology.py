import pytest

from provider_simulator.domain.quirks import known_chains
from provider_simulator.topology import TOPOLOGY, port_of

# The one deployment pin: this literal set mirrors the router-side
# values_sim.yml ids (plus the two listener-only pools named in the topology
# docstring). Other tests derive their expectations from TOPOLOGY itself so a
# pool addition is a one-literal change here, nowhere else.
EXPECTED_POOLS = {
    "eth-sim",
    "eth-solo-sim",
    "eth-duo-sim",
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


def test_eth_duo_sim_has_two_dedicated_providers():
    """Regression guard: eth-duo-sim used to have no row of its own and
    pointed its two upstreams straight at eth-sim's pid 1/2 listeners
    (18545/18546), so a /scenario flip on eth-sim's pid 1 or 2 leaked into
    eth-duo-sim traffic too. It now has its own dedicated pool and ports."""
    rows = {r[2]: r for r in TOPOLOGY if r[0] == "eth-duo-sim"}
    assert set(rows) == {"1", "2"}
    assert rows["1"][3] == (("jsonrpc", "http", 18586),)
    assert rows["2"][3] == (("jsonrpc", "http", 18587),)
    eth_sim_ports = {
        port for pool, _c, _pid, eps in TOPOLOGY if pool == "eth-sim" for (_i, _t, port) in eps
    }
    duo_ports = {port for eps in (rows["1"][3], rows["2"][3]) for (_i, _t, port) in eps}
    assert duo_ports.isdisjoint(eth_sim_ports), "eth-duo-sim must not share ports with eth-sim"


def test_lava_rest_and_grpc_provider_1_are_distinct_pools():
    rest1 = [r for r in TOPOLOGY if r[0] == "lava-sim-rest" and r[2] == "1"]
    grpc1 = [r for r in TOPOLOGY if r[0] == "lava-sim-grpc" and r[2] == "1"]
    assert rest1 and grpc1
    assert rest1[0][3] == (("rest", "http", 18551),)
    assert grpc1[0][3] == (("grpc", "http2", 18548),)


# ── port_of — the one way a caller turns a provider address into a port ──────
#
# Ports used to live in a second table in constants.py, keyed by an older
# provider numbering that disagreed with the pool-local pids here for six
# pools. Nothing read those keys as identity and the tests that did import them
# discarded the keys and re-typed the pool-local pids by hand. The table below
# is now the only source, and port_of is the only reader.


def test_port_of_returns_the_port_the_table_declares():
    """The literals are deliberate. Deriving them from TOPOLOGY would compare
    the table against itself and pass on a wrong port. These numbers are a
    deployed contract — the routers' values files point at them."""
    assert port_of("eth-sim", "1") == 18545
    assert port_of("eth-sim", "3") == 18547
    assert port_of("btc-sim", "2") == 18576
    assert port_of("solana-solo-sim", "1") == 18585
    assert port_of("eth-duo-sim", "2") == 18587


def test_port_of_separates_the_two_transports_of_one_jsonrpc_provider():
    """A JSON-RPC provider serves http and ws on different ports. One pid, two
    answers — so a caller that wants the websocket door must say so."""
    assert port_of("eth-sim", "1", transport="http") == 18545
    assert port_of("eth-sim", "1", transport="ws") == 18557
    assert port_of("eth-sim", "1") == port_of("eth-sim", "1", transport="http")


def test_port_of_distinguishes_the_same_slot_in_different_pools():
    """Slot 1 exists in every pool and means a different machine in each. This
    is the defect the change exists to remove: a lookup keyed on the number
    alone cannot tell these apart."""
    slot_one_ports = {
        pool: port_of(pool, "1", interface, transport)
        for pool, interface, transport in (
            ("eth-sim", "jsonrpc", "http"),
            ("eth-solo-sim", "jsonrpc", "http"),
            ("btc-sim", "jsonrpc", "http"),
            ("ln-sim", "jsonrpc", "http"),
            ("solana-sim", "jsonrpc", "http"),
            ("solana-solo-sim", "jsonrpc", "http"),
            ("lava-sim-grpc", "grpc", "http2"),
            ("lava-sim-rest", "rest", "http"),
            ("lava-sim-tm", "tendermintrpc", "http"),
        )
    }
    assert len(set(slot_one_ports.values())) == len(
        slot_one_ports
    ), f"two pools' slot 1 resolved to the same port: {slot_one_ports}"


def test_port_of_reaches_every_endpoint_in_the_table():
    """Completeness: every listener the server binds is addressable through
    port_of. A row shape port_of cannot read would bind a port no test can
    reach, and the gap would look like a dead listener rather than a lookup
    that cannot express it."""
    for pool, _chain, pid, endpoints in TOPOLOGY:
        for interface, transport, port in endpoints:
            assert (
                port_of(pool, pid, interface, transport) == port
            ), f"port_of could not resolve {pool}:{pid} {interface}/{transport}"


def test_port_of_raises_on_an_unknown_pool_and_names_the_known_ones():
    """A miss must fail loudly. A silent fallback would hand the caller some
    other provider's port and the test would measure the wrong machine."""
    with pytest.raises(KeyError) as excinfo:
        port_of("eth-sim-typo", "1")
    message = str(excinfo.value)
    assert "eth-sim-typo" in message
    assert "known pools" in message, "an unknown pool must answer with the pool list"
    assert "eth-sim" in message, "the pool list must name the real pools"


def test_port_of_raises_on_an_unknown_slot_and_names_that_pools_slots():
    """The message must answer the question the caller asked. The pool was
    right and the slot was wrong, so listing the known pools would send the
    reader looking in the wrong place."""
    with pytest.raises(KeyError) as excinfo:
        port_of("eth-sim", "99")
    message = str(excinfo.value)
    assert "eth-sim:99" in message
    assert "slots" in message and "'1'" in message, f"must list eth-sim's slots, got: {message}"
    assert "known pools" not in message, f"an unknown slot must not answer with pools: {message}"


def test_port_of_raises_when_the_provider_does_not_serve_that_door():
    """gRPC rides http2, so the default http transport must not silently
    resolve to something else. The error names what the provider does serve."""
    with pytest.raises(KeyError) as excinfo:
        port_of("lava-sim-grpc", "1")
    message = str(excinfo.value)
    assert "lava-sim-grpc:1" in message
    assert "grpc/http2" in message, "the error must name the endpoints it does serve"


def test_port_of_raises_when_a_provider_has_no_websocket_door():
    """Only eth-sim's providers have a ws endpoint. Asking any other pool for
    one is a mistake, not an empty answer."""
    with pytest.raises(KeyError):
        port_of("btc-sim", "1", transport="ws")
