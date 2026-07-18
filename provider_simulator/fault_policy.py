"""The one place fault decisions are made.

``decide(scenario, endpoint, provider)`` returns a Verdict for a single request:
which fault to emit (down / hang / drop / rate_limit / error) or ``none`` to let
the chain build the success response. It replaces the copy-pasted fault ladders
in the flat handlers — every transport routes its decision through here, so a
change to the fault rules happens once.

Two rules the ladder folds in:

- **transports filter.** A scenario block's ``transports`` list scopes its effect
  to specific endpoints of the provider. If set and this endpoint's transport is
  not in it, no fault applies — the endpoint serves success. ``None`` = every
  endpoint. Because provider state is per-provider (never shared across chains),
  a provider-wide ``down`` is just "no filter" — there is no universal-down
  special case to carry.

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
"""

import random
from dataclasses import dataclass

from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Provider


@dataclass(frozen=True)
class Verdict:
    kind: str  # none | down | hang | drop | rate_limit | error
    status: int = 200
    error_code: int = 0
    error_message: str = ""
    drop_at: str = "before_headers"


NONE_VERDICT = Verdict(kind="none")
_NONE = NONE_VERDICT  # backward-compatible module-private alias


def resolve_mode(scenario: dict, endpoint: Endpoint, provider: Provider) -> tuple[bool, str]:
    """Apply the transports filter and the fail_first_n window; return
    ``(targeted, effective_mode)``.

    This is the only stateful step of the policy: a targeted request under an
    active ``fail_first_n`` advances the provider's counter, so call it exactly
    once per request. An untargeted endpoint neither faults nor advances the
    window — its effective mode is always ``success``.
    """
    transports = scenario.get("transports")
    targeted = transports is None or endpoint.transport in transports

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
            kind="rate_limit", status=429, error_code=429, error_message="Too many requests"
        )
    if mode == "error" or random.random() < scenario.get("error_probability", 0.0):
        return Verdict(
            kind="error",
            status=scenario.get("http_status", 200),
            error_code=scenario.get("error_code", -32000),
            error_message=scenario.get("error_message", "Internal error"),
        )
    return NONE_VERDICT


def decide(scenario: dict, endpoint: Endpoint, provider: Provider) -> Verdict:
    """Return the fault Verdict for one request on ``endpoint`` of ``provider``.

    ``scenario`` is a ScenarioConfig snapshot dict. Pure except for advancing the
    provider's fail_first_n counter on a targeted endpoint.
    """
    targeted, mode = resolve_mode(scenario, endpoint, provider)
    if not targeted:
        return NONE_VERDICT
    return ladder(mode, scenario)
