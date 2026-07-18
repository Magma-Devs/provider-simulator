"""Listeners: the request-flow template shared by every transport.

A Listener runs the same sequence for all transports (record arrival → snapshot
scenario → parse → fault policy → emit fault or build success → finalize). Only
parsing and wire-shaping differ per transport, and those are the hooks a
subclass implements. serve() returns a ServeResult (a plan of what to send), so
the flow is testable without a socket; the socket adapter is wired in at the
server cut-over.
"""

from provider_simulator.listeners.base import Listener, ParseError, RawRequest, ServeResult
from provider_simulator.listeners.jsonrpc import JsonRpcListener
from provider_simulator.listeners.rest import RestListener
from provider_simulator.listeners.tendermint import TendermintListener

__all__ = [
    "Listener",
    "ParseError",
    "RawRequest",
    "ServeResult",
    "JsonRpcListener",
    "RestListener",
    "TendermintListener",
]
