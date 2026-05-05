# Provider Simulator - Complete Architecture Guide for Beginners

## Table of Contents
1. [What This Project Does](#what-this-project-does)
2. [The Big Picture](#the-big-picture)
3. [Architecture Overview](#architecture-overview)
4. [Module Breakdown](#module-breakdown)
5. [Class Relationships](#class-relationships)
6. [Data Flows](#data-flows)
7. [Deployment Process](#deployment-process)
8. [How Everything Connects](#how-everything-connects)

---

## What This Project Does

Imagine you're testing a smart router (a system that routes requests to blockchain providers). The problem is:
- Testing with real blockchain providers (Google, QuickNode) is **slow** and **unreliable**
- You can't easily simulate failures, delays, or errors
- You can't control what responses you get back

**Solution: This simulator!**

This project creates a **fake blockchain provider** that runs on your server. It:
- Pretends to be 3 different blockchain providers
- Can be told to fail, delay, or return errors
- Gives you a control API to set scenarios before each test
- Works perfectly for integration testing the router

The simulator stores its public domain in one place: `config/base-domain.env`.
Set `BASE_DOMAIN` there once, and derive public URLs from it:
- `https://sim-control.${BASE_DOMAIN}`
- `https://eth-sim-jsonrpc.${BASE_DOMAIN}`

---

## The Big Picture

### Without the Simulator (What Tests Do Now)

```
Your Test
  ↓
Router (on `${BASE_DOMAIN}`)
  ↓
Real Blockchain Node (Google/QuickNode)
  ↓
Slow, unreliable, expensive
```

### With the Simulator (What Tests Will Do)

```
Your Test
  ├─ 1. Call Control API: "Make provider 1 fail, provider 2 return success"
  │     (POST https://sim-control.${BASE_DOMAIN}/scenario)
  │
  └─ 2. Call Router: "Get the latest block number"
       (POST https://eth-sim-jsonrpc.${BASE_DOMAIN}/)
       ↓
       Router picks a healthy provider
       ↓
       Simulator returns the response you configured
       ↓
       Test validates the result

Everything is controllable, fast, and reliable!
```

---

## Architecture Overview

### The Three Layers

```
┌─────────────────────────────────────────────────────────────────┐
│                         TESTING LAYER                           │
│  (smart_router_automation repo)                                 │
│  Tests call the control API to set scenarios, then test routing │
└────────────────────────────┬──────────────────────────────────┘
                             │
                             │ HTTPS Requests
                             ↓
┌─────────────────────────────────────────────────────────────────┐
│                      INFRASTRUCTURE LAYER                        │
│  (provider-simulator repo - what we just built)                 │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  One Pod Running 4 HTTP Servers                         │   │
│  │                                                          │   │
│  │  Server 1: Port 18545 (JSONRPCHandler)                 │   │
│  │  Server 2: Port 18546 (JSONRPCHandler)                 │   │
│  │  Server 3: Port 18547 (JSONRPCHandler)                 │   │
│  │  Server 4: Port 19000 (ControlHandler) ← Control API   │   │
│  │                                                          │   │
│  │  All 4 servers share state: ProviderState[1,2,3]       │   │
│  └─────────────────────────────────────────────────────────┘   │
└────────────────────────────┬──────────────────────────────────┘
                             │
                             │ HTTP Requests
                             ↓
┌─────────────────────────────────────────────────────────────────┐
│                      ROUTER LAYER                               │
│  (smart-router-standalone repo)                                 │
│  The actual smart router that routes requests to our simulator  │
└─────────────────────────────────────────────────────────────────┘
```

### File Structure

```
provider-simulator/
├── config/
│   └── base-domain.env         ← Single source of truth for `BASE_DOMAIN`
│   └── values_sim.yml           ←  Example router values to wire simulator into smart-router
├── server.py                     ← Main Python application
├── stubs.py                      ← Predefined JSON-RPC responses for methods
├── constants.py                  ← Constants like provider ports
├── Dockerfile                    ← How to build the container
├── requirements.txt              ← Python dependencies (empty - no deps!)
├── k8s/
│   ├── deployment.yml           ← K8s Deployment (manages the pod)
│   ├── service.yml              ← K8s Service (internal networking)
│   └── httproute-control.yml    ← K8s HTTPRoute (public exposure)
├── scripts/
│   └── deploy.sh                ← Deployment automation script
├── tests/                     ← (Optional) Unit tests for the simulator
└── docs/
    └── ARCHITECTURE_GUIDE.md    ← This file!
```

---

## Module Breakdown

### 1. `server.py` - The Main Application

This is where all the magic happens. It contains three main pieces:

#### **Part 1: ProviderState (The State Container)**

```python
@dataclass
class ProviderState:
    mode: str                      # What this provider is doing
    latency_ms: int               # How long to delay responses
    error_probability: float       # Chance of error (0.0 to 1.0)
    responses: Dict[str, Any]     # Custom responses for methods
    lock: threading.Lock          # Thread safety (multi-threading)
```

**What it does:**
- Holds the **current state** of one blockchain provider
- Each provider (1, 2, 3) has its own ProviderState instance
- The `lock` ensures that if two threads try to change it at the same time, they don't corrupt the data

**Example:**
```python
provider_1_state = ProviderState(
    mode="rate_limit",           # This provider is being rate-limited
    latency_ms=100,              # Add 100ms delay to responses
    error_probability=0.2,        # 20% of requests fail randomly
    responses={"eth_blockNumber": {"result": "0x1234"}}
)
```

**Methods:**
- `snapshot()` - Get a thread-safe copy of the current state
- `update(cfg)` - Change the state (used by control API)
- `reset_scenario()` - Return scenario config to healthy defaults
- `clear_history()` - Clear history and counters only
- `stats()` / `get_history()` - Read counters and call history snapshots

---

#### **Part 2: JSONRPCHandler (The Blockchain Provider Server)**

```python
class JSONRPCHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Receives requests from the router
        # Returns fake blockchain responses
```

**What it does:**
- Listens on ports **18545, 18546, 18547** (one port per provider)
- Receives JSON-RPC requests (like `eth_blockNumber`)
- Returns responses based on the current ProviderState

**Example flow:**
```
1. Router sends: POST /
   Body: {"jsonrpc":"2.0","method":"eth_blockNumber","id":1}

2. JSONRPCHandler.do_POST() runs:
   a. Check ProviderState for mode="down"? → Return 503 error
   b. Check ProviderState for latency_ms=500? → Sleep 500ms
   c. Check ProviderState for mode="rate_limit"? → Return 429 error
   d. Check error_probability=0.3? → 30% chance return error
   e. Check responses dict for "eth_blockNumber"? → Return custom response
   f. Default: Return method-specific stub from `METHOD_DEFAULTS`
      (`eth_blockNumber` → `"0x1312D00"`, `eth_getBlockByNumber` → block object)

3. Send response back to router
```

**Key Behaviors:**
- `mode="down"` → Return HTTP 503 (provider is offline)
- `mode="rate_limit"` → Return HTTP 429 (too many requests)
- `mode="error"` → Return JSON-RPC error in response body
- `mode="success"` → Return successful JSON-RPC result
- `latency_ms=500` → Delay response by 500 milliseconds
- `error_probability=0.3` → 30% of requests randomly error

---

#### **Part 3: ControlHandler (The Control API Server)**

```python
class ControlHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Receives control commands from tests
        # Updates ProviderState based on commands
    
    def do_GET(self):
        # Allows tests to read current state
```

**What it does:**
- Listens on port **19000**
- Provides endpoints for tests to configure the simulator

**Endpoints:**

| Method | Path | Purpose | Example |
|--------|------|---------|---------|
| POST | `/scenario` | Configure all providers | `{"providers": {"1": {"mode": "down"}, "2": {"mode": "success"}}}` |
| POST | `/reset` | Reset scenario config only | `{}` |
| POST | `/history/clear` | Clear history/counters only | `{}` |
| POST | `/reset/all` | Reset scenario config and clear history | `{}` |
| GET | `/scenario` | Read current state | Returns: `{"providers": {"1": {"mode": "down"}, ...}}` |
| GET | `/health` | Health check | Returns: `{"status": "ok"}` |
| GET | `/stats` | Read per-provider counters | Returns call counts by status |
| GET | `/history` | Read merged ordered call history | Supports filtering by time/provider/method/status/request_id |

**Example flow:**
```
1. Test calls: POST /scenario
   Body: {"providers": {"1": {"mode": "rate_limit"}}}

2. ControlHandler.do_POST() runs:
   a. Parse the JSON body
   b. For provider ID "1", call state.update({"mode": "rate_limit"})
   c. Return {"status": "ok"}

3. Now future requests to provider 1 will get rate-limited!
```

---

#### **Part 4: Main Function (The Orchestrator)**

```python
def main():
    # 1. Create one ProviderState for each provider
    states = {pid: ProviderState() for pid in PROVIDER_PORTS}
    
    # 2. Start 3 JSONRPCHandlers on ports 18545, 18546, 18547
    servers = []
    for pid, port in PROVIDER_PORTS.items():
        srv = HTTPServer(("0.0.0.0", port), JSONRPCHandler)
        srv.state = states[pid]  # Give each handler access to its state
        servers.append(srv)
    
    # 3. Start 1 ControlHandler on port 19000
    ctrl = HTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    ctrl.provider_states = states  # Give control API access to all states
    servers.append(ctrl)
    
    # 4. Start each server in a separate thread
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    
    # 5. Keep running forever (or until Ctrl+C)
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        for s in servers:
            s.shutdown()
```

**What it does:**
- Creates the shared ProviderState objects
- Starts 4 HTTP servers (3 providers + 1 control API)
- Each server runs in its own thread
- All servers share the same ProviderState objects

---

### 2. `Dockerfile` - Container Configuration

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY *.py .
EXPOSE 18545 18546 18547 19000
CMD ["python", "-u", "server.py"]
```

**What it does:**
- **`FROM python:3.12-slim`** - Start with a minimal Python 3.12 image
- **`WORKDIR /app`** - Set working directory inside container
- **`COPY *.py .`** - Copy `server.py` plus supporting modules like `constants.py` and `stubs.py`
- **`EXPOSE 18545 18546 18547 19000`** - Document which ports are used
- **`CMD ["python", "-u", "server.py"]`** - Run the simulator with unbuffered stdout so logs appear immediately

**Why minimal?**
- `3.12-slim` is only ~150MB (vs `3.12-full` which is ~900MB)
- We don't need any extra packages - just Python

---

### 3. `k8s/deployment.yml` - Kubernetes Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: provider-simulator
  namespace: lava-infra
spec:
  replicas: 1
  containers:
    - name: provider-simulator
      image: provider-simulator:latest
      ports:
        - containerPort: 18545
        - containerPort: 18546
        - containerPort: 18547
        - containerPort: 19000
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
          port: 19000
      livenessProbe:
        httpGet:
          path: /health
          port: 19000
```

**What it does:**
- Tells Kubernetes to run 1 pod with our simulator
- Exposes 4 ports from the container
- Sets CPU/memory limits (testing workload is light)
- **Readiness probe**: "Is this pod ready to receive traffic?" (checks `/health` every 5 seconds)
- **Liveness probe**: "Is this pod still alive?" (checks `/health` every 15 seconds, restarts if fails)

**Simple explanation:**
- If the pod crashes, Kubernetes automatically restarts it
- If the pod is slow to start, Kubernetes waits for readiness probe to pass
- If `/health` endpoint stops responding, Kubernetes kills and restarts the pod

---

### 4. `k8s/service.yml` - Internal Networking

```yaml
apiVersion: v1
kind: Service
metadata:
  name: provider-simulator
  namespace: lava-infra
spec:
  type: ClusterIP
  ports:
    - name: provider-1
      port: 18545
    - name: provider-2
      port: 18546
    - name: provider-3
      port: 18547
    - name: control
      port: 19000
```

**What it does:**
- Creates an internal Kubernetes service (like a load balancer inside the cluster)
- Other pods can reach it via: `provider-simulator.lava-infra.svc.cluster.local:18545`
- Distributes traffic if multiple pods exist (though we only have 1)

**Simple explanation:**
- `ClusterIP` means "only accessible inside the Kubernetes cluster"
- The router pods use this service to reach our simulator
- If we scale to 2 pods, traffic automatically load-balances

---

### 5. `k8s/httproute-control.yml` - Public Exposure

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: sim-control-httproute
  namespace: lava-infra
spec:
  parentRefs:
    - name: sr-gateway
  hostnames:
    - "sim-control.${BASE_DOMAIN}"
  rules:
    - backendRefs:
        - name: provider-simulator
          port: 19000
```

**What it does:**
- Makes the control API publicly accessible
- Routes `https://sim-control.${BASE_DOMAIN}/*` to port 19000
- Uses the same gateway as the smart router

**Simple explanation:**
- Tests on external machines can call `sim-control.${BASE_DOMAIN}`
- The gateway forwards requests to our port 19000
- HTTPS and DNS are handled by the gateway

---

### 6. `scripts/deploy.sh` - Deployment Script

```bash
#!/bin/bash

# 1. Build Docker image
docker build -t provider-simulator:latest .

# 2. Import into MicroK8s
docker save provider-simulator:latest | microk8s ctr image import -

# 3. Apply Kubernetes manifests
kubectl apply -f k8s/deployment.yml
kubectl apply -f k8s/service.yml
kubectl apply -f <rendered httproute based on BASE_DOMAIN>

# 4. Wait for pod to be ready
kubectl rollout status deployment/provider-simulator -n lava-infra --timeout=60s

# 5. Restart deployment so the new latest image is actually used
kubectl rollout restart deployment/provider-simulator -n lava-infra
kubectl rollout status deployment/provider-simulator -n lava-infra --timeout=60s
```

**What it does:**
- Automates the entire deployment process
- Builds the Docker image
- Uploads it to MicroK8s (Kubernetes on the same machine)
- Creates the Deployment, Service, and rendered HTTPRoute
- Waits, restarts the deployment, and waits again so the updated `latest` image is used

**Simple explanation:**
- One script does everything needed to deploy
- No manual steps = fewer mistakes
- Verifies pod is running before declaring success

---

## Class Relationships

### Relationship Diagram

```
┌──────────────────────────┐
│    ProviderState         │
│  (holds state)           │
│                          │
│  - mode: str             │
│  - latency_ms: int       │
│  - responses: dict       │
│  - lock: Lock            │
│                          │
│  + snapshot()            │
│  + update(cfg)           │
│  + reset_scenario()      │
│  + clear_history()       │
└──────────────┬───────────┘
               │
      ┌────────┴────────┐
      │                 │
      ↓                 ↓
┌─────────────┐  ┌──────────────┐
│JSONRPCHandle│  │ControlHandler│
│             │  │              │
│ Serves:     │  │ Serves:      │
│ 18545-18547 │  │ 19000        │
│             │  │              │
│ Gets state  │  │ Modifies     │
│ from        │  │ state from   │
│ do_POST()   │  │ do_POST()    │
└─────────────┘  └──────────────┘

main() function:
├─ Creates 3 ProviderState objects
├─ Creates 3 JSONRPCHandler servers (one state each)
├─ Creates 1 ControlHandler server (all states)
└─ Starts all 4 in separate threads
```

### How They Interact

```
Scenario: Test sets provider 1 to "rate_limit"

1. Test calls POST /scenario
   ↓
2. ControlHandler.do_POST() receives request
   ↓
3. ControlHandler looks up: states["1"]
   ↓
4. ControlHandler calls: states["1"].update({"mode": "rate_limit"})
   ↓
5. ProviderState[1].lock.acquire()
   ProviderState[1].mode = "rate_limit"
   ProviderState[1].lock.release()
   ↓
6. ControlHandler responds: {"status": "ok"}
   ↓
7. Router sends request to provider 1
   ↓
8. JSONRPCHandler.do_POST() for provider 1 runs
   ↓
9. JSONRPCHandler calls: state.snapshot()
   ↓
10. ProviderState[1].lock.acquire()
    Returns: {"mode": "rate_limit", "latency_ms": 0, ...}
    ProviderState[1].lock.release()
    ↓
11. JSONRPCHandler checks: if snap["mode"] == "rate_limit"
    ↓
12. JSONRPCHandler returns HTTP 429
    ↓
13. Router receives 429 and tries next provider
```

---

## Data Flows

### Flow 1: Test Setting a Scenario

```
┌────────────────────────────────────────────────────────┐
│  Test (smart_router_automation)                        │
└────────────────────────┬───────────────────────────────┘
                         │
        ┌────────────────┴────────────────┐
        │ POST /scenario with scenario    │
        │ {"providers": {                 │
        │  "1": {"mode": "rate_limit"}    │
        │ }}                              │
        ↓
┌────────────────────────────────────────────────────────┐
│  HTTPS Reverse Proxy (Gateway)                         │
│  Converts: sim-control.${BASE_DOMAIN}/scenario │
│  To: provider-simulator.lava-infra:19000/scenario      │
└────────────────┬───────────────────────────────────────┘
                 │
        ┌────────┴───────┐
        │ Internal HTTP  │
        ↓
┌────────────────────────────────────────────────────────┐
│  ControlHandler on port 19000                          │
│                                                        │
│  1. Parse request body → {"providers": {"1": {...}}}   │
│  2. Access self.server.provider_states["1"]            │
│  3. Call state["1"].update({"mode": "rate_limit"})     │
│  4. Acquire lock                                       │
│  5. Set state["1"].mode = "rate_limit"                 │
│  6. Release lock                                       │
│  7. Return {"status": "ok"}                            │
└────────────────┬───────────────────────────────────────┘
                 │
        ┌────────┴───────┐
        │ HTTP 200 OK    │
        │ {"status": "ok"}│
        ↓
┌────────────────────────────────────────────────────────┐
│  Test                                                  │
│  ✓ Scenario set                                        │
└────────────────────────────────────────────────────────┘
```

### Flow 2: Router Requesting from Provider

```
┌────────────────────────────────────────────────────────┐
│  Router (smart-router-standalone)                      │
│  Wants: eth_blockNumber from provider 1                │
└────────────────┬───────────────────────────────────────┘
                 │
        ┌────────┴───────┐
        │ POST / with    │
        │ {"method":     │
        │  "eth_blockNumber"}│
        │                │
        │ To: provider-  │
        │ simulator.lava-│
        │ infra:18545    │
        ↓
┌────────────────────────────────────────────────────────┐
│  JSONRPCHandler on port 18545                          │
│                                                        │
│  1. Receive POST request                               │
│  2. Call state.snapshot() → {mode: "rate_limit", ...}  │
│  3. Check: if mode == "down"? NO                       │
│  4. Check: if latency_ms > 0? NO                       │
│  5. Check: if mode == "rate_limit"? YES!               │
│  6. Return HTTP 429 with error message                 │
└────────────────┬───────────────────────────────────────┘
                 │
        ┌────────┴───────────┐
        │ HTTP 429           │
        │ Too many requests  │
        ↓
┌────────────────────────────────────────────────────────┐
│  Router                                                │
│  ✗ Provider 1 failed (rate limited)                    │
│  ✓ Tries provider 2 (which is healthy)                 │
│  ✓ Gets response from provider 2                       │
└────────────────────────────────────────────────────────┘
```

### Flow 3: Full End-to-End Test

```
Test Flow:
1. Control API: Set provider 1 to "down", provider 2 to "success"
   ↓
2. Router tries provider 1 → Gets 503 (down) → Tries next
   ↓
3. Router tries provider 2 → Gets 200 with result → Success
   ↓
4. Test asserts: "Request succeeded by using provider 2"

This tests if the router correctly fails over from bad providers!
```

---

## Deployment Process

### Step 1: Build Phase

```
Docker Build:
├─ Create image from python:3.12-slim base
├─ Copy server.py into image
├─ Mark ports 18545, 18546, 18547, 19000 as exposed
└─ Set default command: python server.py

Result: Docker image "provider-simulator:latest"
```

### Step 2: Registry Phase

```
Import to MicroK8s:
├─ Save image to tar file
├─ Pipe to MicroK8s container registry
└─ Image now available in cluster

Result: Image available for Kubernetes to use
```

### Step 3: Kubernetes Phase

```
Apply Manifests:
├─ deployment.yml
│  ├─ Create Deployment resource
│  ├─ Tells Kubernetes: "Run 1 pod of provider-simulator:latest"
│  └─ Pod automatically restarts if it crashes
│
├─ service.yml
│  ├─ Create Service resource
│  ├─ Tells Kubernetes: "Route internal requests to this pod"
│  └─ Accessible via: provider-simulator.lava-infra.svc.cluster.local
│
└─ httproute-control.yml
   ├─ Create HTTPRoute resource
   ├─ Tells Gateway: "Route sim-control.${BASE_DOMAIN} to port 19000"
   └─ Public HTTPS access configured

Result: Pod is running, services are configured, control API is public
```

### Step 4: Verification Phase

```
rollout status:
├─ Waits for Deployment to be ready
├─ Checks readiness probe
└─ Confirms pod is healthy

Result: Deployment complete!
```

---

## How Everything Connects

### Full System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     THE INTERNET                            │
├─────────────────────────────────────────────────────────────┤
│                        Tests                                │
│                (smart_router_automation)                    │
└────────────┬────────────────────────────────┬───────────────┘
             │                                │
  ┌──────────┴──────────┐      ┌─────────────┴──────────┐
  │ HTTPS POST          │      │ HTTPS POST              │
  │ Route requests      │      │ Control API requests    │
  │ eth-sim-jsonrpc...  │      │ sim-control.victoria... │
  │                     │      │                         │
  ↓                     ↓      ↓
┌──────────────────────────────────────────────────────────────┐
│         Cloudflare + Gateway (sr-gateway)                    │
│  - HTTPS termination                                         │
│  - TLS certificate management                               │
│  - Hostname routing                                          │
└────────┬──────────────────────────────────┬──────────────────┘
         │                                  │
         │ Route to consumer              │ Route to control
         │ (eth-sim-jsonrpc)              │ (sim-control)
         │                                │
         ↓                                ↓
┌──────────────────────────────────────────────────────────────┐
│                  K8s Service (lava-infra)                    │
│           provider-simulator (ClusterIP)                     │
│  - Port 18545 → JSONRPCHandler (provider 1)                  │
│  - Port 18546 → JSONRPCHandler (provider 2)                  │
│  - Port 18547 → JSONRPCHandler (provider 3)                  │
│  - Port 19000 → ControlHandler (control API)                 │
└────────┬─────────────────────────────────┬───────────────────┘
         │                                 │
  ┌──────┴──────┐                 ┌────────┴────────┐
  │ Router Calls│                 │ Test Calls      │
  │ JSON-RPC    │                 │ /scenario       │
  │ Methods     │                 │ /reset          │
  │             │                 │ /health         │
  ↓             ↓                 ↓
┌──────────────────────────────────────────────────────────────┐
│              Pod: provider-simulator                         │
│                                                              │
│  Python Process (server.py):                                │
│                                                              │
│  ┌────────────────┬──────────────┬─────────────────────┐   │
│  │ ProviderState  │ ProviderState│ ProviderState       │   │
│  │ [1]            │ [2]          │ [3]                 │   │
│  │                │              │                     │   │
│  │ - mode         │ - mode       │ - mode              │   │
│  │ - latency_ms   │ - latency_ms │ - latency_ms        │   │
│  │ - error_prob   │ - error_prob │ - error_prob        │   │
│  │ - responses    │ - responses  │ - responses         │   │
│  │ - lock         │ - lock       │ - lock              │   │
│  └────┬───────────┴──────┬───────┴────────┬───────────┘   │
│       │                  │                │                │
│       ↓                  ↓                ↓                │
│  ┌──────────────────────────────────────────────────────┐  │
│  │      JSONRPCHandler [1,2,3]                         │  │
│  │  ├─ Receives: {"method": "eth_blockNumber"}         │  │
│  │  ├─ Reads: state.snapshot()                         │  │
│  │  ├─ Checks: mode, latency, error_probability       │  │
│  │  └─ Responds: {...result...} or ...error...        │  │
│  └──────────────────────────────────────────────────────┘  │
│       ↑                                                     │
│       │                                                     │
│  ┌────────────────────────────────────────────────────┐   │
│  │     ControlHandler                                │   │
│  │  ├─ Receives: /scenario, /reset, /health         │   │
│  │  ├─ Modifies: state.update(cfg)                  │   │
│  │  ├─ Reads: state.snapshot()                      │   │
│  │  └─ Responds: {"status": "ok"} or {...state...}  │   │
│  └────────────────────────────────────────────────────┘   │
│                                                              │
└──────────────────────────────────────────────────────────────┘
        ↑
        │
  ┌─────┴──────┐
  │ Connected  │
  │ by:        │
  │ - Service  │
  │ - HTTPRoute│
  │ - Gateway  │
  └────────────┘
```

### Request Journey Example

```
Scenario: Test wants to verify failover behavior

1. TEST sends to Control API:
   POST https://sim-control.${BASE_DOMAIN}/scenario
   Body: {"providers": {"1": {"mode": "down"}, "2": {"mode": "success"}}}

2. Cloudflare routes → Gateway → Service:19000 → ControlHandler.do_POST()

3. ControlHandler:
   - Gets provider states
   - Updates state[1].mode = "down"
   - Updates state[2].mode = "success"
   - Returns 200 OK

4. TEST sends to Router:
   POST https://eth-sim-jsonrpc.${BASE_DOMAIN}/
   Body: {"method": "eth_blockNumber"}

5. Gateway routes → Service:18545 → JSONRPCHandler[1].do_POST()

6. JSONRPCHandler[1]:
   - Reads state[1].snapshot() → {mode: "down"}
   - Checks: if mode == "down"? YES!
   - Returns HTTP 503

7. Router receives 503 → Tries next provider

8. Gateway routes → Service:18546 → JSONRPCHandler[2].do_POST()

9. JSONRPCHandler[2]:
   - Reads state[2].snapshot() → {mode: "success"}
   - Checks all conditions → All pass
   - Returns HTTP 200 with result

10. Router receives 200 → Success!

11. TEST verifies:
    "Router correctly failed over from provider 1 to provider 2"
    ✓ TEST PASSED!
```

---

## Implementation Details

The high-level walkthrough above covers the main flow. This section documents specific behaviors that are easy to miss but matter when extending or debugging the simulator.

### `eth_getBlockByNumber` — block-number rewriting

Most stubs in `stubs.py` are static (e.g. `eth_blockNumber` always returns `"0x1312D00"`). But `eth_getBlockByNumber` is special: `JSONRPCHandler.do_POST` (server.py:249-255) rewrites `result["number"]` from `params[0]` of the request before returning. Named blocks map to fixed hex values:

| Named block in request | `result["number"]` returned |
|---|---|
| `latest` | `0x1312D00` |
| `earliest` | `0x0` |
| `pending` | `0x1312D01` |
| `safe` | `0x1312D00` |
| `finalized` | `0x1312CFF` |
| any hex (e.g. `0x42`) | echoed back unchanged |

The smart router's pruning verification compares the requested block number with the block object's `number` field. Without the rewrite, every `eth_getBlockByNumber` call would return `0x1312D00` regardless of input and the router would reject the response.

### `lava_header_*` — dynamic filter on `/history`

The `/history` endpoint accepts a dynamic-prefix filter: any query parameter beginning with `lava_header_` is interpreted as a captured-header filter. Underscores in the parameter name become hyphens when matching the actual header. Examples:

- `?lava_header_lava_stateful_api=true` → filter where the captured header `lava-stateful-api` equals `true`.
- `?lava_header_lava_consumer_ip=10.0.0.1&lava_header_lava_session=42` → both must match (AND).

Headers are captured at request time inside `JSONRPCHandler.do_POST`: every inbound header whose name starts with `lava-` (case-insensitive) is recorded in the history entry. Each `/history` entry exposes the full captured dict under `"lava_headers"`.

### `responses` lookup order in the success path

When `mode="success"`, the handler picks the result for a given method using this exact chain (server.py:243-244):

1. `state.responses[method]["result"]` — explicit per-method override
2. `state.responses["default"]["result"]` — explicit catch-all override
3. `METHOD_DEFAULTS[method]` — the static stub from `stubs.py`
4. `"0x1"` — final fallback for methods not in `METHOD_DEFAULTS`

This means setting `state.responses = {"default": {"result": "0xABC"}}` makes every method return `0xABC` unless an individual method has its own entry — useful for "all methods return X" test setups without enumerating each one.

### Per-handler `provider_id`

`main()` (server.py:504) attaches a `provider_id` attribute to each JSON-RPC server: `srv.provider_id = pid`. Inside `JSONRPCHandler.do_POST` it's read as `self.server.provider_id`. This is how a single handler class can be reused across the three provider servers — the running handler instance always knows which provider it is when it pushes into history.

### Top-level Python modules only

`Dockerfile` line 3 reads `COPY *.py .` — only `*.py` files at the repo root are copied into the container. Any new runtime module **must live at the top level** (not inside `lib/` or `simulator/` subpackages). This is intentional (the simulator stays flat and dependency-free) but can surprise contributors who reflexively organize new code into subdirectories.

---

## Summary

### The Big Picture (Elevator Pitch)

This project is a **controllable fake blockchain provider** for testing. It:

1. **Runs 3 fake providers** on a single pod (ports 18545-18547)
2. **Can be told to fail** via a control API (port 19000)
3. **Allows tests to verify router behavior** under various failure scenarios
4. **Deployed on Kubernetes** for production-grade reliability

### Key Components

| Component | Purpose | Location |
|-----------|---------|----------|
| **ProviderState** | Holds state per provider | server.py |
| **JSONRPCHandler** | Serves fake blockchain responses | server.py |
| **ControlHandler** | Receives configuration from tests | server.py |
| **Dockerfile** | Container definition | Dockerfile |
| **Deployment** | Kubernetes pod management | k8s/deployment.yml |
| **Service** | Internal networking | k8s/service.yml |
| **HTTPRoute** | Public API exposure | k8s/httproute-control.yml |
| **Deploy script** | Automation | scripts/deploy.sh |

### Data Flow Pattern

```
Tests → Control API → ProviderState → JSONRPCHandler → Router
                    ↑                                      ↓
                    └──────────────────────────────────────┘
```

Tests configure scenarios via the control API, which updates ProviderState. The JSONRPCHandler reads ProviderState and responds accordingly to the router.

---

## Next Steps for Learning

1. **Run locally** (next phase)
   - Start server.py on your machine
   - Curl the endpoints to understand behavior

2. **Deploy** (Phase 4)
   - Run the deploy script
   - Watch logs: `kubectl logs -f deployment/provider-simulator`

3. **Write tests** (Phase 3)
   - Use sim_control.py client to set scenarios
   - Assert router behavior

4. **Debug**
   - Check health: `curl https://sim-control.${BASE_DOMAIN}/health`
   - Read scenario: `curl https://sim-control.${BASE_DOMAIN}/scenario`
   - Watch pod logs: `kubectl logs -f pod/provider-simulator-xxxxx`

---

**That's it!** You now understand the complete architecture. 🎉

