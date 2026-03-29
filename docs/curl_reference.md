# Provider Simulator — curl reference

## Control API

```bash
# health check
curl -i https://sim-control.victoria.magmadevs.com/health

# current scenario state for all providers
curl -si https://sim-control.victoria.magmadevs.com/scenario | python3 -m json.tool

# reset all providers to defaults
curl -si -X POST https://sim-control.victoria.magmadevs.com/reset | python3 -m json.tool

# per-provider call counts + status breakdown
curl -si https://sim-control.victoria.magmadevs.com/stats | python3 -m json.tool
```

---

## History

> **Note on ordering:** history is sorted by `ts` (wall-clock arrival time).
> Because each provider is a separate server, if the router queries them
> sequentially the order naturally reflects which provider was tried first —
> the earliest `ts` was attempted first. There is no explicit "attempt #"
> field; order is inferred from time.

```bash
# last 30 seconds
curl -si "https://sim-control.victoria.magmadevs.com/history?last=30" | python3 -m json.tool

# specific time window
curl -si "https://sim-control.victoria.magmadevs.com/history?from=1774534600&to=1774534700" | python3 -m json.tool

# only errors on provider 2 in the last 2 minutes
curl -si "https://sim-control.victoria.magmadevs.com/history?last=120&provider=2&status=error" | python3 -m json.tool

# all calls for a specific method
curl -si "https://sim-control.victoria.magmadevs.com/history?method=eth_getBlockByNumber" | python3 -m json.tool
```

---

## Scenario control

```bash
# set provider 1 → rate_limit, provider 2 → down
curl -si -X POST https://sim-control.victoria.magmadevs.com/scenario \
  -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"mode":"rate_limit"},"2":{"mode":"down"}}}' | python3 -m json.tool

# set provider 1 → 40 % error probability, 200 ms latency
curl -si -X POST https://sim-control.victoria.magmadevs.com/scenario \
  -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"mode":"error_probability","error_probability":0.4,"latency_ms":200}}}' | python3 -m json.tool
```

---

## Supported JSON-RPC methods

Base URL (provider 1/2/3 on ports 18545/18546/18547 internally, exposed via):
```
https://eth-sim-jsonrpc.victoria.magmadevs.com
```

### eth — chain state
```bash
curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_chainId","params":[],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_protocolVersion","params":[],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_syncing","params":[],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_coinbase","params":[],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_mining","params":[],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_hashrate","params":[],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_accounts","params":[],"id":1}' | python3 -m json.tool
```

### eth — gas / fees
```bash
curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_gasPrice","params":[],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_maxPriorityFeePerGas","params":[],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_feeHistory","params":["0x4","latest",[25,75]],"id":1}' | python3 -m json.tool
```

### eth — state queries
```bash
curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getBalance","params":["0x0000000000000000000000000000000000000000","latest"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getCode","params":["0x0000000000000000000000000000000000000000","latest"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getStorageAt","params":["0x0000000000000000000000000000000000000000","0x0","latest"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getTransactionCount","params":["0x0000000000000000000000000000000000000000","latest"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_call","params":[{"to":"0x0000000000000000000000000000000000000000","data":"0x"},"latest"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_estimateGas","params":[{"to":"0x0000000000000000000000000000000000000000","data":"0x"}],"id":1}' | python3 -m json.tool
```

### eth — blocks
```bash
curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getBlockByNumber","params":["latest",false],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getBlockByHash","params":["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",false],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getBlockTransactionCountByNumber","params":["latest"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getBlockTransactionCountByHash","params":["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getUncleCountByBlockNumber","params":["latest"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getUncleCountByBlockHash","params":["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getUncleByBlockNumberAndIndex","params":["latest","0x0"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getUncleByBlockHashAndIndex","params":["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","0x0"],"id":1}' | python3 -m json.tool
```

### eth — transactions
```bash
curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getTransactionByHash","params":["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getTransactionByBlockNumberAndIndex","params":["latest","0x0"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getTransactionByBlockHashAndIndex","params":["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","0x0"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getTransactionReceipt","params":["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_sendRawTransaction","params":["0x"],"id":1}' | python3 -m json.tool
```

### eth — logs / filters
```bash
curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getLogs","params":[{"fromBlock":"latest","toBlock":"latest"}],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_newFilter","params":[{"fromBlock":"latest","toBlock":"latest"}],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_newBlockFilter","params":[],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_newPendingTransactionFilter","params":[],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getFilterChanges","params":["0x1"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_getFilterLogs","params":["0x1"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_uninstallFilter","params":["0x1"],"id":1}' | python3 -m json.tool
```

### net
```bash
curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"net_version","params":[],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"net_listening","params":[],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"net_peerCount","params":[],"id":1}' | python3 -m json.tool
```

### web3
```bash
curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"web3_clientVersion","params":[],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"web3_sha3","params":["0x68656c6c6f"],"id":1}' | python3 -m json.tool
```

### trace (addon)
```bash
curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_block","params":["latest"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_transaction","params":["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_get","params":["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",["0x0"]],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_call","params":[{"to":"0x0000000000000000000000000000000000000000","data":"0x"},["trace"],"latest"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_callMany","params":[],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_rawTransaction","params":["0x",["trace"]],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_replayTransaction","params":["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",["trace"]],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_replayBlockTransactions","params":["latest",["trace"]],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"trace_filter","params":[{"fromBlock":"latest","toBlock":"latest"}],"id":1}' | python3 -m json.tool
```

### debug (addon)
```bash
curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"debug_traceTransaction","params":["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"debug_traceBlockByNumber","params":["latest"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"debug_traceBlockByHash","params":["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"debug_traceCall","params":[{"to":"0x0000000000000000000000000000000000000000","data":"0x"},"latest"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"debug_getRawBlock","params":["latest"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"debug_getRawHeader","params":["latest"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"debug_getRawReceipts","params":["latest"],"id":1}' | python3 -m json.tool

curl -si -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"debug_getRawTransaction","params":["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],"id":1}' | python3 -m json.tool
```
