
# Provider Simulator — Session Summary

## What we are building
A **provider simulator** for integration testing the smart router, based on:
`docs/simulator_implementation_guide.md`

The simulator replaces real blockchain nodes with a controllable HTTP server,
allowing tests to inject failures, latency, and rate limits.

---

## 3 repositories involved

| # | Repo | Status |
|---|---|---|
| 1 | `provider-simulator` (new) | 🟡 In progress — files empty |
| 2 | `smart-router-standalone` | ⬜ Not started |
| 3 | `smart_router_automation` | ⬜ Not started |

---

## What is DONE

### Repo 1 — `provider-simulator`
- ✅ Created at `/Users/victoria/provider-simulator`
- ✅ `git init` with default branch `develop`
- ✅ Remote added: `git@github.com:Magma-Devs/provider_simulator.git`
- ✅ Filesystem skeleton pushed to GitHub (`develop` branch):
  - `server.py` ← **empty**
  - `Dockerfile` ← **empty**
  - `requirements.txt` ← **empty**
  - `k8s/deployment.yml` ← **empty**
  - `k8s/service.yml` ← **empty**
  - `k8s/httproute-control.yml` ← **empty**
  - `scripts/deploy.sh` ← **empty**
  - `untracked/simulator_implementation_guide.md` (copied from smart_router_automation)
  - `untracked/temp.md`
  - `untracked/temp.txt`

---

## What is NEXT (immediate)

### Step 1 — Fill the empty files in `provider-simulator` from the guide (Section 2)

| File | Content source |
|---|---|
| `server.py` | Guide §2 — full Python simulator server |
| `Dockerfile` | Guide §2 — `FROM python:3.12-slim` |
| `requirements.txt` | Guide §2 — stdlib only, no deps |
| `k8s/deployment.yml` | Guide §2 — Kubernetes Deployment manifest |
| `k8s/service.yml` | Guide §2 — ClusterIP Service manifest |
| `k8s/httproute-control.yml` | Guide §2 — exposes control API at `sim-control.victoria.magmadevs.com` |
| `scripts/deploy.sh` | Guide §2 — build image, import to MicroK8s, apply manifests |

### Step 2 — Commit and push to GitHub

### Step 3 — Move to `smart-router-standalone` (Guide §3)
Add `values/simulator/values_sim.yml` — the new eth-sim chain config.

### Step 4 — Move to `smart_router_automation` (Guide §4)
Add `tests/simulator/` directory with 4 files:
- `__init__.py`
- `sim_control.py`
- `conftest.py`
- `test_router_routing.py`

### Step 5 — Deployment (Guide §5)
SSH into `victoria.magmadevs.com` and run `scripts/deploy.sh`.

---

## Key facts
- Kubernetes namespace: `smart-router`
- Simulator ports: 18545 (P1), 18546 (P2), 18547 (P3), 19000 (control API)
- Control API public URL: `https://sim-control.victoria.magmadevs.com`
- Router test URL: `https://eth-sim-jsonrpc.victoria.magmadevs.com`
- Gateway name: `sr-gateway`
- All files/content are fully specified in `docs/simulator_implementation_guide.md`
