"""The universal fault configuration every simulated provider understands.

These fields describe faults that apply to any chain: outage, latency, errors,
rate-limiting, corruption, dropped connections, and the first-N-fail sequence.
Chain-specific knobs (Solana slot math, ETH logs-lag) live in Quirks instead —
sending one of those here is rejected, so a typo or a wrong-chain knob fails
loudly rather than being silently ignored.

One mode does not apply to every chain: ``port_closed`` closes the port of a gRPC
endpoint, and only a gRPC endpoint can perform it. This class cannot tell which
endpoints a provider has, so the control API refuses the mode for every other
endpoint, where the endpoints are known.

Two fields scope a block to SOME of a provider's endpoints rather than all of
them, and both default to None, meaning every endpoint.

`transports` names wires — "only its ws endpoint". Values are validated against
the closed TRANSPORTS vocabulary, because "grpc" is an interface rather than a
transport and accepting it here would make a fault silently match zero endpoints.

`ports` names ADDRESSES. It exists for the provider that serves the same
interface and the same transport at two different ports, where `transports`
cannot tell the two apart: a test silences one address and requires the traffic
to move to the other. A port the provider does not serve is refused, for the same
reason an unknown transport is — a filter that matches no endpoint means no fault
at all, and a provider answering normally looks exactly like a provider that was
never faulted.

When both are set they AND together: an endpoint must match the transport list
AND the port list to be targeted.
"""

from dataclasses import dataclass, field

from provider_simulator.domain.endpoint import TRANSPORTS
from provider_simulator.domain.introspective_config import IntrospectiveConfig


@dataclass
class ScenarioConfig(IntrospectiveConfig):
    mode: str = "success"  # success | error | rate_limit | down | drop_connection | hang | port_closed
    latency_ms: int = 0
    error_probability: float = 0.0
    error_code: int = -32000
    error_message: str = "Internal error"
    http_status: int = 200
    # JSON-RPC-listener-only: the plain-text body a ``rate_limit`` fault sends.
    # Real providers answer a 429 with prose (or HTML), never a JSON-RPC error
    # envelope, so this is a separate field from error_message rather than a
    # reuse of it — REST/Tendermint/gRPC still build their rate_limit envelope
    # from error_code/error_message and are unaffected by this field.
    rate_limit_body: str = "Rate limit exceeded. Reduce your request rate, or use an API key for a higher limit."
    responses: dict = field(default_factory=dict)  # per-method overrides
    corruption_mode: str | None = None
    missing_field: str | None = None
    blocks_behind: int = 0
    fail_first_n: int = 0
    then_mode: str = "success"
    drop_at: str = "before_headers"
    # A pause holds a reply part way through and then FINISHES it. The
    # Content-Length stays honest, so the client keeps reading and receives the
    # whole body late. That is the opposite of ``drop_at``, which promises a
    # size it never delivers and closes the connection.
    #
    # ``None`` means no pause. ``before_headers`` is refused rather than
    # accepted, because ``latency_ms`` already delays the first byte and two
    # ways to say one thing is how a test measures the wrong one.
    pause_at: str | None = None  # None | after_headers | mid_body
    pause_ms: int = 0
    transports: list[str] | None = None  # endpoint filter; None = all endpoints
    # Address filter, ANDed with transports. The field that makes a provider
    # serving one interface and one transport at two ports separable at all.
    ports: list[int] | None = None  # endpoint filter; None = all endpoints

    # Modes that answer, or refuse to answer, before the write path a pause acts
    # on is ever reached. ``down`` returns a bodiless 503 pre-parse, ``hang``
    # sleeps out the caller's deadline and closes, and ``drop_connection`` hands
    # off to the adapter's drop path. ``port_closed`` receives no request at all.
    # A pause set with any of them is accepted, stored, echoed back by
    # GET /scenario — and does nothing.
    _MODES_THAT_NEVER_REACH_A_PAUSE = ("down", "hang", "drop_connection", "port_closed")

    def _validate(self, cfg: dict) -> None:
        self._validate_transports(cfg)
        self._validate_ports(cfg)
        self._validate_pause(cfg)

    def _validate_transports(self, cfg: dict) -> None:
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

    def _validate_ports(self, cfg: dict) -> None:
        """Refuse a ports list that could not target anything.

        This class cannot tell whether the provider serves a given port — it holds
        no endpoints. So it checks only the shape, and the control API checks the
        ports against the provider's own endpoints, where they are known. Both
        halves matter: a wrong TYPE is caught here even when the API is bypassed,
        and a wrong PORT is caught there, where it can be.

        An empty list is refused rather than read as "no filter". Written down
        because the two readings are opposite — "target nothing" and "target
        everything" — and a caller that built the list from a filter that matched
        nothing would get the second when it meant the first.
        """
        ports = cfg.get("ports")
        if ports is None:
            return
        if not isinstance(ports, (list, tuple)):
            raise ValueError(f"ports must be a list of integers or None, got {ports!r}")
        if not ports:
            raise ValueError(
                "ports must not be empty; use None for 'every endpoint'. An empty list "
                "would target no endpoint, so no fault would apply and the provider "
                "would answer normally"
            )
        bad = [p for p in ports if isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= 65535]
        if bad:
            raise ValueError(f"ports must be integers in [1, 65535], got {bad!r}")

    def _validate_pause(self, cfg: dict) -> None:
        """Refuse a pause that could not possibly happen.

        This file's own rule, stated at the top: a knob that cannot apply is
        rejected "so a typo or a wrong-chain knob fails loudly rather than being
        silently ignored". ``transports`` already works that way. A pause is
        worse than a wrong-chain knob when it is ignored, because a paused reply
        is a CORRECT reply — so a test whose pause never armed reads a fast,
        valid answer and concludes the router was fast.

        Only combinations arriving in the SAME update are checked. ``update()``
        merges by field and hands this hook that one call's keys, so a stored
        ``mode`` is not visible here. Every caller that goes through the
        automation control client sends the whole block at once, which is the
        case this catches.
        """
        pause_at = cfg.get("pause_at")
        mode = cfg.get("mode")
        if pause_at and mode in self._MODES_THAT_NEVER_REACH_A_PAUSE:
            raise ValueError(
                f"pause_at={pause_at!r} cannot apply with mode={mode!r}: that mode answers, "
                "or refuses to answer, before the reply is written, so the hold would never "
                "happen. Use mode='success' for a pause, or drop pause_at"
            )

        transports = cfg.get("transports")
        if pause_at and transports is not None and "http" not in transports:
            raise ValueError(
                f"pause_at={pause_at!r} cannot apply with transports={transports!r}: a pause is "
                "performed by the HTTP adapter, which serves the 'http' transport. The WebSocket "
                "and gRPC wires have their own performers and no pause branch, so the hold would "
                "never happen"
            )

        if cfg.get("pause_ms") and "pause_at" in cfg and not pause_at:
            raise ValueError(
                f"pause_ms={cfg['pause_ms']!r} has no effect without pause_at: nothing decides "
                "WHERE to hold. Set pause_at to 'after_headers' or 'mid_body'"
            )
