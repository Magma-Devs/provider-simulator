# Using the gRPC Simulator

Quickstart for the gRPC sim surface that landed in MAG-1780 with reflection enabled in PR #16. For deploy / prereqs (`grpcurl` install), see [`new_server_setup.md`](new_server_setup.md). For general scenario / history / fault primitive usage, see [`using_the_simulator.md`](using_the_simulator.md).

## What the gRPC sim is

Three independent gRPC servers run inside the simulator pod on ports `18548` / `18549` / `18550`, alongside the existing JSON-RPC and control listeners. Each one implements `cosmos.base.tendermint.v1beta1.Service` (currently `GetLatestBlock` and `GetNodeInfo` — see `handlers_grpc.py` for the full list). Each gRPC server shares its `ProviderState` with the matching JSON-RPC port (`18548` ↔ `18545`, etc.), so one `POST /scenario` call reconfigures both transports for the same logical provider. gRPC reflection is registered (PR #16), so `grpcurl` can discover services without a local `.proto` bundle.

## Two ways to reach it

### Local (port-forward)

Forward one or all three ports, then call over `localhost` without TLS:

```bash
kubectl port-forward -n smart-router deployment/provider-simulator 18548:18548 &
grpcurl -plaintext localhost:18548 list
```

To forward all three providers + control + JSON-RPC at once:

```bash
kubectl port-forward -n smart-router svc/provider-simulator 18545:18545 18546:18546 18547:18547 18548:18548 18549:18549 18550:18550 19000:19000
```

### Public (through the GRPCRoute ingress)

`scripts/deploy.sh` creates a `GRPCRoute` for hostname `lava-sim-grpc.<BASE_DOMAIN>` (see `k8s/grpcroute-lava-sim-grpc.yml`). The Gateway load-balances across the three backend ports. Drop `-plaintext` to keep TLS:

```bash
grpcurl lava-sim-grpc.<BASE_DOMAIN>:443 list
```

## Quickstart commands

These are paste-ready. Replace the target the first time (`localhost:18548` or `lava-sim-grpc.<BASE_DOMAIN>:443`), then reuse the rest as-is.

```bash
# 1. Service catalogue — proves reflection is up
grpcurl -plaintext localhost:18548 list
# → cosmos.base.tendermint.v1beta1.Service
#   grpc.reflection.v1alpha.ServerReflection

# 2. Methods on the cosmos service
grpcurl -plaintext localhost:18548 list cosmos.base.tendermint.v1beta1.Service
# → cosmos.base.tendermint.v1beta1.Service.GetLatestBlock
#   cosmos.base.tendermint.v1beta1.Service.GetNodeInfo

# 3. Request shape for GetLatestBlock
grpcurl -plaintext localhost:18548 describe cosmos.base.tendermint.v1beta1.Service.GetLatestBlock

# 4. Real call — GetLatestBlock returns a Tendermint block at the simulator's
#    default head height (25_000_000 - blocks_behind). Empty request body.
grpcurl -plaintext -d '{}' localhost:18548 cosmos.base.tendermint.v1beta1.Service/GetLatestBlock

# 5. Real call — GetNodeInfo (version probe used during provider warmup)
grpcurl -plaintext -d '{}' localhost:18548 cosmos.base.tendermint.v1beta1.Service/GetNodeInfo
```

Public-hostname equivalents — same commands, just swap `<BASE_DOMAIN>` for your server's actual domain (`config/base-domain.env`) and drop `-plaintext`:

```bash
grpcurl lava-sim-grpc.<BASE_DOMAIN>:443 list
grpcurl -d '{}' lava-sim-grpc.<BASE_DOMAIN>:443 cosmos.base.tendermint.v1beta1.Service/GetLatestBlock
```

## Fault injection on gRPC providers

Mark the provider as `chain_family="grpc"` and apply any of the standard fault primitives — the gRPC handler translates them into `grpc.StatusCode` aborts (see `handlers_grpc.py::_apply_grpc_fault`):

```bash
curl -s -X POST "https://sim-control.<BASE_DOMAIN>/scenario" -H "Content-Type: application/json" -d '{"providers":{"1":{"chain_family":"grpc","mode":"rate_limit"}}}'

grpcurl -plaintext -d '{}' localhost:18548 cosmos.base.tendermint.v1beta1.Service/GetLatestBlock
# → ERROR: Code: ResourceExhausted, Message: Too many requests
```

### Which primitives apply

| Primitive | Applies to gRPC? | Notes |
|---|---|---|
| `mode=down` | yes | aborts `UNAVAILABLE` |
| `mode=hang` | yes | sleeps 30s then `CANCELLED` |
| `mode=drop_connection` | yes (with caveats) | unary RPCs can't legally stream mid-body, so `mid_body` collapses to the same shape as `after_headers` until streaming support lands |
| `mode=rate_limit` | yes | aborts `RESOURCE_EXHAUSTED` |
| `mode=error` | yes | symbolic `error_message` (e.g. `RESOURCE_EXHAUSTED`) wins over integer `error_code`; unrecognised values fall back to `UNKNOWN` |
| `latency_ms` / `error_probability` | yes | identical semantics |
| `corruption_mode="missing_field"` | yes | clears the named proto field via `ClearField` |
| `corruption_mode="wrong_type"` | partial | proto runtime won't accept the type swap, so the request aborts `INTERNAL` instead of returning a malformed message |
| `corruption_mode="truncated"` / `corruption_mode="empty_response"` / `corruption_mode="invalid_proto"` | yes | all abort `UNKNOWN` (the gRPC client sees a parse-failure surface) |
| `corruption_mode="invalid_json"` | no | JSON-only — not meaningful on gRPC |
| `corruption_mode="null_body"` | yes | aborts `UNKNOWN` — a whole-body JSON null has no gRPC shape, so it joins the parse-failure family instead of silently no-opping |
| `blocks_behind` | yes | decrements `block.header.height` in `GetLatestBlockResponse` |

For the cross-family fault primitive table (with the JSON-RPC side alongside), see [`using_the_simulator.md`](using_the_simulator.md#fault-primitives-across-chain-families).

## Troubleshooting

### `Error invoking method: server does not support the reflection API`

The image predates PR #16. Pull develop and redeploy:

```bash
cd ~/provider-simulator && git pull origin develop && bash scripts/deploy.sh
```

As a temporary workaround, point `grpcurl` at the vendored protos:

```bash
grpcurl -plaintext -import-path cosmos_pb2 -proto cosmos/base/tendermint/v1beta1/query.proto localhost:18548 list
```

### `connection refused on :443`

DNS or TLS cert. Check the GRPCRoute exists and the cert covers the hostname:

```bash
kubectl get grpcroute -n smart-router
kubectl describe grpcroute lava-sim-grpc-grpcroute -n smart-router
```

If the route exists but the cert doesn't cover `lava-sim-grpc.<BASE_DOMAIN>` (typical on a fresh server), refresh it:

```bash
cd ~/smart-router-standalone && bash scripts/install_gateway_api_tls_certificate.sh
```

### `connection refused on localhost`

Your `kubectl port-forward` isn't running. Start it and retry:

```bash
kubectl port-forward -n smart-router deployment/provider-simulator 18548:18548 &
```

### `failed to dial target host` (public hostname)

The pod or service can be healthy while the ingress is still wiring up. Re-run the 7-listener verify block in [`new_server_setup.md`](new_server_setup.md#5-verify-all-simulator-surfaces-are-up) to bisect: if the pod-local `grpcurl list` works but the public one doesn't, the issue is in the Gateway / TLS / DNS path, not the sim.
