"""One network endpoint a provider listens on.

An endpoint pairs an application protocol (interface) with the transport that
carries it and the TCP port it is served on. JSON-RPC is served over two
transports — http for request/response and ws for subscriptions — so a JSON-RPC
provider has two endpoints; REST and gRPC providers have one each.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Endpoint:
    interface: str  # application protocol: jsonrpc | rest | grpc | tendermintrpc
    transport: str  # transport protocol:   http | http2 | ws
    port: int
