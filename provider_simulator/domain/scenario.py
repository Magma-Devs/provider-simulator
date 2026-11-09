"""The universal fault configuration every simulated provider understands.

These fields describe faults that apply to any chain: outage, latency, errors,
rate-limiting, corruption, dropped connections, and the first-N-fail sequence.
Chain-specific knobs (Solana slot math, ETH logs-lag) live in Quirks instead —
sending one of those here is rejected, so a typo or a wrong-chain knob fails
loudly rather than being silently ignored.

One mode is not a fault on a request: ``port_closed`` closes the port of each
endpoint that the block targets. This class cannot tell which endpoints a
provider has, and the control API can. So the control API refuses the mode for
a block that targets no endpoint, and for a port with no listener.

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

Two fields change no fault and no body. ``reply_headers`` adds chosen headers to
each reply of a targeted endpoint, such as a ``Retry-After`` on a 429: the HTTP
adapter and the WebSocket adapter write them as headers, and the gRPC adapter as
the header metadata of its reply. ``reply_trailers`` adds chosen trailing
metadata to each reply of a targeted gRPC endpoint, and only the gRPC adapter
writes it, so a block that reaches no gRPC endpoint is refused.

Two fields choose the head that a provider reports. ``blocks_behind`` keeps a
distance from the chain head, so the provider moves when the head moves, and it
reaches every endpoint of the provider. ``frozen_head`` names one block, so the
provider stands still while the head moves, and it reaches only the endpoints
that the ``transports`` and ``ports`` filters target. Only one of the two can
apply, and the control API refuses both together.
"""

import re
import string
from dataclasses import dataclass, field

from provider_simulator.domain.endpoint import TRANSPORTS
from provider_simulator.domain.introspective_config import IntrospectiveConfig

# The characters of an HTTP ``token`` (RFC 9110, section 5.6.2). A header name is
# a token, and so is a media-type parameter value that needs no quotes.
HTTP_TOKEN_CHARS = frozenset("!#$%&'*+-.^_`|~" + string.digits + string.ascii_letters)

# The characters of a chosen header or trailer name. A gRPC metadata name holds
# only these, in lowercase, and the gRPC library ends the call for any other
# character. The gRPC adapter lowercases a chosen name, so capitals are allowed.
_CHOSEN_NAME_CHARS = frozenset("-_." + string.digits + string.ascii_letters)

# A grpc-retry-pushback-ms value that reaches the caller as written: a whole number
# with no leading zero, in the signed 64-bit range. The gRPC library parses the name
# itself and sends any other value as -9223372036854775808, or rewrites it ("0007"
# arrives as "7").
_WHOLE_NUMBER = re.compile(r"0|-?[1-9][0-9]*")
_SIGNED_64_BIT = range(-(2**63), 2**63)


@dataclass
class ScenarioConfig(IntrospectiveConfig):
    mode: str = "success"  # success | error | rate_limit | down | drop_connection | hang | port_closed
    latency_ms: int = 0
    error_probability: float = 0.0
    # The code of an error reply. It is an integer for JSON-RPC, REST, Tendermint
    # RPC and gRPC. A GraphQL provider reads a string: the ``extensions.code`` of
    # the error, such as "REQUEST_TIMEOUT". The empty string gives an error with no
    # ``extensions`` at all, and any integer gives INTERNAL_SERVER_ERROR.
    error_code: int | str = -32000
    error_message: str = "Internal error"
    http_status: int = 200
    # JSON-RPC-listener-only: the plain-text body a ``rate_limit`` fault sends.
    # Real providers answer a 429 with prose (or HTML), never a JSON-RPC error
    # envelope, so this is a separate field from error_message rather than a
    # reuse of it — REST/Tendermint/gRPC still build their rate_limit envelope
    # from error_code/error_message and are unaffected by this field.
    rate_limit_body: str = "Rate limit exceeded. Reduce your request rate, or use an API key for a higher limit."
    # GraphQL-listener-only: how a ``rate_limit`` fault answers. "status" is the
    # HTTP 429 with plain text that every interface sends. "body" is the form of a
    # GraphQL server that reports the limit inside a 200: ``data`` null and one
    # error with the code RESOURCE_EXHAUSTED.
    rate_limit_style: str = "status"  # status | body
    # GraphQL-listener-only: with ``mode="error"``, the whole reply body, sent with
    # ``http_status``. None means the error that the listener builds from
    # ``error_code`` and ``error_message``. A test uses it for a reply of any
    # shape, such as an HTTP 400 with a JSON-RPC body.
    error_body: object = None
    # GraphQL-listener-only: the reply to every request body that cannot be read as
    # a GraphQL request, as ``{"status": 100..599, "body": any JSON value or text}``.
    # None means the reply that the real Sui server gives for that kind of body.
    unreadable_reply: object = None
    responses: dict = field(default_factory=dict)  # per-method overrides
    corruption_mode: str | None = None
    missing_field: str | None = None
    blocks_behind: int = 0
    # The block that the provider reports as its head while the chain head moves.
    # None means the chain head, shifted by blocks_behind.
    frozen_head: int | None = None
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
    # Headers that the provider adds to each reply of a targeted endpoint, as
    # {name: value}. On an ``http`` endpoint: each reply with a status line, which
    # is a success, a fault, the bodiless 503 of ``down`` and the headers of a reply
    # that ``drop_connection`` cuts. On a ``ws`` endpoint: the answer to the upgrade
    # request, the 101 reply or a refusal; a frame carries none. On a gRPC endpoint:
    # the header metadata of each reply, with the names in lowercase. None or {}
    # means none. A chosen header replaces the header of the same name that the
    # simulator writes itself, such as ``Date``. A ``hang`` writes no HTTP reply.
    reply_headers: dict[str, str] | None = None
    # The trailing metadata that a gRPC provider adds to each reply of a targeted
    # endpoint, as {name: value}, with the names in lowercase. A reply that is a
    # status only sends no header metadata: its trailing metadata is all it
    # carries. None or {} means none.
    reply_trailers: dict[str, str] | None = None

    # The headers that frame a reply or the WebSocket handshake, and the
    # Content-Type that names the provider. A chosen value would break the reply,
    # the handshake or the name, so it is refused.
    _HEADERS_THE_SIMULATOR_OWNS = (
        "connection",
        "content-length",
        "content-type",
        "sec-websocket-accept",
        "transfer-encoding",
        "upgrade",
    )

    # The metadata names that the gRPC library of the simulator drops, sends with
    # its own value, or stops on: grpc-timeout ends the simulator process. A name
    # that ends in ``-bin`` carries bytes, and a text value ends the call. Both
    # fields reach a gRPC reply, so both refuse them.
    _NAMES_THE_GRPC_LIBRARY_OWNS = (
        "content-length",
        "content-type",
        "grpc-accept-encoding",
        "grpc-encoding",
        "grpc-internal-encoding-request",
        "grpc-message",
        "grpc-previous-rpc-attempts",
        "grpc-status",
        "grpc-timeout",
        "te",
    )

    # Modes that answer, or refuse to answer, before the write path a pause acts
    # on is ever reached. ``down`` returns a bodiless 503 pre-parse, ``hang``
    # sleeps out the caller's deadline and closes, and ``drop_connection`` hands
    # off to the adapter's drop path. ``port_closed`` receives no request at all.
    # A pause set with any of them is accepted, stored, echoed back by
    # GET /scenario — and does nothing.
    _MODES_THAT_NEVER_REACH_A_PAUSE = ("down", "hang", "drop_connection", "port_closed")

    _RATE_LIMIT_STYLES = ("status", "body")

    def _validate(self, cfg: dict) -> None:
        self._validate_transports(cfg)
        self._validate_ports(cfg)
        self._validate_pause(cfg)
        self._validate_rate_limit_style(cfg)
        self._validate_unreadable_reply(cfg)
        self._validate_chosen(cfg, "reply_headers")
        self._validate_chosen(cfg, "reply_trailers")
        self._validate_frozen_head(cfg)

    def _validate_frozen_head(self, cfg: dict) -> None:
        """Refuse a frozen head that is not a block number.

        A block number is an integer of 0 or more. ``True`` is refused although it
        is an int, and a text is refused although it can name a number: the chains
        compare the value with heights, so any other type would freeze nothing. The
        rules that need the provider, its chain, its endpoints and a
        ``blocks_behind`` that it already holds, are the control API's.
        """
        frozen = cfg.get("frozen_head")
        if frozen is None:
            return
        if isinstance(frozen, bool) or not isinstance(frozen, int) or frozen < 0:
            raise ValueError(f"frozen_head must be a block number (an integer of 0 or more) or null, got {frozen!r}")

    def _validate_chosen(self, cfg: dict, field_name: str) -> None:
        """Refuse chosen reply headers or trailers that could not be sent as they are written.

        A name is letters, digits, '-', '_' and '.', which an HTTP header name and
        a gRPC metadata name both allow. A value is text of visible ASCII
        characters and spaces: a line break would end the header and start
        another one, and a tab ends a gRPC call. Two names that differ only in
        letter case are one header, so the pair is refused. So are the names that
        the simulator writes itself on HTTP (``reply_headers`` only), the names
        that its gRPC library owns, a name that ends in ``-bin``, and a
        ``grpc-retry-pushback-ms`` that is not a whole number in the signed 64-bit
        range with no leading zero: the gRPC library sends another value as
        -9223372036854775808 or rewrites it.

        Only the gRPC adapter ('http2') writes trailers, so ``reply_trailers``
        with a ``transports`` filter without 'http2' is refused here. A ``ports``
        filter is checked by the control API, which knows the endpoints.
        """
        if field_name not in cfg or cfg[field_name] is None:
            return
        chosen = cfg[field_name]
        if not isinstance(chosen, dict):
            raise ValueError(f"{field_name} must be an object of names and text values, or null, got {chosen!r}")
        seen: set[str] = set()
        for name, value in chosen.items():
            if not isinstance(name, str) or not name or any(char not in _CHOSEN_NAME_CHARS for char in name):
                raise ValueError(
                    f"{field_name}: {name!r} is not an HTTP header name that gRPC metadata can hold. A name "
                    "is letters, digits, '-', '_' and '.'"
                )
            if not isinstance(value, str) or any(not " " <= char <= "~" for char in value):
                raise ValueError(
                    f"{field_name}[{name!r}] must be text of visible ASCII characters and spaces, "
                    f"got {value!r}. Write a number as text, such as '5'"
                )
            lowered = name.lower()
            if field_name == "reply_headers" and lowered in self._HEADERS_THE_SIMULATOR_OWNS:
                raise ValueError(
                    f"reply_headers cannot set {name!r}: the simulator writes it itself. Connection, "
                    "Content-Length and Transfer-Encoding frame the reply, Upgrade and Sec-WebSocket-Accept "
                    "frame the WebSocket handshake, and Content-Type names the provider"
                )
            if lowered in self._NAMES_THE_GRPC_LIBRARY_OWNS:
                raise ValueError(
                    f"{field_name} cannot set {name!r}: the gRPC library of the simulator drops that name "
                    "or sends its own value, and grpc-timeout stops the simulator"
                )
            if lowered.endswith("-bin"):
                raise ValueError(
                    f"{field_name} cannot set {name!r}: a gRPC name that ends in '-bin' carries bytes, and a "
                    "text value ends the call"
                )
            if lowered == "grpc-retry-pushback-ms" and not (
                _WHOLE_NUMBER.fullmatch(value) and int(value) in _SIGNED_64_BIT
            ):
                raise ValueError(
                    f"{field_name}[{name!r}] must be a whole number of milliseconds from "
                    f"-9223372036854775808 to 9223372036854775807 with no leading zero, such as "
                    f"'5000', got {value!r}: the gRPC library sends another value as "
                    "-9223372036854775808 or rewrites it"
                )
            if lowered in seen:
                raise ValueError(
                    f"{field_name} names {name!r} twice: names do not depend on letter case, "
                    "so the two entries are one name with two values"
                )
            seen.add(lowered)

        transports = cfg.get("transports")
        if field_name == "reply_trailers" and chosen and transports is not None and "http2" not in transports:
            raise ValueError(
                f"reply_trailers cannot apply with transports={transports!r}: only the gRPC adapter "
                "('http2') writes trailing metadata, so the trailers would reach no reply"
            )

    def _validate_rate_limit_style(self, cfg: dict) -> None:
        style = cfg.get("rate_limit_style")
        if "rate_limit_style" in cfg and style not in self._RATE_LIMIT_STYLES:
            raise ValueError(f"rate_limit_style must be one of {list(self._RATE_LIMIT_STYLES)}, got {style!r}")

    def _validate_unreadable_reply(self, cfg: dict) -> None:
        """The reply to a body that is not a request must be one that can be sent:
        a status that HTTP can carry, and a body. An unknown key is refused, so a
        typo such as ``bodyy`` fails loudly and does not send an empty reply."""
        if "unreadable_reply" not in cfg or cfg["unreadable_reply"] is None:
            return
        reply = cfg["unreadable_reply"]
        status = reply.get("status") if isinstance(reply, dict) else None
        if (
            not isinstance(reply, dict)
            or set(reply) - {"status", "body"}
            or isinstance(status, bool)
            or not isinstance(status, int)
            or not 100 <= status <= 599
        ):
            raise ValueError(
                'unreadable_reply must be null or {"status": an integer from 100 to 599, "body": any JSON value or '
                f"text}}, got {reply!r}"
            )

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
