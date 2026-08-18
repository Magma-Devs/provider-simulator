"""The one place that says what exists: every pool, its providers, the name
each provider answers to, and the endpoints it listens on.

Seven fields per row. Four of them decide what this process does and three
describe the deployment it stands in for, and the difference is worth knowing
before you edit a row.

``build_registry`` reads the pool, the chain, the pid and the endpoints. It
binds one listener per port, so those four decide what the simulator actually
runs. Get one wrong and the process either refuses to start or serves the
wrong door.

``name``, ``is_backup`` and ``group_label`` change nothing about what the
simulator does. They are facts about the DEPLOYMENT, and the only code here
that reads them is ``ControlApi.get_providers``, which serves them on GET
/providers so a caller can ask instead of writing them down:

  * ``name`` is what the router calls this provider. The router reports it in
    the ``Lava-Provider-Address`` response header, and a test reads that header
    to learn which provider served a request. Before it lived here it was
    written by hand in three repositories and no two copies were compared. The
    form is pool, then role, then the word Provider, then the pid, for example
    ``EthBackupProvider4``. The helm chart lowercases it before the router
    reads it, so ``EthBackupProvider4`` arrives as ``ethbackupprovider4`` and
    every comparison must lowercase. Two providers on one chain and
    api-interface may not share a name, or the router exits at startup.

  * ``is_backup`` says whether the router keeps this provider for the backup
    tier, which it consults only after the primary tier is exhausted. Nothing
    in this package behaves differently for a backup provider. It is recorded
    because it cannot be worked out from anything else here, and the slot
    number is only a coincidence, not a rule.

  * ``group_label`` is the cross-validation group the router puts this
    provider in. A cross-validation policy can demand that the answers agree
    across a minimum number of distinct groups (``min_groups`` in the
    router-side values_sim.yml), so a test checking group diversity has to
    know which provider sits in which group. Only eth-sim labels its providers
    today — pids 1 and 2 are ``tier-1`` and pid 3 is ``external``. Every other
    row carries the empty string rather than nothing at all, so a caller
    grouping by label gets one bucket of unlabelled providers instead of a
    missing key.

A pool is one router's provider set (one router = one chain + one application
protocol). Pool names equal the router ids in the router-side values_sim.yml
wherever a router is wired (eth-sim, eth-solo-sim, btc-sim, solana-sim,
lava-sim-grpc, lava-sim-rest, lava-sim-tm, eth-cv-sim), or in the k3d-only
tools/local-cluster/routers.yml in smart_router_automation for eth-duo-sim (the
stake-weight experiment router — canonical has no router wired to this pool).
One pool exists as a bound listener only, with no router wired yet: ln-sim
(upstream LN router pending). eth-cv-sim is the cross-validation topology —
six providers in three groups, wired in values_sim.yml like the rest.

This table is the only source of port numbers. ``constants.py`` used to carry a
parallel set of port dicts keyed by a second, older numbering; those keys
disagreed with the pids here for six pools and nothing read them as identity, so
they were removed. Callers that need a port ask ``port_of(...)`` below.

Row shape: (pool, chain, pid, name, is_backup, group_label, endpoints), where
endpoints is ((interface, transport, port), ...).
Rows and endpoint groups are tuples — structurally immutable, so no caller can
corrupt the process-global table in place. Provider ids restart at "1" inside
each pool, so a pid is only unique together with its pool; ``Provider.key``
("<pool>:<pid>") is the address the control API accepts.

``build_registry`` validates this table at startup and refuses a port used
twice, a duplicate pool:pid, an unknown chain, interface or transport, and a
provider with no endpoints.

Port allocation, and why it looks the way it does
-------------------------------------------------
Handler dispatch is **port-derived**: which chain's handler serves a call is
decided by the listener the call arrived on, not by a per-provider
``chain_family`` flag. Content fault primitives (error / rate_limit / hang /
drop_connection / corruption) are still gated per listener on the scenario's
``chain_family`` matching the listener's own handler chain; ``mode="down"`` is
the universal exception (MAG-2092) and fires on every listener regardless of
``chain_family``.

    18545-18547  ETH JSON-RPC http, primary
    18548-18550  gRPC, primary
    18551-18553  REST, primary
    18554-18556  Tendermint-RPC, primary
    18557-18559  ETH JSON-RPC ws, primary
    18560-18562  ETH JSON-RPC http, backup
    18563-18565  gRPC, backup
    18566-18568  REST, backup
    18569-18571  Tendermint-RPC, backup
    18572-18574  ETH JSON-RPC ws, backup
    18575-18577  BTC JSON-RPC
    18578-18580  LN JSON-RPC
    18581        ETH solo
    18582-18584  Solana JSON-RPC
    18585        Solana solo
    18586-18587  ETH duo
    (next free: 18588)

The ETH backup block sits at 18560-18562 rather than next to its primaries
because 18548-18559 were already claimed by the gRPC / REST / Tendermint /
WebSocket surfaces. The non-JSON-RPC backup tiers then stack contiguously above
it at 18563-18574.

Primaries serve normal traffic; backups are consumed by the smart-router only
after the primary pool is exhausted (PairingListEmptyError → backup fallback in
consumer_session_manager.go). The simulator process is identical across both —
**tier is a router-side concept, not a simulator-side one.** Nothing in this
package behaves differently for a backup provider.
"""

TopologyRow = tuple[str, str, str, str, bool, str, tuple[tuple[str, str, int], ...]]

TOPOLOGY: tuple[TopologyRow, ...] = (
    # eth-sim: 3 primary + 3 backup, each http + ws.
    (
        "eth-sim",
        "eth",
        "1",
        "EthPrimaryProvider1",
        False,
        "tier-1",
        (("jsonrpc", "http", 18545), ("jsonrpc", "ws", 18557)),
    ),
    (
        "eth-sim",
        "eth",
        "2",
        "EthPrimaryProvider2",
        False,
        "tier-1",
        (("jsonrpc", "http", 18546), ("jsonrpc", "ws", 18558)),
    ),
    (
        "eth-sim",
        "eth",
        "3",
        "EthPrimaryProvider3",
        False,
        "external",
        (("jsonrpc", "http", 18547), ("jsonrpc", "ws", 18559)),
    ),
    ("eth-sim", "eth", "4", "EthBackupProvider4", True, "", (("jsonrpc", "http", 18560), ("jsonrpc", "ws", 18572))),
    ("eth-sim", "eth", "5", "EthBackupProvider5", True, "", (("jsonrpc", "http", 18561), ("jsonrpc", "ws", 18573))),
    ("eth-sim", "eth", "6", "EthBackupProvider6", True, "", (("jsonrpc", "http", 18562), ("jsonrpc", "ws", 18574))),
    # eth-solo-sim (MAG-2061): one provider, no backup — the customer-outage
    # deployment shape. Handler dispatch is the default ETH path.
    ("eth-solo-sim", "eth", "1", "EthSoloProvider1", False, "", (("jsonrpc", "http", 18581),)),
    # eth-duo-sim (MAG-2464 follow-up): 2 primaries, no backup tier — the
    # stake-weight experiment topology (k3d-only; see
    # tools/local-cluster/routers.yml in smart_router_automation). Dedicated
    # listeners, distinct from eth-sim's pids 1/2 (18545/18546) — previously
    # this router pointed its two upstreams straight at those, so a /scenario
    # flip on one pool leaked into the other's provider state.
    ("eth-duo-sim", "eth", "1", "EthDuoHighProvider1", False, "", (("jsonrpc", "http", 18586),)),
    ("eth-duo-sim", "eth", "2", "EthDuoLowProvider2", False, "", (("jsonrpc", "http", 18587),)),
    # eth-cv-sim: 6 primaries, no backup tier — the cross-validation topology.
    # Wired in values_sim.yml, the file that deploys the simulator routers to
    # the shared cluster. Cross-validation asks several providers the
    # same question and only answers when enough of them agree; the router
    # config can also demand that the agreeing providers come from different
    # groups. Six providers in three groups of two is the smallest shape that
    # can exercise every rule the router enforces:
    #   - per-group quorum needs max-participants >= min-groups *
    #     agreement-threshold, so two groups needing two matching answers each
    #     needs four providers, and three groups needing two needs six. The
    #     three-provider eth-sim pool cannot satisfy either, and a policy that
    #     asks for it is rejected at startup, crash-looping the router
    #     (smart-router/protocol/rpcsmartrouter/cross_validation_policy.go
    #     Validate).
    #   - a two-against-two split, where the router must refuse rather than
    #     pick a side, needs four providers answering at once.
    #   - a minimum of three distinct groups, and the "too few groups"
    #     refusal, need three groups to exist in the first place.
    # Dedicated listeners for the same isolation reason as eth-duo-sim. The
    # control API keys a fault by "pool:pid", so two pools each having a pid
    # "1" is fine. They resolve to different providers. What is not fine is
    # two pools sharing a listener: that is one provider under one key, so a
    # fault injected for one router's test reaches the other router's traffic,
    # and the resulting failure would look exactly like a router bug.
    ("eth-cv-sim", "eth", "1", "EthCvPrimaryProvider1", False, "tier-1", (("jsonrpc", "http", 18596),)),
    ("eth-cv-sim", "eth", "2", "EthCvPrimaryProvider2", False, "tier-1", (("jsonrpc", "http", 18597),)),
    ("eth-cv-sim", "eth", "3", "EthCvPrimaryProvider3", False, "tier-2", (("jsonrpc", "http", 18598),)),
    ("eth-cv-sim", "eth", "4", "EthCvPrimaryProvider4", False, "tier-2", (("jsonrpc", "http", 18599),)),
    ("eth-cv-sim", "eth", "5", "EthCvPrimaryProvider5", False, "external", (("jsonrpc", "http", 18600),)),
    ("eth-cv-sim", "eth", "6", "EthCvPrimaryProvider6", False, "external", (("jsonrpc", "http", 18601),)),
    # btc-sim (MAG-2089): dedicated BTC listeners; the success branch routes
    # unconditionally through handlers_btc. Primary tier only — a backup tier,
    # if ever needed, extends contiguously upward.
    ("btc-sim", "btc", "1", "BtcPrimaryProvider1", False, "", (("jsonrpc", "http", 18575),)),
    ("btc-sim", "btc", "2", "BtcPrimaryProvider2", False, "", (("jsonrpc", "http", 18576),)),
    ("btc-sim", "btc", "3", "BtcPrimaryProvider3", False, "", (("jsonrpc", "http", 18577),)),
    # ln-sim (MAG-2089): dedicated LN listeners, handlers_lnd on the success
    # branch. No router routes traffic here yet (MAG-1726 tracks the router-side
    # wire-up); the ports are allocated so the pattern stays symmetric.
    ("ln-sim", "ln", "1", "LnPrimaryProvider1", False, "", (("jsonrpc", "http", 18578),)),
    ("ln-sim", "ln", "2", "LnPrimaryProvider2", False, "", (("jsonrpc", "http", 18579),)),
    ("ln-sim", "ln", "3", "LnPrimaryProvider3", False, "", (("jsonrpc", "http", 18580),)),
    # solana-sim (MAG-2231): handlers_solana on the success branch. Reproduces
    # the Solana consistency-filter bug (MAG-1591) — the success handler emits
    # result.context.slot and result.value.lastValidBlockHeight separated by a
    # configurable gap (solana_slot_block_gap), so the router's per-user
    # seenBlock diverges from the endpoint chain-tracker value by more than the
    # 50-block consistency threshold.
    ("solana-sim", "solana", "1", "SolanaPrimaryProvider1", False, "", (("jsonrpc", "http", 18582),)),
    ("solana-sim", "solana", "2", "SolanaPrimaryProvider2", False, "", (("jsonrpc", "http", 18583),)),
    ("solana-sim", "solana", "3", "SolanaPrimaryProvider3", False, "", (("jsonrpc", "http", 18584),)),
    # solana-solo-sim (MAG-2239): the Solana analogue of eth-solo-sim, one
    # provider and no backup. Deliberately a separate pool from solana-sim so a
    # /scenario call on the solo router cannot collide with the solana-sim
    # router's primary-pool state.
    ("solana-solo-sim", "solana", "1", "SolanaSoloProvider1", False, "", (("jsonrpc", "http", 18585),)),
    # lava-sim-grpc (MAG-1780): pids 1-3 primary, 4-6 backup. Shares the same
    # ProviderState the JSON-RPC listeners use — a /scenario call with
    # chain_family="grpc" reconfigures the matching servicer.
    ("lava-sim-grpc", "lava", "1", "LavaGrpcPrimaryProvider1", False, "", (("grpc", "http2", 18548),)),
    ("lava-sim-grpc", "lava", "2", "LavaGrpcPrimaryProvider2", False, "", (("grpc", "http2", 18549),)),
    ("lava-sim-grpc", "lava", "3", "LavaGrpcPrimaryProvider3", False, "", (("grpc", "http2", 18550),)),
    ("lava-sim-grpc", "lava", "4", "LavaGrpcBackupProvider4", True, "", (("grpc", "http2", 18563),)),
    ("lava-sim-grpc", "lava", "5", "LavaGrpcBackupProvider5", True, "", (("grpc", "http2", 18564),)),
    ("lava-sim-grpc", "lava", "6", "LavaGrpcBackupProvider6", True, "", (("grpc", "http2", 18565),)),
    # lava-sim-rest (MAG-1777): pids 1-3 primary, 4-6 backup.
    ("lava-sim-rest", "lava", "1", "LavaRestPrimaryProvider1", False, "", (("rest", "http", 18551),)),
    ("lava-sim-rest", "lava", "2", "LavaRestPrimaryProvider2", False, "", (("rest", "http", 18552),)),
    ("lava-sim-rest", "lava", "3", "LavaRestPrimaryProvider3", False, "", (("rest", "http", 18553),)),
    ("lava-sim-rest", "lava", "4", "LavaRestBackupProvider4", True, "", (("rest", "http", 18566),)),
    ("lava-sim-rest", "lava", "5", "LavaRestBackupProvider5", True, "", (("rest", "http", 18567),)),
    ("lava-sim-rest", "lava", "6", "LavaRestBackupProvider6", True, "", (("rest", "http", 18568),)),
    # lava-sim-tm (MAG-1841): pids 1-3 primary, 4-6 backup.
    ("lava-sim-tm", "lava", "1", "LavaTmPrimaryProvider1", False, "", (("tendermintrpc", "http", 18554),)),
    ("lava-sim-tm", "lava", "2", "LavaTmPrimaryProvider2", False, "", (("tendermintrpc", "http", 18555),)),
    ("lava-sim-tm", "lava", "3", "LavaTmPrimaryProvider3", False, "", (("tendermintrpc", "http", 18556),)),
    ("lava-sim-tm", "lava", "4", "LavaTmBackupProvider4", True, "", (("tendermintrpc", "http", 18569),)),
    ("lava-sim-tm", "lava", "5", "LavaTmBackupProvider5", True, "", (("tendermintrpc", "http", 18570),)),
    ("lava-sim-tm", "lava", "6", "LavaTmBackupProvider6", True, "", (("tendermintrpc", "http", 18571),)),
)


def port_of(
    pool: str,
    pid: str,
    interface: str = "jsonrpc",
    transport: str = "http",
) -> int:
    """Return the TCP port one provider serves one interface/transport on.

    ``pid`` is the pool-local slot, the same number the control API accepts in
    ``<pool>:<pid>``. It restarts at "1" in every pool, so the pool argument is
    what tells two pools' slot 1 apart.

    A JSON-RPC provider has two endpoints — http and ws — so the transport
    argument picks between them. Every other interface has one endpoint, and the
    default transport is wrong for gRPC, which uses http2.

    Raises KeyError on any miss. A miss is a typo or a stale reference, and a
    silent fallback would hand the caller some other provider's port. Each miss
    names what the caller got wrong: an unknown pool lists the known pools, an
    unknown slot lists that pool's slots, and a wrong door lists the doors the
    provider does serve.
    """
    pool_pids = []
    for row_pool, _chain, row_pid, _name, _is_backup, _group, endpoints in TOPOLOGY:
        if row_pool != pool:
            continue
        pool_pids.append(row_pid)
        if row_pid != pid:
            continue
        for row_interface, row_transport, port in endpoints:
            if row_interface == interface and row_transport == transport:
                return port
        served = ", ".join(f"{i}/{t}" for i, t, _p in endpoints)
        raise KeyError(f"provider {pool}:{pid} does not serve {interface}/{transport}; it serves {served}")
    if pool_pids:
        raise KeyError(f"no provider {pool}:{pid} in the topology; {pool} has slots {pool_pids}")
    known = sorted({row_pool for row_pool, _c, _p, _n, _b, _g, _e in TOPOLOGY})
    raise KeyError(f"no pool {pool!r} in the topology; known pools: {known}")
