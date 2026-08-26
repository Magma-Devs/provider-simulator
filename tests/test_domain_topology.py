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


def test_port_of_reaches_every_endpoint_in_the_table():
    """Completeness: every listener the server binds is addressable through
    port_of. A row shape port_of cannot read would bind a port no test can
    reach, and the gap would look like a dead listener rather than a lookup
    that cannot express it."""
    for pool, _chain, pid, _n, _b, _group, endpoints in TOPOLOGY:
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
}

# Slots 4 to 6 of the four six-provider pools that HAVE a backup tier. The
# router consults these only after the primary tier is exhausted, and the values
# file marks them is_backup.
#
# eth-cv-sim, lava-cv-rest-sim and lava-cv-tm-sim are six-provider pools too and
# are absent on purpose: all three are cross-validation topologies,
# cross-validation never reaches a backup, and a provider labelled backup there
# would claim a group the router can never count. Their slots 4 to 6 are
# ordinary primaries.
AGREED_BACKUPS = {
    (pool, pid) for pool in ("eth-sim", "lava-sim-grpc", "lava-sim-rest", "lava-sim-tm") for pid in ("4", "5", "6")
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
