# ADR-002: A request id on every interface

**Status**: Proposed

**Date**: 2026-10-07 (version 2.2: after the review of the session `reviewer`, the runs on the local k3d cluster, and the review of the session `reviewer-2`)

**Deciders**: Victoria

**Context**: A test must read the history rows of one request, to see which providers received it. On JSON-RPC a test does this with `get_history(request_id=..., pool=...)`. Rule R45 of the automation repository requires that call. It does not work on REST or on gRPC. On REST the id comes from the header `X-Request-Id` (`provider_simulator/listeners/rest.py:90`), and the router does not pass that header to a provider: it passes a header only if the chain spec declares it (`protocol/chainlib/base_chain_parser.go:162-204` of smart-router, read at `1949100f`). On gRPC a row has no request id (`provider_simulator/listeners/grpc.py:87-88`). The test of MAG-3800 (the router bug: a wrong provider name gets HTTP 500) must prove that no provider received one request, on REST and on gRPC. The router does not change: Victoria's words, through session 3800. The design document has the facts and their line numbers: [One request flow for every interface](../superpowers/specs/2026-10-06-one-request-flow-design.md).

## Decision

1. Every interface uses one row field, `request_id`, and one filter, `GET /history?request_id=<id>&pool=<pool>`.
2. Each interface names one place where a caller puts the id. The place must pass the router.

   | Interface | The place |
   |---|---|
   | `jsonrpc` | The `id` of the body. No change. |
   | `tendermintrpc`, POST | The `id` of the body. No change. |
   | `rest` | The query parameter `request_id`. Then the header `X-Request-Id`, in any letter case. Then the counter of the simulator. |
   | `grpc` | The field of the request message that the method names. For `AllBalances` it is `address`. |

3. The simulator serves `cosmos.bank.v1beta1.Query/AllBalances`, so that gRPC has a method with such a field.
4. A caller id is not a plain number. The counter of the simulator gives plain numbers, and the filter compares text. This holds for a Tendermint RPC POST too: the automation client sends the id "1" by default (`src/clients/http/tendermint_rpc_client.py:197-200` of the automation repository), so a test that reads Tendermint RPC rows by id must set its own id.
5. A method with no such field has no request id. The row of a provider-wide `down` has no request id.
6. On gRPC the key of a method stays its bare name, for example `GetLatestBlock`. The simulator refuses to start with two served methods of one name.

## Alternatives Considered

### Option 1: A header on REST, and metadata on gRPC

- **Pros**: The same carrier on both interfaces. Nothing is added to the URL or to the message.
- **Cons**: The router passes a header to a provider only if the chain spec declares it. On gRPC the same filtered list becomes the metadata.
- **Why not chosen**: The value does not reach the provider, and the router does not change.

### Option 2: The header that the chain spec declares, `x-cosmos-block-height`

- **Pros**: It passes the router today, on REST and on gRPC.
- **Cons**: It sets the block that the request asks for. So it changes which provider the router chooses. The value must be a block height.
- **Why not chosen**: A tool for a test must not change the behaviour that the test measures.

### Option 3: Record the parameters of each request in a new row field, with a new filter

- **Pros**: No rule for each interface. Any unique parameter identifies a request.
- **Cons**: Every row grows, and each provider keeps 2000 rows by default (`HISTORY_MAX`, `constants.py:94`). A test then has two rules to read rows: by id and by parameter. A new field and a new filter must be kept correct on every interface.
- **Why not chosen**: One rule to read rows is enough, and it exists.

### Option 4: Keep the time window and the method name

- **Pros**: No change to the simulator. The automation helper `rows_after` does this today.
- **Cons**: It cannot separate two tests that send the same method to the same pool in the same window. The router's own polls of that method are in the window too.
- **Why not chosen**: It cannot prove that no provider received one request.

### Option 5: The full gRPC name, `service/method`, as the key

- **Pros**: The key is unique. `Params` is a method of three compiled services: `cosmos.auth.v1beta1.Query`, `cosmos.bank.v1beta1.Query` and `cosmos.staking.v1beta1.Query`.
- **Cons**: It changes the `method` of every gRPC row of today, and the key of every gRPC override. The automation tests read both.
- **Why not chosen**: No served method shares its name with another one today. This is my proposal, and Victoria decides it as question Q12 of the design document.

## Consequences

**Positive**:
- A test reads the rows of one request the same way on every interface.
- Rule R45 can cover REST and gRPC.
- No new row field and no new filter.
- A REST caller that sends the header still works, in any letter case.

**Negative**:
- The place of the id differs for each interface. A test author must know three places: the `id` of the body, the query parameter, and the field of the message.
- On gRPC only a method with a fitting field can carry an id. The rows of `GetLatestBlock` and `GetNodeInfo` stay without one.
- The id is text on REST and on gRPC, and a number on JSON-RPC. A caller must not use a plain number as an id on REST and gRPC.
- A second gRPC method named `Params` cannot be served until the key changes.

**Neutral**:
- The `method` of a row does not change.
- A provider-wide `down` row is as it is today. A test that sets `down` counts those calls with `/stats`. A per-method `down` records the method and the id, as today.
- The Tendermint RPC URL form keeps the counter of the simulator.

## Implementation Notes

- REST: `RestListener.parse_request` reads the query parameter first. With more than one value it takes the first. It does so for the five verbs that reach the listener: GET, POST, PUT, DELETE and HEAD.
- gRPC: the servicer passes the request message to the listener. Today it passes the method name only (`server.py:1159-1169`).
- The router reads the description of a method from the provider: it asks the provider's reflection for the symbol of the service (`protocol/chainlib/grpc.go:790-805` of smart-router). RAN on 2026-10-07: reflection finds the symbol `cosmos.bank.v1beta1.Query` when the bank stubs are loaded in the server process. The list of names given to `reflection.enable_server_reflection` feeds "list services" only. So the server must import the bank stubs and register the servicer, and the name is added to the list for `grpcurl`. The test asks reflection for the symbol.
- VERIFIED BY A RUN on 2026-10-07 at 22:15 on the local k3d cluster, router build `v1.5.8-85-g4369814`, with a probe image: the router passes the REST query parameter `request_id` and the gRPC field `address` of `AllBalances` on to the provider. One history row came back for each id, two times on REST and two times on gRPC (section 11 of the design document, rows A1 and A2).
- The content of `AllBalances` is the content of the REST route `/cosmos/bank/v1beta1/balances/{address}`: one coin, `ulava`, amount `1000000`.
- A row gets its request id before the provider waits. So a read by request id finds a request that a provider holds with `latency_ms` or `hang`.
- Before the pull request merges: one request of `AllBalances` through `lava-sim-grpc-router` on the local k3d cluster, with the branch build, and one read of its row. The probe of 2026-10-07 already showed that the router sends the `address` field on, and that the chain spec lists the method: the reply carries `lava-user-request-type: cosmos.bank.v1beta1.Query/AllBalances` with no `Default-` in front, and a method that no spec lists carries `Default-` (a control run, saved in the evidence folder). The run with the branch build repeats it for the real code.
- **Migration path**: no test that exists must change. The simulator change merges first. Then the request helpers of the automation repository add the id on REST and call `AllBalances` on gRPC.
- A test that requires zero rows first sends a control request and requires one row or more for its id. That proves that the id travels.

## Success Metrics

- Through the router on the local k3d cluster: one REST request with `?request_id=<id>` and one `AllBalances` request each give their rows under `GET /history?request_id=<id>&pool=<pool>`, and no row of another request.
- The test at `tests/test_simulator_grpc.py:546-551` passes with no edit: a `GetLatestBlock` row has no request id.
- The tests of MAG-3800 read rows by id on REST and on gRPC, with a control.

## Review Date

When the router changes which headers it passes to a provider, when a second served gRPC method needs a name that exists, or on 2027-04-06. The earliest of the three. With the first change, one header can carry the id on every interface.
