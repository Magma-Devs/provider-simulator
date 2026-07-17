"""Domain model: pools, providers, endpoints, scenario config, quirks, call log.

This __init__ re-exports the supported public surface. Import from here
(``from provider_simulator.domain import Provider``) rather than from the
implementation modules — internals may move between files as the redesign
progresses; this list is the compatibility contract.
"""

from provider_simulator.domain.call_log import CallLog
from provider_simulator.domain.endpoint import INTERFACES, TRANSPORTS, Endpoint
from provider_simulator.domain.introspective_config import IntrospectiveConfig
from provider_simulator.domain.provider import Pool, Provider
from provider_simulator.domain.quirks import (
    EthQuirks,
    Quirks,
    SolanaQuirks,
    known_chains,
    quirks_for,
)
from provider_simulator.domain.registry import Registry, build_registry
from provider_simulator.domain.scenario import ScenarioConfig

__all__ = [
    "CallLog",
    "Endpoint",
    "EthQuirks",
    "INTERFACES",
    "IntrospectiveConfig",
    "Pool",
    "Provider",
    "Quirks",
    "Registry",
    "ScenarioConfig",
    "SolanaQuirks",
    "TRANSPORTS",
    "build_registry",
    "known_chains",
    "quirks_for",
]
