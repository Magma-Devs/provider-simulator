"""The one place fault decisions are made.

``decide(scenario, endpoint, provider)`` returns a Verdict for a single request:
which fault to emit (down / hang / drop / rate_limit / error) or ``none`` to let
the chain build the success response. It replaces the copy-pasted fault ladders
in the flat handlers — every transport routes its decision through here, so a
change to the fault rules happens once.

Two rules the ladder folds in:

- **transports, ports and callers filters.** A scenario block's ``transports``
  list scopes its effect to specific wires of the provider, its ``ports`` list to
  specific ADDRESSES of the provider, and its ``callers`` list to the addresses
  that REQUESTS come from. If set and this endpoint or this caller does not match,
  no fault applies — the request gets success. ``None`` = every endpoint or every
  caller. They AND together. Because provider state is per-provider (never shared
  across chains), a provider-wide ``down`` is just "no filter" — there is no
  universal-down special case to carry.

  ``ports`` is what makes a provider serving one interface and one transport at
  two addresses separable: ``transports`` cannot tell those two apart, so before
  this field a fault on such a provider always reached both. ``callers`` is what
  makes two callers of one provider separable, such as two pods of one router.

- **fail_first_n sequence.** When set, the first N requests on a targeted
  endpoint get the fault ``mode``; every request after switches to ``then_mode``
  (default ``success``). The counter lives on the provider and is only advanced
  by endpoints the filter targets.

``decide`` is split into two reusable halves so a listener that layers
per-method overrides on top can consume the fail_first_n window exactly once:
``resolve_mode`` applies the filter + window (the stateful half), ``ladder``
maps a mode to a Verdict (the pure half). ``decide`` composes them.

Latency is NOT a Verdict — it modifies timing on both success and fault paths, so
the listener applies it, not the policy.

``mode="port_closed"`` is NOT a Verdict either, for a different reason: a Verdict
answers a request, and a closed port receives none. ``port_closed(scenario,
endpoint)`` is the decision for it. The loop of the port reads it, and not each
request. It goes through ``targets`` like everything else here. Each provider
port has such a loop: a gRPC port, an ``http`` port and a ``ws`` port.
"""

import random
from dataclasses import dataclass

from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Provider


@dataclass(frozen=True)
class Verdict:
    kind: str  # none | down | hang | drop | rate_limit | error
    status: int = 200
    # An integer for every interface but GraphQL, which reads a string (the
    # ``extensions.code`` of its error, or "" for an error with none).
    error_code: int | str = 0
    error_message: str = ""
    drop_at: str = "before_headers"
    # rate_limit kind only: the plain-text body the JSON-RPC listener sends.
    # REST/Tendermint/gRPC keep building their rate_limit shape from
    # error_code/error_message above — only jsonrpc.py reads this field.
    rate_limit_body: str = ""
    # rate_limit kind only, GraphQL listener only: "status" is the HTTP 429 with
    # plain text, and "body" is a 200 with a RESOURCE_EXHAUSTED error. The status
    # of the verdict stays 429 either way: the listener decides what to send.
    rate_limit_style: str = "status"
    # error kind only, GraphQL listener only: the whole reply body, or None for the
    # error that the listener builds.
    error_body: object = None


NONE_VERDICT = Verdict(kind="none")
_NONE = NONE_VERDICT  # backward-compatible module-private alias


def targets(scenario: dict, endpoint: Endpoint, caller: str | None = None) -> bool:
    """Does this scenario block apply to this endpoint, and to this caller?

    The ONE answer to that question. The filters AND together, and each one left
    as ``None`` means "no opinion", so a block with none targets every endpoint
    and every caller exactly as it did before ``ports`` and ``callers`` existed.

    ``caller`` is the text of the address that a request came from
    (``RawRequest.caller``). A request that is being served always passes it, and
    an empty text is a request whose address is unknown: it is none of the listed
    callers. ``None`` means that no request is in question, as when the control API
    asks which endpoints a block names, and the loop of a port asks which port to
    close. A block says which callers it applies to and nothing about an
    endpoint, so the ``callers`` filter is then left out of the answer.

    Exported rather than inlined into ``resolve_mode`` because it is not the only
    caller: the control API asks it which endpoints a block names. The gRPC
    listener once asked it a second time for ``corruption_mode``, with a copy of
    the rule that read ``transports`` and never learned about ``ports``. Since
    gRPC went into ``Listener.serve``, one answer of ``resolve_mode`` gates the
    mode, the latency and the corruption of every interface.
    """
    transports = scenario.get("transports")
    ports = scenario.get("ports")
    callers = scenario.get("callers")
    return (
        (transports is None or endpoint.transport in transports)
        and (ports is None or endpoint.port in ports)
        and (callers is None or caller is None or caller in callers)
    )


def port_closed(scenario: dict, endpoint: Endpoint) -> bool:
    """Does this scenario block close this endpoint's port?

    ``port_closed`` is the one mode that is not a Verdict. A Verdict says what
    to answer a request that arrived, and a closed port receives none: the loop
    of the port reads this function and stops the endpoint's server instead (see
    ``provider_simulator/port_gate.py``). It honours the same ``transports`` and
    ``ports`` filters as every other mode, through ``targets``.

    ``ladder`` has no branch for the mode on purpose. A request can still reach
    a targeted endpoint in the moment between the scenario being written and the
    server stopping. That request arrived at a port that was still open, so it
    is answered as an open port answers.
    """
    return scenario.get("mode") == "port_closed" and targets(scenario, endpoint)


def resolve_mode(scenario: dict, endpoint: Endpoint, provider: Provider, caller: str = "") -> tuple[bool, str]:
    """Apply the filters and the fail_first_n window; return
    ``(targeted, effective_mode)``.

    ``caller`` is the text of the address that the request came from, and an empty
    text is a request with an unknown address (see ``targets``).

    This is the only stateful step of the policy: a targeted request under an
    active ``fail_first_n`` advances the provider's counter, so call it exactly
    once per request. An untargeted request neither faults nor advances the
    window — its effective mode is always ``success``. A request from a caller
    that the ``callers`` filter does not name is untargeted, so the other pod of a
    router does not spend the window of the pod under test.
    """
    targeted = targets(scenario, endpoint, caller)

    mode = scenario.get("mode", "success")
    fail_first_n = scenario.get("fail_first_n", 0)
    if fail_first_n > 0 and targeted:
        # Only targeted endpoints burn the window; once past N, switch to then_mode.
        if provider.consume_fail() > fail_first_n:
            mode = scenario.get("then_mode", "success")

    if not targeted:
        return False, "success"
    return True, mode


def ladder(mode: str, scenario: dict) -> Verdict:
    """Map an effective mode to its fault Verdict (pure — no window state).

    ``error_probability`` still rolls here: a ``success`` mode can turn into an
    error Verdict probabilistically, matching the legacy ladder order.
    """
    if mode == "down":
        return Verdict(kind="down")
    if mode == "hang":
        return Verdict(kind="hang")
    if mode == "drop_connection":
        return Verdict(kind="drop", drop_at=scenario.get("drop_at", "before_headers"))
    if mode == "rate_limit":
        return Verdict(
            kind="rate_limit",
            status=429,
            error_code=429,
            error_message="Too many requests",
            rate_limit_body=scenario.get(
                "rate_limit_body",
                "Rate limit exceeded. Reduce your request rate, or use an API key for a higher limit.",
            ),
            rate_limit_style=scenario.get("rate_limit_style", "status"),
        )
    if mode == "error" or random.random() < scenario.get("error_probability", 0.0):
        return Verdict(
            kind="error",
            status=scenario.get("http_status", 200),
            error_code=scenario.get("error_code", -32000),
            error_message=scenario.get("error_message", "Internal error"),
            error_body=scenario.get("error_body"),
        )
    return NONE_VERDICT


def decide(scenario: dict, endpoint: Endpoint, provider: Provider, caller: str = "") -> Verdict:
    """Return the fault Verdict for one request on ``endpoint`` of ``provider``,
    from the address ``caller``.

    ``scenario`` is a ScenarioConfig snapshot dict. Pure except for advancing the
    provider's fail_first_n counter on a targeted request.
    """
    targeted, mode = resolve_mode(scenario, endpoint, provider, caller)
    if not targeted:
        return NONE_VERDICT
    return ladder(mode, scenario)
