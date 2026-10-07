# ADR-001: One request flow for every interface

**Status**: Proposed

**Date**: 2026-10-07 (version 2, after the review of the session `reviewer`)

**Deciders**: Victoria

**Context**: The simulator serves four interfaces: JSON-RPC, REST, Tendermint RPC and gRPC. `Listener.serve` (`provider_simulator/listeners/base.py:155-261`) is the request flow of three of them, JSON-RPC, REST and Tendermint RPC: the fixed order of steps that answers one request. gRPC has a copy of that flow, `GrpcListener.plan` (`provider_simulator/listeners/grpc.py:76-187`). `server.py` repeats parts of it for WebSocket in two places: the upgrade request (lines 453-552) and the subscribe frames (lines 596-669). So a rule about faults, latency, history rows or request ids must be written in each place.

The places disagree today in six ways: the scope of `latency_ms` on gRPC, the list of per-method fault keys on a subscribe frame, the corruption of a subscribe reply, the canned `body` of a subscribe method, the order of the `down` check and the per-method override on a subscribe frame, and the corruption of a fault reply on gRPC. The first one was run: on gRPC, `latency_ms` reaches an endpoint that the `ports` filter does not name, and on REST it does not.

Victoria asks that a new ability is easy to add on every interface (her comment in section 9.1 of the design document). A new interface is not the reason for this decision: an interface on `http` needs no change to the request flow today. The design document has the facts and their line numbers: [One request flow for every interface](../superpowers/specs/2026-10-06-one-request-flow-design.md).

## Decision

1. `Listener.serve` is the only request flow of the simulator. Every request goes through it: a JSON-RPC, REST, Tendermint RPC or gRPC call, and every WebSocket frame.
2. An interface is a subclass of `Listener` that fills hooks. It has no order of steps of its own.
3. Only code in `provider_simulator/listeners/` writes a history row or decides the fault of a request. A socket adapter in `server.py` performs a `ServeResult`. It answers four protocol cases itself, with no listener and no history row: OPTIONS on REST, a wrong WebSocket path, a bad upgrade request, and a WebSocket frame that is not JSON.
4. The WebSocket upgrade is not a request. It stays a second, smaller decision, in one method of the WebSocket listener.
5. A difference between interfaces that a test can see stays as it is. A hook with a default keeps it. A change of such a difference is its own decision. Section 9.3 of the design document names the twelve differences.
6. Tests pin today's behaviour before the code moves. The work has three steps and six pull requests: 1, 2a, 2b, 2c, 3a and 3b. Victoria chose three steps on 2026-10-06. Pull request 2c makes four differences of the gRPC rows uniform, after the move of 2b. It is part of the plan of section 10.5 of the design document, which Victoria accepted on 2026-10-07 with the words "so this is a plan, we will do it, add it ti the plan".
7. The design must give a real down. Victoria said so on 2026-10-07: "so you need to fix it in design". Its form is the mode that the failover work built and tested for gRPC, `mode="port_closed"` (pull request #133 of the simulator). Her words on 2026-10-07: "we already tested it and learned how to do it in failover". The design extends that mode to the `http` and `ws` endpoints, as step 4 with the pull requests 4a and 4b (section 14.5 of the design document). The mode `down` does not change: it stays an HTTP 503 reply on an open connection.

## Alternatives Considered

### Option 1: Keep the four places, and fix the disagreements where they are

- **Pros**: One small pull request. Four edits remove four of the six disagreements: `provider_simulator/listeners/grpc.py:85`, `server.py:623`, `server.py:666`, and a call of `build_body_override` in the subscribe code. No risk to the replies of gRPC and WebSocket beyond those lines.
- **Cons**: The four places stay. The next rule is written in up to four places again, and nothing checks that a place was not forgotten. Two rules already needed more than one place: the `ports` filter (`provider_simulator/listeners/grpc.py:189-196` records the miss) and the request id.
- **Why not chosen**: This is my proposal, and Victoria decides it as question Q1 of the design document. Her requirement is that a new ability is easy to add. With four places, each ability is added up to four times.

### Option 2: Rebuild the flow as one engine, with a gateway for each interface

- **Pros**: One new vocabulary for the request and the reply. Every interface moves at the same time and is treated the same.
- **Cons**: It rewrites the flow that works for JSON-RPC, REST and Tendermint RPC. Every interface is at risk at once. One large change is hard to review and hard to revert. The request id for REST and gRPC waits for the rebuild.
- **Why not chosen**: The same end state is reached when gRPC and the WebSocket subscribe frames move into the flow that exists. That path changes one interface for each pull request.

### Option 3: Keep two flows, and share functions between them

- **Pros**: A small change to the gRPC listener.
- **Cons**: The order of the steps stays written twice. The disagreement that was run is a difference of order and scope. Both flows already call the same `fault_policy` functions.
- **Why not chosen**: It does not remove the cause.

## Consequences

**Positive**:
- A rule is written one time and every interface obeys it.
- A new ability is added in one place, with one hook where the interfaces differ.
- gRPC obeys the `ports` filter and the `transports` filter for `latency_ms`.
- A subscribe frame obeys `corruption_mode`, all seven per-method fault keys, and a canned `body`.
- A new gRPC method is two changes: one row of a table, and its content in the chain.
- `server.py` loses its request decisions, its history rows and its protobuf classes.
- The tests of 2a and 3a pin behaviour that no test pins today.

**Negative**:
- `Listener` gets five new hooks and one new method: `early_identity`, `build_down`, `unpaid_latency`, `corrupt`, `build_content`, and the method `arrive`. The base class is wider.
- A reader of the gRPC listener must know the flow of the base class.
- Four pull requests, 2a, 2b, 3a and 3b, bring no new feature that a test asked for.
- Twelve differences between interfaces stay. The hooks make them visible, and they do not remove them.
- The cost of a new interface does not change. It is the same today.

**Neutral**:
- `tests/test_listener_grpc.py` moves from `plan` to `serve`, with the same expected values.
- `GrpcPlan` and `GrpcListener.plan` are deleted.
- The names of the new hooks are proposals. The implementation plan settles them.

## Implementation Notes

- Each new hook has a default that keeps `JsonRpcListener`, `RestListener` and `TendermintListener` as they are.
- gRPC: `ServeResult.body` holds a status (a code and a text) or a reply message. One table in `listeners/grpc.py` names each served method, its service, the field that holds the request id, and the function that builds the reply.
- gRPC keeps the registration that the generated code does today. Only its place changes: every protobuf class moves from `server.py` to `listeners/grpc.py`. So `grpcio` refuses a message that does not parse, as today, and a method that is not served keeps its status `UNIMPLEMENTED` and its text.
- The gRPC thread keeps its name, `grpc-<pool>:<pid>`. A test finds it by that name.
- The per-method `error_stub` and `error` of gRPC move to `LavaChain._build_grpc`, where REST and Tendermint RPC have theirs.
- WebSocket: a subclass of `JsonRpcListener` in `listeners/ws.py` answers a subscribe frame and an unsubscribe frame, and decides the upgrade.
- A guard test reads `server.py`. It fails if the file imports `fault_policy` or writes a history row.
- **Migration path**: 2a pins gRPC, 2b moves gRPC, 3a pins the WebSocket rows, 3b moves WebSocket. A pull request can be reverted while it is the newest one that merged. Reverts go in the reverse order of the merges. Step 1, the request id, is ADR-002.

## Success Metrics

- `GrpcListener.serve is Listener.serve` is true.
- `grep -n "fault_policy\|log\.record_arrival\|log\.finalize\|log\.push" server.py` prints nothing.
- The tests of 2a and 3a pass after 2b and 3b, and the only expected values that changed are rows of section 9.2 of the design document that Victoria accepted.
- The next ability is added with one change to `Listener.serve`, and with an override only in the interfaces that differ.

## Review Date

When the next ability after the request id is added, or on 2027-04-06. The earlier of the two.
