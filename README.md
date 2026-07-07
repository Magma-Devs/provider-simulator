# Provider Simulator

A small simulator pod used to test smart-router behaviour against deterministic, fault-injectable backends.

It runs many listeners in a single pod:

- **3 ETH JSON-RPC providers** on ports `18545` / `18546` / `18547` — dispatch to `handlers_eth`.
- **3 BTC JSON-RPC providers** on ports `18575` / `18576` / `18577` — dispatch to `handlers_btc` (MAG-2089).
- **3 LN JSON-RPC providers** on ports `18578` / `18579` / `18580` — dispatch to `handlers_lnd` (MAG-2089).
- **3 gRPC providers** on ports `18548` / `18549` / `18550` (MAG-1780) — Cosmos `Service` with reflection enabled.
- **3 REST providers** on ports `18551` / `18552` / `18553` (MAG-1777).
- **3 Tendermint-RPC providers** on ports `18554` / `18555` / `18556` (MAG-1841).
- **3 WebSocket providers** on ports `18557` / `18558` / `18559` (MAG-1801).
- **3 JSON-RPC backup providers** on `18560` / `18561` / `18562`, plus per-surface backup ranges in `18563`–`18574`.
- **1 control API** on port `19000` — `POST /scenario`, `POST /reset[/all]`, `POST /advance` (MAG-1897 — opt-in advancing eth head), `GET /scenario`, `GET /stats`, `GET /history`, `GET /health`, `GET /ready`.

For each pid (1-3), the ETH / BTC / LN / REST / gRPC / TM / WS primary listeners all share **the same `ProviderState`**, so one `POST /scenario` call reconfigures every transport for the same logical provider.

## Chain families

JSON-RPC handler dispatch is **port-derived** (MAG-2089): the ETH listener pool always calls `handlers_eth`, the BTC pool always calls `handlers_btc`, the LN pool always calls `handlers_lnd`. The `chain_family` field on `/scenario` payloads is still meaningful — REST / gRPC / TM / WS read it to gate **content** fault primitives (`error` / `rate_limit` / `hang` / `drop_connection` / `corruption`) to the transport that authored them, and the JSON-RPC listeners use it to gate content faults to their own pool (ETH listener fires `chain_family="eth"` content faults only, BTC fires `chain_family="btc"` only, LN fires `chain_family="ln"` only). `mode="down"` is the universal exception (MAG-2092): it is honored on every transport regardless of `chain_family` because reachability is provider-wide — a BTC-tagged `mode="down"` still 503s the ETH JSON-RPC, REST, gRPC, TM, and WS surfaces for the same provider id. One word only: `chain_family` holds a blockchain OR a connection type, never both — a combination like "Solana over WebSocket" is not expressible; splitting the field into two is deferred until a test needs such a combination. Unknown values are rejected with HTTP 400.

| `chain_family` | Transport | Status | Listener ports | Where it dispatches |
|---|---|---|---|---|
| `eth` (default) | JSON-RPC | live on main | `18545`–`18547` | `handlers_eth.handle()` — ETH methods + `eth_getBlockByNumber` block-number echo |
| `btc` | JSON-RPC | live on main (MAG-1716, ports moved MAG-2089) | `18575`–`18577` | `handlers_btc.handle()` — BTC RPC method set, see `stubs_btc.py` |
| `ln` | JSON-RPC | live on main (MAG-1726, ports moved MAG-2089) | `18578`–`18580` | `handlers_lnd.handle()` — LND method set (`getinfo`, `listchannels`, `openchannel`, `decodepayreq`, `payinvoice`, `listpeers`), see `stubs_lnd.py` |
| `grpc` | gRPC | live on main | `18548`–`18550` | `handlers_grpc.CosmosBaseTendermintServicer` |
| `rest` | REST | live on main (MAG-1777) | `18551`–`18553` | `handlers_rest.handle()` |
| `tendermintrpc` | JSON-RPC | live on main (MAG-1841) | `18554`–`18556` | `handlers_tendermintrpc` |
| `ws` | WebSocket | live on main (MAG-1801) | `18557`–`18559` | `handlers_ws` |

> Pre-MAG-2089 the BTC and LN JSON-RPC handlers were selected per-provider from `snap["chain_family"]` on the shared ETH listener pool (18545-18547). A test on `btc-sim-router` that set `chain_family="btc"` could be flipped back to `chain_family="eth"` by a concurrent test on `eth-sim-router` against the shared `ProviderState`, contaminating BTC responses. MAG-2089 moved BTC + LN to their own dedicated listener pools and made dispatch port-derived. The `chain_family` field is still set on `/scenario` payloads for fault-primitive gating, but no longer steers handler selection on JSON-RPC.

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
python -u run.py
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
handlers_eth.py        — ETH success-branch dispatch (ports 18545-18547)
handlers_btc.py        — BTC success-branch dispatch (ports 18575-18577, MAG-1716 / MAG-2089)
handlers_lnd.py        — Lightning Network (LND) dispatch (ports 18578-18580, MAG-1726 / MAG-2089)
handlers_grpc.py       — Cosmos gRPC servicer (chain_family="grpc", MAG-1780)
grpc_server.py         — gRPC server bootstrap (3 servers on 18548-18550, reflection on)
stubs.py               — default ETH JSON-RPC results + ERROR_STUBS catalogue
stubs_btc.py           — default BTC JSON-RPC results
stubs_lnd.py           — default LND JSON-RPC results + LND_ERROR_STUBS catalogue
constants.py           — port maps, chain constants, history caps
cosmos_pb2/            — vendored Cosmos protobufs (MAG-1780)
config/base-domain.env — sets BASE_DOMAIN for deploy
config/values_sim.yml  — smart-router Helm values used to wire the sim in
Dockerfile             — image used by scripts/deploy.sh
k8s/                   — Deployment, Service, HTTPRoute (control), GRPCRoute (gRPC sim)
scripts/deploy.sh      — build/import/apply/restart deploy flow
tests/                 — pytest suite (ETH, BTC, LN, gRPC, REST, TM, WS)
```

## Notes

- The Kubernetes deployment is intentionally **1 replica** — history and counters are in-memory; multiple pods would split state.
- `scripts/deploy.sh` always rollout-restarts so the new image (always `:latest`) is actually picked up.
- For debug-domain / clock-injection setup, see **Appendix A** in [`docs/new_server_setup.md`](docs/new_server_setup.md).
