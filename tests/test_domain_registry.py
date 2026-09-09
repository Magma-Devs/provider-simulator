import pytest

import provider_simulator.topology as topology_module
from provider_simulator.domain.registry import build_registry
from provider_simulator.topology import TOPOLOGY


def test_build_registry_maps_topology_rows_one_to_one():
    reg = build_registry()
    # Derived from the table, not hardcoded: the property this test owns is
    # "every row becomes exactly one provider, no silent drop or merge".
    assert set(reg.pools) == {row[0] for row in TOPOLOGY}
    assert len(reg.all_providers()) == len({(pool, pid) for pool, _c, pid, _n, _b, _group, _e in TOPOLOGY})


def test_provider_lookup_by_pool_and_pid():
    reg = build_registry()
    p = reg.provider("eth-sim", "1")
    assert p.key == "eth-sim:1"


def test_provider_lookup_miss_names_the_valid_options():
    reg = build_registry()
    with pytest.raises(KeyError) as exc:
        reg.provider("eth-sim", "99")
    assert "providers in 'eth-sim'" in str(exc.value)
    with pytest.raises(KeyError) as exc:
        reg.provider("no-such-pool", "1")
    assert "pools are" in str(exc.value)


def test_by_port_resolves_provider_and_endpoint():
    reg = build_registry()
    provider, endpoint = reg.by_port(18557)  # eth-sim:1 ws
    assert provider.key == "eth-sim:1"
    assert endpoint.transport == "ws"
    assert endpoint.interface == "jsonrpc"


def test_eth_and_btc_provider_1_are_distinct_objects():
    reg = build_registry()
    assert reg.provider("eth-sim", "1") is not reg.provider("btc-sim", "1")


def test_eth_duo_sim_providers_resolve_and_are_distinct_from_eth_sim():
    reg = build_registry()
    high = reg.provider("eth-duo-sim", "1")
    low = reg.provider("eth-duo-sim", "2")
    assert high.key == "eth-duo-sim:1"
    assert low.key == "eth-duo-sim:2"
    # Distinct objects from eth-sim's pid 1/2 — a /scenario flip on one pool
    # can no longer leak into the other's provider state (the isolation gap
    # dedicated ports close).
    assert high is not reg.provider("eth-sim", "1")
    assert low is not reg.provider("eth-sim", "2")


def test_best_priority_precedence_sim_providers_are_distinct_from_each_other_and_eth_sim():
    """eth-best-sim, eth-priority-sim, and eth-precedence-sim each get their
    own Provider objects — none of them share state with eth-sim, eth-duo-sim,
    or each other.

    Mirrors ``test_eth_duo_sim_providers_resolve_and_are_distinct_from_eth_sim``
    above: a /scenario flip meant for one router's test must not leak into
    another router's traffic. Each pool owns its own listeners, so the
    ``pool:pid`` key the control API uses resolves to a different Provider for
    each — the bare pid ``1`` repeating across pools is expected and harmless.
    """
    reg = build_registry()
    best = [reg.provider("eth-best-sim", pid) for pid in ("1", "2", "3")]
    priority = [reg.provider("eth-priority-sim", pid) for pid in ("1", "2", "3")]
    precedence = [reg.provider("eth-precedence-sim", pid) for pid in ("1", "2")]

    assert [p.key for p in best] == ["eth-best-sim:1", "eth-best-sim:2", "eth-best-sim:3"]
    assert [p.key for p in priority] == [
        "eth-priority-sim:1",
        "eth-priority-sim:2",
        "eth-priority-sim:3",
    ]
    assert [p.key for p in precedence] == ["eth-precedence-sim:1", "eth-precedence-sim:2"]

    others = [
        reg.provider("eth-sim", "1"),
        reg.provider("eth-sim", "2"),
        reg.provider("eth-duo-sim", "1"),
        reg.provider("eth-duo-sim", "2"),
    ]
    all_new = best + priority + precedence
    for provider in all_new:
        for other in others:
            assert provider is not other, (
                f"{provider.key} must be a distinct object from {other.key} — "
                f"sharing one would let a fault meant for one router's test "
                f"reach the other router's traffic"
            )
    # And distinct from each other, pool to pool.
    for i, provider in enumerate(all_new):
        for other in all_new[i + 1 :]:
            assert provider is not other, f"{provider.key} and {other.key} must be distinct objects"


def test_build_registry_reads_patched_topology(monkeypatch):
    small = (("eth-sim", "eth", "1", "EthProvider1", False, "", (("jsonrpc", "http", 18545),)),)
    monkeypatch.setattr(topology_module, "TOPOLOGY", small)
    reg = build_registry()  # default path must see the patched table
    assert len(reg.all_providers()) == 1


def test_build_registry_rejects_duplicate_port():
    rows = [
        ("eth-sim", "eth", "1", "EthProvider1", False, "", [("jsonrpc", "http", 18545)]),
        ("btc-sim", "btc", "1", "BtcProvider1", False, "", [("jsonrpc", "http", 18545)]),  # dup port
    ]
    with pytest.raises(ValueError, match="18545"):
        build_registry(rows)


def test_build_registry_rejects_duplicate_pool_pid():
    rows = [
        ("eth-sim", "eth", "1", "EthProvider1", False, "", [("jsonrpc", "http", 18545)]),
        ("eth-sim", "eth", "1", "EthProvider1", False, "", [("jsonrpc", "http", 18546)]),  # dup pool:pid
    ]
    with pytest.raises(ValueError, match="eth-sim:1"):
        build_registry(rows)


def test_build_registry_rejects_unknown_chain():
    rows = [("mystery-sim", "dogecoin", "1", "MysteryProvider1", False, "", [("jsonrpc", "http", 19999)])]
    with pytest.raises(ValueError, match="dogecoin"):
        build_registry(rows)


def test_build_registry_rejects_two_chains_under_one_pool():
    rows = [
        ("x-sim", "solana", "1", "XProvider1", False, "", [("jsonrpc", "http", 19991)]),
        ("x-sim", "eth", "2", "XProvider2", False, "", [("jsonrpc", "http", 19992)]),  # chain conflict
    ]
    with pytest.raises(ValueError, match="two chains"):
        build_registry(rows)


def test_build_registry_rejects_empty_endpoint_list():
    rows = [("btc-sim", "btc", "4", "BtcProvider4", False, "", [])]
    with pytest.raises(ValueError, match="no endpoints"):
        build_registry(rows)


def test_build_registry_rejects_bad_port_and_bad_names():
    with pytest.raises(ValueError, match="bad port"):
        build_registry([("btc-sim", "btc", "1", "BtcProvider1", False, "", [("jsonrpc", "http", 0)])])
    with pytest.raises(ValueError, match="bad pid"):
        build_registry([("btc-sim", "btc", "", "BtcProvider0", False, "", [("jsonrpc", "http", 19993)])])
    with pytest.raises(ValueError, match="no ':'"):
        build_registry([("btc-sim", "btc", "grpc:1", "BtcProvidergrpc:1", False, "", [("jsonrpc", "http", 19994)])])
    with pytest.raises(ValueError, match="bad pool name"):
        build_registry([("lava:grpc", "lava", "1", "LavaGrpcProvider1", False, "", [("grpc", "http2", 19995)])])


def test_build_registry_rejects_unknown_interface_and_transport():
    with pytest.raises(ValueError, match="unknown interface"):
        build_registry([("btc-sim", "btc", "1", "BtcProvider1", False, "", [("bitcoinrpc", "http", 19996)])])
    with pytest.raises(ValueError, match="unknown transport"):
        build_registry([("btc-sim", "btc", "1", "BtcProvider1", False, "", [("jsonrpc", "grpc", 19997)])])


def test_lava_cv_rest_sim_pool_builds_with_six_rest_providers_in_three_groups():
    """The REST cross-validation pool is built exactly as its rows declare.

    Six primaries, no backup, each on its own REST port, labelled three groups
    of two. Those three facts are what every cross-validation rule on this
    router rests on: per-group quorum across three groups needs
    max-participants >= min-groups * agreement-threshold, which is 3 * 2 = 6 —
    the whole pool. A row that lost its group label, or a seventh provider, or
    a backup counted as a candidate, would each change what the router can be
    asked for while the pool still looked fine.
    """
    reg = build_registry()

    providers = [reg.provider("lava-cv-rest-sim", pid) for pid in ("1", "2", "3", "4", "5", "6")]

    assert [p.key for p in providers] == [f"lava-cv-rest-sim:{n}" for n in range(1, 7)]
    assert [p.name for p in providers] == [f"LavaCvRestPrimaryProvider{n}" for n in range(1, 7)]
    assert not any(p.is_backup for p in providers), (
        "cross-validation never reaches a backup, so a provider labelled backup "
        "here would claim a group the router can never count"
    )
    assert [p.group_label for p in providers] == [
        "voting-group-1",
        "voting-group-1",
        "voting-group-2",
        "voting-group-2",
        "voting-group-3",
        "voting-group-3",
    ]
    assert len({p.group_label for p in providers}) == 3

    # One REST endpoint each, on its own port, contiguous and in order.
    ports = []
    for provider in providers:
        assert len(provider.endpoints) == 1, f"{provider.key} should serve exactly one endpoint"
        endpoint = provider.endpoints[0]
        assert (endpoint.interface, endpoint.transport) == ("rest", "http")
        ports.append(endpoint.port)
    assert ports == [18602, 18603, 18604, 18605, 18606, 18607]

    assert reg.pools["lava-cv-rest-sim"].chain == "lava"


def test_lava_cv_rest_sim_shares_no_state_with_lava_sim_rest():
    """The two REST pools are separate Provider objects on separate ports.

    They are the pair most at risk of being conflated: same chain, same
    interface, and a scenario call addressed by family alone resolves to
    lava-sim-rest. If the cross-validation router reused those listeners, one
    provider would sit under two pool keys, and a fault armed for a
    cross-validation test would land in the traffic of the 30 tests that
    already run against the shared router — reading as a router bug.
    """
    reg = build_registry()

    for pid in ("1", "2", "3"):
        cv_provider = reg.provider("lava-cv-rest-sim", pid)
        shared_provider = reg.provider("lava-sim-rest", pid)
        assert cv_provider is not shared_provider
        assert cv_provider.endpoints[0].port != shared_provider.endpoints[0].port

    cv_ports = {
        endpoint.port
        for pid in ("1", "2", "3", "4", "5", "6")
        for endpoint in reg.provider("lava-cv-rest-sim", pid).endpoints
    }
    shared_ports = {
        endpoint.port
        for pid in ("1", "2", "3", "4", "5", "6")
        for endpoint in reg.provider("lava-sim-rest", pid).endpoints
    }
    assert cv_ports.isdisjoint(shared_ports)
    # Positive control: both sets are non-empty, so the disjointness above is
    # not two empty sets agreeing with each other.
    assert len(cv_ports) == 6 and len(shared_ports) == 6


def test_lava_cv_tm_sim_pool_builds_with_six_tendermint_providers_in_three_groups():
    """The Tendermint cross-validation pool is built exactly as its rows declare.

    Six primaries, no backup, each on its own Tendermint-RPC port, labelled
    three groups of two. Those three facts are what every cross-validation rule
    on this router rests on: per-group quorum across three groups needs
    max-participants >= min-groups * agreement-threshold, which is 3 * 2 = 6 —
    the whole pool. A row that lost its group label, or a seventh provider, or
    a backup counted as a candidate, would each change what the router can be
    asked for while the pool still looked fine.

    The interface is checked as well as the port. ``tendermintrpc`` is the
    string the router matches a policy on, and a row that said ``rest`` here
    would bind a listener that answers a shape this router never sends.
    """
    reg = build_registry()

    providers = [reg.provider("lava-cv-tm-sim", pid) for pid in ("1", "2", "3", "4", "5", "6")]

    assert [p.key for p in providers] == [f"lava-cv-tm-sim:{n}" for n in range(1, 7)]
    assert [p.name for p in providers] == [f"LavaCvTmPrimaryProvider{n}" for n in range(1, 7)]
    assert not any(p.is_backup for p in providers), (
        "cross-validation never reaches a backup, so a provider labelled backup "
        "here would claim a group the router can never count"
    )
    assert [p.group_label for p in providers] == [
        "voting-group-1",
        "voting-group-1",
        "voting-group-2",
        "voting-group-2",
        "voting-group-3",
        "voting-group-3",
    ]
    assert len({p.group_label for p in providers}) == 3

    # One Tendermint-RPC endpoint each, on its own port, contiguous and in order.
    ports = []
    for provider in providers:
        assert len(provider.endpoints) == 1, f"{provider.key} should serve exactly one endpoint"
        endpoint = provider.endpoints[0]
        assert (endpoint.interface, endpoint.transport) == ("tendermintrpc", "http")
        ports.append(endpoint.port)
    assert ports == [18608, 18609, 18610, 18611, 18612, 18613]

    assert reg.pools["lava-cv-tm-sim"].chain == "lava"


def test_lava_cv_tm_sim_shares_no_state_with_lava_sim_tm():
    """The two Tendermint pools are separate Provider objects on separate ports.

    They are the pair most at risk of being conflated: same chain, same
    interface, and a scenario call addressed by family alone resolves to
    lava-sim-tm. If the cross-validation router reused those listeners, one
    provider would sit under two pool keys, and a fault armed for a
    cross-validation test would land in the traffic of the tests that already
    run against the shared router — reading as a router bug.
    """
    reg = build_registry()

    for pid in ("1", "2", "3"):
        cv_provider = reg.provider("lava-cv-tm-sim", pid)
        shared_provider = reg.provider("lava-sim-tm", pid)
        assert cv_provider is not shared_provider
        assert cv_provider.endpoints[0].port != shared_provider.endpoints[0].port

    cv_ports = {
        endpoint.port
        for pid in ("1", "2", "3", "4", "5", "6")
        for endpoint in reg.provider("lava-cv-tm-sim", pid).endpoints
    }
    shared_ports = {
        endpoint.port
        for pid in ("1", "2", "3", "4", "5", "6")
        for endpoint in reg.provider("lava-sim-tm", pid).endpoints
    }
    assert cv_ports.isdisjoint(shared_ports)
    # Positive control: both sets are non-empty, so the disjointness above is
    # not two empty sets agreeing with each other.
    assert len(cv_ports) == 6 and len(shared_ports) == 6


def test_eth_cache_writer_and_reader_pools_build_with_three_primaries_and_three_backups():
    """The two-tier cache pools are built exactly as their rows declare.

    Three primaries and three backups each, one JSON-RPC endpoint per provider,
    on contiguous ports of their own. Each of those facts carries weight.

    The backup tier is the one worth stating. The router answers from either
    cache tier BEFORE it picks any provider, primary tier and backup tier
    alike. A test proving the router skipped the backup needs a backup to
    exist, or it cannot tell "never reached it" from "there was none to
    reach" — and a row that lost its backup flag would take that apart while
    the pool still looked complete.

    No group label on any of the twelve. Neither router carries a
    cross-validation policy, so a label here would name a voting bloc nothing
    ever counts.
    """
    reg = build_registry()

    for pool, first_port in (("eth-cache-writer-sim", 18614), ("eth-cache-reader-sim", 18620)):
        role = "Writer" if "writer" in pool else "Reader"
        providers = [reg.provider(pool, pid) for pid in ("1", "2", "3", "4", "5", "6")]

        assert [p.key for p in providers] == [f"{pool}:{n}" for n in range(1, 7)]
        assert [p.name for p in providers] == [
            f"EthCache{role}PrimaryProvider1",
            f"EthCache{role}PrimaryProvider2",
            f"EthCache{role}PrimaryProvider3",
            f"EthCache{role}BackupProvider4",
            f"EthCache{role}BackupProvider5",
            f"EthCache{role}BackupProvider6",
        ]
        assert [p.is_backup for p in providers] == [False, False, False, True, True, True], (
            f"{pool} must keep three primaries and three backups: without a backup "
            f"tier a test cannot tell 'the router never reached the backup' from "
            f"'there was no backup to reach'"
        )
        assert [p.group_label for p in providers] == [""] * 6, (
            f"{pool} carries no cross-validation policy, so a group label here " f"would name a bloc nothing counts"
        )

        ports = []
        for provider in providers:
            assert len(provider.endpoints) == 1, f"{provider.key} should serve exactly one endpoint"
            endpoint = provider.endpoints[0]
            assert (endpoint.interface, endpoint.transport) == ("jsonrpc", "http")
            ports.append(endpoint.port)
        assert ports == list(range(first_port, first_port + 6))

        assert reg.pools[pool].chain == "eth"


def test_the_two_cache_pools_share_no_state_with_each_other_or_with_eth_sim():
    """Three eth JSON-RPC pools, three separate sets of Provider objects.

    These are the pools most at risk of being conflated. All three serve the
    same chain on the same interface, the two cache pools sit on adjacent port
    blocks, and their names differ by one word. A scenario call addressed by
    family alone resolves to eth-sim.

    If any two shared a listener, one provider would sit under two pool keys.
    A fault armed for the writer's test would then land in the reader's
    traffic, or in the traffic of every test already running against eth-sim —
    and the failure would read exactly like a router bug.
    """
    reg = build_registry()
    slots = ("1", "2", "3", "4", "5", "6")

    writer = [reg.provider("eth-cache-writer-sim", pid) for pid in slots]
    reader = [reg.provider("eth-cache-reader-sim", pid) for pid in slots]
    shared = [reg.provider("eth-sim", pid) for pid in slots]

    for a, b in ((writer, reader), (writer, shared), (reader, shared)):
        for left, right in zip(a, b):
            assert left is not right

    def ports_of(providers):
        return {endpoint.port for p in providers for endpoint in p.endpoints}

    writer_ports, reader_ports, shared_ports = map(ports_of, (writer, reader, shared))

    assert writer_ports.isdisjoint(reader_ports)
    assert writer_ports.isdisjoint(shared_ports)
    assert reader_ports.isdisjoint(shared_ports)
    # Positive control: no set is empty, so the three disjointness checks above
    # are not empty sets agreeing with each other. eth-sim carries twelve ports
    # rather than six because each of its providers has an http and a ws door.
    assert len(writer_ports) == 6
    assert len(reader_ports) == 6
    assert len(shared_ports) == 12
