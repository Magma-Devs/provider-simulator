# Provider Simulator — curl Reference

> Examples below use Victoria's domain. Replace `victoria.magmadevs.com` with the
> server's `BASE_DOMAIN` (set in `config/base-domain.env`).

```bash
# ── set once, use everywhere ────────────────────────────────────────────────
export SIM_CONTROL_URL="https://sim-control.victoria.magmadevs.com"
export SIM_ROUTER_URL="https://eth-sim-jsonrpc.victoria.magmadevs.com"

# direct provider ports — only reachable via port-forward (see bottom of this file)
export P1="http://localhost:18545"
export P2="http://localhost:18546"
export P3="http://localhost:18547"
```

---

## Control API

All endpoints live on `$SIM_CONTROL_URL` (port 19000).

```bash
# ── health check ─────────────────────────────────────────────────────────────
curl -si "$SIM_CONTROL_URL/health"
# → {"status": "ok"}

# ── current scenario config for all providers ────────────────────────────────
curl -s "$SIM_CONTROL_URL/scenario" | python3 -m json.tool
# → {"providers": {"1": {"mode":"success","latency_ms":0,"error_probability":0.0,
#                        "error_code":-32000,"error_message":"Internal error","http_status":200},
#                  "2": {...}, "3": {...}}}

# ── per-provider call counts + status breakdown ──────────────────────────────
curl -s "$SIM_CONTROL_URL/stats" | python3 -m json.tool
# → {"providers": {"1": {"total_requests_all_time":42,
#                        "requests_by_status_all_time":{"success":40,"error":2},
#                        "history_ring_buffer_entries":42},
#                  "2": {...}, "3": {...}}}

# ── reset scenario config only (mode/latency/responses → defaults, history kept)
curl -si -X POST "$SIM_CONTROL_URL/reset"

# ── clear call history and counters only (scenario config kept) ───────────────
curl -si -X POST "$SIM_CONTROL_URL/history/clear"

# ── reset everything — scenario config AND history ───────────────────────────
curl -si -X POST "$SIM_CONTROL_URL/reset/all"

# ── the same three, confined to ONE pool (one router's provider set) ─────────
# Without a pool the reset clears every pool, which is how one test's clean-up
# reaches into another router's providers. With a pool it touches that pool's
# providers only.
#
# Chain heights are different, and only /reset and /reset/all move them —
# /history/clear never does. A height is one value per chain shared by every
# pool on it, so a pool scope narrows which chains get rewound but a sibling
# pool on the same chain still sees it.
#
# The reply names the scope it actually cleared, so a caller can check rather
# than assume.
curl -si -X POST "$SIM_CONTROL_URL/reset/all" -H 'Content-Type: application/json' -d '{"pool":"btc-sim"}'
# → {"status":"scenario reset and history cleared","pool":"btc-sim",
#    "providers":["btc-sim:1","btc-sim:2","btc-sim:3"],"chains":["btc"]}

# A pool that does not exist is a 400 listing the pools that do — never a
# quiet success that clears nothing.
curl -si -X POST "$SIM_CONTROL_URL/reset" -H 'Content-Type: application/json' -d '{"pool":"eth-simm"}'
# → 400 {"error":"no pool 'eth-simm'; pools are ['btc-sim', 'eth-best-sim', ...]"}
```

---

## History

> **`call_order` field:** every entry includes a `call_order` integer (1-based).
> The list is sorted by `ts` (wall-clock arrival time) and then each entry is
> numbered sequentially, so `call_order: 1` is the provider the router hit
> **first**, `call_order: 2` is the second attempt, and so on.

### Two display styles

**With response headers** (use `-si` — useful for checking HTTP status codes):
```bash
curl -si "$SIM_CONTROL_URL/history?last=30"
```

**Pretty-printed JSON, no headers** (use `-s | python3 -m json.tool`):
```bash
curl -s "$SIM_CONTROL_URL/history?last=30" | python3 -m json.tool
```

> Both styles work for every endpoint. Use `-si` when you need headers;
> use `-s | python3 -m json.tool` for readable JSON in the terminal.

---

### Query parameters (all optional, combinable)

| Parameter | Type | Description |
|---|---|---|
| `last=<seconds>` | int | Calls in the last N seconds (shorthand for `from=now-N`) |
| `from=<unix_ts>` | float | Include only calls at or after this timestamp |
| `to=<unix_ts>` | float | Include only calls at or before this timestamp |
| `provider=<id>` | `1`\|`2`\|`3` | Filter to a single provider |
| `method=<name>` | string | Filter to a specific RPC method name |
| `status=<name>` | `success`\|`error`\|`rate_limit`\|`down` | Filter by outcome |
| `request_id=<id>` | int | Filter by the JSON-RPC `id` field echoed in the request |
| `lava_header_<name>=<value>` | string | Filter by a captured Lava header. Underscores in `<name>` become hyphens — e.g. `lava_header_lava_stateful_api=true` matches header `lava-stateful-api: true`. Multiple `lava_header_*` filters AND together. |

---

### Isolating history for one specific request

The simplest 100% correct approach — reset first so history is empty, then query with no filters:

```bash
# 1. wipe history only (leaves scenario config untouched)
curl -s -X POST "$SIM_CONTROL_URL/history/clear"

# 2. send your request
curl -si -X POST "$SIM_ROUTER_URL" \
  -H "Content-Type: application/json" \
  -H "lava-force-cache-refresh: true" \
  -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'

# 3. read history — no filters needed, only your request is in there
curl -s "$SIM_CONTROL_URL/history" | python3 -m json.tool
```

> **Why not filter by method or timestamp?**
> The Lava rpcconsumer makes continuous background calls to providers (sync scoring,
> pruning verification) — so in any live window you will see many entries you did not
> send. Resetting before your request is the only approach that is always correct.

---

### History examples

```bash
# last 30 seconds
curl -s "$SIM_CONTROL_URL/history?last=30" | python3 -m json.tool

# specific time window
curl -s "$SIM_CONTROL_URL/history?from=1774534600&to=1774534700" | python3 -m json.tool

# only errors on provider 2 in the last 2 minutes
curl -s "$SIM_CONTROL_URL/history?last=120&provider=2&status=error" | python3 -m json.tool

# all calls for a specific method
curl -s "$SIM_CONTROL_URL/history?method=eth_getBlockByNumber" | python3 -m json.tool

# only calls matching a specific JSON-RPC id
curl -s "$SIM_CONTROL_URL/history?request_id=1" | python3 -m json.tool

# all calls from provider 1 that succeeded
curl -s "$SIM_CONTROL_URL/history?provider=1&status=success" | python3 -m json.tool
```

#### Example response

```json
{
    "count": 3,
    "history": [
        { "call_order": 1, "provider": "1", "method": "eth_blockNumber", "status": "rate_limit", "request_id": 1, "ts": 1743300001.164, "time": "2026-03-30 10:12:40.164 UTC", "latency_ms": 2 },
        { "call_order": 2, "provider": "2", "method": "eth_blockNumber", "status": "down",       "request_id": null, "ts": 1743300001.331, "time": "2026-03-30 10:12:40.331 UTC", "latency_ms": 0 },
        { "call_order": 3, "provider": "3", "method": "eth_blockNumber", "status": "success",    "request_id": 1, "ts": 1743300001.512, "time": "2026-03-30 10:12:40.512 UTC", "latency_ms": 8 }
    ]
}
```

---

## Scenario control

### Scenario field reference

| Field | Type | Default | Description |
|---|---|---|---|
| `mode` | `success`\|`error`\|`rate_limit`\|`down` | `"success"` | Primary behaviour — see modes table below |
| `latency_ms` | int | `0` | Milliseconds to sleep before responding (0 = no delay) |
| `error_probability` | float 0.0–1.0 | `0.0` | Fraction of requests that randomly return an error (`mode` must be `"success"`) |
| `error_code` | int | `-32000` | JSON-RPC error code returned when mode is `error` or error_probability fires |
| `error_message` | string | `"Internal error"` | JSON-RPC error message returned with the error |
| `http_status` | int | `200` | HTTP status code for error responses (`200` = error in JSON-RPC body; use `400`/`500` etc. for HTTP-level errors) |
| `responses` | `{method: {result: ...}}` | `{}` | Per-method result overrides (see Per-method override below) |

### Provider modes

| Mode | HTTP status | Response body | Use case |
|---|---|---|---|
| `success` | 200 | `{"result": <stub>}` | Normal operation |
| `error` | `http_status` (default 200) | `{"error": {"code": error_code, "message": error_message}}` | Forced JSON-RPC error |
| `rate_limit` | 429 | `{"error": {"code": 429, "message": "Too many requests"}}` | Throttling simulation |
| `down` | 503 | _(empty body)_ | Complete outage — router skips this provider |

---

### Examples

```bash
# ── single provider down ──────────────────────────────────────────────────────
curl -si -X POST "$SIM_CONTROL_URL/scenario" \
  -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"chain_family":"eth","mode":"down"}}}'

# ── two providers down ────────────────────────────────────────────────────────
curl -si -X POST "$SIM_CONTROL_URL/scenario" \
  -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"chain_family":"eth","mode":"down"},"2":{"chain_family":"eth","mode":"down"}}}'

# ── provider 1 rate-limited ───────────────────────────────────────────────────
curl -si -X POST "$SIM_CONTROL_URL/scenario" \
  -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"chain_family":"eth","mode":"rate_limit"}}}'

# ── 200 ms latency on provider 2 (no errors) ─────────────────────────────────
curl -si -X POST "$SIM_CONTROL_URL/scenario" \
  -H "Content-Type: application/json" \
  -d '{"providers":{"2":{"chain_family":"eth","mode":"success","latency_ms":200}}}'

# ── 40 % random error rate on provider 1 ─────────────────────────────────────
# error_probability fires on top of mode=success; keep mode=success here
curl -si -X POST "$SIM_CONTROL_URL/scenario" \
  -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"chain_family":"eth","mode":"success","error_probability":0.4}}}'

# ── forced error with custom code and message ─────────────────────────────────
curl -si -X POST "$SIM_CONTROL_URL/scenario" \
  -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"chain_family":"eth","mode":"error","error_code":-32601,"error_message":"Method not found"}}}'

# ── forced error returned as HTTP 500 (not just JSON-RPC body error) ──────────
curl -si -X POST "$SIM_CONTROL_URL/scenario" \
  -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"chain_family":"eth","mode":"error","error_code":-32000,"http_status":500}}}'

# ── reset all providers back to healthy defaults ──────────────────────────────
curl -si -X POST "$SIM_CONTROL_URL/reset"
```

---

### Per-method response override

Override the stub result for a specific method on a specific provider.
All other methods continue to return their default stubs.

```bash
# make provider 1 return a custom block number for eth_blockNumber
curl -si -X POST "$SIM_CONTROL_URL/scenario" \
  -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"chain_family":"eth","responses":{"eth_blockNumber":{"result":"0xdeadbeef"}}}}}'

# make provider 2 return empty logs for eth_getLogs
curl -si -X POST "$SIM_CONTROL_URL/scenario" \
  -H "Content-Type: application/json" \
  -d '{"providers":{"2":{"chain_family":"eth","responses":{"eth_getLogs":{"result":[]}}}}}'

# override eth_getBalance to return a non-zero value on provider 3
curl -si -X POST "$SIM_CONTROL_URL/scenario" \
  -H "Content-Type: application/json" \
  -d '{"providers":{"3":{"chain_family":"eth","responses":{"eth_getBalance":{"result":"0x1bc16d674ec80000"}}}}}'

# clear all overrides (reset responses to defaults) without touching mode/latency
curl -si -X POST "$SIM_CONTROL_URL/scenario" \
  -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"chain_family":"eth","responses":{}},"2":{"chain_family":"eth","responses":{}},"3":{"chain_family":"eth","responses":{}}}}'
```

---

## Common test recipes

Each recipe shows setup → trigger → verify.

### Router failover — one provider down

```bash
# setup: provider 1 is down
curl -s -X POST "$SIM_CONTROL_URL/reset/all"
curl -s -X POST "$SIM_CONTROL_URL/scenario" \
  -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"chain_family":"eth","mode":"down"}}}'

# trigger
curl -si -X POST "$SIM_ROUTER_URL" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'

# verify: call_order=1 → provider 1 down, call_order=2 → success on 2 or 3
curl -s "$SIM_CONTROL_URL/history" | python3 -m json.tool
```

### Router failover — two providers down (only provider 3 healthy)

```bash
curl -s -X POST "$SIM_CONTROL_URL/reset/all"
curl -s -X POST "$SIM_CONTROL_URL/scenario" \
  -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"chain_family":"eth","mode":"down"},"2":{"chain_family":"eth","mode":"down"}}}'
```

### Rate-limit routing

```bash
curl -s -X POST "$SIM_CONTROL_URL/reset/all"
curl -s -X POST "$SIM_CONTROL_URL/scenario" \
  -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"chain_family":"eth","mode":"rate_limit"},"2":{"chain_family":"eth","mode":"rate_limit"}}}'
```

### Latency test

```bash
curl -s -X POST "$SIM_CONTROL_URL/reset/all"
curl -s -X POST "$SIM_CONTROL_URL/scenario" \
  -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"chain_family":"eth","latency_ms":500},"2":{"chain_family":"eth","latency_ms":1000}}}'
```

### All providers healthy (clean slate)

```bash
curl -s -X POST "$SIM_CONTROL_URL/reset/all"
# verify
curl -s "$SIM_CONTROL_URL/scenario" | python3 -m json.tool
```

---

## Supported JSON-RPC methods

All calls go to `$SIM_ROUTER_URL` (or directly to `$P1` / `$P2` / `$P3` via port-forward).

### eth — chain state
```bash
curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_chainId","params":[],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_protocolVersion","params":[],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_syncing","params":[],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_coinbase","params":[],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_mining","params":[],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_hashrate","params":[],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_accounts","params":[],"id":1}'
```

### eth — gas / fees
```bash
curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_gasPrice","params":[],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_maxPriorityFeePerGas","params":[],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_feeHistory","params":["0x4","latest",[25,75]],"id":1}'
```

### eth — state queries
```bash
curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getBalance","params":["0x0000000000000000000000000000000000000000","latest"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getCode","params":["0x0000000000000000000000000000000000000000","latest"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getStorageAt","params":["0x0000000000000000000000000000000000000000","0x0","latest"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getTransactionCount","params":["0x0000000000000000000000000000000000000000","latest"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_call","params":[{"to":"0x0000000000000000000000000000000000000000","data":"0x"},"latest"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_estimateGas","params":[{"to":"0x0000000000000000000000000000000000000000","data":"0x"}],"id":1}'
```

### eth — blocks
```bash
curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getBlockByNumber","params":["latest",false],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getBlockByHash","params":["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",false],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getBlockTransactionCountByNumber","params":["latest"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getBlockTransactionCountByHash","params":["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getUncleCountByBlockNumber","params":["latest"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getUncleCountByBlockHash","params":["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getUncleByBlockNumberAndIndex","params":["latest","0x0"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getUncleByBlockHashAndIndex","params":["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","0x0"],"id":1}'
```

### eth — transactions
```bash
curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getTransactionByHash","params":["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getTransactionByBlockNumberAndIndex","params":["latest","0x0"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getTransactionByBlockHashAndIndex","params":["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","0x0"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getTransactionReceipt","params":["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_sendRawTransaction","params":["0x"],"id":1}'
```

### eth — logs / filters
```bash
curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getLogs","params":[{"fromBlock":"latest","toBlock":"latest"}],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_newFilter","params":[{"fromBlock":"latest","toBlock":"latest"}],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_newBlockFilter","params":[],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_newPendingTransactionFilter","params":[],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getFilterChanges","params":["0x1"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getFilterLogs","params":["0x1"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_uninstallFilter","params":["0x1"],"id":1}'
```

### net
```bash
curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"net_version","params":[],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"net_listening","params":[],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"net_peerCount","params":[],"id":1}'
```

### web3
```bash
curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"web3_clientVersion","params":[],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"web3_sha3","params":["0x68656c6c6f"],"id":1}'
```

### trace (addon)
```bash
curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_block","params":["latest"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_transaction","params":["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_get","params":["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",["0x0"]],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_call","params":[{"to":"0x0000000000000000000000000000000000000000","data":"0x"},["trace"],"latest"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_callMany","params":[],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_rawTransaction","params":["0x",["trace"]],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_replayTransaction","params":["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",["trace"]],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_replayBlockTransactions","params":["latest",["trace"]],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_filter","params":[{"fromBlock":"latest","toBlock":"latest"}],"id":1}'
```

### debug (addon)
```bash
curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"debug_traceTransaction","params":["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"debug_traceBlockByNumber","params":["latest"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"debug_traceBlockByHash","params":["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"debug_traceCall","params":[{"to":"0x0000000000000000000000000000000000000000","data":"0x"},"latest"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"debug_getRawBlock","params":["latest"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"debug_getRawHeader","params":["latest"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"debug_getRawReceipts","params":["latest"],"id":1}'

curl -si -X POST "$SIM_ROUTER_URL" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"debug_getRawTransaction","params":["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],"id":1}'
```

---

## Direct provider access (port-forward)

Bypass the router and hit a specific provider pod directly.
Requires an active `kubectl port-forward` session (see `kubectl_reference.md`).

```bash
# start port-forward in one terminal (forwards all provider ports + control)
kubectl port-forward -n lava-infra svc/provider-simulator \
  18545:18545 18546:18546 18547:18547 19000:19000

# in another terminal — set local env vars
export P1="http://localhost:18545"
export P2="http://localhost:18546"
export P3="http://localhost:18547"
export SIM_CONTROL_URL="http://localhost:19000"

# hit provider 1 directly (bypasses router scoring/load-balancing)
curl -si -X POST "$P1" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'

# set provider 2 to error mode and test it directly
curl -si -X POST "http://localhost:19000/scenario" \
  -H "Content-Type: application/json" \
  -d '{"providers":{"2":{"chain_family":"eth","mode":"error","error_code":-32000}}}'

curl -si -X POST "$P2" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'
# → HTTP 200, body: {"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"Internal error"}}

# confirm via history
curl -s "http://localhost:19000/history?provider=2" | python3 -m json.tool
```
