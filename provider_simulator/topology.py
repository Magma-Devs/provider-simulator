"""The one place that says what exists: every pool, its providers, and the
endpoints each provider listens on.

A pool is one router's provider set (one router = one chain + one application
protocol). Pool names equal the router ids in the router-side values_sim.yml
wherever a router is wired (eth-sim, eth-solo-sim, btc-sim, solana-sim,
lava-sim-grpc, lava-sim-rest, lava-sim-tm), or in the k3d-only
tools/local-cluster/routers.yml in smart_router_automation for eth-duo-sim (the
stake-weight experiment router — canonical has no router wired to this pool).
Two pools exist as bound listeners only, with no router wired yet: ln-sim
(upstream LN router pending) and solana-solo-sim (the Solana analogue of
eth-solo-sim; named by that pattern).

This table is the only source of port numbers. ``constants.py`` used to carry a
parallel set of port dicts keyed by a second, older numbering; those keys
disagreed with the pids here for six pools and nothing read them as identity, so
they were removed. Callers that need a port ask ``port_of(...)`` below.

Row shape: (pool, chain, pid, ((interface, transport, port), ...)).
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

TopologyRow = tuple[str, str, str, tuple[tuple[str, str, int], ...]]

TOPOLOGY: tuple[TopologyRow, ...] = (
    # eth-sim: 3 primary + 3 backup, each http + ws (SimProvider1-3 / SimBackup1-3)
    ("eth-sim", "eth", "1", (("jsonrpc", "http", 18545), ("jsonrpc", "ws", 18557))),
    ("eth-sim", "eth", "2", (("jsonrpc", "http", 18546), ("jsonrpc", "ws", 18558))),
    ("eth-sim", "eth", "3", (("jsonrpc", "http", 18547), ("jsonrpc", "ws", 18559))),
    ("eth-sim", "eth", "4", (("jsonrpc", "http", 18560), ("jsonrpc", "ws", 18572))),
    ("eth-sim", "eth", "5", (("jsonrpc", "http", 18561), ("jsonrpc", "ws", 18573))),
    ("eth-sim", "eth", "6", (("jsonrpc", "http", 18562), ("jsonrpc", "ws", 18574))),
    # eth-solo-sim (MAG-2061): one provider, no backup — the customer-outage
    # deployment shape. Handler dispatch is the default ETH path.
    ("eth-solo-sim", "eth", "1", (("jsonrpc", "http", 18581),)),
    # eth-duo-sim (MAG-2464 follow-up): 2 primaries, no backup tier — the
    # stake-weight experiment topology (k3d-only; see
    # tools/local-cluster/routers.yml in smart_router_automation). Dedicated
    # listeners, distinct from eth-sim's pids 1/2 (18545/18546) — previously
    # this router pointed its two upstreams straight at those, so a /scenario
    # flip on one pool leaked into the other's provider state.
    ("eth-duo-sim", "eth", "1", (("jsonrpc", "http", 18586),)),
    ("eth-duo-sim", "eth", "2", (("jsonrpc", "http", 18587),)),
    # btc-sim (MAG-2089): dedicated BTC listeners; the success branch routes
    # unconditionally through handlers_btc. Primary tier only — a backup tier,
    # if ever needed, extends contiguously upward.
    ("btc-sim", "btc", "1", (("jsonrpc", "http", 18575),)),
    ("btc-sim", "btc", "2", (("jsonrpc", "http", 18576),)),
    ("btc-sim", "btc", "3", (("jsonrpc", "http", 18577),)),
    # ln-sim (MAG-2089): dedicated LN listeners, handlers_lnd on the success
    # branch. No router routes traffic here yet (MAG-1726 tracks the router-side
    # wire-up); the ports are allocated so the pattern stays symmetric.
    ("ln-sim", "ln", "1", (("jsonrpc", "http", 18578),)),
    ("ln-sim", "ln", "2", (("jsonrpc", "http", 18579),)),
    ("ln-sim", "ln", "3", (("jsonrpc", "http", 18580),)),
    # solana-sim (MAG-2231): handlers_solana on the success branch. Reproduces
    # the Solana consistency-filter bug (MAG-1591) — the success handler emits
    # result.context.slot and result.value.lastValidBlockHeight separated by a
    # configurable gap (solana_slot_block_gap), so the router's per-user
    # seenBlock diverges from the endpoint chain-tracker value by more than the
    # 50-block consistency threshold.
    ("solana-sim", "solana", "1", (("jsonrpc", "http", 18582),)),
    ("solana-sim", "solana", "2", (("jsonrpc", "http", 18583),)),
    ("solana-sim", "solana", "3", (("jsonrpc", "http", 18584),)),
    # solana-solo-sim (MAG-2239): the Solana analogue of eth-solo-sim, one
    # provider and no backup. Deliberately a separate pool from solana-sim so a
    # /scenario call on the solo router cannot collide with the solana-sim
    # router's primary-pool state.
    ("solana-solo-sim", "solana", "1", (("jsonrpc", "http", 18585),)),
    # lava-sim-grpc (MAG-1780): pids 1-3 primary, 4-6 backup. Shares the same
    # ProviderState the JSON-RPC listeners use — a /scenario call with
    # chain_family="grpc" reconfigures the matching servicer.
    ("lava-sim-grpc", "lava", "1", (("grpc", "http2", 18548),)),
    ("lava-sim-grpc", "lava", "2", (("grpc", "http2", 18549),)),
    ("lava-sim-grpc", "lava", "3", (("grpc", "http2", 18550),)),
    ("lava-sim-grpc", "lava", "4", (("grpc", "http2", 18563),)),
    ("lava-sim-grpc", "lava", "5", (("grpc", "http2", 18564),)),
    ("lava-sim-grpc", "lava", "6", (("grpc", "http2", 18565),)),
    # lava-sim-rest (MAG-1777): pids 1-3 primary, 4-6 backup.
    ("lava-sim-rest", "lava", "1", (("rest", "http", 18551),)),
    ("lava-sim-rest", "lava", "2", (("rest", "http", 18552),)),
    ("lava-sim-rest", "lava", "3", (("rest", "http", 18553),)),
    ("lava-sim-rest", "lava", "4", (("rest", "http", 18566),)),
    ("lava-sim-rest", "lava", "5", (("rest", "http", 18567),)),
    ("lava-sim-rest", "lava", "6", (("rest", "http", 18568),)),
    # lava-sim-tm (MAG-1841): pids 1-3 primary, 4-6 backup.
    ("lava-sim-tm", "lava", "1", (("tendermintrpc", "http", 18554),)),
    ("lava-sim-tm", "lava", "2", (("tendermintrpc", "http", 18555),)),
    ("lava-sim-tm", "lava", "3", (("tendermintrpc", "http", 18556),)),
    ("lava-sim-tm", "lava", "4", (("tendermintrpc", "http", 18569),)),
    ("lava-sim-tm", "lava", "5", (("tendermintrpc", "http", 18570),)),
    ("lava-sim-tm", "lava", "6", (("tendermintrpc", "http", 18571),)),
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
    silent fallback would hand the caller some other provider's port.
    """
    for row_pool, _chain, row_pid, endpoints in TOPOLOGY:
        if row_pool != pool or row_pid != pid:
            continue
        for row_interface, row_transport, port in endpoints:
            if row_interface == interface and row_transport == transport:
                return port
        served = ", ".join(f"{i}/{t}" for i, t, _p in endpoints)
        raise KeyError(
            f"provider {pool}:{pid} does not serve {interface}/{transport}; it serves {served}"
        )
    known = sorted({row_pool for row_pool, _c, _p, _e in TOPOLOGY})
    raise KeyError(f"no provider {pool}:{pid} in the topology; known pools: {known}")
