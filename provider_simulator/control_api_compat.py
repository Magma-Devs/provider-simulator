"""Translate the legacy /scenario format to the new pool:pid domain shapes.

The old control API addressed providers by a bare global pid ("1".."20") plus a
``chain_family`` field, and put every knob (including the Solana / logs quirks)
in one flat block. The redesign addresses providers by ``pool:pid`` and splits
the block into a ScenarioConfig update, a Quirks update, and a ``transports``
filter. This module maps one legacy block to the new shapes so existing clients
and tests keep working during the migration window. It is temporary — deleted
once nothing sends the old format.

Two legacy behaviours are reproduced exactly:

- **Pool + pid.** The global pid + chain_family map to (pool, local pid) per the
  fixed table below (pids 1-3 depend on chain_family; 4-20 are fixed).
- **Fault scope.** Legacy content faults fired only on the transport named by
  chain_family; ``down`` fired provider-wide. So a ``down`` block translates to
  no ``transports`` filter (every endpoint), and any other block to a filter of
  the chain_family's owning transport.
"""

# chain_family -> pool, for the primary pids "1".."3".
_POOL_1_3 = {
    "eth": "eth-sim",
    "ws": "eth-sim",
    "btc": "btc-sim",
    "ln": "ln-sim",
    "solana": "solana-sim",
    "rest": "lava-sim-rest",
    "grpc": "lava-sim-grpc",
    "tendermintrpc": "lava-sim-tm",
}

# Fixed pid -> (pool, new local pid) for pids that don't depend on chain_family.
_FIXED_PID = {
    "4": ("eth-sim", "4"),
    "5": ("eth-sim", "5"),
    "6": ("eth-sim", "6"),
    "7": ("lava-sim-grpc", "4"),
    "8": ("lava-sim-grpc", "5"),
    "9": ("lava-sim-grpc", "6"),
    "10": ("lava-sim-rest", "4"),
    "11": ("lava-sim-rest", "5"),
    "12": ("lava-sim-rest", "6"),
    "13": ("lava-sim-tm", "4"),
    "14": ("lava-sim-tm", "5"),
    "15": ("lava-sim-tm", "6"),
    "16": ("eth-sim", "4"),
    "17": ("eth-sim", "5"),
    "18": ("eth-sim", "6"),
    "19": ("eth-solo-sim", "1"),
    "20": ("solana-solo-sim", "1"),
}
# pids 16-18 are the eth-sim ws backups — their owning transport is ws.
_WS_FIXED_PIDS = {"16", "17", "18"}

# chain_family -> the transport its content faults own.
_OWNING_TRANSPORT = {
    "eth": "http",
    "btc": "http",
    "ln": "http",
    "solana": "http",
    "ws": "ws",
    "grpc": "http2",
    "rest": "http",
    "tendermintrpc": "http",
}

# Legacy flat keys that belong to the new ScenarioConfig (same names).
_SCENARIO_KEYS = {
    "mode",
    "latency_ms",
    "error_probability",
    "error_code",
    "error_message",
    "http_status",
    "responses",
    "corruption_mode",
    "missing_field",
    "blocks_behind",
    "fail_first_n",
    "then_mode",
    "drop_at",
}

# Legacy quirk keys -> new quirk field names.
_ETH_QUIRK_KEYS = {"logs_indexed_up_to", "logs_lag_mode"}
_SOLANA_RENAME = {
    "solana_slot_block_gap": "slot_block_gap",
    "solana_slot_offset": "slot_offset",
    "solana_unknown_method_mode": "unknown_method_mode",
}


def translate_block(old_pid: str, cfg: dict) -> tuple[str, str, dict, dict]:
    """Map one legacy /scenario block to (pool, new_pid, scenario_update,
    quirks_update). ``scenario_update`` carries the ``transports`` filter.

    Raises ValueError on a missing/unknown chain_family or an unknown pid —
    the legacy API rejected those the same way.
    """
    old_pid = str(old_pid)
    chain_family = cfg.get("chain_family")
    if chain_family is None:
        raise ValueError(f"legacy block for pid {old_pid!r} is missing chain_family")
    if chain_family not in _OWNING_TRANSPORT:
        raise ValueError(f"unknown chain_family {chain_family!r} for pid {old_pid!r}")

    if old_pid in _FIXED_PID:
        pool, new_pid = _FIXED_PID[old_pid]
        owning = "ws" if old_pid in _WS_FIXED_PIDS else _OWNING_TRANSPORT[chain_family]
    elif old_pid in ("1", "2", "3"):
        pool = _POOL_1_3[chain_family]
        new_pid = old_pid
        owning = _OWNING_TRANSPORT[chain_family]
    else:
        raise ValueError(f"unknown legacy pid {old_pid!r}")

    scenario_update = {k: cfg[k] for k in cfg if k in _SCENARIO_KEYS}
    # down is provider-wide (no filter); every other block is scoped to the
    # chain_family's owning transport.
    if cfg.get("mode") != "down":
        scenario_update["transports"] = [owning]

    quirks_update: dict = {}
    for key, value in cfg.items():
        if key in _ETH_QUIRK_KEYS:
            quirks_update[key] = value
        elif key in _SOLANA_RENAME:
            quirks_update[_SOLANA_RENAME[key]] = value

    return pool, new_pid, scenario_update, quirks_update
