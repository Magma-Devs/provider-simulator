"""One network endpoint a provider listens on.

An endpoint pairs an application protocol (interface) with the transport that
carries it and the TCP port it is served on. JSON-RPC is served over two
transports — http for request/response and ws for subscriptions — so a JSON-RPC
provider has two endpoints; REST and gRPC providers have one each.

INTERFACES and TRANSPORTS are the closed vocabularies. Everything that names an
interface or a transport (the topology table, a scenario's transports filter)
validates against these, so a typo like "grpc"-as-a-transport fails loudly at
config time instead of silently matching zero endpoints.
"""

from dataclasses import dataclass

INTERFACES = ("jsonrpc", "rest", "grpc", "tendermintrpc")
TRANSPORTS = ("http", "http2", "ws")


@dataclass(frozen=True)
class Endpoint:
    interface: str  # application protocol — one of INTERFACES
    transport: str  # transport protocol — one of TRANSPORTS
    port: int
