## Summary

A single Python pod is deployed on `victoria.magmadevs.com` alongside the existing router. It pretends to be three real blockchain nodes by running three HTTP servers on three ports. Tests call a control API on that same pod to set what each fake node returns — success, error, rate limit, slow, or down — before sending a request through the real router, which routes to the fake nodes instead of Google or QuickNode.

When the unified router ships, nothing needs to change: the unified router will still make HTTP calls to backend nodes, the simulator is still an HTTP server, the control API is still the same, and the tests are still the same. Only `values_sim.yml` needs one URL update to point the new router at the simulator.

---

## What I am suggesting

**Option A — HTTP simulator pod in `lava-infra`, with a runtime control API.**

This is not Option B (gRPC static-providers). The full Lava Protocol stack stays intact — consumer pod, provider pod, everything. The only change is the last hop: instead of calling Google or QuickNode, the `lavap provider` pod calls a fake Python HTTP server that returns whatever the test has configured.

---

## Why Not Option B (gRPC Static-Providers)

Option B bypasses the `lavap provider` pods entirely. The consumer talks gRPC directly to the simulator, skipping the provider pod layer completely.

The future direction states:

> *"In the future there will be a unified version — no separated consumer/provider — it will be one smart router that uses WRS to route RPC requests in the best way to the best node."*

This means:

- Option B is wired to the **current** consumer/provider gRPC split. When the unified router ships, the `static-providers` gRPC interface disappears. Option B becomes useless and the entire test infrastructure needs to be rewritten.
- Option A uses **HTTP** — the same interface the future unified router will use to call backend nodes. The same simulator, the same control API, the same tests will work unchanged when the unified router ships.

Additionally, Option B would miss regressions introduced in the `lavap provider` pod, because that layer is bypassed. Option A tests the full stack.

---

## The Three Parts

### Part 1 — It is Option A (HTTP simulator)

The simulator is a Python HTTP server that sits where the real blockchain node (Google, QuickNode) sits today. The full stack stays intact:

```
Your test
  → Router (victoria.magmadevs.com)
    → lavap consumer pod
      → lavap provider pod
        → HTTP simulator  ← new pod, replaces Google/QuickNode
```

Nothing in the router stack changes. The only difference from production is the last hop goes to a fake HTTP server instead of a real node.

---

### Part 2 — Three independent HTTP servers in one pod

One simulator pod runs **three separate HTTP servers** on three ports. Each port pretends to be a different provider:

```
port 18545  →  "Provider 1"
port 18546  →  "Provider 2"
port 18547  →  "Provider 3"
```

The router's config (`values_sim.yml`) points each of its three provider slots to a different port. This is what gives WRS three targets to score and choose between.

---

### Part 3 — A control API so tests set the scenario before each test

This is the addition on top of plain Option A. A fourth server runs on port 19000 and is exposed publicly via a Kubernetes HTTPRoute at `sim-control.victoria.magmadevs.com`. Before sending a test request, your test calls this API to configure what each provider will return:

```
Test calls control API:
  "provider 1 → rate limit, provider 2 → down, provider 3 → success"

Then test calls the router:
  → WRS sees provider 1 and 2 are bad → chooses provider 3 → returns success

Test asserts the result.
```

Without the control API you would need to restart the simulator pod to change scenarios, which is too slow and brittle for regression tests.

---

## Architecture

There are two completely different things that both have "provider" in the name. It is important to keep them separate:

| Name | What it is | How many | Who creates it |
|---|---|---|---|
| `lavap provider pod` | Part of the existing Lava Protocol router stack. Bridges gRPC ↔ HTTP. Already exists in production. | 3 pods (one per configured provider) | Helm chart, created automatically when `values_sim.yml` lists 3 providers |
| `provider-simulator pod` | **The new thing being built.** A single Python pod with 3 HTTP servers inside it. Replaces Google/QuickNode. | **1 pod total** | New `k8s/deployment.yml` |

```
Your test
  │                                              │
  │  HTTPS POST to                               │  POST to
  │  eth-sim-jsonrpc.victoria.magmadevs.com      │  sim-control.victoria.magmadevs.com
  ▼                                              ▼
Gateway (Kubernetes, victoria.magmadevs.com:443)
  │                                              │
  ▼                                              ▼
lavap rpcconsumer pod                    ┌─────────────────────────────────────┐
  │                                      │   provider-simulator pod  (1 pod)   │
  │  gRPC  ┌─────────────────────┐       │                                     │
  ├───────▶│ lavap provider pod  │─HTTP─▶│  port 18545  (server for provider 1)│
  │        └─────────────────────┘       │                                     │
  │  gRPC  ┌─────────────────────┐       │  port 18546  (server for provider 2)│
  ├───────▶│ lavap provider pod  │─HTTP─▶│                                     │
  │        └─────────────────────┘       │  port 18547  (server for provider 3)│
  │  gRPC  ┌─────────────────────┐       │                                     │
  └───────▶│ lavap provider pod  │─HTTP─▶│  port 19000  (control API)          │
           └─────────────────────┘       └─────────────────────────────────────┘

  ↑ these 3 pods already exist,           ↑ this is 1 new pod with 4 threads
    created by Helm chart                   inside, one per port
```

- The **3 `lavap provider` pods** are part of the existing router stack. They are created automatically by the Helm chart when `values_sim.yml` configures 3 providers. Each one connects to a **different port** on the simulator pod.
- The **1 `provider-simulator` pod** is what gets built. It runs 4 Python HTTP servers in threads on 4 ports. Each server independently returns whatever behaviour the test has configured for that provider.

**Nothing in the router stack changes.** The `lavap rpcconsumer` pod, the `lavap provider` pods, the Gateway, Cloudflare — all identical to production. The only new component is the single simulator pod, which replaces Google/QuickNode as the HTTP backend.

---

## What Gets Built — Three Repositories

The simulator is **automation infrastructure**. It is owned by the automation team, lives in its own repository, and is deployed independently. It does not mix into the router repo.

### Repository 1: `provider-simulator` (new, owned by automation team)

```
provider-simulator/
  server.py                ← four-server Python process (3 JSON-RPC + 1 control)
  Dockerfile               ← python:3.12-slim, exposes ports 18545-18547 + 19000
  requirements.txt         ← no external dependencies (stdlib only)
  k8s/
    deployment.yml         ← single-replica Deployment in lava-infra namespace
    service.yml            ← ClusterIP Service, all four ports
    httproute-control.yml  ← exposes control API at sim-control.victoria.magmadevs.com
  scripts/
    deploy.sh              ← builds image, imports into MicroK8s, applies k8s manifests
```

Deployed independently onto `victoria.magmadevs.com`. The automation team owns its deployment lifecycle — no router team involvement needed.

### Repository 2: `smart-router-standalone` (router team, minimal change)

Only one new file is added — no simulator code touches this repo:

```
values/simulator/values_sim.yml   ← adds eth-sim chain pointing to simulator URLs
```

This file is applied via `helm upgrade --values values/simulator/values_sim.yml` and tells the router about the three simulated providers:
`http://provider-simulator.lava-infra.svc.cluster.local:18545/18546/18547`

That is the only change in the router repo. The router team does not own or maintain the simulator.

### Repository 3: `smart_router_automation` (automation team)

New directory: `tests/simulator/`

```
tests/simulator/
  sim_control.py         ← SimulatorControl client class (calls the control API)
  conftest.py            ← sim_control fixture, sim_router_url fixture,
                            autouse reset_simulator_after_test fixture
  test_router_routing.py ← regression tests: WRS, failover, latency, recovery
```

---

## Supported Scenarios Per Provider

| Scenario | What the simulator returns | What the router sees |
|---|---|---|
| `success` | `{"jsonrpc":"2.0","result":"..."}` | Valid response, scores provider high |
| `error` | `{"jsonrpc":"2.0","error":{...}}` | Application error |
| `rate_limit` | HTTP 429 | Provider is throttling |
| `down` | HTTP 503 | Provider is unavailable |
| `latency` | Success, but after N ms sleep | Slow provider, WRS scores it lower |
| `error_probability` | Randomly fails X% of requests | Flaky provider |

---

## Test Flow (Step by Step)

For every simulator test:

```
1. autouse fixture calls POST /reset               → all three providers healthy

2. test body calls sim_control.set_scenario(...)   → configures the scenario

3. test calls http_client.post(sim_router_url, ...) → sends request to router

4. router's WRS/retry logic runs against the configured scenario

5. test asserts the response

6. autouse fixture calls POST /reset again (cleanup)
```

---

## Regression Scenarios This Infrastructure Enables

| Test | Scenario configured | What is being tested |
|---|---|---|
| Router routes to only healthy provider | P1: rate_limit, P2: down, P3: success | Failover + WRS avoidance of bad providers |
| Router recovers when provider comes back | P1: down → then success | WRS re-admission after recovery |
| Router handles all providers rate-limited | P1/P2/P3: rate_limit | Graceful degradation, no crash |
| Router prefers fast provider | P1: 10ms, P2: 800ms, P3: 800ms | WRS latency scoring |
| Router retries on transient errors | P1/P2: error_probability=0.5, P3: success | Retry logic |
| Single flaky provider | P1: error_probability=0.3, P2/P3: success | QoS scoring under partial failure |

---

## What Does Not Change

- All existing product tests (`tests/product/etherium_compare/`) — untouched, still hit `eth-jsonrpc.victoria.magmadevs.com`
- All existing infrastructure tests — untouched
- The router deployment — no changes to consumer pods, provider pods, Gateway, Cloudflare
- The Helm chart — only a new `--values values/simulator/values_sim.yml` is added on upgrade

---

## Two Endpoints Run Simultaneously

```
eth-jsonrpc.victoria.magmadevs.com      → real providers (Google, QuickNode)  ← existing tests
eth-sim-jsonrpc.victoria.magmadevs.com  → simulator providers (ports 18545/18546/18547)  ← new simulator tests
```

Production tests are never affected.

---

## Future-Proofing

When the unified smart router ships (no more consumer/provider pod split):

- The HTTP simulator pod continues to work unchanged — it is still an HTTP server returning configured responses
- The control API continues to work unchanged — it is independent of Lava Protocol internals
- The tests continue to work unchanged — they call the control API and assert HTTP responses
- The only change needed is updating `values_sim.yml` to point the unified router at the simulator URLs

Option B (gRPC static-providers) would require a complete rewrite at that point because the gRPC relay protocol between consumer and provider pods will no longer exist.

---

## Open Question for DevOps

This proposal does **not** require asking DevOps about `static-providers` support in the Helm chart. It only requires:

1. Permission to deploy a new pod (`provider-simulator`) in the `lava-infra` namespace
2. Adding an HTTPRoute for `sim-control.victoria.magmadevs.com`
3. Running `helm upgrade` with an additional `--values` file to add the `eth-sim` chain

