"""The universal fault configuration every simulated provider understands.

These fields describe faults that apply to any chain: outage, latency, errors,
rate-limiting, corruption, dropped connections, and the first-N-fail sequence.
Chain-specific knobs (Solana slot math, ETH logs-lag) live in Quirks instead —
sending one of those here is rejected, so a typo or a wrong-chain knob fails
loudly rather than being silently ignored.

The `transports` field, when set, scopes the block's effect to specific
endpoints of the provider (e.g. only its ws wire). None means every endpoint.
Values are validated against the closed TRANSPORTS vocabulary — "grpc" is an
interface, not a transport, and accepting it here would make a fault silently
match zero endpoints.
"""

from dataclasses import dataclass, field

from provider_simulator.domain.endpoint import TRANSPORTS
from provider_simulator.domain.introspective_config import IntrospectiveConfig


@dataclass
class ScenarioConfig(IntrospectiveConfig):
    mode: str = "success"  # success | error | rate_limit | down | drop_connection | hang
    latency_ms: int = 0
    error_probability: float = 0.0
    error_code: int = -32000
    error_message: str = "Internal error"
    http_status: int = 200
    responses: dict = field(default_factory=dict)  # per-method overrides
    corruption_mode: str | None = None
    missing_field: str | None = None
    blocks_behind: int = 0
    fail_first_n: int = 0
    then_mode: str = "success"
    drop_at: str = "before_headers"
    transports: list[str] | None = None  # endpoint filter; None = all endpoints

    def _validate(self, cfg: dict) -> None:
        transports = cfg.get("transports")
        if transports is None:
            return
        bad = [t for t in transports if t not in TRANSPORTS]
        if bad:
            raise ValueError(
                f"unknown transport(s) {bad}; valid transports are {list(TRANSPORTS)} "
                "(interfaces like 'grpc'/'rest' are not transports — gRPC runs over "
                "'http2', REST over 'http')"
            )
