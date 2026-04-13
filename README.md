# Provider Simulator

A small HTTP JSON-RPC simulator for smart-router testing.

It runs:
- **3 simulated providers** on ports `18545`, `18546`, `18547`
- **1 control API** on port `19000`

Use it to test router behavior under:
- success
- rate limiting
- provider downtime
- JSON-RPC errors
- latency
- probabilistic failures

## TL;DR

### Run locally

No external Python dependencies are required.

```bash
python -u server.py
```

Quick checks:

```bash
curl -s http://localhost:19000/health
curl -s http://localhost:19000/scenario
curl -s -X POST http://localhost:19000/reset
```

Example JSON-RPC request to provider 1:

```bash
curl -s -X POST http://localhost:18545 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'
```

### Deploy to server

On the deployment server:

```bash
cd ~/provider-simulator
bash scripts/deploy.sh
```

For a fresh server or full router wiring, use the setup guide below.

## Main docs

- **Fresh server setup:** [`docs/new_server_setup.md`](docs/new_server_setup.md)
- **Quick curl examples:** [`docs/curl_reference.md`](docs/curl_reference.md)
- **kubectl commands:** [`docs/kubectl_reference.md`](docs/kubectl_reference.md)
- **Deploy after push:** [`docs/DEPLOY_AFTER_PUSH.md`](docs/DEPLOY_AFTER_PUSH.md)
- **Architecture guide:** [`docs/ARCHITECTURE_GUIDE.md`](docs/ARCHITECTURE_GUIDE.md)
- **Code/class walkthrough:** [`docs/CLASS_REFERENCE.md`](docs/CLASS_REFERENCE.md)
- **Data flow explanations:** [`docs/DATA_FLOWS.md`](docs/DATA_FLOWS.md)
- **Docs index / learning path:** [`docs/START_HERE.md`](docs/README.md)

## Public URLs

Public URLs are derived from `BASE_DOMAIN` in `config/base-domain.env`.

Typical endpoints:
- `https://sim-control.<BASE_DOMAIN>`
- `https://eth-sim-jsonrpc.<BASE_DOMAIN>`

## Repo layout

- `server.py` — simulator + control API
- `stubs.py` — default JSON-RPC method responses
- `constants.py` — shared ports/constants
- `k8s/` — Kubernetes manifests
- `scripts/deploy.sh` — build/import/apply/restart deploy flow
- `config/values_sim.yml` — router values used to wire simulator into smart-router

## Notes

- `scripts/deploy.sh` already performs the rollout restart.
- The simulator Kubernetes deployment is intentionally **1 replica**.
- For debug-domain / clock-injection setup, see **Appendix A** in [`docs/new_server_setup.md`](docs/new_server_setup.md).

