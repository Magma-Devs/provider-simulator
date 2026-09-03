# Using the Provider Simulator

This doc covers **how to drive the simulator once it's deployed**. For first-time deploy / verification, see [`new_server_setup.md`](new_server_setup.md). For the exhaustive JSON-RPC curl catalogue, see [`curl_reference.md`](curl_reference.md). For the gRPC quickstart, see [`using_grpc.md`](using_grpc.md).

## URLs

```bash
# replace with the BASE_DOMAIN of your server (config/base-domain.env)
export SIM_CONTROL_URL="https://sim-control.victoria.magmadevs.com"
export SIM_ROUTER_URL="https://eth-sim-jsonrpc.victoria.magmadevs.com"
export SIM_GRPC_URL="lava-sim-grpc.victoria.magmadevs.com:443"
```

Local (port-forwarded) equivalents:

```bash
export SIM_CONTROL_URL="http://localhost:19000"
# Primary tier (pids 1-3, shared ProviderState across surfaces):
#   JSON-RPC      : 18545 / 18546 / 18547
#   gRPC          : 18548 / 18549 / 18550
#   REST          : 18551 / 18552 / 18553
#   Tendermint-RPC: 18554 / 18555 / 18556
#   WebSocket     : 18557 / 18558 / 18559
# Backup tier (distinct pid per surface, independent ProviderState):
#   JSON-RPC      : pids  4- 6 → 18560 / 18561 / 18562
#   gRPC          : pids  7- 9 → 18563 / 18564 / 18565
#   REST          : pids 10-12 → 18566 / 18567 / 18568
#   Tendermint-RPC: pids 13-15 → 18569 / 18570 / 18571
#   WebSocket     : pids 16-18 → 18572 / 18573 / 18574
```

## Primary vs backup pools

Primary provider ids `1-3` are wired to the smart-router as **primary** providers across every surface (one `ProviderState` per pid backs JSON-RPC + gRPC + REST + TM + WS at the same time — a `/scenario` POST on pid `1` reconfigures every primary transport). Backup pids `4-18` are wired with `is_backup: true` and form **per-surface backup pools** — each surface gets its own pid range and its own independent `ProviderState` per pid so the backup tiers can be configured without colliding with each other or with the primary.

| Surface | Primary pids | Primary ports | Backup pids | Backup ports |
|---|---|---|---|---|
| JSON-RPC | 1, 2, 3 | 18545-18547 | 4, 5, 6 | 18560-18562 |
| gRPC | 1, 2, 3 | 18548-18550 | 7, 8, 9 | 18563-18565 |
| REST | 1, 2, 3 | 18551-18553 | 10, 11, 12 | 18566-18568 |
| Tendermint-RPC | 1, 2, 3 | 18554-18556 | 13, 14, 15 | 18569-18571 |
| WebSocket | 1, 2, 3 | 18557-18559 | 16, 17, 18 | 18572-18574 |

From the simulator's point of view every pool is identical — same handler module per surface, same `ProviderState` shape, same `/scenario` payload shape. Tier is a router-side concept: the smart-router consults a surface's backup pool only after that surface's primary pool is exhausted on a given request (`PairingListEmptyError` → backup fallback in `consumer_session_manager.go:826`). The simulator's job is to expose listeners that the router-side `is_backup: true` flag in `values_sim.yml` can route to.

This means `sim_control.set_scenario({"4": {"mode": "down"}, ...})` works exactly the same way as for primaries — set fault modes per backup pid to drive that surface's backup-tier resilience tests.

```bash
# all JSON-RPC primaries down, all JSON-RPC backups healthy
# → drives a JSON-RPC backup-tier activation
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"chain_family":"eth","mode":"down"},"2":{"chain_family":"eth","mode":"down"},"3":{"chain_family":"eth","mode":"down"},
                    "4":{"chain_family":"eth","mode":"success"},"5":{"chain_family":"eth","mode":"success"},"6":{"chain_family":"eth","mode":"success"}}}'

# gRPC-specific backup activation — primaries down on pid 1 (which also takes
# down the JSON-RPC/REST/TM/WS primaries on pid 1 because they share state),
# backup gRPC pool (pids 7-9) responds healthy.
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"chain_family":"eth","mode":"down"},"2":{"chain_family":"eth","mode":"down"},"3":{"chain_family":"eth","mode":"down"},
                    "7":{"chain_family":"grpc","mode":"success"},"8":{"chain_family":"grpc","mode":"success"},"9":{"chain_family":"grpc","mode":"success"}}}'

# REST-only backup activation (pids 10-12).
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" \
  -d '{"providers":{"10":{"chain_family":"rest","mode":"success"},"11":{"chain_family":"rest","mode":"success"},"12":{"chain_family":"rest","mode":"success"}}}'
```

## Set a scenario

`POST /scenario` reconfigures one or more providers. Only the fields you send are updated; everything else is preserved. The same call works whether the provider is serving JSON-RPC (ETH/BTC) or gRPC — `chain_family` selects the success-branch handler.

### Minimal — one provider down

```bash
curl -si -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"chain_family":"eth","mode":"down"}}}'
```

### Full body shape

```json
{
  "providers": {
    "1": {
      "chain_family": "eth",
      "mode": "success",
      "latency_ms": 0,
      "error_probability": 0.0,
      "error_code": -32000,
      "error_message": "Internal error",
      "http_status": 200,
      "corruption_mode": null,
      "missing_field": null,
      "blocks_behind": 0,
      "drop_at": "before_headers",
      "responses": { "eth_blockNumber": {"result": "0xdeadbeef"} }
    },
    "2": { "chain_family": "btc", "mode": "success" },
    "3": { "chain_family": "grpc", "mode": "rate_limit" }
  }
}
```

Every field is optional. Defaults match `ProviderState.reset_scenario()`.

## Reset state

Three reset endpoints — pick based on what you need to clear:

| Endpoint | Scenario config | History + counters |
|---|---|---|
| `POST /reset` | reset to defaults | preserved |
| `POST /history/clear` | preserved | cleared |
| `POST /reset/all` | reset to defaults | cleared |

```bash
# typical between-test cleanup
curl -s -X POST "$SIM_CONTROL_URL/reset/all"
```

> The smart-router exposes its own QoS-score reset on the debug domain (`POST https://debug.<BASE_DOMAIN>/debug/reset-scores`). The two are independent — reset both between simulator tests for proper isolation.

## Read history

`GET /history` returns the merged, time-sorted ring buffer across all providers. Every entry carries `call_order` (1 = first attempted), `correlation_group` (groups calls within a 50ms window with the same request id + method), and `lava_headers`.

```bash
# clean isolation pattern — clear, fire, read
curl -s -X POST "$SIM_CONTROL_URL/history/clear"
curl -s -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'
curl -s "$SIM_CONTROL_URL/history" | python3 -m json.tool
```

Filters (all combinable):

```bash
curl -s "$SIM_CONTROL_URL/history?last=60"
curl -s "$SIM_CONTROL_URL/history?provider=2&status=error"
curl -s "$SIM_CONTROL_URL/history?method=eth_getBlockByNumber"
curl -s "$SIM_CONTROL_URL/history?request_id=1"
curl -s "$SIM_CONTROL_URL/history?last=120&lava_header_lava_stateful_api=true"
```

For the full filter catalogue and example responses, see [`curl_reference.md`](curl_reference.md#history).

## Read aggregate stats

```bash
curl -s "$SIM_CONTROL_URL/stats" | python3 -m json.tool
# → per-provider total_calls + calls_by_status (all-time, survives ring-buffer rollover)
```

## Fault primitives across chain families

The 6 primitives below apply to every provider regardless of `chain_family`. The JSON-RPC handler dispatches via `JSONRPCHandler.do_POST`; the gRPC handler dispatches via `_apply_grpc_fault` in `handlers_grpc.py`. Behaviour is parallel by design.

| Primitive | JSON-RPC effect | gRPC effect |
|---|---|---|
| `mode=down` | HTTP 503, body never parsed | abort `UNAVAILABLE` |
| `mode=hang` | accept request, sleep 30s, close | accept, `await asyncio.sleep(30)`, abort `CANCELLED` |
| `mode=drop_connection` | close socket at `drop_at` point | empty initial metadata then abort `UNAVAILABLE` (unary collapses `mid_body` to `after_headers`) |
| `mode=rate_limit` | HTTP 429 + JSON-RPC error body | abort `RESOURCE_EXHAUSTED` |
| `mode=error` | `http_status` + JSON-RPC error body using `error_code` / `error_message` | abort with `grpc.StatusCode` matched from `error_message` (symbolic name) or `error_code` (int) — falls back to `UNKNOWN` |
| `corruption_mode` | byte-/structural-level corruption of the JSON body (truncated / invalid_json / empty_response / missing_field / wrong_type / null_body — the whole body becomes the JSON literal `null`) | `missing_field` clears the proto field; `truncated` / `empty_response` / `invalid_proto` / `null_body` abort `UNKNOWN`; `wrong_type` aborts `INTERNAL` |
| `blocks_behind` | shifts `eth_blockNumber` head and named-tag block numbers | decrements `block.header.height` in `GetLatestBlockResponse` |
| `latency_ms` | `time.sleep` before responding | `await asyncio.sleep` before responding |
| `error_probability` | random `mode=error` per request | random gRPC abort per request |

The fault ladder is evaluated in the order above (first match wins). Only `latency_ms` does not short-circuit — it sleeps and then continues to the next branch.

## Common recipes

### Failover — one provider down

```bash
curl -s -X POST "$SIM_CONTROL_URL/reset/all"
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"chain_family":"eth","mode":"down"}}}'
curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'
curl -s "$SIM_CONTROL_URL/history" | python3 -m json.tool
```

### Two providers down — only provider 3 healthy

```bash
curl -s -X POST "$SIM_CONTROL_URL/reset/all"
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"chain_family":"eth","mode":"down"},"2":{"chain_family":"eth","mode":"down"}}}'
```

### Mixed chain families on the same pod

```bash
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"chain_family":"eth"},"2":{"chain_family":"btc"},"3":{"chain_family":"grpc"}}}'
```

The JSON-RPC handler on port 18545 will dispatch ETH; port 18546 will dispatch BTC; the gRPC servicer on 18550 will serve Cosmos. The same `ProviderState` row drives both transports — fault primitives set on `"3"` apply to both port 18547 (JSON-RPC) and port 18550 (gRPC), but only the gRPC port serves Cosmos traffic for that provider.

### Forced error with a custom code

```bash
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"chain_family":"eth","mode":"error","error_code":-32601,"error_message":"Method not found"}}}'
```

### 40% random errors

```bash
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"chain_family":"eth","mode":"success","error_probability":0.4}}}'
```

### Per-method response override

```bash
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"chain_family":"eth","responses":{"eth_blockNumber":{"result":"0xdeadbeef"}}}}}'
```

### Per-method error override (named catalogue)

`stubs.ERROR_STUBS` keeps a single named-error catalogue. Use `error_stub` to inject one without re-typing the envelope:

```bash
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"chain_family":"eth","responses":{"eth_call":{"error_stub":"revert"}}}}}'
```

## REST surface (planned, not yet on develop)

`chain_family="rest"` and REST listener ports `18551` / `18552` / `18553` are reserved in `constants.py` and `k8s/service.yml` but the listeners are not bound on develop yet. MAG-1777 (REST sim) merged on a feature branch and is staged to land — this doc will pick up REST recipes once it does.

## Backup-tier listeners per surface

Each surface boots an additional pool of listeners on dedicated ports above the JSON-RPC backup at 18560-18562. The handler binding is identical to the matching primary — only the router-side `is_backup: true` flag in `values_sim.yml` and the pid (which selects a distinct `ProviderState`) differ:

```
Surface         Primary range   Backup range   Backup pids
─────────────   ─────────────   ─────────────  ───────────
JSON-RPC        18545-18547     18560-18562    4, 5, 6
gRPC            18548-18550     18563-18565    7, 8, 9
REST            18551-18553     18566-18568    10, 11, 12
Tendermint-RPC  18554-18556     18569-18571    13, 14, 15
WebSocket       18557-18559     18572-18574    16, 17, 18
```

Per-surface backups give each tier its own pid range so a `/scenario` POST can address a single surface's backup pool without accidentally configuring another surface's backup. The primary tier remains pid-shared across all surfaces (pid `1` reconfigures every primary at once), but every backup pid maps to exactly one surface's backup listener.
