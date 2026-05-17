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
# JSON-RPC providers (primary tier — ids 1-3): 18545 / 18546 / 18547
# JSON-RPC providers (backup tier  — ids 4-6): 18560 / 18561 / 18562
# gRPC providers                              : 18548 / 18549 / 18550
```

## Primary vs backup pools

Provider ids `1-3` are wired to the smart-router as **primary** providers; ids `4-6` are wired with `is_backup: true` and form the **backup pool**. From the simulator's point of view both pools are identical — same `JSONRPCHandler`, same `ProviderState`, same `/scenario` payload shape. Tier is a router-side concept: the smart-router consults the backup pool only after the primary pool is exhausted on a given request (`PairingListEmptyError` → backup fallback in `consumer_session_manager.go:826`).

This means `sim_control.set_scenario({4: "down", 5: "success", 6: "success"})` works exactly the same way as for primaries — set fault modes per backup id to drive backup-tier resilience tests.

```bash
# all primaries down, all backups healthy — drives a backup-tier activation
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"mode":"down"},"2":{"mode":"down"},"3":{"mode":"down"},
                    "4":{"mode":"success"},"5":{"mode":"success"},"6":{"mode":"success"}}}'
```

## Set a scenario

`POST /scenario` reconfigures one or more providers. Only the fields you send are updated; everything else is preserved. The same call works whether the provider is serving JSON-RPC (ETH/BTC) or gRPC — `chain_family` selects the success-branch handler.

### Minimal — one provider down

```bash
curl -si -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"mode":"down"}}}'
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
| `corruption_mode` | byte-/structural-level corruption of the JSON body (truncated / invalid_json / empty_response / missing_field / wrong_type) | `missing_field` clears the proto field; `truncated` / `empty_response` / `invalid_proto` abort `UNKNOWN`; `wrong_type` aborts `INTERNAL` |
| `blocks_behind` | shifts `eth_blockNumber` head and named-tag block numbers | decrements `block.header.height` in `GetLatestBlockResponse` |
| `latency_ms` | `time.sleep` before responding | `await asyncio.sleep` before responding |
| `error_probability` | random `mode=error` per request | random gRPC abort per request |

The fault ladder is evaluated in the order above (first match wins). Only `latency_ms` does not short-circuit — it sleeps and then continues to the next branch.

## Common recipes

### Failover — one provider down

```bash
curl -s -X POST "$SIM_CONTROL_URL/reset/all"
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"mode":"down"}}}'
curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'
curl -s "$SIM_CONTROL_URL/history" | python3 -m json.tool
```

### Two providers down — only provider 3 healthy

```bash
curl -s -X POST "$SIM_CONTROL_URL/reset/all"
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"mode":"down"},"2":{"mode":"down"}}}'
```

### Mixed chain families on the same pod

```bash
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"chain_family":"eth"},"2":{"chain_family":"btc"},"3":{"chain_family":"grpc"}}}'
```

The JSON-RPC handler on port 18545 will dispatch ETH; port 18546 will dispatch BTC; the gRPC servicer on 18550 will serve Cosmos. The same `ProviderState` row drives both transports — fault primitives set on `"3"` apply to both port 18547 (JSON-RPC) and port 18550 (gRPC), but only the gRPC port serves Cosmos traffic for that provider.

### Forced error with a custom code

```bash
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"mode":"error","error_code":-32601,"error_message":"Method not found"}}}'
```

### 40% random errors

```bash
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"mode":"success","error_probability":0.4}}}'
```

### Per-method response override

```bash
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"responses":{"eth_blockNumber":{"result":"0xdeadbeef"}}}}}'
```

### Per-method error override (named catalogue)

`stubs.ERROR_STUBS` keeps a single named-error catalogue. Use `error_stub` to inject one without re-typing the envelope:

```bash
curl -s -X POST "$SIM_CONTROL_URL/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"responses":{"eth_call":{"error_stub":"revert"}}}}}'
```

## REST surface (planned, not yet on develop)

`chain_family="rest"` and REST listener ports `18551` / `18552` / `18553` are reserved in `constants.py` and `k8s/service.yml` but the listeners are not bound on develop yet. MAG-1777 (REST sim) merged on a feature branch and is staged to land — this doc will pick up REST recipes once it does.
