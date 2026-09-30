import pytest

from provider_simulator.domain.quirks import known_chains
from provider_simulator.domain.registry import build_registry
from provider_simulator.topology import TOPOLOGY, port_of, ports_of

# The one deployment pin: this literal set mirrors the router-side
# values_sim.yml ids (plus the two listener-only pools named in the topology
# docstring). Other tests derive their expectations from TOPOLOGY itself so a
# pool addition is a one-literal change here, nowhere else.
EXPECTED_POOLS = {
    "eth-sim",
    "eth-solo-sim",
    "eth-duo-sim",
    "eth-cv-sim",
    "eth-best-sim",
    "eth-priority-sim",
    "eth-precedence-sim",
    "btc-sim",
    "ln-sim",
    "solana-sim",
    "solana-solo-sim",
    "lava-sim-grpc",
    "lava-sim-rest",
    "lava-sim-tm",
    "lava-cv-rest-sim",
    "lava-cv-tm-sim",
    "eth-cache-writer-sim",
    "eth-cache-reader-sim",
    "eth-resp-sim",
    # The ten failover pools (MAG-3916). Every one carries "failover" in its
    # name so a reader can tell at a glance which pools belong to that suite,
    # and the word after it says what makes that router different.
    "eth-failover-prodlimits-sim",
    "eth-failover-timing-sim",
    "eth-failover-twoaddr-sim",
    "eth-failover-real-sim",
    "eth-failover-cv-sim",
    "eth-failover-archive-sim",
    "eth-failover-mixed-sim",
    "eth-failover-excluded-sim",
    "eth-failover-ineligible-sim",
    "eth-failover-noarchive-sim",
}


def test_every_port_is_unique():
    ports = [port for _pool, _chain, _pid, _n, _b, _group, eps in TOPOLOGY for (_i, _t, port) in eps]
    assert len(ports) == len(set(ports)), "duplicate port in TOPOLOGY"


def test_every_pool_pid_is_unique():
    keys = [(pool, pid) for pool, _chain, pid, _n, _b, _group, _eps in TOPOLOGY]
    assert len(keys) == len(set(keys)), "duplicate (pool, pid) in TOPOLOGY"


def test_every_chain_is_known():
    for _pool, chain, _pid, _n, _b, _group, _eps in TOPOLOGY:
        assert chain in known_chains(), f"unknown chain {chain!r}"


def test_expected_pools_present():
    assert {row[0] for row in TOPOLOGY} == EXPECTED_POOLS


def test_rows_are_structurally_immutable():
    # Tuples all the way down — no caller can corrupt the process-global table.
    assert isinstance(TOPOLOGY, tuple)
    for row in TOPOLOGY:
        assert isinstance(row, tuple)
        assert isinstance(row[6], tuple)
        for spec in row[6]:
            assert isinstance(spec, tuple)


def test_eth_sim_provider_1_has_http_and_ws():
    rows = [r for r in TOPOLOGY if r[0] == "eth-sim" and r[2] == "1"]
    assert len(rows) == 1
    eps = rows[0][6]
    assert (("jsonrpc", "http", 18545) in eps) and (("jsonrpc", "ws", 18557) in eps)


def test_eth_failover_real_sim_has_a_websocket_door_on_every_provider():
    """A caller that hangs up mid-request cannot be reproduced over plain HTTP.
    The behaviour that needs it is a failover one: a healthy node must not lose
    score because the caller disconnected. So one failover pool carries a
    WebSocket door beside its HTTP one, and this is that pool.

    The WebSocket ports must belong to this pool alone. A fault addressed at
    one of these slots must never reach another router's provider.
    """
    rows = {r[2]: r for r in TOPOLOGY if r[0] == "eth-failover-real-sim"}
    assert set(rows) == {"1", "2", "3"}
    assert rows["1"][6] == (("jsonrpc", "http", 18640), ("jsonrpc", "ws", 18670))
    assert rows["2"][6] == (("jsonrpc", "http", 18641), ("jsonrpc", "ws", 18671))
    assert rows["3"][6] == (("jsonrpc", "http", 18642), ("jsonrpc", "ws", 18672))

    ws_ports = {port for r in rows.values() for (_i, transport, port) in r[6] if transport == "ws"}
    other_ports = {
        port
        for pool, _c, _pid, _n, _b, _group, eps in TOPOLOGY
        if pool != "eth-failover-real-sim"
        for (_i, _t, port) in eps
    }
    assert ws_ports.isdisjoint(other_ports), "the new WebSocket ports must belong to this pool alone"


def test_port_of_still_separates_the_two_doors_of_the_failover_real_pool():
    """Adding a WebSocket door must not break an HTTP lookup. port_of raises
    only when ONE interface and transport sits at two addresses; a second
    transport is a different question, so every existing HTTP lookup on this
    pool keeps exactly one answer.
    """
    assert port_of("eth-failover-real-sim", "1") == 18640
    assert port_of("eth-failover-real-sim", "2") == 18641
    assert port_of("eth-failover-real-sim", "3") == 18642
    assert port_of("eth-failover-real-sim", "1", transport="ws") == 18670


def test_eth_duo_sim_has_two_dedicated_providers():
    """Regression guard: eth-duo-sim used to have no row of its own and
    pointed its two upstreams straight at eth-sim's pid 1/2 listeners
    (18545/18546), so a /scenario flip on eth-sim's pid 1 or 2 leaked into
    eth-duo-sim traffic too. It now has its own dedicated pool and ports."""
    rows = {r[2]: r for r in TOPOLOGY if r[0] == "eth-duo-sim"}
    assert set(rows) == {"1", "2"}
    assert rows["1"][6] == (("jsonrpc", "http", 18586),)
    assert rows["2"][6] == (("jsonrpc", "http", 18587),)
    eth_sim_ports = {
        port for pool, _c, _pid, _n, _b, _group, eps in TOPOLOGY if pool == "eth-sim" for (_i, _t, port) in eps
    }
    duo_ports = {port for eps in (rows["1"][6], rows["2"][6]) for (_i, _t, port) in eps}
    assert duo_ports.isdisjoint(eth_sim_ports), "eth-duo-sim must not share ports with eth-sim"


def test_eth_cv_sim_has_six_dedicated_providers():
    """eth-cv-sim carries six providers on its own listeners.

    Six is not a round number picked for comfort. Cross-validation can be
    configured so each provider group must reach its own agreement before the
    groups are compared, and the router rejects such a policy at startup
    unless max-participants >= min-groups * agreement-threshold. Three groups
    each needing two matching answers therefore needs six providers. Drop this
    pool below six and those policies stop being loadable — the router
    crash-loops rather than failing a test, so the guard belongs here.

    The ports must also be disjoint from every other pool: the control API
    keys an injected fault by provider id, so a shared listener would let one
    router's fault reach another router's traffic, and the resulting failure
    would look exactly like a router bug.
    """
    rows = {r[2]: r for r in TOPOLOGY if r[0] == "eth-cv-sim"}
    assert set(rows) == {"1", "2", "3", "4", "5", "6"}
    for pid, expected_port in zip(("1", "2", "3", "4", "5", "6"), (18596, 18597, 18598, 18599, 18600, 18601)):
        assert rows[pid][6] == (("jsonrpc", "http", expected_port),)
        assert rows[pid][1] == "eth"

    cv_ports = {port for r in rows.values() for (_i, _t, port) in r[6]}
    other_ports = {
        port for pool, _c, _pid, _n, _b, _g, eps in TOPOLOGY if pool != "eth-cv-sim" for (_i, _t, port) in eps
    }
    assert cv_ports.isdisjoint(other_ports), "eth-cv-sim must not share ports with any other pool"


def test_lava_rest_and_grpc_provider_1_are_distinct_pools():
    rest1 = [r for r in TOPOLOGY if r[0] == "lava-sim-rest" and r[2] == "1"]
    grpc1 = [r for r in TOPOLOGY if r[0] == "lava-sim-grpc" and r[2] == "1"]
    assert rest1 and grpc1
    assert rest1[0][6] == (("rest", "http", 18551),)
    assert grpc1[0][6] == (("grpc", "http2", 18548),)


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


def test_every_endpoint_in_the_table_is_reachable_through_a_lookup():
    """Completeness: every listener the server binds is addressable. A row shape
    no lookup can read would bind a port no test can reach, and the gap would
    look like a dead listener rather than a lookup that cannot express it.

    This asks ports_of rather than port_of, and compares the WHOLE set of ports
    each door serves rather than one port at a time. One shipped row —
    eth-failover-twoaddr-sim:1 — serves jsonrpc/http at two addresses, and
    port_of refuses that question on purpose instead of answering with whichever
    address it met first. Asking one port at a time could not see the second
    address at all.

    port_of is checked too, in both directions: it answers wherever the question
    has one answer, and refuses wherever it does not. A regression that made it
    answer an ambiguous question by picking one would fail here.
    """
    for pool, _chain, pid, _n, _b, _group, endpoints in TOPOLOGY:
        by_door: dict[tuple[str, str], set[int]] = {}
        for interface, transport, port in endpoints:
            by_door.setdefault((interface, transport), set()).add(port)
        for (interface, transport), ports in by_door.items():
            found = set(ports_of(pool, pid, interface, transport))
            assert found == ports, (
                f"ports_of returned {sorted(found)} for {pool}:{pid} "
                f"{interface}/{transport}, the table says {sorted(ports)}"
            )
            if len(ports) == 1:
                assert port_of(pool, pid, interface, transport) == next(iter(ports))
            else:
                with pytest.raises(KeyError):
                    port_of(pool, pid, interface, transport)


def test_the_two_address_pool_really_serves_two_addresses():
    """The one pool whose whole purpose is the two-address shape, pinned as a
    literal.

    The test above compares each lookup against the table, so if somebody
    removed the second address BOTH sides would change together and it would
    still pass — it would simply stop exercising the shape. Then
    eth-failover-twoaddr-sim would be an ordinary one-address provider, its
    router's test would pass without ever moving traffic between two addresses,
    and nothing would say so.

    Measured: deleting the second address from the row leaves the test above
    green. That is why this one exists and why the ports are written out rather
    than read from the row.
    """
    rows = [row for row in TOPOLOGY if row[0] == "eth-failover-twoaddr-sim"]
    assert len(rows) == 1, "the pool holds exactly one provider — that is the point of it"
    _pool, chain, pid, name, is_backup, group, endpoints = rows[0]
    assert chain == "eth"
    assert pid == "1"
    assert name == "EthFailoverTwoaddrSoloProvider1"
    assert is_backup is False, "one provider cannot be the tier behind itself"
    assert group == "", "no cross-validation policy here, so no group to claim"
    assert endpoints == (("jsonrpc", "http", 18638), ("jsonrpc", "http", 18639)), (
        "one provider, the SAME interface and transport, TWO ports. A router "
        "gives it two node-urls; a test silences one and requires the traffic to "
        "move to the other."
    )
    assert sorted(ports_of("eth-failover-twoaddr-sim", "1")) == [18638, 18639]


# ── one provider, two addresses on the same door ──────────────────────────────
#
# A provider can serve the SAME interface and transport at two DIFFERENT ports.
# One shipped row does it — eth-failover-twoaddr-sim:1, added for MAG-3916 —
# and these tests still supply their own rows beside it. Two reasons they stay:
# they cover the shapes no pool has yet (three addresses, a mix of shared and
# unshared doors), and they keep working if that pool is ever renamed or
# retired, which is when a helper quietly stops being tested.

# Ports chosen above everything the real table binds, so a mistake here cannot
# collide with a real provider and read as that provider misbehaving.
_TWO_ADDRESS_ROWS = (
    (
        "scratch-sim",
        "eth",
        "1",
        "ScratchProvider1",
        False,
        "",
        (("jsonrpc", "http", 18901), ("jsonrpc", "http", 18902)),
    ),
    ("scratch-sim", "eth", "2", "ScratchProvider2", False, "", (("jsonrpc", "http", 18903),)),
)


@pytest.fixture
def two_address_topology(monkeypatch):
    """Replace the table with two rows: one provider on two addresses, one on one.

    The single-address row is the control. Without it a bug that returned every
    port in the table would pass every assertion below.
    """
    monkeypatch.setattr("provider_simulator.topology.TOPOLOGY", _TWO_ADDRESS_ROWS)
    return _TWO_ADDRESS_ROWS


def test_ports_of_returns_one_port_for_an_ordinary_provider():
    """Every provider in the shipped table serves one port per door, so this is
    the shape almost every caller meets."""
    assert ports_of("eth-sim", "1") == (18545,)
    assert ports_of("eth-sim", "1", transport="ws") == (18557,)
    assert ports_of("lava-sim-grpc", "1", "grpc", "http2") == (ports_of("lava-sim-grpc", "1", "grpc", "http2")[0],)
    assert len(ports_of("btc-sim", "2")) == 1


def test_ports_of_returns_every_address_in_table_order(two_address_topology):
    """Both addresses, in the order the table lists them.

    Order matters because a test that silences 'the first address' and probes
    'the second' needs the two to mean the same thing on every run.
    """
    assert ports_of("scratch-sim", "1") == (18901, 18902)
    assert ports_of("scratch-sim", "2") == (18903,), "the single-address control must still return one"


def test_port_of_refuses_a_provider_with_two_addresses(two_address_topology):
    """``port_of`` must not answer a question with two answers.

    Returning the first would hand back one of two addresses with nothing said.
    A test that silenced the other one would then probe the address it did NOT
    silence and report that silencing a node changed nothing — a false pass on
    the exact behaviour it was written to check.
    """
    with pytest.raises(KeyError) as excinfo:
        port_of("scratch-sim", "1")
    message = str(excinfo.value)
    assert "scratch-sim:1" in message
    assert "2 addresses" in message, f"the error must say how many, got: {message}"
    assert "18901" in message and "18902" in message, f"it must name both ports, got: {message}"
    assert "ports_of" in message, f"it must name the call that does answer, got: {message}"

    # The control: one address is still a question with one answer.
    assert port_of("scratch-sim", "2") == 18903


def test_ports_of_raises_the_same_misses_as_port_of(two_address_topology):
    """A miss is a typo either way, so both calls must fail the same way.

    An empty tuple would be worse than an exception here: the caller would
    iterate over nothing and report that no address was reached.
    """
    with pytest.raises(KeyError) as excinfo:
        ports_of("scratch-sim-typo", "1")
    assert "known pools" in str(excinfo.value)

    with pytest.raises(KeyError) as excinfo:
        ports_of("scratch-sim", "99")
    assert "slots" in str(excinfo.value)

    with pytest.raises(KeyError) as excinfo:
        ports_of("scratch-sim", "1", transport="ws")
    assert "does not serve" in str(excinfo.value)


def test_the_registry_binds_both_addresses_of_one_provider():
    """The helper is only half of it: the simulator must actually bind both.

    Without this the pair could be readable and unreachable — two ports in the
    table, one listener, and a test blaming the router for a node that was never
    listening.
    """
    reg = build_registry(rows=_TWO_ADDRESS_ROWS)
    assert reg.ports() == [18901, 18902, 18903], f"every address must bind a listener, got {reg.ports()}"

    # Both addresses resolve to the SAME provider, which is the point: one node,
    # two ways in. Two providers would be a different topology and a different test.
    first, _ = reg.by_port(18901)
    second, _ = reg.by_port(18902)
    assert first.key == second.key == "scratch-sim:1"
    # The control: the single-address provider is a different one.
    other, _ = reg.by_port(18903)
    assert other.key == "scratch-sim:2"


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


# The names agreed on 2026-08-17. Literals on purpose: a person chose these, so
# the only honest check is the chosen name against the one in the table.
# Deriving the expected value would compare the table against itself.
AGREED_NAMES = {
    # Three selection-policy pools. Nothing tells the providers inside one
    # apart, so each takes the role its pool is named for: Best, Priority,
    # Precedence. Those three roles were reserved for exactly these pools.
    ("eth-best-sim", "1"): "EthBestProvider1",
    ("eth-best-sim", "2"): "EthBestProvider2",
    ("eth-best-sim", "3"): "EthBestProvider3",
    ("eth-priority-sim", "1"): "EthPriorityProvider1",
    ("eth-priority-sim", "2"): "EthPriorityProvider2",
    ("eth-priority-sim", "3"): "EthPriorityProvider3",
    ("eth-precedence-sim", "1"): "EthPrecedenceProvider1",
    ("eth-precedence-sim", "2"): "EthPrecedenceProvider2",
    # Cross-validation pool: six providers, nothing to tell them apart, so
    # Primary — the same choice btc-sim, ln-sim and solana-sim make.
    ("eth-cv-sim", "1"): "EthCvPrimaryProvider1",
    ("eth-cv-sim", "2"): "EthCvPrimaryProvider2",
    ("eth-cv-sim", "3"): "EthCvPrimaryProvider3",
    ("eth-cv-sim", "4"): "EthCvPrimaryProvider4",
    ("eth-cv-sim", "5"): "EthCvPrimaryProvider5",
    ("eth-cv-sim", "6"): "EthCvPrimaryProvider6",
    ("eth-sim", "1"): "EthPrimaryProvider1",
    ("eth-sim", "2"): "EthPrimaryProvider2",
    ("eth-sim", "3"): "EthPrimaryProvider3",
    ("eth-sim", "4"): "EthBackupProvider4",
    ("eth-sim", "5"): "EthBackupProvider5",
    ("eth-sim", "6"): "EthBackupProvider6",
    ("eth-solo-sim", "1"): "EthSoloProvider1",
    ("eth-duo-sim", "1"): "EthDuoHighProvider1",
    ("eth-duo-sim", "2"): "EthDuoLowProvider2",
    ("btc-sim", "1"): "BtcPrimaryProvider1",
    ("btc-sim", "2"): "BtcPrimaryProvider2",
    ("btc-sim", "3"): "BtcPrimaryProvider3",
    ("ln-sim", "1"): "LnPrimaryProvider1",
    ("ln-sim", "2"): "LnPrimaryProvider2",
    ("ln-sim", "3"): "LnPrimaryProvider3",
    ("solana-sim", "1"): "SolanaPrimaryProvider1",
    ("solana-sim", "2"): "SolanaPrimaryProvider2",
    ("solana-sim", "3"): "SolanaPrimaryProvider3",
    ("solana-solo-sim", "1"): "SolanaSoloProvider1",
    ("lava-sim-grpc", "1"): "LavaGrpcPrimaryProvider1",
    ("lava-sim-grpc", "2"): "LavaGrpcPrimaryProvider2",
    ("lava-sim-grpc", "3"): "LavaGrpcPrimaryProvider3",
    ("lava-sim-grpc", "4"): "LavaGrpcBackupProvider4",
    ("lava-sim-grpc", "5"): "LavaGrpcBackupProvider5",
    ("lava-sim-grpc", "6"): "LavaGrpcBackupProvider6",
    ("lava-sim-rest", "1"): "LavaRestPrimaryProvider1",
    ("lava-sim-rest", "2"): "LavaRestPrimaryProvider2",
    ("lava-sim-rest", "3"): "LavaRestPrimaryProvider3",
    ("lava-sim-rest", "4"): "LavaRestBackupProvider4",
    ("lava-sim-rest", "5"): "LavaRestBackupProvider5",
    ("lava-sim-rest", "6"): "LavaRestBackupProvider6",
    ("lava-sim-tm", "1"): "LavaTmPrimaryProvider1",
    ("lava-sim-tm", "2"): "LavaTmPrimaryProvider2",
    ("lava-sim-tm", "3"): "LavaTmPrimaryProvider3",
    ("lava-sim-tm", "4"): "LavaTmBackupProvider4",
    ("lava-sim-tm", "5"): "LavaTmBackupProvider5",
    ("lava-sim-tm", "6"): "LavaTmBackupProvider6",
    # The REST cross-validation pool. Six primaries and no backup, so every
    # slot takes the Primary role. The pool contributes "LavaCvRest" — its own
    # name minus the trailing "-sim" — and the role adds no word the pool
    # already put there.
    ("lava-cv-rest-sim", "1"): "LavaCvRestPrimaryProvider1",
    ("lava-cv-rest-sim", "2"): "LavaCvRestPrimaryProvider2",
    ("lava-cv-rest-sim", "3"): "LavaCvRestPrimaryProvider3",
    ("lava-cv-rest-sim", "4"): "LavaCvRestPrimaryProvider4",
    ("lava-cv-rest-sim", "5"): "LavaCvRestPrimaryProvider5",
    ("lava-cv-rest-sim", "6"): "LavaCvRestPrimaryProvider6",
    ("lava-cv-tm-sim", "1"): "LavaCvTmPrimaryProvider1",
    ("lava-cv-tm-sim", "2"): "LavaCvTmPrimaryProvider2",
    ("lava-cv-tm-sim", "3"): "LavaCvTmPrimaryProvider3",
    ("lava-cv-tm-sim", "4"): "LavaCvTmPrimaryProvider4",
    ("lava-cv-tm-sim", "5"): "LavaCvTmPrimaryProvider5",
    ("lava-cv-tm-sim", "6"): "LavaCvTmPrimaryProvider6",
    # The two-tier cache pair. Nothing tells the three primaries in one pool
    # apart, so they take Primary, the same choice btc-sim and eth-cv-sim make.
    # Slots 4 to 6 are the backup tier, so they take Backup. The pool word
    # "cache" is not repeated by either role, so neither is dropped.
    ("eth-cache-writer-sim", "1"): "EthCacheWriterPrimaryProvider1",
    ("eth-cache-writer-sim", "2"): "EthCacheWriterPrimaryProvider2",
    ("eth-cache-writer-sim", "3"): "EthCacheWriterPrimaryProvider3",
    ("eth-cache-writer-sim", "4"): "EthCacheWriterBackupProvider4",
    ("eth-cache-writer-sim", "5"): "EthCacheWriterBackupProvider5",
    ("eth-cache-writer-sim", "6"): "EthCacheWriterBackupProvider6",
    ("eth-cache-reader-sim", "1"): "EthCacheReaderPrimaryProvider1",
    ("eth-cache-reader-sim", "2"): "EthCacheReaderPrimaryProvider2",
    ("eth-cache-reader-sim", "3"): "EthCacheReaderPrimaryProvider3",
    ("eth-cache-reader-sim", "4"): "EthCacheReaderBackupProvider4",
    ("eth-cache-reader-sim", "5"): "EthCacheReaderBackupProvider5",
    ("eth-cache-reader-sim", "6"): "EthCacheReaderBackupProvider6",
    # eth-resp-sim: the role is Primary and it survives, because there is no
    # role called Resp for the pool's own word to merge with. The same shape as
    # eth-cv-sim above. EthRespProvider1 was proposed and is wrong -- the naming
    # rule rejects an empty role.
    ("eth-resp-sim", "1"): "EthRespPrimaryProvider1",
    ("eth-resp-sim", "2"): "EthRespPrimaryProvider2",
    ("eth-resp-sim", "3"): "EthRespPrimaryProvider3",
    # ── The ten failover pools, named 2026-09-28 (MAG-3916) ──────────────────
    #
    # Every name follows the standing rule <Pool><Role>Provider<slot>: the pool
    # name CamelCased with "-sim" dropped, then the role, then the slot number
    # the control API answers to. The roles are the ones already in use —
    # Primary for a provider the router tries first, Backup for one it keeps
    # until every primary has failed.
    #
    # eth-failover-twoaddr-sim is the exception, and deliberately: it holds ONE
    # provider serving two addresses, so it has neither a primary tier nor a
    # backup tier to belong to. Solo is the role already used for that shape by
    # eth-solo-sim and solana-solo-sim.
    ("eth-failover-prodlimits-sim", "1"): "EthFailoverProdlimitsPrimaryProvider1",
    ("eth-failover-prodlimits-sim", "2"): "EthFailoverProdlimitsPrimaryProvider2",
    ("eth-failover-prodlimits-sim", "3"): "EthFailoverProdlimitsPrimaryProvider3",
    ("eth-failover-prodlimits-sim", "4"): "EthFailoverProdlimitsBackupProvider4",
    ("eth-failover-prodlimits-sim", "5"): "EthFailoverProdlimitsBackupProvider5",
    ("eth-failover-prodlimits-sim", "6"): "EthFailoverProdlimitsBackupProvider6",
    ("eth-failover-timing-sim", "1"): "EthFailoverTimingPrimaryProvider1",
    ("eth-failover-timing-sim", "2"): "EthFailoverTimingPrimaryProvider2",
    ("eth-failover-timing-sim", "3"): "EthFailoverTimingPrimaryProvider3",
    ("eth-failover-twoaddr-sim", "1"): "EthFailoverTwoaddrSoloProvider1",
    ("eth-failover-real-sim", "1"): "EthFailoverRealPrimaryProvider1",
    ("eth-failover-real-sim", "2"): "EthFailoverRealPrimaryProvider2",
    ("eth-failover-real-sim", "3"): "EthFailoverRealPrimaryProvider3",
    ("eth-failover-cv-sim", "1"): "EthFailoverCvPrimaryProvider1",
    ("eth-failover-cv-sim", "2"): "EthFailoverCvPrimaryProvider2",
    ("eth-failover-cv-sim", "3"): "EthFailoverCvPrimaryProvider3",
    ("eth-failover-archive-sim", "1"): "EthFailoverArchivePrimaryProvider1",
    ("eth-failover-archive-sim", "2"): "EthFailoverArchivePrimaryProvider2",
    ("eth-failover-archive-sim", "3"): "EthFailoverArchivePrimaryProvider3",
    ("eth-failover-archive-sim", "4"): "EthFailoverArchiveBackupProvider4",
    ("eth-failover-archive-sim", "5"): "EthFailoverArchiveBackupProvider5",
    ("eth-failover-archive-sim", "6"): "EthFailoverArchiveBackupProvider6",
    ("eth-failover-mixed-sim", "1"): "EthFailoverMixedPrimaryProvider1",
    ("eth-failover-mixed-sim", "2"): "EthFailoverMixedPrimaryProvider2",
    ("eth-failover-mixed-sim", "3"): "EthFailoverMixedPrimaryProvider3",
    ("eth-failover-mixed-sim", "4"): "EthFailoverMixedBackupProvider4",
    ("eth-failover-mixed-sim", "5"): "EthFailoverMixedBackupProvider5",
    ("eth-failover-mixed-sim", "6"): "EthFailoverMixedBackupProvider6",
    ("eth-failover-excluded-sim", "1"): "EthFailoverExcludedPrimaryProvider1",
    ("eth-failover-excluded-sim", "2"): "EthFailoverExcludedPrimaryProvider2",
    ("eth-failover-excluded-sim", "3"): "EthFailoverExcludedBackupProvider3",
    ("eth-failover-excluded-sim", "4"): "EthFailoverExcludedBackupProvider4",
    ("eth-failover-ineligible-sim", "1"): "EthFailoverIneligiblePrimaryProvider1",
    ("eth-failover-ineligible-sim", "2"): "EthFailoverIneligiblePrimaryProvider2",
    ("eth-failover-ineligible-sim", "3"): "EthFailoverIneligibleBackupProvider3",
    ("eth-failover-ineligible-sim", "4"): "EthFailoverIneligibleBackupProvider4",
    ("eth-failover-noarchive-sim", "1"): "EthFailoverNoarchivePrimaryProvider1",
    ("eth-failover-noarchive-sim", "2"): "EthFailoverNoarchivePrimaryProvider2",
    ("eth-failover-noarchive-sim", "3"): "EthFailoverNoarchiveBackupProvider3",
    ("eth-failover-noarchive-sim", "4"): "EthFailoverNoarchiveBackupProvider4",
}

# The pools that HAVE a backup tier, and which of their slots it is. The router
# consults a backup only after the primary tier is exhausted, and the values file
# marks them is_backup.
#
# Two shapes, because a backup tier does not always start at slot 4. A
# six-provider pool has three primaries and slots 4 to 6 behind them. A
# four-provider pool has two primaries and slots 3 and 4 behind them. Writing
# both out is the point: a single "the last three slots" rule would silently
# accept a pool whose tiers were split somewhere else.
_SIX_PROVIDER_POOLS_WITH_A_BACKUP_TIER = (
    "eth-sim",
    "lava-sim-grpc",
    "lava-sim-rest",
    "lava-sim-tm",
    "eth-cache-writer-sim",
    "eth-cache-reader-sim",
    # Three of the ten failover pools (MAG-3916). Each needs a full three-primary
    # tier to fail before the backups answer.
    "eth-failover-prodlimits-sim",
    "eth-failover-archive-sim",
    "eth-failover-mixed-sim",
)

# Four-provider failover pools (MAG-3916): two primaries, then two backups. Two
# primaries are enough where the test only has to exhaust the primary tier, and
# four providers cost two ports each instead of three.
_FOUR_PROVIDER_POOLS_WITH_A_BACKUP_TIER = (
    "eth-failover-excluded-sim",
    "eth-failover-ineligible-sim",
    "eth-failover-noarchive-sim",
)

# eth-cache-writer-sim and eth-cache-reader-sim carry a backup tier because the
# router answers from either cache tier BEFORE it picks any provider, primary
# tier and backup tier alike. Without a backup tier a test cannot tell "the
# router never reached the backup" from "there was no backup to reach".
#
# eth-cv-sim, lava-cv-rest-sim and lava-cv-tm-sim are six-provider pools too and
# are absent on purpose: all three are cross-validation topologies,
# cross-validation never reaches a backup, and a provider labelled backup there
# would claim a group the router can never count. Their slots 4 to 6 are
# ordinary primaries. eth-failover-cv-sim is absent for the same reason, and it
# holds three providers rather than six.
#
# Three more failover pools are absent because they have no second tier at all:
# eth-failover-timing-sim and eth-failover-real-sim are three primaries each, and
# eth-failover-twoaddr-sim is a single provider serving two addresses.
AGREED_BACKUPS = {(pool, pid) for pool in _SIX_PROVIDER_POOLS_WITH_A_BACKUP_TIER for pid in ("4", "5", "6")} | {
    (pool, pid) for pool in _FOUR_PROVIDER_POOLS_WITH_A_BACKUP_TIER for pid in ("3", "4")
}


def test_every_row_carries_the_agreed_name():
    """The name is what the router reports in Lava-Provider-Address, and it is
    the only thing a test can use to learn which provider served a request.
    Until it lived here it was written by hand in three repositories, and no
    two copies were ever compared."""
    named = {(pool, pid): name for pool, _chain, pid, name, _backup, _group, _eps in TOPOLOGY}
    assert named == AGREED_NAMES


def test_no_two_providers_share_a_name_once_lowercased():
    """The helm chart renders every name through lower before the router reads
    it, so two names differing only in case arrive identical. A router refuses
    to start when two of its providers share a name, so comparing the names as
    written would miss the collision that actually stops a deploy."""
    seen: dict[str, str] = {}
    for pool, _chain, pid, name, _backup, _group, _eps in TOPOLOGY:
        low = name.lower()
        clash = seen.get(low)
        assert clash is None, f"{pool}:{pid} and {clash} both lowercase to {low!r}"
        seen[low] = f"{pool}:{pid}"


def test_every_name_is_non_empty_and_carries_no_space():
    """The chart renders the name through lower and a space-to-hyphen replace.
    A space would silently become a hyphen, so the deployed name would differ
    from the one written here and every comparison against it would miss."""
    for pool, _chain, pid, name, _backup, _group, _eps in TOPOLOGY:
        assert name, f"{pool}:{pid} has no name"
        assert " " not in name, f"{pool}:{pid} has a space in its name {name!r}"


def test_the_backup_rows_are_the_ones_the_values_file_marks():
    """Which providers sit in the backup tier is a fact about the deployment,
    not something the simulator can work out. Nothing in this package behaves
    differently for a backup provider, so this column exists only to record
    the fact for a caller that needs it."""
    flagged = {(pool, pid) for pool, _c, pid, _n, is_backup, _group, _e in TOPOLOGY if is_backup}
    assert flagged == AGREED_BACKUPS


def test_every_backup_flag_is_a_real_boolean():
    """A truthy string would pass an ``if`` and read as a backup for ever."""
    for pool, _chain, pid, _name, is_backup, _group, _eps in TOPOLOGY:
        assert (
            is_backup is True or is_backup is False
        ), f"{pool}:{pid} has is_backup={is_backup!r}, which is not True or False"
