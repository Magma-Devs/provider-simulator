"""The one place that says what exists: every pool, its providers, and the
endpoints each provider listens on.

A pool is one router's provider set (one router = one chain + one application
protocol). This table mirrors the router-side values_sim.yml. All port numbers
match today's deployment, so routers need no change. Adding a provider or a pool
is one row here; the registry validates the table at startup.

Row shape: (pool, chain, pid, [(interface, transport, port), ...]).
Provider ids restart at "1" inside each pool.
"""

TopologyRow = tuple[str, str, str, list[tuple[str, str, int]]]

TOPOLOGY: list[TopologyRow] = [
    # eth-sim: 3 primary + 3 backup, each http + ws (SimProvider1-3 / SimBackup1-3)
    ("eth-sim", "eth", "1", [("jsonrpc", "http", 18545), ("jsonrpc", "ws", 18557)]),
    ("eth-sim", "eth", "2", [("jsonrpc", "http", 18546), ("jsonrpc", "ws", 18558)]),
    ("eth-sim", "eth", "3", [("jsonrpc", "http", 18547), ("jsonrpc", "ws", 18559)]),
    ("eth-sim", "eth", "4", [("jsonrpc", "http", 18560), ("jsonrpc", "ws", 18572)]),
    ("eth-sim", "eth", "5", [("jsonrpc", "http", 18561), ("jsonrpc", "ws", 18573)]),
    ("eth-sim", "eth", "6", [("jsonrpc", "http", 18562), ("jsonrpc", "ws", 18574)]),
    ("eth-solo", "eth", "1", [("jsonrpc", "http", 18581)]),
    ("btc-sim", "btc", "1", [("jsonrpc", "http", 18575)]),
    ("btc-sim", "btc", "2", [("jsonrpc", "http", 18576)]),
    ("btc-sim", "btc", "3", [("jsonrpc", "http", 18577)]),
    ("ln-sim", "ln", "1", [("jsonrpc", "http", 18578)]),
    ("ln-sim", "ln", "2", [("jsonrpc", "http", 18579)]),
    ("ln-sim", "ln", "3", [("jsonrpc", "http", 18580)]),
    ("solana-sim", "solana", "1", [("jsonrpc", "http", 18582)]),
    ("solana-sim", "solana", "2", [("jsonrpc", "http", 18583)]),
    ("solana-sim", "solana", "3", [("jsonrpc", "http", 18584)]),
    ("solana-solo", "solana", "1", [("jsonrpc", "http", 18585)]),
    ("lava-sim-grpc", "lava", "1", [("grpc", "http2", 18548)]),
    ("lava-sim-grpc", "lava", "2", [("grpc", "http2", 18549)]),
    ("lava-sim-grpc", "lava", "3", [("grpc", "http2", 18550)]),
    ("lava-sim-grpc", "lava", "4", [("grpc", "http2", 18563)]),
    ("lava-sim-grpc", "lava", "5", [("grpc", "http2", 18564)]),
    ("lava-sim-grpc", "lava", "6", [("grpc", "http2", 18565)]),
    ("lava-sim-rest", "lava", "1", [("rest", "http", 18551)]),
    ("lava-sim-rest", "lava", "2", [("rest", "http", 18552)]),
    ("lava-sim-rest", "lava", "3", [("rest", "http", 18553)]),
    ("lava-sim-rest", "lava", "4", [("rest", "http", 18566)]),
    ("lava-sim-rest", "lava", "5", [("rest", "http", 18567)]),
    ("lava-sim-rest", "lava", "6", [("rest", "http", 18568)]),
    ("lava-sim-tm", "lava", "1", [("tendermintrpc", "http", 18554)]),
    ("lava-sim-tm", "lava", "2", [("tendermintrpc", "http", 18555)]),
    ("lava-sim-tm", "lava", "3", [("tendermintrpc", "http", 18556)]),
    ("lava-sim-tm", "lava", "4", [("tendermintrpc", "http", 18569)]),
    ("lava-sim-tm", "lava", "5", [("tendermintrpc", "http", 18570)]),
    ("lava-sim-tm", "lava", "6", [("tendermintrpc", "http", 18571)]),
]


def iter_rows() -> list[TopologyRow]:
    """Return the topology rows (a list; callers must not mutate it)."""
    return TOPOLOGY
