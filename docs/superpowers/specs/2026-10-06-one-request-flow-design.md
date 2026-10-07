# One request flow for every interface: design

| | |
|---|---|
| Status | Proposed, version 2.2. Victoria reviews it. No code changes before she approves. One probe patch exists for the runs of section 11. It is the last commit of the branch `worktree-one-request-flow-design`, marked NOT FOR MERGE. It is not pushed, and it must stay out of any pull request of these documents. |
| Date | 2026-10-07 |
| Author | The design session (an AI assistant in Victoria's session), with inputs from session 3800 |
| Reviewed by | The session `reviewer`, on 2026-10-07: 15 findings. I checked each one against the code, and all 15 hold. Section 18 lists what they changed. |
| Reviewed again by | The session `reviewer-2`, on 2026-10-07, on Victoria's order: 14 findings on the verification work and on version 2.2. Its report is in the evidence folder (`reviewer-2-report.md`). Section 18 says which findings are fixed. |
| Decided by Victoria | The size: three steps (2026-10-06, in the design session). The router does not change (her words, through session 3800). On 2026-10-07, in her words: the design must give a real down ("so you need to fix it in design"); the test adjustments of section 10.5 ("so this is a plan, we will do it, add it ti the plan"); the documents and the probe are committed ("for this session - all the work that you need to commit - commit it"). The form of the real down is the one of the failover work, `mode="port_closed"`: her words, "we already tested it and learned how to do it in failover - didnt' we?". That mode exists for gRPC since pull request #133 of the simulator. Its place in the plan, a fourth step, is my plan. |
| Decision records | [ADR-001](../../adr/001-one-request-flow.md), [ADR-002](../../adr/002-request-id-on-every-interface.md) |
| Pictures | https://claude.ai/artifact/Wk7ioyLuE8sz2v8yq4Q3EK (private to Victoria until she shares it) |
| Simulator code read at | `e80acbf`, the `main` commit of 2026-10-05 |
| Router code read at | `1949100f`, a `main` commit of smart-router of 2026-10-06 |

## 1. Summary

> **Victoria's comment, 2026-10-07, on "four interfaces" and on "Three of them":** "which one you should always name it not only give numbers."
>
> **Reply:** Done, in this document, in both decision records and on the picture page. Each count now names its items.

The simulator serves four interfaces: JSON-RPC, REST, Tendermint RPC and gRPC. Three of them, JSON-RPC, REST and Tendermint RPC, share one request flow, `Listener.serve`.
gRPC has a copy of that flow. WebSocket repeats parts of it in two places: the upgrade request and the subscribe frames.

So a rule that we add to the flow must be added to each copy.
The request id is the current example. A test can read the history rows of one JSON-RPC request.
It cannot do that for one REST request or one gRPC request.

This design has three steps and six pull requests: 1, 2a, 2b, 2c, 3a and 3b. Pull request 2c was added on 2026-10-07: Victoria accepted the plan of section 10.5, which names it. On the same day she said that the design must give a real down ("so you need to fix it in design"). Its form is the mode of the failover work, `mode="port_closed"`, which exists for gRPC (Victoria: "we already tested it and learned how to do it in failover"). Section 14.5 extends it to `http` and `ws`, as a fourth step with two pull requests, 4a and 4b.

1. **Step 1.** REST and gRPC get a request id that passes the router. One pull request.
2. **Step 2.** gRPC uses `Listener.serve`, and its copy is deleted. Two pull requests: 2a adds tests that pin today's gRPC behaviour, and 2b moves the code.
3. **Step 3.** The WebSocket subscribe frames use `Listener.serve`, and only the listeners package writes history rows. Two pull requests: 3a adds tests that pin today's WebSocket rows, and 3b moves the code.

A pull request can be reverted while it is the newest one that merged. Reverts go in the reverse order of the merges.

What steps 2 and 3 give: a rule about faults, latency, history rows or ids is written one time, and one package writes history rows. They do not make a new interface cheaper. An interface on `http` needs no change to the request flow today (section 14.1).

No reply and no history row changes, except the changes that section 9 lists.

> **Victoria's comment, 2026-10-07:** "i need it visualised how it was vs how do you suggest it create arficat 0 technical as architect would do wiht all the picuttrs/ chart. frows"
>
> **Reply:** The page is https://claude.ai/artifact/Wk7ioyLuE8sz2v8yq4Q3EK. It has six figures: the code today, the proposed code, the ten steps of `Listener.serve` with their hooks, the classes today and proposed, Facade against Template Method, and the request id through the router.

## 2. How each fact is labelled

| Label | Meaning |
|---|---|
| READ | I read the code at the commit named above. Nothing was run. |
| RAN | I ran it on 2026-10-06 or 2026-10-07 on Victoria's Mac, in memory, with no socket. Section 16 gives the commands. |
| MEASURED BY 3800 | Session 3800 sent a request on the local k3d cluster on 2026-10-06 and read the result. One reading, unless a count is given. |
| RAN BY THE REVIEWER | The session `reviewer` ran it in memory on 2026-10-07. |
| RAN ON THE CLUSTER, also written "PROVEN BY A RUN", "VERIFIED BY A RUN" and "Measured on 2026-10-07" | On 2026-10-07 the session `design` or the session `worker` sent requests through a router of the local k3d cluster, or ran test files on it, and read the result. The cluster then ran simulator commit `e80acbf` and router build `v1.5.8-85-g4369814`. The builds were read from the pods `provider-simulator` (`GET /version`), `eth-sim-router`, `lava-sim-grpc-router` and `eth-failover-prodlimits-sim-router` (`/bin/smart-router version`). Section 16.5 gives the commands and the saved outputs. |
| RAN, a socket experiment | A script opened real local sockets on Victoria's Mac, with no cluster: `real_down_http_probe.py` and `reflection_symbol_probe.py`. Section 16.5 gives them. |
| READ BY `reviewer-2` | The session `reviewer-2` read the code on 2026-10-07 and gave the lines. I did not open them, unless the sentence says so. |
| NOT CHECKED | Nobody checked it. Section 17 holds the full list. |

A fact with no label is READ.

Session 3800 is closed since 2026-10-07. Its readings stay as it recorded them.

Two facts about the cluster of those measurements, both from session 3800:

- The simulator on the cluster ran `e80acbf`, the commit that I read. Session 3800 read it from `GET /version` on 2026-10-06.
- The router on the cluster ran the build `v1.5.8-85-g4369814`. That build is 22 commits older than the router commit that I read.

## 3. Words

Every word below is the name that the code or `CONTEXT.md` uses. A word marked "new" is a proposal.

| Word | Meaning | Where it is defined |
|---|---|---|
| Interface | The application protocol of an endpoint: `jsonrpc`, `rest`, `grpc` or `tendermintrpc`. | `CONTEXT.md`; `INTERFACES` in `provider_simulator/domain/endpoint.py:16` |
| Transport | What carries the interface: `http`, `ws` or `http2`. | `CONTEXT.md`; `TRANSPORTS` in `provider_simulator/domain/endpoint.py:17` |
| Request flow (new glossary entry) | The fixed order of steps that answers one request. | `Listener.serve`, `provider_simulator/listeners/base.py:155-261` |
| Hook | A method of `Listener` that a subclass fills for its interface. | `provider_simulator/listeners/base.py:263-313` |
| Socket adapter | A class that owns a socket. It reads the bytes and performs a `ServeResult`. | `server.py:1-29` |
| History row (new glossary entry) | One call in the history of a provider. `CONTEXT.md` has the entries "Call log" (one provider's record of its calls) and "History" (the recent calls of the ring buffer). A history row is one of those calls. | `CallLog._new_entry`, `provider_simulator/domain/call_log.py:70-96` |
| Request id (new glossary entry) | The value of the `request_id` field of a history row. | `provider_simulator/domain/call_log.py:85` |
| Ability (new) | Something that a test can make an interface do or read. The request id is an ability. | Section 14.4 |
| Copy | A second place that repeats steps of the request flow. | Section 4.2 |

In the sections below, `base.py`, `grpc.py`, `rest.py`, `jsonrpc.py`, `tendermint.py` and `ws.py` are files of `provider_simulator/listeners/`. `chains/` and `domain/` are folders of `provider_simulator/`. A path that starts with `protocol/` is a file of the router.

Two notes about words:

- **GraphQL is an interface, not a transport.** It travels on `http`, which the simulator already serves. WebSocket is a transport. ABCI is not an interface of the simulator: the interface is `tendermintrpc`.
- **The word "listener".** `CONTEXT.md` tells a writer not to call an endpoint a "listener". The class `Listener` is a different thing: it is the request flow of one endpoint. This design keeps the class name. Question Q15 in section 15 asks if the glossary needs an entry for it.

## 4. What the code does today

### 4.1 The request flow

`Listener.serve` (`provider_simulator/listeners/base.py:155-261`) runs these ten steps in this order:

1. Record the arrival: a history row with the status `in_flight`.
2. Take a snapshot of the provider's scenario.
3. Resolve the mode. `fault_policy.resolve_mode` applies the `transports` filter, the `ports` filter and the `fail_first_n` window.
4. If the mode is `down`, answer 503 with no body. The request is not parsed.
5. Parse the request. This is the hook `parse_request`.
6. Merge the per-method override of the seven fault keys: `mode`, `latency_ms`, `error_probability`, `error_code`, `error_message`, `http_status` and `drop_at` (`_METHOD_OVERRIDE_KEYS`, `base.py:65-73`).
7. Choose one reply: a canned body, a fault (`fault_policy.ladder`), or the success content of the chain (`Chain.build_success`).
8. Attach the latency, the corruption and the pause to the reply.
9. Finish the history row: method, status, latency and request id.
10. Return a `ServeResult`. The socket adapter performs it.

JSON-RPC, REST and Tendermint RPC use this flow. A WebSocket frame that is not a subscribe frame uses it too (`server.py:587`).

### 4.2 The copies

| Place | Lines | What it repeats |
|---|---|---|
| `GrpcListener.plan` | `provider_simulator/listeners/grpc.py:76-187` | Steps 1 to 4 and 7 to 9, for every gRPC call. The file gives its reason at lines 3-10: a gRPC reply is a status or a protobuf message, and `ServeResult` describes an HTTP reply. |
| `_WsHandler._serve_subscription_frame` | `server.py:596-669` | Steps 1 to 4 and 6 to 9, for a subscribe frame and an unsubscribe frame. It runs step 6 before step 4: the per-method override comes before the `down` check (`server.py:613-630`). |
| `_WsHandler.do_GET` and `_refuse_upgrade` | `server.py:453-459` and `server.py:492-552` | Steps 2 to 4 and 7 for the request that opens a WebSocket, with its own history rows. |

### 4.3 Who writes a history row, and who asks the fault policy

Six places write a history row:

| Place | Lines | When |
|---|---|---|
| `Listener.serve` | `provider_simulator/listeners/base.py:158-260` | Every JSON-RPC, REST and Tendermint RPC call, and every WebSocket frame that is not a subscribe frame |
| `GrpcListener.plan` | `provider_simulator/listeners/grpc.py:77-88` | Every gRPC call |
| `_HttpListenerHandler._run` | `server.py:135` | The arrival, before the socket adapter reads the body. `serve` then finishes the same row. |
| `_WsHandler._refuse_upgrade` | `server.py:499-508` | A refused WebSocket upgrade |
| `_WsHandler._serve_subscription_frame` | `server.py:611-662` | A subscribe frame or an unsubscribe frame |
| `_WireSubscriptions.emit` | `server.py:406-415` | An event that the control API pushes with `POST /ws/emit` |

Three files decide the fault of a request: `base.py:167` and `:216`; `grpc.py:84` and `:197`; `server.py:456`, `:613` and `:628`.

Two more places call `fault_policy` and do not decide a request. `provider_simulator/port_gate.py:52` reads if a scenario closes a gRPC port. `provider_simulator/control_api.py:219` checks a scenario before it is stored. Steps 1, 2 and 3 do not change them. The proposal for a real down (section 14.5) changes both: it gives a gate to `http` and `ws` ports, and it removes one refusal of that check.

### 4.4 Where the copies disagree today

| # | Subject | `Listener.serve` | The copy | Label |
|---|---|---|---|---|
| 1 | `latency_ms`, when a `ports` or `transports` filter does not name the endpoint | Applies no latency (`base.py:168`) | gRPC applies the latency (`grpc.py:85`) | RAN, three times. RAN BY THE REVIEWER. |
| 2 | Per-method override of the fault keys | The seven keys of step 6 | A subscribe frame reads five: `mode`, `latency_ms`, `drop_at`, `error_code`, `error_message` (`server.py:623`). gRPC reads none. | READ |
| 3 | `corruption_mode` on a success reply | Applied (`base.py:246-248`) | Not applied to the reply of a subscribe frame (`server.py:666`) | READ |
| 4 | A canned `body` override for one method | Applied on JSON-RPC (`jsonrpc.py:77-84`) | Not applied to a subscribe frame | READ |
| 5 | The order of the `down` check and the per-method override | `down` first (`base.py:172-174`). A provider-wide `down` wins. | A subscribe frame merges the override first (`server.py:613-630`). The override wins. | READ. RAN BY THE REVIEWER. |
| 6 | `corruption_mode` on a `rate_limit` reply or an `error` reply | Applied (`base.py:243-248`) | gRPC does not apply it: each fault returns before the corruption code (`grpc.py:91-150`, `:153`) | RAN |
| 7 | The `ports` filter and the gRPC corruption faults | | The gRPC copy missed the filter once. `grpc.py:189-196` records it. | READ |

The reading for row 1, with `latency_ms: 500` and two endpoints on ports 40001 and 40002:

| Filter in the scenario | REST, through `Listener.serve` | gRPC, through `GrpcListener.plan` |
|---|---|---|
| `ports: [40001]` | 40001: 500 ms. 40002: 0 ms. | 40001: 500 ms. 40002: 500 ms. |
| `transports: ["ws"]` | 0 ms on both | 500 ms on both |

The `ports` case of row 1 cannot happen with today's topology. Only one provider serves one interface and one transport at two ports: `eth-failover-twoaddr-sim:1`, on JSON-RPC (RAN). No gRPC provider has two ports.

The reading for row 5, for one frame `eth_subscribe` (RAN BY THE REVIEWER): with `mode: down` and `responses.eth_subscribe.mode: success`, the subscribe code sends the reply and registers one subscription. `Listener.serve` gives no reply, and the row is `down`.

### 4.5 The request id today

| Interface | Where the id comes from | Lines | Through the router |
|---|---|---|---|
| `jsonrpc`, on `http` and `ws` | The `id` of the JSON-RPC body. A success row takes the id of the reply, and `EthChain` puts 1 there when the caller sent none. | `base.py:239` and `:309-310`; `chains/eth.py:102` | It passes. MEASURED BY 3800. Rule R45 of the automation repository depends on it. |
| `tendermintrpc`, POST | The `id` of the body | `tendermint.py:79` | NOT CHECKED |
| `tendermintrpc`, URL form | A counter of the simulator | `tendermint.py:64` | The caller cannot choose it. |
| `rest` | The header `X-Request-Id`, read with this exact letter case. With no such header: a counter of the simulator. | `rest.py:90`, on the plain dictionary that `server.py:141` makes | The router does not pass the header (READ in the router). |
| `grpc` | Nothing. The row has no request id. | `grpc.py:87-88`. The test `tests/test_simulator_grpc.py:546-551` asserts it. | |

Why the header is lost: the router passes a request header to a provider only if the chain spec declares that header (`HandleHeaders`, `protocol/chainlib/base_chain_parser.go:162-204`). One header passes with no declaration: `content-type`, on a REST method that carries a body (`protocol/chainlib/base_chain_parser.go:136-160`). For gRPC the same filtered list becomes the call metadata (`protocol/chainlib/grpc.go:778-787`).

Session 3800 measured a REST request with the header: the row held the counter. That reading agrees with the router code. It cannot prove the point alone, because a header in another letter case also gives the counter (RAN: `x-request-id` gives the counter).

What does pass:

- REST: the query string. The router adds it to the path that it sends (`protocol/chainlib/rest.go:365` and `:400`). MEASURED BY 3800: a request with `?pagination.key=PROBE90b3c994&x-request-id=PROBE90b3c994` got HTTP 200, and the reply echoed the value.
- gRPC: the fields of the request message. The router builds the message again from the caller's bytes and sends it (`protocol/chainlib/grpc.go:820-873`). NOT CHECKED on a router: both methods that the simulator serves have an empty request.

### 4.6 Per-method overrides today

A scenario can carry `responses`: an override for one method. The code reads it in three kinds of places:

| Place | Lines | What it reads |
|---|---|---|
| The request flow | `base.py:191-214` | The seven fault keys of step 6, and the canned `body` of JSON-RPC |
| The chains | `chains/eth.py:105-119`, `chains/lava.py:164-176`, `:236-251` and `:323-324`, and the BTC, LN and Solana chains | `error_stub`, `error`, `result`, `body`, and the status keys. A chain also reads the entry `default` when the method has no entry. |
| The copies | `grpc.py:123-150` and `server.py:619-626` | gRPC: `error_stub` and `error`, with the entry `default` (`grpc.py:125`). A subscribe frame: the five fault keys of row 2 of section 4.4. |

## 5. Goals

1. A test reads the history rows of one REST request and of one gRPC request with the call that it uses for JSON-RPC.
2. One request flow serves every request: a JSON-RPC, REST, Tendermint RPC or gRPC call, and every WebSocket frame. A rule about faults, latency, history rows or ids is written one time. The WebSocket upgrade is not a request. It stays a second, smaller decision (section 8.4).
3. Only code in `provider_simulator/listeners/` writes a history row or decides the fault of a request.
4. A new ability is added in one place for every interface, and a new gRPC method is added in two places (section 14).
5. No reply and no history row changes, except the changes that section 9 lists.

## 6. Not in this change

"Not in this change" means: this design does not edit it. Each thing below keeps working as it works today, and its tests must pass after each pull request (section 12).

- The router. Victoria's words, through session 3800: the router does not change.
- A GraphQL interface. It needs no step of this design (section 14.1). It is a different piece of work.
- The meaning of a per-method override. `body`, `result`, `error_stub` and the status keys keep their meaning on each interface.
- The rule for a provider-wide `down`: its row has no request id. A per-method `down` records the method and the id today (`base.py:217-227`), and that stays.
- The request id of the Tendermint RPC URL form.
- The cache simulator (`provider_simulator/cache_sim.py`, `listeners/cache_grpc.py`) and the RESP proxy (`provider_simulator/resp_proxy.py`). They are not provider endpoints.
- A new library, a framework, a second process, or a new name for a class that exists.

## 7. Constraints

These answers are for the group "Constraints" of the question list that Victoria chose.

| Question | Answer |
|---|---|
| Systems that the simulator works with | The router, which does not change. The automation repository, which reads `/history` through `tests/simulator/sim_control.py`. This design adds no port, no pool and no provider, so `config/values_sim.yml` does not change. |
| Technology in use | Python's standard-library servers (`http.server`, `socketserver`) and threads. `grpcio`, `grpcio-reflection` and `protobuf` for gRPC (`requirements.txt`). This design adds no library. |
| Where it runs | One process in one pod, with its state in memory (`k8s/deployment.yml:11` sets one replica). This design keeps that. |
| Time | No date is fixed for the test of MAG-3800 (the router bug: a wrong provider name gets HTTP 500). This is my assumption. Victoria saw it and did not correct it. |
| Debt that we accept | The differences between interfaces in section 9.3 stay. Each one needs its own decision. |
| Direction | More methods and more abilities: Victoria's comment in section 9.1. More interfaces: the router has a GraphQL interface since 2026-10-05 (smart-router pull request #480, a GraphQL interface for the Sui chain). |
| Who works on it | Victoria and her assistant sessions. Each pull request needs one approving review. The `shared-skill` of this repository records that rule from a reading of 2026-09-16. I did not read the ruleset. |
| How it is released | CI publishes the simulator image on each push to `main` (`.github/workflows/publish-image.yml`). The cluster must run the new image before a test can use a new feature. |

## 8. The design

### 8.1 The picture

Today:

```
socket adapter (server.py)           decision                                   content
----------------------------------   ----------------------------------------   -------------------
HTTP handler ----------------------> Listener.serve --------------------------> Chain.build_success
WebSocket, other frames -----------> Listener.serve --------------------------> Chain.build_success
WebSocket, upgrade ----------------> its own decision and rows, in server.py
WebSocket, subscribe frames -------> its own copy of the flow, in server.py
gRPC servicer ---------------------> GrpcListener.plan, a copy of the flow ---> Chain.build_success
```

After step 3:

```
socket adapter (server.py)           decision (provider_simulator/listeners)    content
----------------------------------   ----------------------------------------   -------------------
HTTP handler ----------+
gRPC adapter ----------+-----------> Listener.serve, the one request flow ----> Chain.build_success
WebSocket, each frame -+                  |
                                          +--> fault_policy and CallLog
WebSocket, upgrade ----------------> one method of the WebSocket listener ----> fault_policy and CallLog
```

Both decisions of the right picture are in `provider_simulator/listeners/`. No code outside that package calls `fault_policy` for a request or writes a history row.

What each part is responsible for after step 3:

| Part | Its one job |
|---|---|
| A socket adapter, in `server.py` | Own the socket. Turn the bytes into a `RawRequest`. Perform a `ServeResult`. It also answers four protocol cases itself, with no listener and no history row: OPTIONS on REST (`server.py:333-347`), a wrong WebSocket path and a bad upgrade request (`server.py:438-448`), and a WebSocket frame that is not JSON (`server.py:576-579`). |
| `Listener`, in `listeners/base.py` | The request flow: the order of the steps. |
| A `Listener` subclass, one for each interface | Read the request of that interface. Shape the replies of that interface. |
| `fault_policy` | Decide which fault applies. |
| A `Chain` | Build the success content. Hold the chain head. |
| `Provider`, `ScenarioConfig`, `CallLog` | Hold the state of one provider. |

The base class owns the order and a subclass fills the steps. This is the Template Method pattern. The code has it today for JSON-RPC, REST and Tendermint RPC. This design brings in gRPC and the WebSocket subscribe frames. It does not add a layer.

### 8.2 The request id (step 1, ADR-002)

**The rule.** The request id of a call is a value that the caller chooses and that the interface can carry to the provider through the router. Each interface names one place for it.

| Interface | Where the caller puts the id | Change |
|---|---|---|
| `jsonrpc` | The `id` of the body | None |
| `tendermintrpc`, POST | The `id` of the body | None |
| `rest` | The query parameter `request_id`. A caller that talks to the simulator directly can still use the header `X-Request-Id`. | New |
| `grpc` | The field of the request message that the method names. For `AllBalances` it is `address`. | New |

**One field and one filter.** The history row field stays `request_id`. The filter stays `GET /history?request_id=<id>&pool=<pool>`. `_filter_history` compares the two values as text (`provider_simulator/control_api.py:906-908`).

**An id must not be a plain number.** The counter of the simulator gives the numbers 1, 2, 3 to the REST calls that carry no id (`rest.py:52-62`), and it counts the router's own polls. The filter compares text. So the caller id `1` and the counter value 1 are the same for the filter (RAN: the filter for `request_id=1` matches both rows). A caller must send an id that is not a plain number, for example a word and a random part.

**REST.** For the five verbs that reach the listener, GET, POST, PUT, DELETE and HEAD, `RestListener.parse_request` reads the id in this order:

1. The query parameter `request_id`. With more than one value, the first value.
2. The header `X-Request-Id`, in any letter case.
3. The counter of the simulator, as today.

An empty value is no id, on REST and on gRPC. OPTIONS does not reach the listener and writes no row.

The row's `method` stays the verb and the route template. No REST route uses a parameter with this name: a search for `request_id` in `stubs_rest.py` and in `provider_simulator/chains/lava.py` finds nothing (RAN).

**gRPC.** The simulator serves one more method: `AllBalances` of the service `cosmos.bank.v1beta1.Query`.

- Its request has the field `address`. The caller chooses the address, and the address has no meaning for the router's choice of a provider.
- The compiled messages are in the repository: `cosmos_pb2/cosmos/bank/v1beta1/query_pb2.py` (RAN: the request has the fields `address`, `pagination` and `resolve_denom`).
- The reply has the content of the REST route `/cosmos/bank/v1beta1/balances/{address}`: one coin, `ulava`, amount `1000000`.
- The history row has the method `AllBalances` and the address as its request id.
- Reflection must list the service. The router reads the description of a method from the provider (`protocol/chainlib/grpc.go:790-805`).
- `GetLatestBlock` and `GetNodeInfo` have an empty request. Their rows keep no request id, and the test at `tests/test_simulator_grpc.py:546-551` stays true.
- The other methods of the bank service answer `UNIMPLEMENTED`.

**The gRPC key is the bare method name.** The history row, the key of `responses` and `GET /history?method=` use `GetLatestBlock`, not `cosmos.base.tendermint.v1beta1.Service/GetLatestBlock`. The bare name is not unique: `Params` is a method of three compiled services, `cosmos.auth.v1beta1.Query`, `cosmos.bank.v1beta1.Query` and `cosmos.staking.v1beta1.Query` (RAN). This design keeps the bare name, and the simulator refuses to start with two served methods of the same name. Question Q12 asks if the key must be the full name.

**What does not change.**

- The row of a provider-wide `down` has no request id. A dead node does not read the request. A test that sets `down` counts those calls with `/stats`, as rule R45 of the automation repository says today.
- A row gets its request id before the provider waits. `Listener.serve` finishes the row (`base.py:254-260`), and the socket adapter waits after that (`server.py:146-157`). So a read by request id finds a request that a provider holds with `latency_ms` or `hang`. I read this, and session 3800 read it again.

### 8.3 gRPC in the request flow (step 2, ADR-001)

`GrpcListener` becomes a subclass of `Listener`. `GrpcListener.plan` and `GrpcPlan` are deleted. The gRPC listener fills hooks, and it has no order of steps of its own.

**The reply.** `ServeResult.body` is typed `object` today. For gRPC it holds one of two things:

- a status: a gRPC status code and a text. This is what `GrpcPlan(action="abort")` holds today.
- a reply message, built from the data of the chain.

The texts and the codes stay as they are: `UNAVAILABLE` and "provider down", `CANCELLED` and "hang timeout" after 30 seconds, `UNAVAILABLE` and "connection dropped", `RESOURCE_EXHAUSTED` and "Too many requests", and the code that `error_message` or `error_code` names.

**The method table.** `listeners/grpc.py` gets one table with one row for each served method:

| Method | Service | Field that holds the request id | Reply |
|---|---|---|---|
| `GetLatestBlock` | `cosmos.base.tendermint.v1beta1.Service` | none | Today's `build_latest_block` (`server.py:1135-1146`) |
| `GetNodeInfo` | `cosmos.base.tendermint.v1beta1.Service` | none | Today's `build_node_info` (`server.py:1148-1157`) |
| `AllBalances` | `cosmos.bank.v1beta1.Query` | `address` | New in step 1 |

A new gRPC method is then two changes: one row of this table, and its content in the chain.

**Who owns what.**

| File | It owns |
|---|---|
| `listeners/grpc.py` | Every protobuf class: the servicer classes, their registration with the server and with reflection, the method table and the functions that build a reply message. The registration is the one that the generated code does today (`add_ServiceServicer_to_server`). Only its place changes. |
| `server.py` | The gRPC server, the `PortGate` loop (`server.py:1199-1261`) and one function that performs a `ServeResult` on a call: wait for the latency, or wait 30 seconds for a hang, then send the status or return the reply message. It names no protobuf class. |

Three things stay exactly as they are, because the generated registration stays:

- A request message that does not parse is refused by `grpcio` before the listener sees it. It writes no history row. A client reads the status `UNKNOWN` (RAN on the bank service).
- A method of a registered service that the simulator does not serve answers `UNIMPLEMENTED`. The generated base class raises "Method not implemented!" (`cosmos_pb2/cosmos/base/tendermint/v1beta1/query_pb2_grpc.py:83-84`), and a client reads the text `Unexpected <class 'NotImplementedError'>: Method not implemented!` (RAN on the bank service). Today these are `GetSyncing`, `GetBlockByHeight`, `GetLatestValidatorSet`, `GetValidatorSetByHeight` and `ABCIQuery`.
- `mode="port_closed"` works as it does today, and the thread keeps its name, `grpc-<pool>:<pid>` (`server.py:1456`). A test finds the thread by that name (`tests/test_simulator_grpc_port_closed.py:550`).

**The per-method error of gRPC.** `error_stub` and `error` move from `GrpcListener.plan` to `LavaChain._build_grpc`, with the entry `default`. REST and Tendermint RPC have theirs in the same class (`chains/lava.py:164-176` and `:236-251`). The chain returns the error as plain data, and the listener turns it into a status code. The chain still imports nothing of `grpcio`.

**New hooks of `Listener`.** gRPC differs from the HTTP interfaces in four ways. Each one becomes a hook with a default that keeps `JsonRpcListener`, `RestListener` and `TendermintListener` as they are. The names are proposals.

| Hook | What it answers | Default | gRPC |
|---|---|---|---|
| `early_identity` | What does a provider-wide `down` row record about the call, before the request is parsed? | Method `*`, no request id | The method name, no request id |
| `build_down` | What is the reply of a `down` provider? | 503 with no body | The status `UNAVAILABLE` |
| `unpaid_latency` | Which `latency_ms` does a row record when the provider did not wait (`down`, `hang`)? | The configured value | 0 |
| `corrupt` | How does this interface corrupt a reply? Can the corruption change the row's status? | Mark the reply, and the socket adapter corrupts the bytes. The status stays. | It acts on a reply message only. `missing_field` clears a field. `wrong_type`, `invalid_proto`, `empty_response`, `truncated` and `null_body` turn the message into a status, and the row gets the label `error`. It leaves a status as it is, and it ignores `invalid_json`. |

`build_down` does not make a provider unreachable. It keeps the two replies that the mode `down` gives today. In the mode `down` a provider still accepts the connection and answers. Section 14.5 says what a real down is. Since Victoria's decision of 2026-10-07 this design adds one as a step of its own: `mode="port_closed"` on every transport. The hook `build_down` and the mode `down` do not change with it.

### 8.4 WebSocket in the request flow, and one writer of history rows (step 3, ADR-001)

A WebSocket endpoint gets its own listener class in `listeners/ws.py`. It is a subclass of `JsonRpcListener`. The proposed name is `JsonRpcWsListener`.

| Today, in `server.py` | After step 3 |
|---|---|
| `_serve_subscription_frame` copies the flow (`:596-669`) | The adapter gives every text frame that is JSON to `listener.serve`. The listener answers a subscribe frame and an unsubscribe frame from `WsSubscriptions`. Every other frame goes to the chain, as today. |
| `do_GET` decides the fault of the upgrade and `_refuse_upgrade` writes the rows (`:453-552`) | One method of the listener decides and writes the row. The adapter performs the result: complete the handshake, refuse it, hang, or drop. |
| `_WireSubscriptions.emit` writes the row of a pushed event (`:406-415`) | `listeners/ws.py` writes the row. `server.py` keeps the frame bytes. |
| `_HttpListenerHandler._run` records the arrival (`:135`) | The adapter calls one method of the listener, which records the arrival. The row is still written before the body is read. |

**The upgrade stays a second decision.** It is not a request, so it does not go through `Listener.serve`. Its code moves into the package, and its five rules stay (section 9.3, rows 8 to 12).

**A subscribe frame under a provider-wide `down`.** Today the subscribe code merges the per-method override before the `down` check, and `Listener.serve` checks `down` first (section 4.4, row 5). This design proposes the order of `Listener.serve` for a subscribe frame: the frame then behaves as every other WebSocket frame. That is a change on purpose (section 9.2, row 6). Question Q11 asks Victoria to choose. The other choice keeps today's order with one more hook.

This step adds one hook to `Listener` and one method. The names are proposals.

| New | What it answers | Default | WebSocket listener |
|---|---|---|---|
| The hook `build_content` | Where does the success content come from? | The chain (`base.py:233-236`) | `WsSubscriptions` for a subscribe frame and an unsubscribe frame. The chain for every other frame. |
| The method `arrive` | How does an adapter record the arrival before it reads the body? | It writes the `in_flight` row. | The same |

The adapter passes the connection in the request, in a new optional field of `RawRequest`. The listener needs it to register a subscription on the right connection.

**The guard.** A new test reads `server.py` and fails if the file imports `fault_policy` or calls `log.record_arrival`, `log.finalize` or `log.push`. The test is what keeps goal 3 true after this work.

### 8.5 Per-method overrides after step 3

The meaning of each key does not change. The places do:

| Kind of key | Read by | After step 3 |
|---|---|---|
| The seven fault keys of step 6, and the canned `body` of JSON-RPC | The request flow | One place. A subscribe frame uses it too. |
| `error_stub`, `error`, `result`, `body`, the status keys and the entry `default` | The chains | The gRPC error keys join them. |

gRPC and Tendermint RPC still do not merge the fault keys for one method. Their hook `method_key` returns `None`, as `TendermintListener.method_key` does today (`tendermint.py:83-86`).

## 9. What changes for a caller

### 9.1 New abilities

| Step | Change |
|---|---|
| 1 | REST: the row records the query parameter `request_id`. |
| 1 | gRPC: the simulator serves `AllBalances`, and its row records the address as the request id. |

> **Victoria's comment, 2026-10-07:** "we need to support more methods"
>
> "we need to think about each interface ability we will nee d to implmenet in future so it will be easily applied /changed/ added - lije todau with request id"
>
> **Reply:** Section 14 is new for this. Section 14.2 says how to add a method on each interface. Section 14.3 says how to add an ability. Section 14.4 is a table of the abilities of each interface: each "no" in it is an ability that an interface can get later. Question Q14 asks which abilities you need next.

### 9.2 Behaviour that changes on purpose

| # | Step | Today | After | Why |
|---|---|---|---|---|
| 1 | 1 | The header `X-Request-Id` is read with this exact letter case. `x-request-id` gives the counter. | The header is read in any letter case. | A header name has no letter case in HTTP. Step 1 edits this line (`rest.py:90`). |
| 2 | 2b | `latency_ms` on gRPC ignores the `ports` filter and the `transports` filter. A scenario whose filter does not name the gRPC endpoint still delays that endpoint. | That one case changes: a scenario whose filter does not name the gRPC endpoint does not delay it, as on JSON-RPC, REST and Tendermint RPC. A scenario with no filter, or with a filter that names the endpoint, delays gRPC exactly as today. | Row 1 of section 4.4. The copy is deleted, and the rule of the flow applies. |
| 3 | 3b | A `corruption_mode` is ignored on the reply of a subscribe frame. | It applies. The subscription is registered, and the caller cannot read its id from the corrupted reply. `GET /ws/subscriptions` shows it until the connection closes. | Row 3 of section 4.4. |
| 4 | 3b | A per-method `error_probability` is ignored on a subscribe frame. | It applies. | Row 2 of section 4.4. |
| 5 | 3b | A canned `body` for a subscribe method, for example `eth_subscribe`, is ignored. | It applies: the caller gets the canned body, and no subscription is registered. | Row 4 of section 4.4. |
| 6 | 3b | For a subscribe frame, a per-method override is applied before a provider-wide `down`. With `mode: down` and `responses.eth_subscribe.mode: success` the subscribe succeeds. The `down` row of a subscribe frame records the method and the request id. | `down` comes first, as for every other WebSocket frame: no reply, the connection closes, and the row records `*` and no request id. | Row 5 of section 4.4. This row needs Victoria's decision: Q11. |

Rows 1, 3, 4 and 5 make a setting work that is ignored today. Rows 2 and 6 replace a behaviour of today.

**The tests that use latency on gRPC keep working** (row 2). They name the gRPC endpoint or send no filter:

- The simulator test `test_latency_ms_delays_reply` sends `{"latency_ms": 300}` with no filter (`tests/test_simulator_grpc.py:111-113` and `:422-424`).
- The automation client sends the filter `["http", "http2"]` for every mode except `down`, and `http2` is the transport of a gRPC endpoint. For `down` it sends no filter (`_resolve_transports`, `tests/simulator/sim_control.py:114-119` of the automation repository).
- A search of the automation tests on 2026-10-07 found no gRPC scenario with its own `transports` or `ports` filter.

A per-method `http_status` also reaches a subscribe frame after step 3b. It changes nothing there: a WebSocket frame has no HTTP status (`server.py:372` and `:671-697`).

No test pins row 6 in one direction or the other (RAN BY THE REVIEWER). The one per-method override for a subscribe method in `tests/` is a per-method `down` under a provider-wide `success` (`tests/test_simulator_ws.py:978`). Both orders give the same result for it.

### 9.3 Differences between interfaces that stay

Today the interfaces behave differently in the twelve ways below. Example: for a provider in the mode `down`, a gRPC history row records the method name, and a REST history row records `*`.

This design keeps each of the twelve exactly as it is today. So steps 2 and 3 change nothing that a test can read here.

To make one of them the same on every interface is a different change. Example: make the gRPC `down` row record `*` too. Such a change alters a reply or a history row, so a test in the automation repository can break. That is why it is not part of steps 2 and 3. Victoria decides each one alone, and each one gets its own pull request. Question Q2 asks if one of them must be done now.

**gRPC against JSON-RPC, REST and Tendermint RPC**

| # | Difference | Kept by |
|---|---|---|
| 1 | A provider-wide `down` row on gRPC records the method. On JSON-RPC, REST and Tendermint RPC it records `*`. The test `tests/test_listener_grpc.py:46-54` asserts it. | `early_identity` |
| 2 | A `down` row and a `hang` row on gRPC record `latency_ms` 0. On JSON-RPC, REST and Tendermint RPC they record the configured value, and the provider did not wait that time. | `unpaid_latency` |
| 3 | gRPC labels a reply message that a corruption turned into a status as `error`. JSON-RPC, REST and Tendermint RPC label a corrupted reply `success`. | `corrupt` |
| 4 | On gRPC a corruption never touches a status: a `rate_limit` reply, an `error` reply or a per-method error. On JSON-RPC, REST and Tendermint RPC it also corrupts a `rate_limit` reply and an `error` reply (RAN: gRPC keeps `RESOURCE_EXHAUSTED`, and REST sends a corrupted 429). | `corrupt` |
| 5 | On gRPC the corruption mode `invalid_json` does nothing. | `corrupt` |
| 6 | gRPC and Tendermint RPC do not merge the fault keys for one method. | `method_key` |

**Between the chains**

| # | Difference | Kept by |
|---|---|---|
| 7 | REST prefers `http_status` to `status`, and Tendermint RPC prefers `status` (`chains/lava.py:110-118`). The canned reply is `body` on REST and Tendermint RPC, and `result` on gRPC. | The chains |

**The WebSocket upgrade against a request**

| # | Difference | Kept by |
|---|---|---|
| 8 | The upgrade applies no `latency_ms`, and its rows record 0. | The upgrade method |
| 9 | The upgrade applies no corruption and no per-method override. | The upgrade method |
| 10 | A refused upgrade writes one complete row. An upgrade that succeeds writes no row. | The upgrade method |
| 11 | The upgrade has its own reply bodies, for example `{"error": "provider down"}`. | The upgrade method |
| 12 | Each upgrade uses one count of the `fail_first_n` window (`tests/test_simulator_ws.py:1354-1397` asserts it). | The upgrade method |

### 9.4 If a difference is made uniform: which tests of today fail

Victoria, 2026-10-07, about section 9.3: "but it should be changed ,will the old test work?". This table is the answer as far as a READ gives it. Two read-only helpers did the reading on 2026-10-07. Their reports are in the evidence folder: `helper-simulator-tests-report.md` for the simulator's tests, and `helper-automation-grpc-ws-rest-report.md` for the automation tests that touch gRPC, WebSocket and REST providers. I opened these cited lines myself: `tests/test_listener_grpc.py:46-54`, `tests/test_listener_jsonrpc.py:40-47`, `tests/test_null_body_wire.py:46-54`, `tests/test_simulator_ws.py:1354-1397`, and `websocket/test_ws_upgrade_rate_limit_holdoff.py:656-659` of the automation repository. The other lines are the helpers' reading. No uniform behaviour exists in code, so nothing here is a run.

| Row of 9.3 | Simulator tests that pin today's behaviour | Automation tests that pin it | If the row is made uniform |
|---|---|---|---|
| 1, the method of a provider-wide `down` row | gRPC side: `tests/test_listener_grpc.py:46-54` ("GetLatestBlock"). JSON-RPC side: `tests/test_listener_jsonrpc.py:40-47` and `tests/test_simulator.py:1070-1074` (`*`). REST and Tendermint RPC: none. | None found. One gRPC test reads `down` rows with no method filter. | An `http` listener answers `down` before it parses the body, so it cannot know the method. Uniform means that gRPC records `*`. Then one simulator test changes: `tests/test_listener_grpc.py:46`. |
| 2, `latency_ms` of a `down` row and a `hang` row | None | None found | No test of today fails. |
| 3, the status label of a corrupted reply | None | None found | No test of today fails. |
| 4, a corruption on a `rate_limit` reply or an `error` reply | JSON-RPC side: `tests/test_null_body_wire.py:46-54` and `tests/test_simulator_ws.py:821-847`. gRPC, REST, Tendermint RPC: none. | None found | The side to change is gRPC, and no test pins it. A status has no body to corrupt, so the uniform behaviour must be defined first. |
| 5, `invalid_json` on gRPC | None | None found | No test of today fails. |
| 6, the merge of fault keys for one method on gRPC and Tendermint RPC | None | None found | No test of today fails. |
| 7, `http_status` or `status` first; `body` or `result` | REST: `tests/test_chains_lava.py:113`, `tests/test_simulator_rest.py:552` and `:574`. Tendermint RPC: `tests/test_chains_lava.py:227`. gRPC: `tests/test_chains_lava.py:257`, `tests/test_simulator_grpc_port_closed.py:518`. | Cache tests set `responses.<method>.result` and `error_stub` | The tests of the side that changes fail. This row is the meaning of the override keys, which question Q5 leaves alone. |
| 8, the upgrade applies no `latency_ms` | None | None found | No test of today fails. |
| 9, the upgrade applies no corruption and no per-method override | No direct assertion. Two test classes of `tests/test_simulator_ws.py` (`TestCorruptionModes` at line 620, `TestWsPerMethodFaultOverrides` at line 931) set the scenario before the handshake and need the upgrade to succeed. | None found | Those two classes fail if the upgrade applies the corruption or the override. |
| 10, a refused upgrade writes one row; a successful upgrade writes none | None | `websocket/test_ws_upgrade_rate_limit_holdoff.py:656-659` requires a row with the method `ws_upgrade` and the status `rate_limit` | That automation test fails if a refused upgrade stops writing its row. A row for a successful upgrade breaks no test that was found. |
| 11, the upgrade's own reply bodies | Status lines only: `tests/test_simulator_ws.py:569`, `:579`, `:592` | The same automation test reads "429" and "too many requests" in the router's frame | A change of a body breaks no simulator test. The automation test needs the status 429. |
| 12, each upgrade uses one count of `fail_first_n` | `tests/test_simulator_ws.py:1354-1397` | None found | That simulator test fails if an upgrade uses no count. |

In short: six rows have no test on any side (2, 3, 5, 6, 8, and 11 for the bodies), so a uniform behaviour there breaks no test of today, and it also has no test that proves it. Six rows have a test that pins today's behaviour (1, 4, 7, 9, 10, 12). "None found" for the automation side rests on a helper's search of `tests/simulator/` and `tests/infrastructure/unit/`. The proof for each row is a run of both suites with the uniform behaviour built.

## 10. The three steps

Each pull request gets its own implementation plan. A pull request can be reverted while it is the newest one that merged. After a later one merged, the later one is reverted first.

### Step 1: a request id on REST and gRPC

| | |
|---|---|
| Scope | Section 8.2 |
| Code | `listeners/rest.py`: read the id from the query, and the header in any letter case. `listeners/grpc.py`: `plan` receives the request message and records the id. `server.py`: register the bank service and pass the request message on. `chains/lava.py`: the content of `AllBalances`. |
| Documents | `CONTEXT.md`: the entry "Request id". `docs/using_the_simulator.md`, `docs/using_grpc.md` and `docs/curl_reference.md` (line 116 says that the filter is the JSON-RPC `id`): the rule and the new method. |
| Tests | Section 12 |
| Before the merge | One measurement on the local k3d cluster with the branch build: send one `AllBalances` request through `lava-sim-grpc-router` and read the row. Section 11 says why. |
| Done when | The simulator tests pass in CI. The measurement shows the address in the row. The author of the tests of MAG-3800 has the field name and the filter. |
| Code that step 2b replaces | The servicer class of the bank service and the change to `plan`, about 15 lines. Question Q13 offers another order that replaces nothing. |

### Step 2: gRPC uses `Listener.serve`

| | Pull request 2a: pin | Pull request 2b: move |
|---|---|---|
| Scope | Tests only. They record what gRPC does today. | Section 8.3 |
| Code | None | `listeners/base.py`: the four hooks `early_identity`, `build_down`, `unpaid_latency` and `corrupt`, with defaults. `listeners/grpc.py`: the listener, the method table and every protobuf class. `server.py`: the gRPC adapter performs a `ServeResult`. `chains/lava.py`: the per-method error of gRPC. |
| Documents | None | `CLAUDE.md:44`, `server.py:13` and `:1101`, and `provider_simulator/control_api.py:35` name `GrpcListener.plan`. |
| Done when | The new tests pass on `main` with no code change. | The socket tests of 2a pass with no edit. The grid test of 2a moves from `plan` to `serve`, and no expected value changes. `GrpcListener.serve is Listener.serve`. `GrpcPlan` does not exist. |

### Step 3: WebSocket uses `Listener.serve`, and one package writes history rows

| | Pull request 3a: pin | Pull request 3b: move |
|---|---|---|
| Scope | Tests only. They record the WebSocket rows of today. | Section 8.4 |
| Code | None | `listeners/base.py`: the hook `build_content` and the method `arrive`. `listeners/ws.py`: the WebSocket listener. `server.py`: the WebSocket adapter loses its decisions and its rows. |
| Documents | None | The paragraph about WebSocket in `CLAUDE.md` |
| Done when | The new tests pass on `main` with no code change. | The tests of 3a pass. The only edits to them are the rows of section 9.2 that Victoria accepted. `tests/test_simulator_ws.py`, `tests/test_ws_subscriptions.py` and `tests/test_ws_protocol.py` pass. The guard test passes. |

With the proposal of Q11, step 3 uses no hook of step 2. So step 2 and step 3 can merge in either order.

### 10.5 Test adjustments, decided by Victoria on 2026-10-07

Her words: "if there are test that can be easily adjusted we can do it", and then "we will do it, add it ti the plan". Section 9.4 and section 14.5 have the facts behind each part.

| Part | Repository | What changes | When |
|---|---|---|---|
| A | automation | Twelve test files say "refuse the connection" where the provider answers HTTP 503: the nine files of the table of section 14.5 other than the classifier file, and `test_primary_backup_failover.py`, `test_backup_tier_failover.py` and `test_router_retry_failover.py`. Correct the words in them, and in the three places that set no `down` (section 14.5). No assertion changes: every one of them stays on `down`. The session `worker` verifies the full list with a wider search before it edits. | Any time. It does not depend on the simulator. |
| B | automation | `test_router_retry_error_classifier_tiers.py`: the two tests with the label "Priority 0 — DetectConnectionError (TCP refused) … Internal" get the label of the rule that they run on: HTTP 503, `NODE_SERVICE_UNAVAILABLE`, `External`. | Any time. |
| B2 | automation | Two new tests for the rule that those labels named: an `http` provider that refuses the connection, with `mode="port_closed"`. Expected class: `PROTOCOL_CONNECTION_REFUSED`. Their count of `down` rows does not apply, because a closed port stores no row. | After the real down of section 14.5 is in the simulator. |
| C | simulator | Difference 1 of section 9.3 becomes uniform: a provider-wide `down` row on gRPC records `*`. One test changes one expected value: `tests/test_listener_grpc.py:46-54`. | A pull request of its own, 2c, after 2b. |
| D | simulator | Differences 2, 5 and 6 of section 9.3 become uniform. No simulator test pins them today, so each one gets a new test. Their directions, after the search below: **2**: every interface records `latency_ms` 0 on a provider-wide `down` row and on a `hang` row, because the provider did not wait; the `http` listeners change, and the hook `unpaid_latency` is not needed. **5**: the control API refuses `corruption_mode="invalid_json"` for a provider that has only gRPC endpoints, by the rule that a field which cannot apply is refused (`add-simulator-entity/SKILL.md:260-266`). **6**: gRPC and Tendermint RPC merge the fault keys of one method, as JSON-RPC and REST do. | Pull request 2c. |
| E | none | Differences 3, 4, 7, 9, 10, 11 and 12 stay, for the reasons in section 9.4. | |

Why 2c is its own pull request: 2b moves code, and its proof is that the tests of 2a pass with the same expected values. A change of behaviour in 2b removes that proof. In 2c each uniform behaviour shows as a named change of an expected value.

**The search of all automation tests for the `http` side** (Victoria, 2026-10-07: "so search it"). A read-only helper read every test under `tests/` of the automation repository at commit `58e8953`. Its report is `helper-http-side-of-the-differences-report.md` in the evidence folder. Nothing was run. What it gives for the directions:

- Difference 1. No test compares a method with `*`. If an `http` `down` row got the real method and no request id, no test fails. If it also got the request id, two asserts fail: `test_router_retry_error_classifier_tiers.py:738` and `:982`, because one attempt is then counted two times. An `http` listener answers `down` before it parses the body, so it does not know the method of a JSON-RPC request. So part C keeps its direction: gRPC records `*`.
- Difference 2. Two tests read `latency_ms` of a row, both of a `success` row. No test sets `latency_ms` with `down` or with `hang`. So the direction of part D breaks no test.
- Difference 3. No test reads the status of a corrupted row, so it could change with no failing test. It stays all the same: the two labels describe two different results for the caller, an error status on gRPC and a 200 with a broken body on the `http` listeners.
- Difference 6. One test needs the merge on JSON-RPC, and I opened its lines: `chaintracker/test_prober_health_ignores_relay_only_failures.py:467-476` sets `responses={"eth_call": {"mode": "drop_connection", "drop_at": "after_headers"}}`. So the merge cannot be removed from JSON-RPC, and part D adds it to gRPC and Tendermint RPC.
- Difference 7. Two tests of `cross_validation_enhancements/test_cv_answer_comparison.py` (lines 388-391 and 618-619) set `body` and `status` for a JSON-RPC method. They fail if JSON-RPC read only `result`. So difference 7 stays.

With 2c the simulator work has six pull requests in three steps (1, 2a, 2b, 2c, 3a, 3b), and the real down of section 14.5 as a step of its own.

## 11. Risks and assumptions

These answers are for the group "Risk Assessment" of the question list that Victoria chose.

| Question | Step 1 | Step 2 | Step 3 |
|---|---|---|---|
| What breaks if it fails | REST and gRPC history rows | Every gRPC reply and gRPC row: the 12 gRPC endpoints | WebSocket upgrades and subscriptions: the 9 `ws` endpoints. The arrival row of every HTTP call: the 135 `http` endpoints. |
| How we notice | The simulator tests in CI. Then the k3d measurement. | The tests of 2a, in CI. Then the automation tests on the k3d cluster with the branch build. | The tests of 3a and the guard test, in CI. Then the automation tests on the k3d cluster. |
| Rollback | Revert the pull request, while it is the newest merged one | Revert 2b. 2a can stay: it holds tests only. | Revert 3b. 3a can stay. |
| If the step cannot be finished | The tests of MAG-3800 keep the time window and the method, as `rows_after` does today in the automation repository. | gRPC keeps its copy. Rows 1 and 6 of section 4.4 can be fixed where they are. | WebSocket keeps its two places. Rows 2, 3 and 4 of section 4.4 can be fixed where they are. |
| Cost of being wrong | The gRPC half of the step: one method that no test uses | Two pull requests of work. The tests of 2a keep their value. | Two pull requests of work. The tests of 3a keep their value. |

**Assumptions.**

| # | Assumption | What we know | What proves it |
|---|---|---|---|
| A1 | The router sends the REST query string to the provider. | READ in the router. MEASURED BY 3800, one reading, with the parameter `pagination.key`. RAN by me on 2026-10-07 through `lava-sim-rest-router` (router build `v1.5.8-85-g4369814`, simulator `e80acbf`): three requests to `cosmos/bank/v1beta1/balances/<address>` with `request_id=<text>` next to `pagination.key=<text>`, in both orders. Each one got HTTP 200 and the right `inbound_key`. The three requests were: `pagination.key` alone; `request_id` then `pagination.key`; `pagination.key` then `request_id`. That run shows one thing: the known parameter `pagination.key` arrives when the unknown parameter `request_id` is next to it, on one route, with GET, on one router. It does not show that `request_id` itself arrives. The run of 22:15 in the next cell shows that. | PROVEN BY A RUN on 2026-10-07 at 22:15 (19:15 UTC), with the probe image in the cluster. Two requests with `?request_id=A1-design-2215-a` and `…-b` went through `lava-sim-rest-router`. Each got HTTP 200. `GET /history?pool=lava-sim-rest&request_id=<id>` returned `"count": 1` for each id: one row with that `request_id`, the method `GET /cosmos/bank/v1beta1/balances/{address}`, and the provider that the reply named (`LavaRestPrimaryProvider1`, then `LavaRestPrimaryProvider2`). The control request with no `request_id` got the counter id 19. |
| A2 | The router sends the `address` field of `AllBalances` to the provider. | READ in the router, in three places. The listener keeps the bytes of the request and gives them to `SendRelay`. `ParseMsg` stores them as `Msg`. `SendNodeMsg` builds the message again from them and sends it (`protocol/chainlib/grpc.go`). Nobody measured it. It cannot be tried before the simulator serves the method. READ in `protocol/chainlib/grpc.go:790-805`: the router builds the message from a descriptor that it reads from the provider's gRPC reflection. The run of row A3 gave an error text that names reflection as the source. So the field arrives only if reflection finds the symbol, and that needs the bank module loaded in the server (row A3). | PROVEN BY A RUN on 2026-10-07 at 22:15 (19:15 UTC), with the probe image in the cluster: the simulator served `AllBalances`, listed the bank service in reflection, and stored `address` on the row. Two calls through `lava-sim-grpc-router` with the addresses `lava1-A2-design-2216-a` and `…-b`. Each got the status OK and one coin, `ulava` `1000000`, with `lava-user-request-type: cosmos.bank.v1beta1.Query/AllBalances`. `GET /history?pool=lava-sim-grpc&request_id=<address>` returned `"count": 1` for each address: one row with that `request_id`, the method `AllBalances`, and the provider that the reply named (`LavaGrpcPrimaryProvider3`, then `LavaGrpcPrimaryProvider1`). |
| A3 | The chain spec that the k3d gRPC router loads lists `cosmos.bank.v1beta1.Query/AllBalances` for gRPC. | READ by a sub-agent on 2026-10-06: the router `lava-sim-grpc-router` loads its spec from GitHub when its pod starts (`--use-static-spec=https://github.com/magma-devs/lava-specs/tree/main/`), and that spec lists the method through `cosmossdk.json`. VERIFIED BY A RUN on 2026-10-07: one `AllBalances` call through `lava-sim-grpc-router` (port 30004). The router accepted the method: the reply carries `lava-user-request-type: cosmos.bank.v1beta1.Query/AllBalances`, names six providers in `lava-provider-address` and has `lava-retries: 5`. The call then failed with the status `UNKNOWN` and this text: `failed to get method descriptor ErrMsg: failed to find service descriptor ErrMsg: Symbol not found: cosmos.bank.v1beta1.Query {service:cosmos.bank.v1beta1.Query,descriptor-source:reflection,reflection-timeout:5s}`. The control call `GetLatestBlock` on the same channel answered OK. | The proof that the spec lists the method is the header: `lava-user-request-type` holds the method name with no `Default-` in front. A method that no spec lists is accepted too, through a default entry, and its header starts with `Default-` (`protocol/chainlib/base_chain_parser.go:553` and `:590-591` at build `4369814`, read by `reviewer-2`). PROVEN BY A RUN WITH A CONTROL on 2026-10-07 at 22:27, by the session `worker`, two readings each, saved in `control-runs/1a-a3-probe-through-router.txt` and `control-runs/1a-unlisted-method-through-router.txt` of the evidence folder. `AllBalances` gave `lava-user-request-type: cosmos.bank.v1beta1.Query/AllBalances`, with no `Default-`. The method `/cosmos.bank.v1beta1.Query/NoSuchMethodProbe`, which no spec lists, gave `lava-user-request-type: Default-cosmos.bank.v1beta1.Query/NoSuchMethodProbe`. The status, the error text, `lava-retries: 5` and the six provider names were the same for both, so only this header separates the two cases. On a failed call the value is in the trailers; on a successful call it is in the headers. What the router needs from the provider: it asks the provider's reflection for the SYMBOL `cosmos.bank.v1beta1.Query`. RAN on 2026-10-07 (`reflection_symbol_probe.py`, three cases, `grpcio` 1.81.1): the symbol is found when the bank module is loaded in the server process, also when its name is not in the list given to `reflection.enable_server_reflection`; it is not found when the module is not loaded. The list feeds "list services" only. So the requirement for step 1 is that the server imports the bank stubs and registers the servicer. The test for it must ask reflection for the symbol. This holds for a router whose descriptor source is `reflection`, which the error text of this run shows. |
| A4 | The generated gRPC registration works for the bank service, from another file than `server.py`. | RAN, twice, on macOS, in memory: a servicer class for `cosmos.bank.v1beta1.Query` with the one method `AllBalances`, registered with `add_QueryServicer_to_server`, answers, and it reads the address. | The tests of 2a, in CI, on Linux. |
| A5 | No automation test depends on a behaviour that section 9.2 changes. | READ: by default the automation client names the `http` and `http2` transports. I searched the automation tests on 2026-10-07, with one search for each row: no gRPC scenario has its own `transports` or `ports` filter, no WebSocket test sets a per-method override for a subscribe method or a corruption, and the code writes the header as `X-Request-Id`. A search is weaker than a run. | A run of the automation tests on the k3d cluster with the branch build, before each merge. |
| A6 | The tests of 2a and 3a cover what 2b and 3b move. | They do not exist yet. | A person reviews the list of pinned behaviours in the pull requests 2a and 3a. Section 12 has the lists. |

**The largest risk** is A2. Without it the gRPC request id has no carrier, and the gRPC half of step 1 has no use. So step 1 measures it before its pull request merges.

**What was tried through a router.** Victoria's rule of 2026-10-07 is that no decision rests on an assumption, and that each open question is verified by the actual code and by a run. On 2026-10-07 from 21:10 Docker ran and the local k3d cluster was up: 37 pods Ready, the router sets `default` and `failover`, simulator commit `e80acbf` (read from `/version` of the pod), router build `v1.5.8-85-g4369814` (read from three router pods). A1, A2 and A3 are verified by a run. The run for A1 and A2 used a probe image, `provider-simulator:probe-request-id`: commit `e80acbf` and the probe patch of section 16.5. Victoria added the permission rules for it on 2026-10-07. The simulator ran the probe image from 22:15 to 22:16. Then the deployment went back to `ghcr.io/magma-devs/provider-simulator:main`: `/version` named commit `e80acbf` again, all 32 routers took a pairing reset, and one request through `eth-sim-router`, `lava-sim-rest-router`, `lava-sim-grpc-router` and `eth-failover-prodlimits-sim-router` each got an answer. One thing stayed in the memory of `lava-sim-grpc-router`: the descriptor of the bank service, which it had read from reflection. An `AllBalances` call then got `UNIMPLEMENTED`, "Method not found!", from the provider. Victoria ordered a restart of that router pod. After it, at 22:19, `GetLatestBlock` answered OK, `AllBalances` gave the reflection text of row A3 again, and all pods were Ready. Two facts follow, both observed on the same router pod. The router reads a descriptor that it does not have at the time of the call: the pod that failed at 21:27 served `AllBalances` at 22:15 with no restart of the router. And the router keeps a descriptor that it has read for the life of its pod: it still sent `AllBalances` to a provider that no longer served it. So a new gRPC method on the simulator needs no router restart.

## 12. Tests

| Pull request | Test | File |
|---|---|---|
| 1 | The REST id comes from the query. The query wins over the header. The header works in any letter case. With no id, the counter stays. | `tests/test_listener_rest.py` |
| 1 | Through a socket: `GET /history?request_id=<id>&pool=<pool>` returns the rows of one REST request. | `tests/test_simulator_rest.py` |
| 1 | The content of `AllBalances`. | `tests/test_chains_lava.py` |
| 1 | The row of `AllBalances` holds the address. The row of `GetLatestBlock` holds no id. | `tests/test_listener_grpc.py` |
| 1 | Through a real gRPC client: `AllBalances` answers, and reflection lists the bank service. | `tests/test_simulator_grpc.py` |
| 2a | A grid of `plan` results. For each mode (`success`, `down`, `hang`, `drop_connection`, `rate_limit`, `error`), each corruption mode and each per-method override (`error_stub`, `error`, `result`, the entry `default`): the status code, the text, and the row's method, status, `latency_ms` and request id. It includes a fault with a corruption (row 4 of section 9.3). | `tests/test_listener_grpc.py` |
| 2a | Through a real gRPC client: the status text of each fault, `rate_limit` on a gRPC provider, a per-method `error_stub`, a method that is not served (the status `UNIMPLEMENTED` and its text), and a message that does not parse (the status `UNKNOWN`, and no history row). | `tests/test_simulator_grpc.py` |
| 2b | The grid of 2a moves from `plan` to `serve`, with the same expected values. | `tests/test_listener_grpc.py` |
| 2b | On gRPC, `latency_ms` obeys the `ports` filter and the `transports` filter. | `tests/test_listener_grpc.py` |
| 2b | `GrpcListener.serve is Listener.serve`. It replaces the test at `tests/test_fault_policy.py:211-228`, which checks a method that 2b deletes. | `tests/test_fault_policy.py` |
| 3a | The row of a refused upgrade, for each fault: `down`, `rate_limit`, `error`, `hang` and `drop_connection`. Its reply body. No row for an upgrade that succeeds. | `tests/test_simulator_ws.py` |
| 3a | The row of a pushed event, and the rows of a subscribe frame and an unsubscribe frame: method, status and request id. | `tests/test_simulator_ws.py` |
| 3a | A frame that is not JSON gets no reply and no row. | `tests/test_simulator_ws.py` |
| 3b | One test for each row of section 9.2 that names 3b and that Victoria accepted. | `tests/test_simulator_ws.py` |
| 3b | The guard: `server.py` has no `fault_policy` import and writes no history row. | A new test file |

The socket tests that exist do not pin most of what 2b and 3b move (RAN BY THE REVIEWER, and I ran the searches again):

- `tests/test_simulator_ws.py` reads `/history` in one place, line 810, for an `eth_blockNumber` frame. No test names the row of a refused upgrade, of a pushed event or of a subscribe frame.
- `tests/test_simulator_grpc.py` asserts no status text, sets no `rate_limit` on a gRPC provider, and uses no `error_stub`.

That is why 2a and 3a come first.

Four rules for every pull request:

- **Every test of the repository passes.** CI runs `pytest tests/`, which is every test file. That includes the tests of the parts that this design does not edit: the cache simulator (`tests/test_cache_sim.py`, `tests/test_cache_sim_wire.py`, `tests/test_cache_sim_matches_a_real_cache.py`, `tests/test_control_api_cache.py`) and the RESP proxy (`tests/test_resp_proxy.py`, `tests/test_resp_store.py`, `tests/test_resp_control.py`, `tests/test_resp_control_routes.py`, `tests/test_resp_wiring.py`). No pull request of this design edits one of those files.
- **Linux decides.** CI runs on `ubuntu-latest` (`.github/workflows/lint-and-test.yml`). Sockets behave differently on macOS and on Linux. A green run on a Mac does not prove a socket test.
- **An expected value is not edited to pass.** In 2b and 3b a pinned value that fails is a row for section 9.2 that Victoria must accept, or it is a defect of the pull request.
- **The automation tests run on the k3d cluster with the branch build before each merge of 1, 2b and 3b.** All suites run, the cache suites and the RESP suites too. This is the check for assumption A5.

### 12.1 Additions to the pin lists of 2a and 3a, from the reads of 2026-10-07

Three read-only helpers listed what the tests of both repositories read today and what no simulator test pins. Their reports are in the evidence folder: `helper-simulator-tests-report.md`, `helper-automation-grpc-ws-rest-report.md` and `helper-cache-and-resp-report.md`. The line numbers below are their reading at `e80acbf` and at automation commit `58e8953`. Each item is a behaviour that 2b or 3b can lose with no failing test, so 2a and 3a pin it first.

**2a, gRPC.**

1. A per-method `error_stub`: the status code and the status text. The simulator sends the name of the status as the message, and an automation test reads thirteen codes (`failover/test_grpc_requests_are_retried_or_returned_at_once_by_the_rule_of_each_error_status.py:35-36`, `:201-213`, `:306`). Also a per-method `error`, and the `default` entry of `responses`.
2. The row holds its method before the latency wait. An automation test reads the row while the provider still waits (`failover/test_rest_and_grpc_requests_are_answered_when_a_provider_answers_after_the_attempt_window.py:87-90`, `:445`, `:518`).
3. `fail_first_n=3` with `then_mode="success"` under `down` gives exactly three `down` rows (`failover/test_rest_and_grpc_requests_return_to_the_pinned_primary_after_it_failed_its_first_requests.py:297`, `:362`).
4. The status `UNAVAILABLE` for `down` and for `drop_connection`: a strict expected failure of six automation tests matches only that status (`tests/simulator/_rest_and_grpc_failover_steps.py:164`, `:326`).
5. The four status texts: "provider down" and "connection dropped" (pinned today in `tests/test_simulator_grpc_port_closed.py:177` and `:316`), "hang timeout" and "Too many requests" (not pinned today).
6. The row fields of a gRPC call: `latency_ms`, and the status labels `hang`, `drop_connection` and `rate_limit`.
7. `rate_limit` and `error_stub` over a socket; the status `UNIMPLEMENTED` of a method that is not served; a message that does not parse; the `UNKNOWN` fallback; the corruption codes (`wrong_type` gives `INTERNAL`, four others give `UNKNOWN`, `missing_field` clears a field).
8. Reflection: a test that asks reflection for the symbol of a served service (section 11, row A3).
9. The name `server._GRPC_PORT_POLL_S` stays importable: two tests use it (`tests/test_simulator_grpc_port_closed.py:386`, `:732`).
10. `drop_at` on gRPC: for `after_headers` and `mid_body` the initial metadata arrives before the call ends (section 14.6, point 1).
11. An `error_stub` with a name that is not a status gives `UNKNOWN`, and the key `message` gives the text (section 14.6, point 3).
12. A gRPC call under `pause_at` is not delayed, and `rate_limit` gives `RESOURCE_EXHAUSTED` with "Too many requests" and no body text (section 14.6, point 4).

Fourteen test functions are rewritten by 2b, because they call `plan` or read `GrpcPlan`: the thirteen functions of `tests/test_listener_grpc.py`, and `tests/test_fault_policy.py::test_the_grpc_listener_asks_the_same_predicate_rather_than_its_own`.

**3a, WebSocket.**

1. A refused upgrade: its HTTP status (503, 429, 400), its row with the method `ws_upgrade` and its status, and the forms of `drop_at` (`server.py:532-552`). An automation test needs the row with `ws_upgrade` and `rate_limit`, and the status 429 (`websocket/test_ws_upgrade_rate_limit_holdoff.py:656-659`, `:682`).
2. The row of a pushed event (`server.py:406-415`), the row of a subscribe frame, and a frame that is not JSON (`server.py:576-579`).
3. One entry of `/ws/subscriptions` for each subscribe, with its pool and its provider id.
4. A `ws` row keeps `transport`, `port` and `method`: an automation test counts rows by them (`chaintracker/test_fork_detection_reporting.py:323-329`, `:371`). A frame row carries the lava headers of the upgrade (`tests/test_simulator_ws.py:772-816`).
5. The arrival row of an `http` call is written before the body is read. Three tests pin it and must pass with no edit in 3b: `tests/test_simulator.py:2820`, `:2862` and `:2887`.
6. `WsSubscriptions()` with no argument: three tests build it so (`tests/test_control_api_cache.py:30`, `tests/test_cache_sim_wire.py:321`, `tests/test_resp_control.py:286`).
7. A subscription belongs to one connection: an unsubscribe from another connection answers false and removes nothing, and a close removes the subscriptions of its own connection (section 14.6, point 2).

**The shared wiring of `server.py`, for 2a.** No simulator test proves today that `start` binds the cache simulator port and serves the `/cache/` routes over HTTP, that the RESP proxy forwards in the session server, or that the simulator starts with no `grpcio`. 2b and 3b edit `start` and `__init__`, so 2a adds one test for each of the three.

## 13. The two repositories

| Pull request | Simulator repository | Automation repository |
|---|---|---|
| 1 | Merges first. | Then: the request helpers add the id, and the tests of MAG-3800 read rows by id. |
| 2a, 3a | Merge. Tests only. | No change. |
| 2b, 3b | Merge. | No test changes: its tests must pass as they are. Three skill pages change in a paired pull request, because they name code that 2b changes. `writing-simulator-tests/SKILL.md:180` and `failover/references/what-the-simulator-can-tell-you.md:58` name `GrpcListener.plan`. `add-simulator-entity/SKILL.md:41-42` describes the four listeners. The rule is at `add-simulator-entity/SKILL.md:802-804`: "Change the code, change the skill, same PR." |

The order of step 1 has a reason. The CI of the automation repository checks out the default branch of the simulator. So the simulator change must be on `main` first. The `shared-skill` of this repository states this rule. I did not read the workflow of the automation repository.

**What a test does after step 1.** Session 3800 wrote the first automation tests of MAG-3800. It is closed since 2026-10-07. This is the contract that the next author of those tests can use:

| Interface | Send | Read |
|---|---|---|
| REST | Add `request_id=<a unique value>` to the query string. | `sim_control.get_history(request_id=<the value>, pool="lava-sim-rest")` |
| gRPC | Call `cosmos.bank.v1beta1.Query/AllBalances` with a unique `address`. | `sim_control.get_history(request_id=<the address>, pool="lava-sim-grpc")` |

Three notes for those tests:

- The unique value must not be a plain number (section 8.2). A plain number can equal a counter value of the simulator. The same holds for a Tendermint RPC POST: the automation client sends the id "1" by default (`src/clients/http/tendermint_rpc_client.py:197-200`).
- The id is text on REST and on gRPC. The parameter `request_id` of `get_history` is typed as a number today (`tests/simulator/sim_control.py:2161`).
- The `failover` skill has the rule "Assert on the reply, never on an absence in the history" (`references/what-the-simulator-can-tell-you.md:190-193`), because the router polls every provider all the time. A request id makes one absence provable: zero rows for ONE id. So that skill page gets this exception in the automation pull request that follows pull request 1.
- A test that requires zero rows needs a control. First send the same request with no fault and require one row or more for its id. That proves that the id travels. Then send the request under test. The helper of session 3800 in pull request 1432 of the automation repository, `assert_request_reached_no_provider`, takes such a control request.

### 13.1 The topology does not change

Victoria, 2026-10-07: "topology will be affected?". No step of this design adds, removes or renumbers a pool, a provider, an endpoint or a port.

- The data stays: `provider_simulator/topology.py`, and the vocabulary `INTERFACES = ("jsonrpc", "rest", "grpc", "tendermintrpc")` and `TRANSPORTS = ("http", "http2", "ws")` (`provider_simulator/domain/endpoint.py:16-17`).
- RAN ON THE CLUSTER: the probe for the request id changed `server.py`, `provider_simulator/listeners/grpc.py` and `provider_simulator/listeners/rest.py` and no topology file. It worked through the pools `lava-sim-rest` and `lava-sim-grpc` as they are, with no change to a router config. That includes the new gRPC method of step 1.
- What changes is the wiring in `server.py` that gives each endpoint of the topology its listener: the table `_HTTP_ADAPTERS` (`server.py:1308-1313`) and the loop of `SimulatorServer.start` (`server.py:1400-1411`). In 2b the 12 gRPC endpoints get a listener that is a subclass of `Listener`, on the same ports and the same thread. In 3b the row `("jsonrpc", "ws")` gets the new WebSocket listener, on the same 9 ports. In 4a each of the 135 `http` endpoints and the 9 `ws` endpoints gets a port gate, built from the topology.
- Four simulator tests guard the topology and its mirrors, and they must pass with no edit in every pull request: `tests/test_domain_topology.py`, `tests/test_control_api_providers.py`, `tests/test_service_publishes_every_port.py` and `tests/test_values_sim_matches_topology.py`. They passed at `e80acbf` and with the probe.
- So `k8s/service.yml`, `k8s/deployment.yml`, `config/values_sim.yml` and `tools/local-cluster/routers.yml` need no change. The two new tests of part B2 run on a failover router that exists.

## 14. What it costs to add an interface, a method or an ability

### 14.1 An interface

GraphQL on `http` is the example. The router names a GraphQL request by its root fields, and it sends the caller's body to the provider as it is (`protocol/chainlib/chainproxy/rpcInterfaceMessages/graphqlMessage.go`).

This cost is the same today and after step 3. Steps 2 and 3 change no row of this table.

| File | Change |
|---|---|
| `provider_simulator/listeners/graphql.py` | New: a `Listener` subclass. It reads the body, names the method, and shapes a fault and a success as GraphQL replies. |
| A `Chain` class | New content, or a new chain. A new chain also needs a row in `CHAINS` (`chains/__init__.py:24`), a row in `_QUIRKS_BY_CHAIN` (`domain/quirks.py:41`) and an entry in `tests/test_chains_block_hash_agreement.py`. |
| `provider_simulator/domain/endpoint.py:16` | One word in `INTERFACES`. |
| `server.py:1308` | One row in `_HTTP_ADAPTERS`, and a handler class of three lines, as `_JsonRpcHttpHandler` is today. |
| `provider_simulator/topology.py`, `k8s/service.yml`, `config/values_sim.yml` | The rows of the new pool, its ports and its router nodes. Two tests read the last two files: `tests/test_service_publishes_every_port.py` and `tests/test_values_sim_matches_topology.py`. I did not read those tests. |
| Not edited | `listeners/base.py`, `fault_policy.py`, `domain/call_log.py`, `domain/scenario.py` |

Three questions about GraphQL stay open, and this design does not answer them:

- Does the simulator need a GraphQL parser library to find the root fields?
- Where does the request id travel? The router sends the body as it is, so a value in the body reaches the provider. Which member of the body holds the id is not decided.
- Which chain does it serve first?

### 14.2 A method

| Interface | Today | After step 2 |
|---|---|---|
| JSON-RPC, Ethereum shown | One entry in `ETH_METHOD_DEFAULTS`, `stubs.py`. A method with no entry answers `0x1`. Special behaviour goes in `EthChain.build_success`. | The same |
| REST | One route in `REST_METHOD_DEFAULTS`, `stubs_rest.py`. A path with no route answers 404. | The same |
| Tendermint RPC | One entry in `TENDERMINT_METHOD_DEFAULTS`, `stubs_tendermintrpc.py`. A method with no entry answers the error -32601. | The same |
| gRPC | Three places: a method of `_Servicer` and a builder function in `server.py`, and the data in `LavaChain._build_grpc`. | Two places: one row of the method table in `listeners/grpc.py`, and the data in `LavaChain._build_grpc`. |
| WebSocket subscriptions | One entry in `SUBSCRIBE_METHODS` and its pair in `UNSUBSCRIBE_METHODS`, `stubs_ws.py`. | The same |

### 14.3 An ability

After step 3, an ability is added in these places, in this order. The request id is the example.

1. **One step of `Listener.serve`.** It is written one time. Step 9 writes `request_id` into the row.
2. **One hook with a default, only if the interfaces differ.** The hook `request_id` reads the `id` of the body by default. `RestListener` overrides it for the query parameter. `GrpcListener` overrides it for the field of the message.
3. **One field of `ScenarioConfig`, only if a test must switch the ability on.** The field is checked in one place. The request id needs none.
4. **One field of the history row and one filter, only if a test must read it.** The request id uses the field and the filter that exist.

Today the same ability needs a change in up to four places: `Listener.serve`, `GrpcListener.plan`, and the two WebSocket places in `server.py`.

**Two kinds of ability.** The four places above are for an ability of the request flow: a decision or a record, such as the request id, the scope of `latency_ms` or a per-method fault key. Three abilities of section 14.4 are of another kind: `port_closed`, `pause_at` and subscriptions. The socket adapter performs them: it closes a port, it holds the bytes of a reply, or it keeps a connection open. Such an ability needs a change in `server.py` for each transport that gets it: `http`, `ws` or `http2`. Steps 2 and 3 do not make that change smaller.

### 14.4 The abilities of each interface

Each "no" is an ability that an interface can get later. The last column says who performs the ability, and so what kind of change it is.

| Ability | JSON-RPC on `http` | JSON-RPC on `ws` | REST | Tendermint RPC | gRPC | Performed by |
|---|---|---|---|---|---|---|
| The row holds the caller's request id | yes | yes | step 1: the query parameter | yes for POST. The URL form gets a counter. | step 1: `AllBalances` | The request flow |
| The faults `down`, `hang`, `drop_connection`, `rate_limit`, `error` | yes | yes | yes | yes | yes, as status codes | The request flow decides. The socket adapter performs `hang` and `drop_connection`. |
| `port_closed`: the port really closes | no; yes with the real-down step proposed in section 14.5 | the same | the same | the same | yes | The socket adapter and its server |
| `latency_ms` obeys the `ports` and `transports` filters | yes | yes | yes | yes | step 2 | The request flow |
| Per-method fault keys | yes | yes. A subscribe frame reads five of the seven keys until step 3. | yes | no | no | The request flow |
| Per-method content override | `result`, `error_stub`, `error`, `body` | The same. A `body` for a subscribe frame: step 3. | `body`, `error_stub`, `error` | `body`, `error_stub`, `error` | `result`, `error_stub`, `error` | The chains |
| `corruption_mode` | yes | yes. The reply of a subscribe frame: step 3. | yes | yes | yes, on a reply message | The request flow marks the reply. The socket adapter corrupts the bytes. On gRPC the listener does both. |
| A pause inside a reply, `pause_at` | yes | no | yes | yes | no | The socket adapter |
| Subscriptions, and `POST /ws/emit` | no | yes | no | no | no | The WebSocket listener and the WebSocket adapter |

### 14.5 A real down

**How `down` works today.** The skills `failover` and `writing-simulator-tests` record it, and the code agrees.

- A test sets it with `ProviderConfig(mode="down")` through `set_scenario`. For `down` the automation client sends no `transports` filter (`_resolve_transports`, `tests/simulator/sim_control.py:114-119` of the automation repository). So `down` reaches every endpoint of the provider: `http`, `ws` and `http2`. For every other mode the client sends the filter `["http", "http2"]`.
- `ProviderConfig(mode="down", ports=[18638])` makes one address of a provider `down`.
- The provider still accepts the connection, and it answers at once. On JSON-RPC, REST and Tendermint RPC it answers HTTP 503 with no body. On gRPC it answers the status `UNAVAILABLE` with the text "provider down", and the connection stays open. A WebSocket upgrade gets HTTP 503. Two simulator tests pin the facts of this section, and both passed on 2026-10-07: `test_down_returns_503_with_no_body` in `tests/test_simulator.py`, and the 15 tests of `tests/test_control_api_port_closed.py`.

**What `down` is not.** The same skills record two limits.

1. **It does not refuse a connection.** "On an HTTP listener no simulator fault refuses a connection." Only gRPC has such a fault: `mode="port_closed"` stops the gRPC server of the endpoint, and a new connection is refused (`server.py:1212-1259`, `provider_simulator/port_gate.py`). For every other endpoint the control API answers HTTP 400 (`provider_simulator/control_api.py:227-237`, in the function at lines 175-253).
2. **It does not keep a provider out.** "`down` and `drop_connection` cause a retry inside one request. They do not block a provider for the next request: the router tries the same providers again." A provider that is gone for a whole test "asks for something no scenario field gives".

Both quotes are from `references/what-the-simulator-can-tell-you.md` of the `failover` skill, at lines 163-165 and 242-252.

**What this design does to `down`: nothing.** `fault_policy.py` decides if `down` reaches an endpoint, and no step edits it. The hook `build_down` of section 8.3 keeps the two replies. The one place that touches `down` is the subscribe frame of question Q11.

**Measured on 2026-10-07: for the router, `down` is an HTTP 503 reply.** Local k3d cluster, simulator `e80acbf`, router build `v1.5.8-85-g4369814`. I set the three primaries of the pool `eth-failover-prodlimits-sim` to `down`, and sent one `net_version` request through `eth-failover-prodlimits-sim-router`. Its `RelayRetryLimit` is 2, read from `/debug/runtime-config`.

- The caller got HTTP 500 after 0.025 s. The body holds `insufficient results ErrMsg: HTTP 503 503: Node temporarily unavailable`. `Lava-Provider-Address` named the three primaries, and `Lava-Retries` was 2. No backup answered.
- The router's log for that request names the class of the error: `"error_name":"NODE_SERVICE_UNAVAILABLE"`, `"error_code":"2006"`, `"retryable":"true"`.
- The simulator stored three rows with the status `down`, the method `*` and no request id.

For a refused connection the router has another class, `LavaErrorConnectionRefused`. The `failover` skill records it for gRPC `port_closed` (`references/what-the-simulator-can-tell-you.md:77-85`). So a test that sets `down` exercises the router's handling of HTTP 503. It does not exercise the router's handling of a refused connection.

**The ten automation test files that say "refuse the connection" and set `down`. RAN on 2026-10-07** on the same cluster: 88 tests, 0 failed, 30 passed, 6 xfail, 52 skipped with a stated reason (`ten-tests.xml` in the evidence folder).

| Test file | Result of the run |
|---|---|
| `failover/test_archive_traffic_cannot_use_a_backup_without_the_addon.py` | 1 passed |
| `failover/test_the_addon_filter_applies_inside_both_tiers.py` | 1 passed |
| `failover/test_a_backup_serves_when_every_primary_goes_quiet.py` | 1 xfail, MAG-3978 |
| `failover/test_a_backup_serves_when_every_primary_is_rate_limited.py` | 1 xfail, MAG-3978 |
| `failover/test_archive_traffic_fails_over_normally_when_every_provider_has_the_addon.py` | 1 xfail, MAG-3978 |
| `failover/test_one_quiet_primary_does_not_wake_the_backup_tier.py` | 1 xfail, MAG-3978 |
| `failover/test_two_quiet_main_nodes_still_do_not_wake_the_backup_tier.py` | 1 xfail, MAG-3978 |
| `test_router_retry_error_classifier_tiers.py` | 10 passed, 1 skipped |
| `chaintracker/test_composite_score_recovery_lag_after_provider_heals.py` | 1 xfail, MAG-2387 |
| `chaintracker/test_chainstate.py` | 18 passed, 51 skipped |

What the words mean for each file. This part is READ, by a helper and in part by me; it is not a run, because no `http` provider can refuse a connection today.

- Nine files need only a provider that fails at once, and HTTP 503 gives that. They read the status of the reply, its result, `Lava-Provider-Address`, `Lava-Retries`, the chain state and a score. The helper read in the router code that a refused connection gives the same retry, the same count against the retry limit and the same headers. I did not check those router lines. So in these nine files the words are wrong, and the tests are sound.
- One file tests another rule than the rule that it names: `test_router_retry_error_classifier_tiers.py`. Two of its tests carry the label "Priority 0 — DetectConnectionError (TCP refused)", and they name the class `Internal` in a docstring and in a log row (lines 638-661, 706 and 868-874, read by me). They do not assert the class, and that is why they pass. The header of the file has the same wrong words (lines 10 and 21: its table gives "HTTP 503 with empty body" as the example of a connection error). They set `down` (lines 678 and 924, read by me). The router has two rules, in `protocol/common/error_classifier.go` and `error_codes.go` of smart-router. HTTP 503 gives `NODE_SERVICE_UNAVAILABLE`, code 2006, `External`. The text "connection refused" gives `PROTOCOL_CONNECTION_REFUSED`, code 1002, `Internal`. At the build that ran, `4369814`, the classifier lines are 445 and 473 for HTTP 503, and 743-744 and 846 for a refused connection (read by `reviewer-2`, and then by me with `git show 4369814:protocol/common/error_classifier.go`). I read the same two rules at lines 380 and 753 in the working tree of `~/smart-router`, which is commit `af7f41aa`, v1.5.7-10, 106 commits before that build. The two entries of `error_codes.go` are at lines 17-20 and 224-227 at both commits. My run showed code 2006. So the two tests pass on the HTTP 503 rule. The rule for a refused connection has no test on an `http` provider.
- With a real down those two tests can test the rule that they name. Their count of `down` rows must then change, because a closed port stores no row.
- The ten files are not the full set. The review of `reviewer-2` found three more test files that call `down` a refused connection and set it, and I opened their lines: `test_primary_backup_failover.py` (the words at lines 785 and 1048, `down` at line 310, the count of `down` calls at line 329), `test_backup_tier_failover.py` (the words at line 709, `down` at line 487, the count at line 260) and `test_router_retry_failover.py` (the words at line 246, the count at line 517). All three assert on `calls_by_status["down"]`, as the classifier file does. So these three were not in the run of the 88 tests. They stay correct while they set `down`, because `down` stores a row for each call. Their words are wrong. With a closed port their counts would be zero. The header of `test_router_retry_failover.py` (lines 10-21) already states the true fact: the provider accepts the connection and answers HTTP 503 with an empty body. Three more places have the wrong words and set no `down`: `tests/simulator/_default_retry_limit.py:205`, `failover/test_whole_answer_delivered_when_a_reply_finishes_after_the_window.py:46-47` and `:142-146`, and the helper `_set_blocks_down` in `chaintracker/test_chainstate.py:922-949`, which has no caller.
- Three conditions hold for "the tests are sound" of the nine files (READ BY `reviewer-2`, its finding 3). A test that moves to `port_closed` must close every door of the provider: the automation client sends the filter `["http", "http2"]` for every mode but `down`, so the `ws` port would stay open (pull request 4b changes that). The stop status of MAG-3978, HTTP 500, is written for providers that answered HTTP 503 (`tests/simulator/_default_retry_limit.py:57-59`). And two of the nine use `down` to lower scores in a warm-up; nobody read how the router scores a refused connection against an HTTP 503.

**Victoria's decision of 2026-10-07: the design must give a real down.** Her words, after I wrote that this design does not bring one: "so you need to fix it in design". She chose no form for it. A real down is the first meaning: the port of the provider refuses a connection. It is an ability of the socket adapter. For an `http` endpoint or a `ws` endpoint it needs four things:

1. Stop the server of that endpoint, and close its listening socket.
2. Close the connections that are open.
3. Start a server on the same port when the mode ends.
4. Make the control call wait until the port changed.

**Its form: `mode="port_closed"` on every transport.** Victoria, 2026-10-07: "why is it even a question we already tested it and learned how to do it in failover". The mode exists for gRPC since pull request #133 of the simulator ("a port_closed fault closes a gRPC provider's port for real"), with two automation tests and its text in the `failover` skill. This section extends the same mode to `http` and `ws`. The gRPC loop and `PortGate` are the model. Some parts exist, and the list after the table names the parts that do not.

| Part | Today | With this design |
|---|---|---|
| The wish "this port is closed" | `fault_policy.port_closed(scenario, endpoint)` (`provider_simulator/fault_policy.py:85-99`). It reads the mode and the `transports` and `ports` filters. It does not name gRPC. | No change. |
| The state of one port, and the wait of the control call | The class `PortGate` (`provider_simulator/port_gate.py:39`). `Simulator.start` makes one for each gRPC port (`server.py:1449-1450`). | One `PortGate` for each `http` port and each `ws` port too. |
| The loop that closes and opens the port | `_serve` in `_run_grpc_in_thread` (`server.py`). It stops the gRPC server, or starts a new one on the same port. | A second loop of the same shape for a `_SimThreadingHTTPServer`. To close: `shutdown()`, then `server_close()`, then close each client socket that is open. To open: a new server object on the same port, and a new `serve_forever` thread. |
| The control API | It refuses the mode for an endpoint that is not gRPC, with HTTP 400 (`provider_simulator/control_api.py:227-237`). | That one refusal goes. The other refusals stay: no endpoint is targeted, the port has no gate (HTTP 409), and a field that acts on one request is set with the mode. |
| `/ready`, `POST /reset`, `POST /reset/all`, the time-to-live sweep | They read the gates, and they wait for a port to open again. | No change in the rule. They get the gates of the `http` and `ws` ports. |
| `down` | HTTP 503 on an open connection. | No change. A test chooses: `down` for a provider that answers 503, `port_closed` for a provider that cannot be reached. |

**RAN on 2026-10-07, on macOS, with the simulator's own class `_SimThreadingHTTPServer`** (the script `real_down_http_probe.py`, section 16). It binds a local port and touches nothing shared.

1. `shutdown()` and `server_close()` took 0.5 s. (A read, not a result of the run: 0.5 s is the default poll interval of `serve_forever`.)
2. A new connection after the close got `ConnectionRefusedError: [Errno 61] Connection refused`.
3. A request that was in flight at the close still got its HTTP 200. So the close of the listening socket alone is not enough.
4. With the open client sockets closed too, the request in flight got `RemoteDisconnected`. The time from the start of the close was 0.5 s in my run and 0.001 s in the two control readings: it includes the wait for `shutdown()`, which depends on where the poll loop is.
5. A new server on the same port answered at once. `allow_reuse_address` is 1 on that class.
6. A READ, not a result of the run: no line of `server.py` or of `provider_simulator/` sets `protocol_version`, so the simulator's handlers speak HTTP/1.0, where one connection carries one request. The experiment used a handler class of its own, not a handler of the simulator. Nobody looked at the connections that the router keeps.

The experiment did not use a listener of the simulator, a request of the real handler under `hang`, `latency_ms` or `pause_at`, a history row, or a `ws` connection. It ended about one second after it cut the socket, so it does not show what the cut-off worker thread does next. The session `worker` ran it again two times at 22:28 from the main checkout at `e80acbf`, and saved the output in `control-runs/1b-real-down-http-probe.txt` of the evidence folder. Both readings gave the outcomes of results 1 to 5 again (0.498 s and 0.501 s for the close).

**What the proposal must also cover.** The review of `reviewer-2` found these gaps (its finding 6). I opened the lines of point 1 myself. The other line numbers are its reading at `e80acbf`.

1. **Every scenario write waits on the gates, not only a reset.** `apply_scenario` collects the gates of each provider that it writes (`provider_simulator/control_api.py:451`) and waits for them (`:460`). After `_PORT_SETTLE_S`, 5.0 s (`:31`), it answers HTTP 500. With a gate on each of the 135 `http` ports and the 9 `ws` ports, every `set_scenario` of every test would wait for a pass of each port of each provider that it writes. So the step must limit the wait to the ports whose wish changed, and it must measure the time of a `set_scenario` before and after.
2. `/ready` waits at most 1.0 s for the lock (`server.py:758-761`), and an `http` close took 0.5 s in the experiment. The loop needs a small poll interval.
3. `Simulator.stop()` uses a list of server objects that `start()` makes one time (`server.py:1397`, `:1408-1411`, `:1434`, `:1496-1504`). A server that is made when a port opens again is a new object. It must enter that list, and it needs its wiring again: `srv.listener` and `srv.subscriptions` (`:1409-1410`).
4. Nothing records the open client sockets today (`server.py:87-107`). `WsSubscriptions` records subscriptions, not connections (`provider_simulator/listeners/ws.py:32-95`). This part is new code.
5. Five simulator tests assert the refusal that this step removes, and they must change (READ BY `reviewer-2`; my own search found the asserted text at four of these places): `tests/test_control_api_port_closed.py:51-59` and `:62-68`, and `tests/test_simulator_grpc_port_closed.py:421-431`, `:818-823` and `:856-863`. One more test pins a text only: `tests/test_control_api_port_closed.py:42-48`. By the same read no automation test breaks: none expects the refusal from a live simulator, and the two tests that send `port_closed` today close gRPC providers.
6. The automation client sends the filter `["http", "http2"]` for `port_closed` (`tests/simulator/sim_control.py:114-119`). So `ProviderConfig(mode="port_closed")` on an eth provider would close its `http` port and leave its `ws` port open. For a provider that cannot be reached, the client must send no filter for `port_closed`, as it does for `down`. Two more places there read the texts of the HTTP 400: `sim_control.py:456-457` and `tests/simulator/_grpc_replies.py:57-68` and `:139-177`.
7. Documents that say the mode is for gRPC only: `README.md:109` and `:122`, `docs/using_grpc.md:85`, `docs/using_the_simulator.md:172-180` and `CLAUDE.md:44` of the simulator; `failover/references/what-the-simulator-can-tell-you.md:20`, `:163-165`, `:254-279` and `writing-simulator-tests/SKILL.md:142` of the automation skills.
8. Two more refusals stay, and the table does not name them: the mode inside a per-method override (`control_api.py:256-261`) and the mode as a `then_mode` (`:1188-1192`).
9. Texts in code that name gRPC must change with the step: `provider_simulator/fault_policy.py:90`, `provider_simulator/port_gate.py:1-7` and `:15-16`, `control_api.py:241` and `:505`. A test pins the text of line 241.
10. The acceptance of `port_closed` on an `http` or `ws` endpoint is a change of behaviour: today the answer is HTTP 400. It is a seventh change on purpose, and it belongs to this step, not to section 9.2.
11. The loop is for PROVIDER endpoints only. The control server and the RESP control server use the class `_SimThreadingHTTPServer` too (`server.py:1413`, `:1085`, `:1430`), and they keep their plain loop.

**Three other ways, and why not.**

- Accept the connection and reset it at once. The router then reads "connection reset", not "connection refused". That is the fault `drop_connection`, which exists.
- A Kubernetes NetworkPolicy, or a change of the Service. The `failover` skill measured that a NetworkPolicy blocks new connections only and misses the first second of a new pod (`what-the-simulator-can-tell-you.md:315-328`). The simulator's own tests cannot use it, because they run with no cluster.
- A new mode name. `port_closed` has its rules, its tests and its skill text. A second name for the same state of a port gives two things to keep equal.

**Not verified. Each one needs the code and a run.**

1. What the router does with an `http` provider that refuses the connection: the error class, the retry, the failover to a backup, and the effect on the health of the endpoint. The class `LavaErrorConnectionRefused` is measured for gRPC only.
2. A `ws` endpoint: the subscriptions that are open when the port closes, the entries of `/ws/subscriptions`, and how the router connects again.
3. The same experiment on Linux, in the simulator's CI.
4. How long the control call waits on a cluster with 135 `http` endpoints and 9 `ws` endpoints.

**Where it fits: step 4, two pull requests.** This work does not depend on step 2 or on step 3, so it can merge before them or after them. Its decision is decision 7 of ADR-001; it needs no new decision record.

**The condition for a separate step.** The session `reviewer-2` relayed Victoria's words of 2026-10-07 from its own session: "it is separate if we don't break existig tests by this design. because existig tests usng several down." And on the choice of the mode: "this you don't ask me you verify it in the code". What the code says (READ BY `reviewer-2` at `e80acbf` and at automation commit `58e8953`. Of these lines I opened `provider_simulator/listeners/grpc.py:91-93` and `provider_simulator/fault_policy.py:85-99` myself. The two counts are its reading; a helper of mine counted 13 files that read `calls_by_status["down"]`):

- `down` and `port_closed` share no line. `down` is a reply (`provider_simulator/listeners/base.py:172-174` and `:217-227`, `provider_simulator/listeners/grpc.py:91-93`). `port_closed` is not a reply (`provider_simulator/fault_policy.py:85-99`). So this step does not touch what `down` does.
- `down` cannot become the real down. 56 test files under `tests/simulator` set `down`, and 12 of them read the simulator's own count or rows of `down` calls. A closed port stores nothing. So the real down must be another mode, and `port_closed` is the mode that already means it.
- One thing is NOT proven by a read, and it decides the condition: every scenario write waits on the gates of the provider that it writes (point 1 above). With a gate on each `http` and `ws` port, each of the 56 files that set `down` goes through that wait at each `set_scenario`. Only the code of this step and a run of both suites show if an existing test breaks or slows. That run is the gate of pull request 4a: it does not merge before it.

| Pull request | Repository | Content | Tests |
|---|---|---|---|
| 4a | simulator | The loop that closes and opens the port of a PROVIDER `http` or `ws` endpoint. A `PortGate` for each such port. A record of the open client sockets, so that a close ends them. The control API accepts the mode for these endpoints and waits only for the ports whose wish changed. The texts and the documents of points 7 and 9 above. | A new connection is refused. A request in flight ends. A `ws` connection ends, with a subscription and with none, and `/ws/subscriptions` loses its entries. The port opens again by `success`, by another mode, by `POST /reset`, by `POST /reset/all` and by the time-to-live sweep. `/ready` leaves a closed port out. The control port, the cache simulator port and the RESP ports cannot be closed. The three tests that pin today's refusal change (`tests/test_control_api_port_closed.py:42-48`, `:51-59`, `:62-68`). The time of one `set_scenario` is measured before and after. |
| 4b | automation | The client sends no filter for `port_closed`, as it does for `down`, so every door of the provider closes. The texts of `sim_control.py:456-457` and `_grpc_replies.py`. The skill pages of point 7. | Part B2 of section 10.5: two tests for the rule `PROTOCOL_CONNECTION_REFUSED` on an `http` provider. The tests of the nine files stay on `down`. |

- **Order:** 4a merges first, then 4b, because the CI of the automation repository checks out the default branch of the simulator.
- **Before 4a merges:** a run on the local k3d cluster with the branch build. It must show what the router does with an `http` provider that refuses the connection (the class in the router's log, the retry, the failover to a backup) and with a `ws` endpoint that closes. These are the points of the list "Not verified" above.
- **Rollback:** revert 4a while it is the newest merged one. The tests of 4b then read the HTTP 400 of the simulator as a setup problem, through `a_refused_port_closed_reads_as_setup` in `tests/simulator/_grpc_replies.py`, and not as a router fault.

The second meaning, a provider that stays out for a whole test, is a behaviour of the router. The simulator cannot give it with one field. The `failover` skill names two ways: use a backup tier, or make the router switch the endpoint off with many failed requests in a row (`what-the-simulator-can-tell-you.md:242-252`). The second way has a measured limit: in four experiments (not pinned with `down`, 300 requests; pinned with `drop_connection`, 400; pinned with the archive extension, 120; pinned with `down` after a clean reset, 400) exactly one endpoint switched off and the second one never did (`failover/references/the-mechanism.md:432-437`, MAG-4025). A closed port can change this meaning too: the skill records that a gRPC router which starts while the three primaries are closed serves from its backups only (`what-the-simulator-can-tell-you.md:287-291`). That is measured for gRPC, and not for `http`.

### 14.6 The configuration surface: every setting, and the place that applies it

Victoria, 2026-10-07: "do you aware of all setting and abilities the simualtor can do today?" and "how can you write suche huge designe wothout investigating how sumilator works . configure scenariou etc?". Versions 1 to 2.1 of this design were written from the code of the request flow, with no inventory of the settings. This section is that inventory. A read-only helper made it from the main checkout at `e80acbf` (`helper-inventory-of-settings-report.md` in the evidence folder; nothing was run). I read `provider_simulator/domain/scenario.py` and `provider_simulator/domain/quirks.py` in full myself, and I opened the lines of the five points at the end of this section.

**The counts, with names.**

- 18 scenario fields (`ScenarioConfig`, `provider_simulator/domain/scenario.py:41-73`): `mode`, `latency_ms`, `error_probability`, `error_code`, `error_message`, `http_status`, `rate_limit_body`, `responses`, `corruption_mode`, `missing_field`, `blocks_behind`, `fail_first_n`, `then_mode`, `drop_at`, `pause_at`, `pause_ms`, `transports`, `ports`.
- 13 keys of one entry of `responses`: the seven fault keys `mode`, `latency_ms`, `error_probability`, `error_code`, `error_message`, `http_status`, `drop_at`; and the content keys `error_stub`, `error`, `result`, `body`, `status`, `message`.
- 7 corruption modes: `truncated`, `missing_field`, `invalid_json`, `empty_response`, `wrong_type`, `null_body`, `invalid_proto`.
- 5 quirk fields (`provider_simulator/domain/quirks.py:28-38`): `logs_indexed_up_to` and `logs_lag_mode` for eth; `slot_block_gap`, `slot_offset` and `unknown_method_mode` for Solana. Bitcoin, Lightning and Lava have none.
- 31 control routes: 22 on the control port 19000, and 9 on the RESP control port 19101. The helper's report lists each one with its filters.

**Field by place.** The four places of today: S is `Listener.serve` with its hooks; G is `GrpcListener.plan`; U is the WebSocket upgrade; F is the WebSocket subscribe frame. "no" means that the place does not apply the field.

| Field | S | G | U | F |
|---|---|---|---|---|
| `mode` | yes | yes | yes | yes |
| `mode="port_closed"` | refused by the control API | performed by the gRPC serve loop, not by `plan` | refused | refused |
| `latency_ms` | yes | yes, and the filters are ignored (section 9.2) | no | yes |
| `error_probability` | yes | yes | yes | provider-wide yes; per-method no |
| `error_code`, `error_message` | yes | yes: a status name or a status number | the message only | yes |
| `http_status` | yes; not on a WebSocket frame | no | no | no |
| `rate_limit_body` | JSON-RPC only | no | no | yes |
| `responses`, the fault keys | seven keys; none on Tendermint RPC | no | no | five keys: not `error_probability`, not `http_status` |
| `responses`, the content keys | the chains; `body` on JSON-RPC | `error_stub`, `error`, the `default` entry, `result` | no | no |
| `corruption_mode`, `missing_field` | yes | yes, by its own rules | no | a fault reply yes; a success reply no |
| `blocks_behind` | the chains; not on Solana | the chain | no | no |
| `fail_first_n`, `then_mode` | yes | yes | yes, one count for each upgrade | yes |
| `drop_at` | yes | yes | yes | yes |
| `pause_at`, `pause_ms` | `http` only | no | no | no |
| `transports`, `ports` | yes | yes for the mode and the corruption | yes | yes |
| the quirks | the chains | none: Lava has no quirk | no | no |

**Fields that sections 8.3, 9.2, 9.3 and 14.4 did not name before:** `rate_limit_body`, `missing_field` as a field, `blocks_behind`, `then_mode`, `drop_at`, `pause_ms`, the five quirks, and 29 of the 31 routes. The table above and the helper's report hold them now.

**What the moves must keep. Five points that the design did not have.**

1. **gRPC `drop_at`.** For `after_headers` and `mid_body` the gRPC adapter sends the initial metadata before it ends the call (`server.py:1178-1184`, opened by me). The simulator's tests accept `UNAVAILABLE` or `CANCELLED` for each form, so a loss of this fails no test today. Pull request 2a pins it, and 2b keeps it in the gRPC adapter.
2. **A subscription belongs to one connection.** An unsubscribe removes only a subscription of its own connection, and a close removes the rest (`server.py:556`, `:591-594`, `:657`; the helper's reading). One listener serves all connections of an endpoint. So the WebSocket listener of 3b must get the connection with each frame, and 3a pins: an unsubscribe from another connection answers false and removes nothing.
3. **gRPC `error_stub`.** Its text comes from the key `message`, and a name that is not a status gives `UNKNOWN` (`provider_simulator/listeners/grpc.py:128` and `:133`, opened by me). The REST lookup of an `error_stub` raises `KeyError` for an unknown name (`provider_simulator/chains/lava.py:166-167`, opened by me). ADR-001 moves the gRPC keys to `LavaChain._build_grpc`. That move must keep the gRPC lookup with its fallback and must not reuse the REST lookup. 2a pins an unknown name.
4. **`serve` hands over more than `plan` uses.** After the flow decides a reply, it sets `pause_at` and `pause_ms` on the result (`provider_simulator/listeners/base.py:249-252`, opened by me), and a `rate_limit` verdict carries an HTTP status 429 and a `rate_limit_body` (`provider_simulator/fault_policy.py:138-147`, opened by me). gRPC has no pause and no body text. So the gRPC hooks of 2b must drop these three, and 2a pins: a gRPC call under a pause is not delayed, and `rate_limit` gives `RESOURCE_EXHAUSTED` with "Too many requests".
5. **No change, by a read.** `blocks_behind`, `fail_first_n`, `then_mode`, `missing_field` and the quirks pass through `serve` and `plan` the same way. The 31 routes, the time-to-live sweep, the history ring, the chain heads, the batch reply, HEAD and OPTIONS are outside the code that 2b and 3b move.

Points 1, 3 and 4 are added to the pin list of 2a in section 12.1, and point 2 to the pin list of 3a.

## 15. Open questions for Victoria

Each question has my proposal. Q1 to Q10 are the questions of round 1 in the chat. Q11 to Q15 are new.

| # | Question | My proposal |
|---|---|---|
| Q1 | The shape of steps 2 and 3. (a) Keep `Listener` and move gRPC and the WebSocket subscribe frames into it. (b) Rebuild with an engine class and a gateway class for each interface. (c) Do neither: fix rows 1 to 6 of section 4.4 where they are, in one pull request, and keep the four places. | (a). Your comment in section 9.1 asks that a new ability is easy to add. With (c) an ability is still written in up to four places. |
| Q2 | Keep the twelve differences of section 9.3, or make some uniform now? | Keep all twelve now. |
| Q3 | Accept the six changes of section 9.2? | Yes. Q11 is about row 6. |
| Q4 | REST: the query parameter `request_id`, and it wins over the header `X-Request-Id`? | Yes. |
| Q5 | This design does not edit four things, so they work after it as they work today: the rule for a provider-wide `down`, the id of the Tendermint RPC URL form, the meaning of the override keys, and the cache simulator with the RESP proxy. Do you want this design to change one of them? | No. Their tests still run in each pull request, and they must pass with no edit. |
| Q6 | GraphQL as a fourth step? | No. It needs no step of this design. Plan it when a test needs it. |
| Q7 | When do the documents enter git? | In their own pull request, before step 1. |
| Q8 | Tickets: one story for each step, and which epic? MAG-1939 is the epic "Automation Development — ongoing", and its status is Done (read in Jira on 2026-10-06). | One story for each step. You name the epic. |
| Q9 | Who makes the k3d measurement of step 1? | Answered by the work of 2026-10-07: the session `design` started Docker on Victoria's word and made the runs for A1 and A3 (section 11). The run for A2 loads a probe image into the shared cluster, and it waits for her word. |
| Q10 | gRPC: the `address` of `AllBalances` carries the id? | Yes. It depends on assumption A2. |
| Q11 | A subscribe frame under a provider-wide `down`: (a) it behaves as every other WebSocket frame, which is row 6 of section 9.2, or (b) today's order stays, with one more hook? | (a). |
| Q12 | The gRPC key: (a) the bare method name, as today, and the simulator refuses two served methods of one name, or (b) the full name, `service/method`? | (a) now. (b) changes every gRPC row and every gRPC override of today. |
| Q13 | The order of the gRPC id: (a) in step 1, with REST, or (b) after step 2, as the first new row of the method table? | (a). Step 1 then measures assumption A2, the largest risk, first. The cost is about 15 lines that 2b replaces. |
| Q14 | Which abilities must each interface get next? | Decided by Victoria on 2026-10-07: the design must give a real down ("so you need to fix it in design"). Its form is decided too: the mode of the failover work, `mode="port_closed"` ("we already tested it and learned how to do it in failover"). Section 14.5 extends it to `http` and `ws` as step 4, with the pull requests 4a and 4b; its decision is decision 7 of ADR-001. No other "no" cell of section 14.4 is planned now. This question is closed. |
| Q15 | The glossary: the new entries "Request flow", "History row", "Request id" and "Ability". Does the class `Listener` need an entry? | Add the four entries. Add one for `Listener` that says how it differs from an endpoint. |

### 15.1 The state of each question on 2026-10-07

Victoria, 2026-10-07: "do not leave follow ups or unfinihsed verification or desisions", and "almost every your open question you can check in smart router automatin code or in smart router". So each question is closed here by her words or by evidence. A row that says "the design session" is my decision, and she can overrule it.

| # | Closed as | By what |
|---|---|---|
| Q1 | (a): keep `Listener`, and move gRPC and the WebSocket subscribe frames into it. | Victoria accepted the plan that rests on it: 2c after 2b ("so this is a plan, we will do it, add it ti the plan"). Two reviews found no reason against it. |
| Q2 | Differences 1, 2, 5 and 6 become uniform in 2c. Differences 3, 4, 7, 9, 10, 11 and 12 stay. | Victoria: "if there are test that can be easily adjusted we can do it". Section 9.4 has the tests of each row. |
| Q3 | The six changes of section 9.2 are accepted. | READ by two helpers: no simulator test and no automation test asserts a behaviour that one of the six removes. The proof by a run is the tests of 2a and 3a before and after the move. |
| Q4 | Yes: the query parameter `request_id`, and it wins over the header. | PROVEN BY A RUN (row A1). The router does not pass the header `X-Request-Id` (measured by 3800). |
| Q5 | No change to the four things. Every test must pass in each pull request. | Victoria: "we should verify we don't ruin them by changinf infra". READ by a helper (`helper-cache-and-resp-report.md`): the cache simulator and the RESP proxy import nothing from the listeners or from `fault_policy`. They share wiring with the code that 2b and 3b edit: `start`, `__init__`, the `/ready` read and the port lock in `server.py`. The tests that show a break of that wiring: `tests/test_resp_wiring.py`, `tests/test_resp_control_routes.py`, `tests/test_control_api_cache.py` and `tests/test_cache_sim_wire.py` in the simulator; in the automation repository the eight files of the cache simulator, the seven RESP files and `test_cache_sim_wiring.py`. A full run of the automation cache tests needs four shapes of the k3d cluster and one Docker run, so it is planned as its own run before 2b and before 3b merge. |
| Q6 | No step for GraphQL. | It is an interface on `http` and needs no change of the flow (review finding F3 of the first review). |
| Q7 | The documents are in git: commit on the branch `worktree-one-request-flow-design`, local. | Victoria's order of 2026-10-07. A push and a pull request need her go, and the probe commit stays out of it. |
| Q8 | One story for each pull request: 1, 2a, 2b, 2c, 3a, 3b, 4a, 4b. | The design session. A Jira write needs Victoria's go, so the stories are drafted when she says. |
| Q9 | Answered. | The runs of section 11. |
| Q10 | Yes: the `address` of `AllBalances` carries the id. | PROVEN BY A RUN (row A2). |
| Q11 | (a): a subscribe frame under a provider-wide `down` behaves as every other WebSocket frame. | The design session. READ by a helper: no simulator test and no automation test runs a subscribe frame under a provider-wide `down`. |
| Q12 | (a): the bare method name stays the gRPC key. | The design session. (b) changes every gRPC row and every gRPC override of today. |
| Q13 | (a): the gRPC id is in step 1, with REST. | The design session. Its risk was assumption A2, and A2 is proven. |
| Q14 | Closed: the real down is step 4, with `port_closed` on every transport. | Victoria's words (the header of this document). |
| Q15 | The four entries and the entry for `Listener` go into `CONTEXT.md` with pull request 1. | The design session. |

## 16. How to repeat what I ran

Run each command from the root of a checkout of the simulator, with its virtual environment.

**The counts of the topology and the provider with two addresses:**

```
python -c "
import collections
from provider_simulator.topology import TOPOLOGY
eps = [(r[0], r[2], i, t, p) for r in TOPOLOGY for (i, t, p) in r[6]]
print(len({r[0] for r in TOPOLOGY}), 'pools', len(TOPOLOGY), 'providers', len(eps), 'endpoints')
print(dict(collections.Counter(e[2] for e in eps)), dict(collections.Counter(e[3] for e in eps)))
two = collections.defaultdict(list)
for pool, pid, i, t, p in eps: two[(pool, pid, i, t)].append(p)
print({k: v for k, v in two.items() if len(v) > 1})
"
```

The reading of 2026-10-06: 36 pools, 146 providers, 156 endpoints. By interface: `jsonrpc` 114, `rest` 18, `tendermintrpc` 12, `grpc` 12. By transport: `http` 135, `ws` 9, `http2` 12.

**Row 1 of section 4.4, the latency and the filters:**

```
python -c "
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners import RawRequest, RestListener
from provider_simulator.listeners.grpc import GrpcListener
def measure(interface, transport, make, call, cfg):
    a, b = Endpoint(interface, transport, 40001), Endpoint(interface, transport, 40002)
    provider = Pool(name='lava-probe', chain='lava').add_provider('1', [a, b])
    provider.scenario.update(cfg)
    return {ep.port: call(make(provider, ep)) for ep in (a, b)}
rest = lambda l: l.serve(RawRequest(verb='GET', path='/cosmos/base/tendermint/v1beta1/blocks/latest')).latency_ms
grpc = lambda l: l.plan('GetLatestBlock').latency_ms
for cfg in ({'latency_ms': 500, 'ports': [40001]}, {'latency_ms': 500, 'transports': ['ws']}):
    print(cfg, 'REST', measure('rest', 'http', RestListener, rest, cfg), 'gRPC', measure('grpc', 'http2', GrpcListener, grpc, cfg))
"
```

**The counter, the header case, and row 6 of section 4.4:**

```
python -c "
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners import RawRequest, RestListener
from provider_simulator.listeners.grpc import GrpcListener
path = '/cosmos/base/tendermint/v1beta1/blocks/latest'
ep = Endpoint('rest', 'http', 40001)
p = Pool(name='lava-probe', chain='lava').add_provider('1', [ep])
l = RestListener(p, ep)
l.serve(RawRequest(verb='GET', path=path))
l.serve(RawRequest(verb='GET', path=path, headers={'X-Request-Id': '1'}))
l.serve(RawRequest(verb='GET', path=path, headers={'x-request-id': 'lower'}))
print('REST row ids:', [repr(r['request_id']) for r in p.log.get_history()])
p.scenario.update({'mode': 'rate_limit', 'corruption_mode': 'wrong_type'})
r = l.serve(RawRequest(verb='GET', path=path))
print('REST rate_limit with wrong_type:', r.status, r.corruption_mode)
g = Endpoint('grpc', 'http2', 40002)
pg = Pool(name='lava-probe-g', chain='lava').add_provider('1', [g])
pg.scenario.update({'mode': 'rate_limit', 'corruption_mode': 'wrong_type'})
print('gRPC rate_limit with wrong_type:', GrpcListener(pg, g).plan('GetLatestBlock').status_code)
"
```

The reading of 2026-10-07: the REST row ids are `1` (the counter), `'1'` (the header) and `2` (the counter again, for the header in lower case). REST gives 429 with the corruption `wrong_type` on the reply. gRPC gives `RESOURCE_EXHAUSTED`.

**Assumption A4, the generated registration.** A throwaway script of about 70 lines, not in the repository. It starts a `grpc.aio` server on a port that the system chooses, with a servicer class for `cosmos.bank.v1beta1.Query` that has the one method `AllBalances`, registered with `add_QueryServicer_to_server`. The reading of 2026-10-07, two runs, Python 3.12.12 and `grpcio` 1.81.1:

| Call | What the client reads |
|---|---|
| `AllBalances` with the address `lava1probe` | One coin, `ulava`, `1000000`. The servicer read the address. |
| `TotalSupply`, a method that is not served | `UNIMPLEMENTED`, `Unexpected <class 'NotImplementedError'>: Method not implemented!` |
| `AllBalances` with bytes that are not a message | `UNKNOWN`, `Unexpected <class 'google.protobuf.message.DecodeError'>: Error parsing message with type 'cosmos.bank.v1beta1.QueryAllBalancesRequest'`. The servicer is not called. |
| `NoSuchMethod` | `UNIMPLEMENTED`, `Method not found!` |

The reviewer's three scripts, `check_ws.py`, `check_grpc.py` and `check_ids.py`, are in the scratchpad of the session `reviewer`. That folder is temporary.

### 16.5 The runs of 2026-10-07 on the local k3d cluster

The cluster: `smart-router-local`, helm revision 15 of 2026-10-05, the router sets `default` and `failover`. Read from the pods: simulator commit `e80acbf`, router build `v1.5.8-85-g4369814`. The shell of the session has `KUBECONFIG` set to the tunnel of the canonical server. So each `kubectl` call names `--kubeconfig /Users/victoria/.config/k3d/kubeconfig-smart-router-local.yaml --context k3d-smart-router-local`, and each test run sets `CLUSTER_PROFILE=local`.

The scripts are in the evidence folder `/Users/victoria/smart_router_automation/handoffs/evidence-2026-10-07-one-request-flow-design/`.

| What | How | Result |
|---|---|---|
| A1, the REST query string | `curl "http://localhost:30003/cosmos/bank/v1beta1/balances/lava1designprobe?request_id=REQ-not-a-number&pagination.key=PROBE"`, and the same with the two parameters in the other order | HTTP 200, `"inbound_key": "PROBE"`. The rows got the counter ids 470 to 472. |
| A3, the spec lists `AllBalances` | From the root of a simulator checkout: `python a3_probe.py localhost:30004 lava1designprobeA3` | `GetLatestBlock`: OK. `AllBalances`: accepted by the router, six providers tried, then `Symbol not found: cosmos.bank.v1beta1.Query {descriptor-source:reflection}`. |
| What the router gets from a `down` provider | `POST http://localhost:31000/scenario` with `{"providers": {"eth-failover-prodlimits-sim:1": {"mode": "down"}, …:2, …:3}}`, one `net_version` to `http://localhost:30046`, the router pod's log for the `Lava-Guid`, then `POST /reset` with `{"pool": "eth-failover-prodlimits-sim"}` | Section 14.5. |
| A real close of an `http` port | From the root of a simulator checkout: `python real_down_http_probe.py`. Local ports only. | Section 14.5, the six results. |
| The simulator suite at `e80acbf` | `python -m pytest tests -q -p no:cacheprovider` | `1549 passed in 163.07s`. With the probe patch: `1549 passed in 160.33s`. |
| The probe patch for A1 and A2 | `probe-request-id.patch`: 32 lines added in `server.py`, `provider_simulator/listeners/grpc.py` and `provider_simulator/listeners/rest.py`. It is the last commit of the branch. Image `provider-simulator:probe-request-id`, built on the Mac with `docker build <worktree> -t provider-simulator:probe-request-id --build-arg SIM_GIT_COMMIT=<commit> --build-arg SIM_GIT_DESCRIBE=v1.5.5-4-ge80acbf-probe-request-id`. | Loaded at 22:15 after Victoria added two permission rules: `k3d image import provider-simulator:probe-request-id -c smart-router-local`, then `kubectl … set image deploy/provider-simulator provider-simulator=provider-simulator:probe-request-id`, a pairing reset on the 32 routers, the probes of section 11, and the way back with `…=ghcr.io/magma-devs/provider-simulator:main`. |
| The probe alone, with no router | The session `worker`, 22:28, from the worktree: a REST request in memory with `request_id`, one with none, and `AllBalances` straight to the gRPC port. The same script on the main checkout at `e80acbf` as the control. | `control-runs/1c-patched-simulator-alone.txt`: the row holds the text id; with no id it holds the counter 1; `AllBalances` OK and one row with the address. `control-runs/1c-control-unpatched-simulator-alone.txt`: the row holds the counter, `AllBalances` gives `UNIMPLEMENTED`, and the filter gives no row. So the result depends on the patch. |
| The ten test files | From the automation main checkout: `env CLUSTER_PROFILE=local KUBECONFIG=/Users/victoria/.config/k3d/kubeconfig-smart-router-local.yaml .venv/bin/python -m pytest <the ten files> --alluredir=<scratch> --junitxml=<scratch>/ten-tests.xml -p no:cacheprovider` | `ten-tests.xml` and `ten-tests.log` in the evidence folder: 88 tests, 30 passed, 6 xfail, 52 skipped. Only 12 of the 88 set `down`: the seven failover tests, the composite score test, three tests of the classifier file, and `test_ttl_expiry_returns_unknown[solana-sim-router]`. |
| The suite with the probe patch | `python -m pytest tests -q -p no:cacheprovider` in the worktree | `probe-suite.txt` in the evidence folder: `1549 passed in 160.33s`. It shows that nothing old broke. The rows above show that the probe stores the ids. |
| Reflection and the bank service | From the root of a simulator checkout: `python <evidence folder>/reflection_symbol_probe.py` | Three cases, section 11, row A3. |

## 17. Not checked

1. The automation tests, for a dependence on a behaviour that section 9.2 changes (assumption A5), beyond the default transports that the reviewer read.
2. Done on 2026-10-07: a real `AllBalances` request through the router (assumptions A2 and A3, section 11). Not checked for the real down of section 14.5: what the router does with an `http` provider that refuses the connection, and a `ws` endpoint that closes.
3. The moved gRPC registration on Linux (assumption A4).
4. A WebSocket request through the router.
5. The Tendermint RPC URL form through the router.
6. The cost of a `/history` read with no filter, over all providers.
7. The files in `docs/` other than `docs/using_the_simulator.md`, `docs/using_grpc.md` and line 116 of `docs/curl_reference.md`. `docs/using_grpc.md` names `handlers_grpc.py` and `ProviderState`, which do not exist.
8. The router's cache, for a REST request that differs only in `request_id`, and if the address of `AllBalances` changes the router's choice of a provider. READ in the `cache-tests` skill (`references/how-to-test-cache.md:245`): the cache key is the byte-identical request. So a request with a unique id is a new key each time, and one test cannot use a unique request id and a cache hit together. In the runs of section 11 the two REST requests with different ids went to two providers and neither was served from the cache. A run for the cache itself was not made.
9. The BTC, LN and Solana chains, past their reads of `responses`.
10. The two tests that read `k8s/service.yml` and `config/values_sim.yml` (section 14.1).

## 18. Version 2: what the review changed

The session `reviewer` sent 15 findings on 2026-10-07. Its report is in its scratchpad, which is temporary. I checked each finding against the code. All 15 hold.

| Finding | What it said | Where this document changed |
|---|---|---|
| F1 | A subscribe frame applies the per-method override before the `down` check, and `Listener.serve` checks `down` first. | Sections 4.2 and 4.4 (row 5), 8.4, 9.2 (row 6), Q11 |
| F2 | The socket tests that exist do not pin what steps 2 and 3 move. | The pull requests 2a and 3a: sections 1, 10, 11 and 12 |
| F3 | An interface on `http` is cheap today, so GraphQL is not a reason for steps 2 and 3. Four of the disagreements have a local fix. | Sections 1, 5 (goal 4), 14.1, Q1 (option c), ADR-001 |
| F4 | A step cannot always be reverted alone. | Sections 1, 10 and 11 |
| F5 | The WebSocket upgrade stays a second decision, and the adapter answers four protocol cases itself. | Sections 5 (goal 2), 8.1, 8.4, 9.3 (rows 8 to 12), ADR-001 |
| F6 | The list of differences was not complete. | Sections 4.4 (row 6), 8.3 (the hook `corrupt`), 9.3 (rows 4 and 5) |
| F7 | One sentence of section 9.2 was wrong for the gRPC row, and `http_status` has nothing to change on a frame. | Section 9.2 |
| F8 | No rule for a gRPC message that does not parse, and for the text of a method that is not served. | Section 8.3: the generated registration stays |
| F9 | The bare gRPC method name is not unique. | Section 8.2, Q12 |
| F10 | A caller id that is a plain number equals a counter value. | Sections 8.2 and 13, ADR-002 |
| F11 | A per-method `down` row has a request id. | Sections 6 and 8.2 |
| F12 | Eight small facts. | Sections 3, 4.2, 4.3, 4.5 and 8.2, and the header table |
| F13 | Steps 2 and 3 named no document to update, and one thread name that a test uses. | Sections 8.3 and 10 |
| F14 | Another order for the gRPC id. | Q13 |
| F15 | The decision records gave counts with no names. | Both decision records |

**Version 2.2, 2026-10-07.** Victoria's words: "so you need to fix it in design", "this desugn shold be verified 100%", and "all questin that you have you MUST verify by actual code and run". Four inputs changed the document: runs on the local k3d cluster by the sessions `design` and `worker`; the reports of read-only helpers (one on the skills, of which I checked six lines, and three on the tests); Victoria's decisions of that day; and the review of the session `reviewer-2`, 14 findings. The places that changed:

- Section 1 and the header: six pull requests; the decisions of 2026-10-07 in Victoria's words; what is her decision and what is my proposal.
- Section 2: three new labels for the kinds of run and read.
- Section 9.4, new: which tests of today fail if a difference of section 9.3 is made uniform.
- Section 10.5, new: the test adjustments that Victoria accepted, and pull request 2c.
- Section 11: A1, A2 and A3 are proven by a run, A3 with a control.
- Section 13: three skill pages change with 2b.
- Section 14.5: what `down` is for the router; the run of the ten test files; a proposal for a real down, the eleven points that it must also cover, and what is not verified.
- Section 15: Q9 and Q14. Section 16.5, new: the runs and their saved outputs. Section 17, item 2. Line 128 and the table of section 14.4.

Findings of `reviewer-2` that are fixed here: 1, 4, 5, 6, 7, 8, 9, 10, 11 and the items of 12 and 13 that the list above covers. Findings 2 and 3 are fixed too: the files that call `down` a refused connection are thirteen, and section 14.5 and part A of section 10.5 name the three that were missing and the three conditions.

**Version 2.1, 2026-10-07.** Questions of Victoria changed six places: the note under the hook table of section 8.3, row 2 of section 9.2 with the list of tests under the table, the first paragraphs of section 9.3, the assumptions of section 11, sections 14.3 to 14.5, and question Q14. Version 2 said in section 14.4 that each missing ability is "a hook or a field" after step 3. That was wrong for three abilities: `port_closed`, `pause_at` and subscriptions.
