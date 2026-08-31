# Provider Simulator - Quick Reference Card

## One-Page Cheat Sheet

---

## 🎯 What It Does
A **fake blockchain provider** pod that pretends to be 3 different providers on 3 ports. Tests configure it via a control API, then the router requests from it. Used for testing router behavior under various failure scenarios.

---

## 📊 Architecture at a Glance

```
Tests ─→ Control API (port 19000)  ─→ ControlHandler ─→ ProviderState[1,2,3]
              ↓
         Updates state
              ↓
Router ─→ Provider 1/2/3 (ports 18545-47) ─→ JSONRPCHandler ─→ Read state ─→ Return response
```

---

## 📁 Files & Their Purpose

| File | Purpose |
|------|---------|
| `server.py` | Main Python app (all classes) |
| `config/base-domain.env` | Single source of truth for `BASE_DOMAIN` |
| `Dockerfile` | Container image (python:3.12-slim) |
| `requirements.txt` | Python deps (empty - stdlib only) |
| `k8s/deployment.yml` | K8s pod management |
| `k8s/service.yml` | Internal networking |
| `k8s/httproute-control.yml` | Public API exposure |
| `scripts/deploy.sh` | Deploy automation |

---

## 🔑 Classes & Methods

### ProviderState
**Holds state of one provider**
- `snapshot()` → Get state safely (thread-safe)
- `update(cfg)` → Change scenario config
- `reset_scenario()` → Back to healthy scenario config
- `clear_history()` → Clear history and counters

**Instance variables:**
- `mode` → "success" | "error" | "rate_limit" | "down"
- `latency_ms` → Delay in milliseconds
- `error_probability` → Chance of error (0.0-1.0)
- `responses` → Custom responses per method
- `lock` → Thread safety

---

### JSONRPCHandler
**Serves fake blockchain responses (3 ports)**

**Main method: do_POST()**
1. Get state snapshot
2. Check mode == "down"? → Return 503
3. Check latency_ms? → Sleep
4. Check mode == "rate_limit"? → Return 429
5. Check error_probability? → Random error
6. Check custom responses? → Return configured
7. Return default result

**Port mapping:**
- Port 18545 → Provider 1
- Port 18546 → Provider 2
- Port 18547 → Provider 3

---

### ControlHandler
**Configure simulator (port 19000)**

**Endpoints:**
- `POST /scenario` → Set provider modes
- `POST /reset` → Reset scenario config to healthy
- `POST /history/clear` → Clear history/counters only
- `POST /reset/all` → Reset scenario + clear history
- All three take an optional `{"pool": "<pool>"}` body. Without it they clear
  every pool. With it they touch that pool's providers only. An unknown pool
  is a 400 naming the pools that exist.
- Chain heights follow a weaker rule, and only `/reset` and `/reset/all` move
  them at all — clearing history never does. A height is one value per chain,
  shared by every pool on it, so scoping narrows which chains are rewound but
  cannot stop a sibling pool on the same chain from seeing it.
- `GET /scenario` → Read current state
- `GET /health` → Health check
- `GET /stats` → Per-provider counters
- `GET /history` → Ordered call history with filters

**Body format for /scenario:**
```json
{
  "providers": {
    "1": {"chain_family": "eth", "mode": "rate_limit"},
    "2": {"chain_family": "eth", "mode": "down"},
    "3": {"chain_family": "eth", "mode": "success", "latency_ms": 100}
  }
}
```

---

## 🔄 Provider Modes

| Mode | HTTP Status | Meaning | Use Case |
|------|-------------|---------|----------|
| `success` | 200 | Normal response | Working provider |
| `error` | 200 (error body) | JSON-RPC error | Provider failure |
| `rate_limit` | 429 | Too many requests | Rate limiting |
| `down` | 503 | Service unavailable | Provider offline |

**Modifiers:**
- `latency_ms: 100` → Delay response 100ms
- `error_probability: 0.3` → 30% of requests error
- `responses: {...}` → Custom response per method

---

## 📊 Request Flow Summary

### Control Request (Test → Control API)
```
Test: POST /scenario {providers: {1: {mode: "down"}}}
       ↓
ControlHandler parses and validates
       ↓
Updates: provider_states["1"].update({mode: "down"})
       ↓
Returns: 200 {"status": "ok"}
```

### Router Request (Router → Simulator)
```
Router: POST / {method: "eth_blockNumber"}
       ↓
JSONRPCHandler.do_POST() runs
       ↓
state.snapshot() → {mode: "down", ...}
       ↓
Checks: mode == "down"? YES
       ↓
Returns: 503 Service Unavailable
```

---

## 🧵 Thread Safety

**Why locks?**
- ControlHandler might update state while JSONRPCHandler reads it
- Lock ensures reads are consistent

**When is lock acquired?**
- `snapshot()` → Acquire lock, read all fields, release, return copy
- `update()` → Acquire lock, modify fields, release
- JSONRPCHandler → Calls snapshot() (lock acquired inside)

**Result:** No corruption, consistent reads

---

## 📈 Common Scenarios

### Scenario 1: Test Failover
```
1. Set: provider 1 = "down", provider 2 = "success"
2. Router tries provider 1 → 503
3. Router tries provider 2 → 200
✓ Test passes (failover works)
```

### Scenario 2: Rate Limiting
```
1. Set: provider 1 = "rate_limit"
2. Router tries provider 1 → 429
3. Router skips provider 1, tries provider 2
✓ Test passes (rate limit avoidance works)
```

### Scenario 3: Flaky Provider
```
1. Set: provider 1 = {error_probability: 0.5}
2. Send 10 requests
3. ~5 fail, router retries
✓ Test checks: most requests succeed despite flakiness
```

### Scenario 4: Slow Provider
```
1. Set: provider 1 = {latency_ms: 2000}
2. Router requests provider 1
3. JSONRPCHandler sleeps 2 seconds
4. Router gets slow response or timeout, tries provider 2
✓ Test passes (timeout handling works)
```

---

## 🚀 Deployment

### Build
```bash
docker build -t provider-simulator:latest .
```

### Deploy to K8s
```bash
bash scripts/deploy.sh
```

Set `BASE_DOMAIN` once in `config/base-domain.env`, then derive public URLs from it.

### Verify
```bash
curl https://sim-control.${BASE_DOMAIN}/health
# Expected: {"status": "ok"}
```

---

## 🔗 Relationships

```
main()
├─ Creates 3 ProviderState objects
├─ Creates 3 JSONRPCHandlers (one state each)
├─ Creates 1 ControlHandler (all states)
└─ Starts all 4 in separate threads

ControlHandler
└─ Accesses all ProviderState objects
   ├─ Reads: snapshot()
   └─ Modifies: update()

JSONRPCHandler[1,2,3]
└─ Each accesses one ProviderState
   └─ Reads: snapshot()
```

---

## 🎬 HTTP Request/Response Examples

### Control API: Set Scenario

**Request:**
```http
POST https://sim-control.${BASE_DOMAIN}/scenario
Content-Type: application/json

{"providers": {"1": {"chain_family": "eth", "mode": "rate_limit"}}}
```

**Response:**
```http
HTTP/1.1 200 OK
Content-Type: application/json
Content-Length: 18

{"status": "ok"}
```

---

### Control API: Get State

**Request:**
```http
GET https://sim-control.${BASE_DOMAIN}/scenario
```

**Response:**
```http
HTTP/1.1 200 OK
Content-Type: application/json

{
  "providers": {
    "1": {"mode": "rate_limit", "latency_ms": 0, "error_probability": 0.0},
    "2": {"mode": "down", "latency_ms": 0, "error_probability": 0.0},
    "3": {"mode": "success", "latency_ms": 0, "error_probability": 0.0}
  }
}
```

---

### Router Request: Success

**Request:**
```http
POST http://provider-simulator.smart-router.svc.cluster.local:18545/
Content-Type: application/json

{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}
```

**Response (mode="success") for `eth_blockNumber`:**
```http
HTTP/1.1 200 OK
Content-Type: application/json

{"jsonrpc":"2.0","id":1,"result":"0x1312D00"}
```

---

### Router Request: Rate Limited

**Response (mode="rate_limit"):**
```http
HTTP/1.1 429 Too Many Requests
Content-Type: application/json

{"jsonrpc":"2.0","id":1,"error":{"code":429,"message":"Too many requests"}}
```

---

### Router Request: Down

**Response (mode="down"):**
```http
HTTP/1.1 503 Service Unavailable
Content-Length: 0

(empty body)
```

---

## 📋 Test Pattern

```python
def test_scenario():
    # Setup: Configure simulator
    sim_control.set_scenario({
        1: "rate_limit",
        2: "down",
        3: "success"
    })
    
    # Test: Call router
    response = http_client.post(sim_router_url, json={
        "method": "eth_blockNumber"
    })
    
    # Verify: Check result
    assert response.status_code == 200
    
    # Cleanup: Reset simulator
    sim_control.reset()
```

---

## 🔍 Debugging Checklist

| Issue | Check |
|-------|-------|
| Pod won't start | `kubectl logs deployment/provider-simulator` |
| Control API unreachable | `curl https://sim-control.${BASE_DOMAIN}/health` |
| Router gets 503 | Check: Did you set provider state? |
| Router gets 429 | Check: mode="rate_limit" is set? |
| Wrong response | Check: custom responses configured? |
| Test fails randomly | Check: error_probability set? |
| Slow responses | Check: latency_ms set? |

---

## 📚 Documentation

- **ARCHITECTURE_GUIDE.md** - Big picture overview
- **CLASS_REFERENCE.md** - Deep dive into each class
- **DATA_FLOWS.md** - How requests flow through
- **README.md** - Learning guide & navigation

---

## 💾 Key Files to Know

| File | What to look at |
|------|------------------|
| `server.py` | `ProviderState`, `JSONRPCHandler`, `ControlHandler`, `main()` |
| `stubs.py` | `METHOD_DEFAULTS` for JSON-RPC method fallback responses |
| `constants.py` | Port mappings and shared constants |
| `scripts/deploy.sh` | Build/import/apply/restart deployment flow |
| `config/base-domain.env` | `BASE_DOMAIN` used to generate public hostnames |

---

```bash
# Test locally
python server.py

# Build Docker image
docker build -t provider-simulator:latest .

# Deploy
bash scripts/deploy.sh

# Check logs
kubectl logs -f deployment/provider-simulator -n smart-router

# Check pod status
kubectl get pods -n smart-router | grep provider-simulator

# Check services
kubectl get svc -n smart-router | grep provider-simulator

# Check HTTP routes
kubectl get httproute -n smart-router | grep sim-control

# Test health
curl https://sim-control.${BASE_DOMAIN}/health

# Set scenario
curl -X POST https://sim-control.${BASE_DOMAIN}/scenario \
  -H "Content-Type: application/json" \
  -d '{"providers":{"1":{"chain_family":"eth","mode":"down"}}}'

# Get state
curl https://sim-control.${BASE_DOMAIN}/scenario

# Reset
curl -X POST https://sim-control.${BASE_DOMAIN}/reset
```

---

## 🎓 One Sentence per Component

| Component | One Sentence |
|-----------|--------------|
| ProviderState | Holds the current configuration (mode, latency, error%) for one provider with thread-safe locking. |
| JSONRPCHandler | Receives HTTP POST requests and returns fake blockchain responses based on the current ProviderState. |
| ControlHandler | Receives configuration commands from tests and updates all ProviderState objects. |
| main() | Creates 3 ProviderStates, 3 JSONRPCHandler servers (one per state), 1 ControlHandler server (all states), and starts them all in separate threads. |

---

## 🏁 The Cycle

```
1. Test sets scenario via Control API
   ↓
2. ControlHandler updates ProviderState
   ↓
3. Test calls router
   ↓
4. Router calls simulator (JSONRPCHandler)
   ↓
5. JSONRPCHandler reads ProviderState
   ↓
6. JSONRPCHandler returns response based on state
   ↓
7. Router processes response
   ↓
8. Test verifies behavior
   ↓
9. Test calls reset via Control API
   ↓
10. ControlHandler resets all ProviderStates
```

---

**For detailed information, see the full documentation files!**

