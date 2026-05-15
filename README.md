# Provider Simulator

A small simulator pod used to test smart-router behaviour against deterministic, fault-injectable backends.

It runs **7 listeners** in a single pod:

- **3 JSON-RPC providers** on ports `18545` / `18546` / `18547` — dispatch to ETH or BTC chain handlers based on each provider's `chain_family`.
- **1 control API** on port `19000` — `POST /scenario`, `POST /reset[/all]`, `GET /scenario`, `GET /stats`, `GET /history`, `GET /health`.
- **3 gRPC providers** on ports `18548` / `18549` / `18550` (MAG-1780) — Cosmos `Service` with reflection enabled.

All 6 providers share the same per-provider `ProviderState`, so one `POST /scenario` call reconfigures both transports for the same logical provider.

## Chain families

Each provider's `chain_family` is set per-scenario and selects the dispatch path on the success branch. Fault branches (down / hang / drop / rate_limit / error / corruption) are chain-agnostic.

| `chain_family` | Transport | Status | Where it dispatches |
|---|---|---|---|
| `eth` (default) | JSON-RPC | live on develop | `handlers_eth.handle()` — ETH methods + `eth_getBlockByNumber` block-number echo |
| `btc` | JSON-RPC | live on develop | `handlers_btc.handle()` — BTC RPC method set, see `stubs_btc.py` |
| `grpc` | gRPC | live on develop | `handlers_grpc.CosmosBaseTendermintServicer` on a separate port range |
| `rest` | REST | planned (MAG-1777, not yet on develop) | — |

> The `chain_family="rest"` path and ports `18551` / `18552` / `18553` are reserved in `constants.py` and the k8s service for future REST sim work. They are **not** bound yet on develop.

## Fault-injection primitives

All primitives apply across chain families (with chain-appropriate translations — see `handlers_grpc.py` for the gRPC mapping).

| Field | Values | Effect |
|---|---|---|
| `mode` | `success` \| `error` \| `rate_limit` \| `down` \| `hang` \| `drop_connection` | Primary behaviour |
| `latency_ms` | int | Sleep N ms before responding |
| `error_probability` | 0.0–1.0 | Random error on top of `success` |
| `error_code` / `error_message` / `http_status` | int / str / int | Customise the error returned |
| `corruption_mode` | `truncated` \| `invalid_json` \| `empty_response` \| `missing_field` \| `wrong_type` | Break the response body |
| `missing_field` | str | Which field to drop / retype |
| `blocks_behind` | int | Shift the head reported by `eth_blockNumber` / `eth_getBlockByNumber` (and the gRPC `GetLatestBlock` height) |
| `drop_at` | `before_headers` \| `after_headers` \| `mid_body` | Where `drop_connection` cuts the socket |

## TL;DR — run locally

Requires Python 3.12. gRPC support needs `grpcio` / `grpcio-reflection` / `protobuf` (see `requirements.txt`); the JSON-RPC side is stdlib-only.

```bash
pip install -r requirements.txt
python -u server.py
```

Quick checks:

```bash
curl -s http://localhost:19000/health
curl -s -X POST http://localhost:19000/reset/all
curl -s -X POST http://localhost:18545 -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'
grpcurl -plaintext localhost:18548 list
```

Run the test suite (boots all listeners in-process on isolated test ports):

```bash
pytest tests/ -v
```

## How to set up (fresh server)

See **[`docs/new_server_setup.md`](docs/new_server_setup.md)** — covers prereqs (`curl`, `grpcurl`), deploy, the 7-listener verification, smart-router wiring, and the debug-domain appendix.

## How to use (once setup is done)

- **[`docs/using_the_simulator.md`](docs/using_the_simulator.md)** — scenario / reset / history / fault primitives across chain families with paste-ready examples.
- **[`docs/using_grpc.md`](docs/using_grpc.md)** — gRPC quickstart: reflection, two ways to reach the gRPC sim, real `grpcurl` commands, fault injection on gRPC providers, troubleshooting.
- **[`docs/curl_reference.md`](docs/curl_reference.md)** — exhaustive curl catalogue for the JSON-RPC surface and all supported methods.

## Public URLs

Derived from `BASE_DOMAIN` in `config/base-domain.env`:

- `https://sim-control.<BASE_DOMAIN>` — control API (HTTPRoute in this repo).
- `https://eth-sim-jsonrpc.<BASE_DOMAIN>` — JSON-RPC sim through the smart-router (wired in `values_sim.yml`).
- `lava-sim-grpc.<BASE_DOMAIN>:443` — gRPC sim ingress (GRPCRoute, MAG-1780).

## Other docs

- **[`docs/kubectl_reference.md`](docs/kubectl_reference.md)** — common kubectl commands for this pod.
- **[`docs/DEPLOY_AFTER_PUSH.md`](docs/DEPLOY_AFTER_PUSH.md)** — push-to-deploy flow.
- **[`docs/ARCHITECTURE_GUIDE.md`](docs/ARCHITECTURE_GUIDE.md)** — code/system walkthrough.
- **[`docs/CLASS_REFERENCE.md`](docs/CLASS_REFERENCE.md)** — class-by-class breakdown.
- **[`docs/DATA_FLOWS.md`](docs/DATA_FLOWS.md)** — request lifecycles.
- **[`docs/START_HERE.md`](docs/START_HERE.md)** — docs index / learning path.

## Repo layout

```
server.py              — process entry: JSON-RPC servers, gRPC servers, control API
handlers_eth.py        — ETH success-branch dispatch (chain_family="eth")
handlers_btc.py        — BTC success-branch dispatch (chain_family="btc", MAG-1716)
handlers_grpc.py       — Cosmos gRPC servicer (chain_family="grpc", MAG-1780)
grpc_server.py         — gRPC server bootstrap (3 servers on 18548-18550, reflection on)
stubs.py               — default ETH JSON-RPC results + ERROR_STUBS catalogue
stubs_btc.py           — default BTC JSON-RPC results
constants.py           — port maps, chain constants, history caps
cosmos_pb2/            — vendored Cosmos protobufs (MAG-1780)
config/base-domain.env — sets BASE_DOMAIN for deploy
config/values_sim.yml  — smart-router Helm values used to wire the sim in
Dockerfile             — image used by scripts/deploy.sh
k8s/                   — Deployment, Service, HTTPRoute (control), GRPCRoute (gRPC sim)
scripts/deploy.sh      — build/import/apply/restart deploy flow
tests/                 — pytest suite (ETH, BTC, gRPC)
```

## Notes

- The Kubernetes deployment is intentionally **1 replica** — history and counters are in-memory; multiple pods would split state.
- `scripts/deploy.sh` always rollout-restarts so the new image (always `:latest`) is actually picked up.
- For debug-domain / clock-injection setup, see **Appendix A** in [`docs/new_server_setup.md`](docs/new_server_setup.md).
