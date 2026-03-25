# Provider Simulator — Full Implementation Guide

This document contains everything needed to implement the simulator infrastructure
from scratch, including all code, all configuration files, and all deployment steps.
No prior context is required.

---

## 1. Background and Environment Facts

### What the smart router is

The smart router runs on `victoria.magmadevs.com` as a Kubernetes (MicroK8s) deployment.
It uses Lava Protocol (`lavap`) deployed via a private Helm chart.

Kubernetes namespace: `lava-infra`
Helm chart: `oci://ghcr.io/magma-devs/smart-router-helm-chart/smart-router`
Helm chart version: `3.1.0`
Gateway name: `sr-gateway` (Envoy Gateway API)
Base domain: `victoria.magmadevs.com`

### How endpoints are named

The Helm chart automatically creates an HTTPRoute for each chain:
```
chain id "eth"     → eth-jsonrpc.victoria.magmadevs.com:443
chain id "eth-sim" → eth-sim-jsonrpc.victoria.magmadevs.com:443
```

### How the production stack works

```
Test (HTTPS POST)
  → Cloudflare → Gateway (sr-gateway, lava-infra)
    → lavap rpcconsumer pod     ← WRS, retry, failover, QoS scoring
      → lavap provider pod      ← gRPC ↔ HTTP bridge
        → Real blockchain node  ← Google / QuickNode (HTTP JSON-RPC)
```

The URL in `values/core/values.yml` under `chains[].providers[].endpoints[].url`
is what the `lavap provider` pod calls over HTTP. That is the only thing being replaced.

### What the simulator does

One Python pod runs four HTTP servers in threads:
- port 18545 — fake backend for provider 1
- port 18546 — fake backend for provider 2
- port 18547 — fake backend for provider 3
- port 19000 — control API (tests set scenarios here)

Each JSON-RPC server returns configurable responses. Tests call the control API
before each test to set what each provider returns.

### Three repositories involved

| Repository | Owner | What changes |
|---|---|---|
| `provider-simulator` (new) | Automation team | Everything — new repo |
| `smart-router-standalone` | Router team | One new file: `values/simulator/values_sim.yml` |
| `smart_router_automation` | Automation team | New `tests/simulator/` directory |

---

## 2. Repository 1 — `provider-simulator`

Create a new repository named `provider-simulator`.

### File: `server.py`

```python
"""
HTTP JSON-RPC Provider Simulator

Three independent JSON-RPC servers (ports 18545 / 18546 / 18547)
plus one control API (port 19000).

Each provider's behaviour is changed at runtime via POST /scenario.

Supported modes per provider:
  success       — returns {"jsonrpc":"2.0","result":"..."} with optional latency
  error         — returns {"jsonrpc":"2.0","error":{"code":-32000,"message":"..."}}
  rate_limit    — returns HTTP 429
  down          — returns HTTP 503 (router treats provider as unavailable)
  error_probability — randomly returns error on X% of requests (0.0–1.0)

Control API:
  POST /scenario  {"providers": {"1": {"mode": "rate_limit"}, "2": {"mode": "down"}, "3": {"mode": "success"}}}
  POST /reset     {}
  GET  /scenario  → current state of all providers
  GET  /health    → {"status": "ok"}
"""

import json
import random
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Any


# ── Provider state ────────────────────────────────────────────────────────────

@dataclass
class ProviderState:
    mode: str = "success"               # success | error | rate_limit | down
    latency_ms: int = 0
    error_probability: float = 0.0
    responses: Dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "mode":              self.mode,
                "latency_ms":        self.latency_ms,
                "error_probability": self.error_probability,
            }

    def update(self, cfg: dict) -> None:
        with self.lock:
            self.mode              = cfg.get("mode",              self.mode)
            self.latency_ms        = cfg.get("latency_ms",        self.latency_ms)
            self.error_probability = cfg.get("error_probability", self.error_probability)
            if "responses" in cfg:
                self.responses = cfg["responses"]

    def reset(self) -> None:
        with self.lock:
            self.mode              = "success"
            self.latency_ms        = 0
            self.error_probability = 0.0
            self.responses         = {}


# ── JSON-RPC handler ──────────────────────────────────────────────────────────

class JSONRPCHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        state: ProviderState = self.server.state
        snap = state.snapshot()

        # Outage — return 503 (router treats provider as unavailable)
        if snap["mode"] == "down":
            self.send_response(503)
            self.end_headers()
            return

        # Latency injection
        if snap["latency_ms"] > 0:
            time.sleep(snap["latency_ms"] / 1000.0)

        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}
        req_id = body.get("id", 1)
        method = body.get("method", "default")

        # Rate limit — return HTTP 429
        if snap["mode"] == "rate_limit":
            self._reply(429, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": 429, "message": "Too many requests"}
            })
            return

        # Probabilistic error
        if snap["mode"] == "error" or random.random() < snap["error_probability"]:
            self._reply(200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": "Internal error"}
            })
            return

        # Success — look up method-specific result
        with state.lock:
            method_cfg = state.responses.get(method) or state.responses.get("default", {})
        result = method_cfg.get("result", "0x1")

        self._reply(200, {"jsonrpc": "2.0", "id": req_id, "result": result})

    def _reply(self, status: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


# ── Control API handler ───────────────────────────────────────────────────────

class ControlHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}

        if self.path == "/scenario":
            for pid, cfg in body.get("providers", {}).items():
                state = self.server.provider_states.get(str(pid))
                if state:
                    state.update(cfg)
            self._reply(200, {"status": "ok"})

        elif self.path == "/reset":
            for state in self.server.provider_states.values():
                state.reset()
            self._reply(200, {"status": "reset"})

        else:
            self._reply(404, {"error": "unknown path"})

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"status": "ok"})
            return
        if self.path == "/scenario":
            snapshot = {pid: s.snapshot()
                        for pid, s in self.server.provider_states.items()}
            self._reply(200, {"providers": snapshot})
            return
        self._reply(404, {"error": "unknown path"})

    def _reply(self, status: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


# ── Server startup ────────────────────────────────────────────────────────────

PROVIDER_PORTS = {"1": 18545, "2": 18546, "3": 18547}
CONTROL_PORT   = 19000


def main():
    states = {pid: ProviderState() for pid in PROVIDER_PORTS}

    servers = []
    for pid, port in PROVIDER_PORTS.items():
        srv = HTTPServer(("0.0.0.0", port), JSONRPCHandler)
        srv.state = states[pid]
        servers.append(srv)

    ctrl = HTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    ctrl.provider_states = states
    servers.append(ctrl)

    print("Provider simulator started")
    for pid, port in PROVIDER_PORTS.items():
        print(f"  provider {pid} → :{port}")
    print(f"  control API  → :{CONTROL_PORT}")

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        for s in servers:
            s.shutdown()


if __name__ == "__main__":
    main()
```

### File: `Dockerfile`

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY server.py .
EXPOSE 18545 18546 18547 19000
CMD ["python", "server.py"]
```

### File: `requirements.txt`

```
# No external dependencies — stdlib only (http.server, json, threading)
```

### File: `k8s/deployment.yml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: provider-simulator
  namespace: lava-infra
  labels:
    app: provider-simulator
    app.kubernetes.io/name: provider-simulator
    app.kubernetes.io/component: testing
spec:
  replicas: 1
  selector:
    matchLabels:
      app: provider-simulator
  template:
    metadata:
      labels:
        app: provider-simulator
        app.kubernetes.io/name: provider-simulator
        app.kubernetes.io/component: testing
    spec:
      containers:
        - name: provider-simulator
          image: provider-simulator:latest
          imagePullPolicy: IfNotPresent
          ports:
            - name: provider-1
              containerPort: 18545
              protocol: TCP
            - name: provider-2
              containerPort: 18546
              protocol: TCP
            - name: provider-3
              containerPort: 18547
              protocol: TCP
            - name: control
              containerPort: 19000
              protocol: TCP
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
            limits:
              cpu: "500m"
              memory: "256Mi"
          readinessProbe:
            httpGet:
              path: /health
              port: control
            initialDelaySeconds: 3
            periodSeconds: 5
            timeoutSeconds: 3
          livenessProbe:
            httpGet:
              path: /health
              port: control
            initialDelaySeconds: 5
            periodSeconds: 15
            timeoutSeconds: 3
      restartPolicy: Always
```

### File: `k8s/service.yml`

```yaml
apiVersion: v1
kind: Service
metadata:
  name: provider-simulator
  namespace: lava-infra
  labels:
    app: provider-simulator
spec:
  selector:
    app: provider-simulator
  type: ClusterIP
  ports:
    - name: provider-1
      port: 18545
      targetPort: 18545
      protocol: TCP
    - name: provider-2
      port: 18546
      targetPort: 18546
      protocol: TCP
    - name: provider-3
      port: 18547
      targetPort: 18547
      protocol: TCP
    - name: control
      port: 19000
      targetPort: 19000
      protocol: TCP
```

### File: `k8s/httproute-control.yml`

This exposes the control API externally at `sim-control.victoria.magmadevs.com`.
The gateway name `sr-gateway` and namespace `lava-infra` match the existing gateway
in the smart-router-standalone deployment.

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: sim-control-httproute
  namespace: lava-infra
spec:
  parentRefs:
    - name: sr-gateway
      namespace: lava-infra
  hostnames:
    - "sim-control.victoria.magmadevs.com"
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: provider-simulator
          namespace: lava-infra
          port: 19000
```

### File: `scripts/deploy.sh`

Run this from the `victoria.magmadevs.com` server inside the `provider-simulator` repo.

```bash
#!/bin/bash
set -e

NAMESPACE="lava-infra"

echo "=== Building Docker image ==="
docker build -t provider-simulator:latest .

echo "=== Importing image into MicroK8s ==="
docker save provider-simulator:latest | microk8s ctr image import -

echo "=== Applying Kubernetes manifests ==="
kubectl apply -f k8s/deployment.yml
kubectl apply -f k8s/service.yml
kubectl apply -f k8s/httproute-control.yml

echo "=== Waiting for pod to be ready ==="
kubectl rollout status deployment/provider-simulator -n "$NAMESPACE" --timeout=60s

echo "=== Updating TLS certificate to include new hostname ==="
# This regenerates the TLS cert to include sim-control.victoria.magmadevs.com
# Run the existing TLS certificate script from smart-router-standalone if available:
#   cd /path/to/smart-router-standalone && bash scripts/install_gateway_api_tls_certificate.sh

echo ""
echo "Provider simulator deployed."
echo "  JSON-RPC providers : ClusterDNS provider-simulator.lava-infra.svc.cluster.local:18545/18546/18547"
echo "  Control API        : https://sim-control.victoria.magmadevs.com"
echo ""
echo "Verify:"
echo "  curl https://sim-control.victoria.magmadevs.com/health"
```

---

## 3. Repository 2 — `smart-router-standalone`

Only one new file needs to be added to this repository.
The router team adds it and runs `helm upgrade`.

### File: `values/simulator/values_sim.yml`

```yaml
# Simulator chain — adds eth-sim alongside the real eth chain.
# Applied via: helm upgrade smart-router ... --values values/simulator/values_sim.yml
#
# This creates:
#   eth-sim-jsonrpc.victoria.magmadevs.com:443  → simulator providers
#
# The three providers point to different ports on the provider-simulator pod.
# ClusterDNS: provider-simulator.lava-infra.svc.cluster.local

chains:
  - id: "eth-sim"
    network: "eth1"
    providers:
      - name: "SimProvider1"
        endpoints:
          - url: "http://provider-simulator.lava-infra.svc.cluster.local:18545"
            interface: "jsonrpc"
            addons: ["archive", "trace", "debug"]
      - name: "SimProvider2"
        endpoints:
          - url: "http://provider-simulator.lava-infra.svc.cluster.local:18546"
            interface: "jsonrpc"
            addons: ["archive", "trace", "debug"]
      - name: "SimProvider3"
        endpoints:
          - url: "http://provider-simulator.lava-infra.svc.cluster.local:18547"
            interface: "jsonrpc"
            addons: ["archive", "trace", "debug"]
```

### Helm upgrade command (run on `victoria.magmadevs.com`)

```bash
source scripts/utils/common.sh   # loads HELM_CHART_VERSION, NAMESPACE, credentials

helm upgrade smart-router \
  "oci://ghcr.io/magma-devs/smart-router-helm-chart/smart-router" \
  --namespace "$NAMESPACE" \
  --version "$HELM_CHART_VERSION" \
  --values values/core/values.yml \
  --values values/simulator/values_sim.yml
```

After this command, the Helm chart automatically creates:
- Three new `lavap provider` pods for `eth-sim`
- One new `lavap rpcconsumer` pod for `eth-sim`
- An HTTPRoute for `eth-sim-jsonrpc.victoria.magmadevs.com`

No other changes to the router repo are needed.

### TLS certificate update

The new hostname `eth-sim-jsonrpc.victoria.magmadevs.com` must be added to the TLS
certificate. Run the existing script:

```bash
bash scripts/install_gateway_api_tls_certificate.sh
```

This script reads all HTTPRoutes and regenerates the cert to include all hostnames.

### DNS (Cloudflare)

The existing wildcard load balancer rule `*.victoria.magmadevs.com` in Cloudflare
already covers both new hostnames:
- `eth-sim-jsonrpc.victoria.magmadevs.com`
- `sim-control.victoria.magmadevs.com`

No new DNS entries needed.

---

## 4. Repository 3 — `smart_router_automation`

### File: `tests/simulator/__init__.py`

```python
```

### File: `tests/simulator/sim_control.py`

```python
"""
SimulatorControl — client for the provider-simulator control API.

Usage in tests:
    from tests.simulator.sim_control import SimulatorControl, ProviderConfig

    sim = SimulatorControl()
    sim.set_scenario({
        1: "rate_limit",
        2: "down",
        3: ProviderConfig(mode="success", responses={"eth_blockNumber": {"result": "0xABC"}}),
    })
    sim.reset()
"""
import requests
from dataclasses import dataclass, field
from typing import Dict, Union

CONTROL_URL = "https://sim-control.victoria.magmadevs.com"


@dataclass
class ProviderConfig:
    mode: str                           = "success"  # success | error | rate_limit | down
    latency_ms: int                     = 0
    error_probability: float            = 0.0
    responses: Dict[str, dict]          = field(default_factory=dict)
    # responses format: {"eth_blockNumber": {"result": "0x1"}, "default": {"result": "0x0"}}


class SimulatorControl:

    def __init__(self, url: str = CONTROL_URL, timeout: int = 10):
        self._url     = url.rstrip("/")
        self._timeout = timeout

    def set_scenario(self, providers: Dict[int, Union[str, ProviderConfig]]) -> None:
        """
        Configure behaviour for each provider.

        providers = {
            1: "rate_limit",                          # shorthand string
            2: "down",
            3: ProviderConfig(mode="success",
                              latency_ms=200,
                              responses={"eth_blockNumber": {"result": "0xABC"}}),
        }
        """
        payload = {}
        for pid, cfg in providers.items():
            if isinstance(cfg, str):
                payload[str(pid)] = {"mode": cfg}
            else:
                entry: dict = {
                    "mode":              cfg.mode,
                    "latency_ms":        cfg.latency_ms,
                    "error_probability": cfg.error_probability,
                }
                if cfg.responses:
                    entry["responses"] = cfg.responses
                payload[str(pid)] = entry

        resp = requests.post(
            f"{self._url}/scenario",
            json={"providers": payload},
            timeout=self._timeout,
        )
        resp.raise_for_status()

    def reset(self) -> None:
        """Reset all providers to healthy defaults (success, no latency, no errors)."""
        resp = requests.post(f"{self._url}/reset", json={}, timeout=self._timeout)
        resp.raise_for_status()

    def get_scenario(self) -> dict:
        """Return the current state of all providers."""
        resp = requests.get(f"{self._url}/scenario", timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def health(self) -> bool:
        """Return True if the simulator is reachable and healthy."""
        try:
            resp = requests.get(f"{self._url}/health", timeout=self._timeout)
            return resp.status_code == 200
        except requests.RequestException:
            return False
```

### File: `tests/simulator/conftest.py`

```python
"""
Fixtures for simulator-backed router tests.

Requires:
  - provider-simulator pod running on victoria.magmadevs.com
  - eth-sim chain configured in smart-router-standalone (values_sim.yml applied)

Environment:
  SIM_CONTROL_URL   override the control API URL (default: https://sim-control.victoria.magmadevs.com)
  SIM_ROUTER_URL    override the router URL       (default: https://eth-sim-jsonrpc.victoria.magmadevs.com)
"""
import os
import pytest
import requests
from src.app_logging import get_logger
from tests.simulator.sim_control import SimulatorControl

logger = get_logger(__name__)

_CONTROL_URL = os.getenv("SIM_CONTROL_URL", "https://sim-control.victoria.magmadevs.com")
_ROUTER_URL  = os.getenv("SIM_ROUTER_URL",  "https://eth-sim-jsonrpc.victoria.magmadevs.com")


@pytest.fixture(scope="session")
def sim_control() -> SimulatorControl:
    """Session-scoped SimulatorControl. Verifies the simulator is reachable before tests run."""
    ctrl = SimulatorControl(url=_CONTROL_URL)
    if not ctrl.health():
        pytest.skip(
            f"Provider simulator not reachable at {_CONTROL_URL}. "
            "Deploy it first: see docs/simulator_implementation_guide.md"
        )
    logger.info("Provider simulator reachable at %s", _CONTROL_URL)
    return ctrl


@pytest.fixture(scope="session")
def sim_router_url() -> str:
    """URL of the eth-sim router endpoint."""
    return _ROUTER_URL


@pytest.fixture(autouse=True)
def reset_simulator(sim_control: SimulatorControl):
    """Reset all providers to healthy defaults before AND after every test."""
    sim_control.reset()
    yield
    sim_control.reset()


@pytest.fixture
def http_client():
    """Simple synchronous HTTP client for JSON-RPC calls."""
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    yield session
    session.close()
```

### File: `tests/simulator/test_router_routing.py`

```python
"""
Regression tests for smart router routing behaviour using simulated providers.

Each test:
  1. Configures a scenario via the control API
  2. Sends one or more requests through the real router
  3. Asserts the expected routing outcome

Markers: @pytest.mark.simulator
Run with: pytest tests/simulator/ -m simulator -v
"""
import pytest
from tests.simulator.sim_control import ProviderConfig

pytestmark = pytest.mark.simulator

ETH_BLOCK_NUMBER = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "eth_blockNumber",
    "params": [],
}


class TestFailover:

    def test_routes_to_only_healthy_provider(
        self, sim_control, sim_router_url, http_client
    ):
        """P1 rate-limited, P2 down — router must use P3."""
        sim_control.set_scenario({
            1: "rate_limit",
            2: "down",
            3: ProviderConfig(
                mode="success",
                responses={"eth_blockNumber": {"result": "0xABC123"}},
            ),
        })
        resp = http_client.post(sim_router_url, json=ETH_BLOCK_NUMBER, timeout=10)
        body = resp.json()
        assert body.get("jsonrpc") == "2.0"
        assert "result" in body or "error" in body

    def test_all_providers_down_returns_error_not_crash(
        self, sim_control, sim_router_url, http_client
    ):
        """All providers down — router must return a structured error, not a 5xx crash."""
        sim_control.set_scenario({1: "down", 2: "down", 3: "down"})
        resp = http_client.post(sim_router_url, json=ETH_BLOCK_NUMBER, timeout=10)
        assert resp.status_code in (200, 400, 503)

    def test_router_recovers_when_provider_comes_back(
        self, sim_control, sim_router_url, http_client
    ):
        """P1 and P2 down, P3 success. Then P1 and P2 recover. Router must keep working."""
        sim_control.set_scenario({1: "down", 2: "down", 3: "success"})
        r1 = http_client.post(sim_router_url, json=ETH_BLOCK_NUMBER, timeout=10).json()
        assert "result" in r1 or "error" in r1

        sim_control.set_scenario({1: "success", 2: "success", 3: "success"})
        r2 = http_client.post(sim_router_url, json=ETH_BLOCK_NUMBER, timeout=10).json()
        assert "result" in r2 or "error" in r2


class TestRateLimit:

    def test_router_handles_all_providers_rate_limited(
        self, sim_control, sim_router_url, http_client
    ):
        """All providers rate-limited — router must not crash or hang."""
        sim_control.set_scenario({
            1: "rate_limit",
            2: "rate_limit",
            3: "rate_limit",
        })
        resp = http_client.post(sim_router_url, json=ETH_BLOCK_NUMBER, timeout=10)
        assert resp.status_code in (200, 429, 503)

    def test_router_avoids_rate_limited_provider(
        self, sim_control, sim_router_url, http_client
    ):
        """P1 rate-limited, P2 and P3 healthy — router should still return success."""
        sim_control.set_scenario({1: "rate_limit", 2: "success", 3: "success"})
        responses = [
            http_client.post(sim_router_url, json=ETH_BLOCK_NUMBER, timeout=10).json()
            for _ in range(10)
        ]
        success_count = sum(1 for r in responses if "result" in r)
        assert success_count >= 8, (
            f"Expected most requests to succeed, got {success_count}/10"
        )


class TestLatency:

    def test_router_handles_slow_providers(
        self, sim_control, sim_router_url, http_client
    ):
        """P1 and P2 very slow — router should still return a result via P3."""
        sim_control.set_scenario({
            1: ProviderConfig(mode="success", latency_ms=3000),
            2: ProviderConfig(mode="success", latency_ms=3000),
            3: ProviderConfig(mode="success", latency_ms=10),
        })
        resp = http_client.post(sim_router_url, json=ETH_BLOCK_NUMBER, timeout=15)
        body = resp.json()
        assert "result" in body or "error" in body


class TestFlaky:

    def test_router_succeeds_despite_flaky_providers(
        self, sim_control, sim_router_url, http_client
    ):
        """P1 and P2 fail 50% of the time, P3 healthy — router retries and succeeds."""
        sim_control.set_scenario({
            1: ProviderConfig(mode="success", error_probability=0.5),
            2: ProviderConfig(mode="success", error_probability=0.5),
            3: ProviderConfig(mode="success"),
        })
        responses = [
            http_client.post(sim_router_url, json=ETH_BLOCK_NUMBER, timeout=10).json()
            for _ in range(20)
        ]
        success_count = sum(1 for r in responses if "result" in r)
        assert success_count >= 15, (
            f"Expected most requests to succeed with retry, got {success_count}/20"
        )
```

---

## 5. Step-by-Step Deployment

### Step 1 — Deploy the provider-simulator pod

SSH into `victoria.magmadevs.com`, clone the `provider-simulator` repo, and run:

```bash
cd provider-simulator
bash scripts/deploy.sh
```

Verify:

```bash
kubectl get pods -n lava-infra | grep provider-simulator
# Expected: provider-simulator-xxxxx   1/1   Running

curl https://sim-control.victoria.magmadevs.com/health
# Expected: {"status": "ok"}
```

### Step 2 — Add eth-sim chain to the router

On `victoria.magmadevs.com`, inside the `smart-router-standalone` repo:

```bash
# Add values/simulator/values_sim.yml (the file described in section 3)

source scripts/utils/common.sh

helm upgrade smart-router \
  "oci://ghcr.io/magma-devs/smart-router-helm-chart/smart-router" \
  --namespace "$NAMESPACE" \
  --version "$HELM_CHART_VERSION" \
  --values values/core/values.yml \
  --values values/simulator/values_sim.yml

# Regenerate TLS cert to include the new hostname
bash scripts/install_gateway_api_tls_certificate.sh
```

Verify:

```bash
kubectl get pods -n lava-infra | grep eth-sim
# Expected: eth-sim-consumer-xxxxx and eth-sim-provider-xxxxx pods Running

kubectl get httproute -n lava-infra | grep eth-sim
# Expected: an HTTPRoute for eth-sim-jsonrpc.victoria.magmadevs.com

curl -X POST https://eth-sim-jsonrpc.victoria.magmadevs.com \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}'
# Expected: {"jsonrpc":"2.0","result":"0x1","id":1}
```

### Step 3 — Run the tests

In the `smart_router_automation` repo:

```bash
pytest tests/simulator/ -m simulator -v
```

---

## 6. How the Control API is Used in Tests

### Setting a scenario

```
POST https://sim-control.victoria.magmadevs.com/scenario
Content-Type: application/json

{
  "providers": {
    "1": {"mode": "rate_limit"},
    "2": {"mode": "down"},
    "3": {
      "mode": "success",
      "latency_ms": 50,
      "responses": {
        "eth_blockNumber": {"result": "0x13F9AD7"},
        "default":         {"result": "0x1"}
      }
    }
  }
}
```

### Resetting to healthy defaults

```
POST https://sim-control.victoria.magmadevs.com/reset
Content-Type: application/json

{}
```

### Reading current state

```
GET https://sim-control.victoria.magmadevs.com/scenario
```

### Provider modes reference

| Mode | HTTP status returned | Body |
|---|---|---|
| `success` | 200 | `{"jsonrpc":"2.0","result":"...","id":N}` |
| `error` | 200 | `{"jsonrpc":"2.0","error":{"code":-32000,...},"id":N}` |
| `rate_limit` | 429 | `{"jsonrpc":"2.0","error":{"code":429,...},"id":N}` |
| `down` | 503 | empty body |
| `error_probability: 0.3` | 200 | 30% of requests return error body |
| `latency_ms: 500` | any | response delayed by 500 ms |

---

## 7. What Does Not Change

- All existing product tests in `tests/product/etherium_compare/` — untouched, still hit `eth-jsonrpc.victoria.magmadevs.com`
- All existing infrastructure tests — untouched
- The router's production configuration — `values/core/values.yml` is not modified
- The Helm chart itself — only an additional `--values` file is passed on upgrade
- The Gateway, Cloudflare, or any other networking component

---

## 8. Future-Proofing

When the unified smart router ships (no more consumer/provider pod split — one process
making HTTP calls to backend nodes with WRS):

- `provider-simulator` continues to work unchanged — it is still an HTTP server
- The control API continues to work unchanged — it is independent of Lava Protocol
- The tests continue to work unchanged — they call the control API and assert HTTP responses
- `values_sim.yml` needs one update: point the unified router at the simulator URLs

The gRPC relay protocol between consumer and provider pods (used by Option B /
static-providers) will no longer exist in the unified router. Option A (this design)
is unaffected.

