# Provider Simulator - Data Flows & Request Cycles

## Table of Contents
1. [Overview](#overview)
2. [Request Types](#request-types)
3. [Data Flow Diagrams](#data-flow-diagrams)
4. [Complete Request Cycles](#complete-request-cycles)
5. [State Transitions](#state-transitions)
6. [Thread Interaction](#thread-interaction)
7. [Common Scenarios](#common-scenarios)

---

## Overview

The provider simulator is a request-response system with **two types of requests**:

1. **Control Requests** - From tests to the simulator (configure behavior)
2. **Router Requests** - From the router to the simulator (get blockchain data)

Both use HTTP, but go to different ports and have different purposes.

The only simulator-owned public domain value is `BASE_DOMAIN` in `config/base-domain.env`.
Public URLs in this guide are derived from it.

---

## Request Types

### Type 1: Control Requests (Tests → Simulator)

**Who sends:** Tests (smart_router_automation)  
**Where to:** `sim-control.${BASE_DOMAIN}` (port 19000)  
**What:** Configure provider behavior  
**Handler:** ControlHandler

#### Endpoints

| Method | Path | Purpose | Body |
|--------|------|---------|------|
| POST | `/scenario` | Set provider modes | `{"providers": {"1": {...}, "2": {...}}}` |
| POST | `/reset` | Reset to healthy | `{}` |
| GET | `/scenario` | Read current state | (no body) |
| GET | `/health` | Health check | (no body) |

**Examples:**

```json
// Set scenario
POST /scenario
{
  "providers": {
    "1": {"mode": "rate_limit"},
    "2": {"mode": "down"},
    "3": {"mode": "success", "latency_ms": 100}
  }
}

// Reset
POST /reset
{}

// Read state
GET /scenario

// Health check
GET /health
```

---

### Type 2: Router Requests (Router → Simulator)

**Who sends:** Smart router  
**Where to:** `provider-simulator.lava-infra.svc.cluster.local:18545/18546/18547`  
**What:** Get blockchain data (JSON-RPC calls)  
**Handler:** JSONRPCHandler (3 instances)

#### Format

All requests are JSON-RPC 2.0:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "eth_blockNumber",
  "params": []
}
```

**Examples:**

```json
// Block number
{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}

// Get balance
{"jsonrpc":"2.0","id":2,"method":"eth_getBalance","params":["0x123...","latest"]}

// Call
{"jsonrpc":"2.0","id":3,"method":"eth_call","params":[{...},"latest"]}
```

---

## Data Flow Diagrams

### Flow 1: Simple Success Path

```
┌─────────────────────────┐
│  Router                 │
│  Wants: eth_blockNumber │
└────────────┬────────────┘
             │
             │ HTTP POST
             │ ProviderState = "success"
             ↓
┌─────────────────────────────────────────────────┐
│ JSONRPCHandler (port 18545)                     │
│                                                 │
│ 1. Get state snapshot                           │
│    ✓ mode = "success"                           │
│    ✓ latency_ms = 0                             │
│    ✓ error_probability = 0.0                    │
│                                                 │
│ 2. Check all conditions:                        │
│    ✗ mode == "down"? NO                         │
│    ✗ latency_ms > 0? NO                         │
│    ✗ mode == "rate_limit"? NO                   │
│    ✗ mode == "error"? NO                        │
│    ✗ random < error_probability? NO             │
│                                                 │
│ 3. Return success:                              │
│    HTTP 200                                     │
│    {"jsonrpc":"2.0","id":1,"result":"0x1"}    │
└────────────┬────────────────────────────────────┘
             │
             │ HTTP Response
             ↓
┌─────────────────────────┐
│  Router                 │
│  Result: 0x1            │
│  Status: SUCCESS ✓      │
└─────────────────────────┘
```

---

### Flow 2: Down (Outage) Path

```
┌─────────────────────────┐
│  Router                 │
│  Wants: eth_blockNumber │
└────────────┬────────────┘
             │
             │ HTTP POST
             │ ProviderState = "down"
             ↓
┌─────────────────────────────────────────────────┐
│ JSONRPCHandler (port 18545)                     │
│                                                 │
│ 1. Get state snapshot                           │
│    snapshot = {                                 │
│      mode: "down",                              │
│      latency_ms: 0,                             │
│      error_probability: 0.0                     │
│    }                                            │
│                                                 │
│ 2. Check: if mode == "down"?                    │
│    ✓ YES! → Exit immediately                    │
│                                                 │
│ 3. Return:                                      │
│    HTTP 503 (Service Unavailable)               │
│    (empty body)                                 │
└────────────┬────────────────────────────────────┘
             │
             │ HTTP 503 Response
             ↓
┌─────────────────────────┐
│  Router                 │
│  Result: 503            │
│  Status: PROVIDER DOWN  │
│  Action: TRY NEXT PROVIDER                      │
└─────────────────────────┘
```

---

### Flow 3: Rate Limit Path

```
┌─────────────────────────┐
│  Router                 │
│  Wants: eth_blockNumber │
└────────────┬────────────┘
             │
             │ HTTP POST
             │ ProviderState = "rate_limit"
             ↓
┌─────────────────────────────────────────────────┐
│ JSONRPCHandler (port 18545)                     │
│                                                 │
│ 1. Get state snapshot                           │
│    mode = "rate_limit"                          │
│                                                 │
│ 2. Check conditions (skip down, latency)        │
│                                                 │
│ 3. Check: if mode == "rate_limit"?              │
│    ✓ YES!                                       │
│                                                 │
│ 4. Return:                                      │
│    HTTP 429 (Too Many Requests)                 │
│    {"jsonrpc":"2.0","id":1,                    │
│     "error":{"code":429,                        │
│     "message":"Too many requests"}}             │
└────────────┬────────────────────────────────────┘
             │
             │ HTTP 429 Response
             ↓
┌──────────────────────────────────┐
│  Router                          │
│  Result: 429 (rate limited)      │
│  Status: PROVIDER OVERLOADED     │
│  Action: TRY NEXT PROVIDER       │
└──────────────────────────────────┘
```

---

### Flow 4: Latency Injection Path

```
┌─────────────────────────┐
│  Router                 │
│  Wants: eth_blockNumber │
│  Sends request at 0ms   │
└────────────┬────────────┘
             │
             │ HTTP POST
             │ ProviderState = {
             │   mode: "success",
             │   latency_ms: 500
             │ }
             ↓
┌─────────────────────────────────────────────────┐
│ JSONRPCHandler (port 18545)                     │
│                                                 │
│ 1. Get state snapshot                           │
│    latency_ms = 500                             │
│                                                 │
│ 2. Check: if latency_ms > 0?                    │
│    ✓ YES!                                       │
│                                                 │
│ 3. Sleep:                                       │
│    time.sleep(500 / 1000.0)  # Sleep 0.5 sec   │
│    ⏳ 500ms delay ⏳                            │
│                                                 │
│ 4. Continue processing...                       │
│    (all checks pass)                            │
│                                                 │
│ 5. Return:                                      │
│    HTTP 200                                     │
│    {"jsonrpc":"2.0","id":1,"result":"0x1"}    │
└────────────┬────────────────────────────────────┘
             │
             │ HTTP Response (arrives at ~500ms)
             ↓
┌──────────────────────────────────┐
│  Router                          │
│  Request sent: 0ms               │
│  Response received: 500ms        │
│  Delay observed: 500ms           │
│  Status: SUCCESS (but slow)      │
└──────────────────────────────────┘
```

---

### Flow 5: Error Probability Path

```
┌─────────────────────────┐
│  Router                 │
│  Wants: eth_blockNumber │
└────────────┬────────────┘
             │
             │ HTTP POST
             │ ProviderState = {
             │   mode: "success",
             │   error_probability: 0.3
             │ }
             ↓
┌─────────────────────────────────────────────────┐
│ JSONRPCHandler (port 18545)                     │
│                                                 │
│ 1. Get state snapshot                           │
│    error_probability = 0.3                      │
│                                                 │
│ 2. Check: if random.random() < 0.3?             │
│    random.random() generates value 0.0–1.0      │
│                                                 │
│ Case A (30% of requests):                       │
│   Generated: 0.15                               │
│   0.15 < 0.3? ✓ YES → Return error              │
│   HTTP 200 with error body                      │
│                                                 │
│ Case B (70% of requests):                       │
│   Generated: 0.85                               │
│   0.85 < 0.3? ✗ NO → Continue to success        │
│   HTTP 200 with result                          │
└────────────┬────────────────────────────────────┘
             │
             ├─ 30% of time: HTTP 200 (error)
             └─ 70% of time: HTTP 200 (success)
             ↓
┌──────────────────────────────────┐
│  Router                          │
│  Status: Mixed (flaky provider)  │
│  Action: Retry failed requests   │
└──────────────────────────────────┘
```

---

### Flow 6: Control API Update Path

```
┌─────────────────────────────────────────────┐
│  Test (smart_router_automation)             │
│                                             │
│  sim_control.set_scenario({                 │
│    1: "rate_limit",                         │
│    2: "down",                               │
│    3: "success"                             │
│  })                                         │
└────────────┬────────────────────────────────┘
             │
             │ HTTP POST /scenario
             │ Body: {"providers": {"1": {...}, "2": {...}, "3": {...}}}
             ↓
┌───────────────────────────────────────────────────────────────┐
│ ControlHandler.do_POST() (port 19000)                         │
│                                                               │
│ 1. Parse request:                                             │
│    if self.path == "/scenario"? ✓ YES                         │
│                                                               │
│ 2. Iterate over providers in request:                         │
│    for pid, cfg in {"1": {...}, "2": {...}, ...}.items():     │
│                                                               │
│    Iteration 1:                                               │
│    pid = "1", cfg = {"mode": "rate_limit"}                    │
│    state = self.server.provider_states["1"]                   │
│    state.update({"mode": "rate_limit"})                       │
│      ↓ Acquires lock                                          │
│      ↓ Sets state.mode = "rate_limit"                         │
│      ↓ Releases lock                                          │
│                                                               │
│    Iteration 2:                                               │
│    pid = "2", cfg = {"mode": "down"}                          │
│    state = self.server.provider_states["2"]                   │
│    state.update({"mode": "down"})                             │
│      ↓ Acquires lock                                          │
│      ↓ Sets state.mode = "down"                               │
│      ↓ Releases lock                                          │
│                                                               │
│    Iteration 3:                                               │
│    pid = "3", cfg = {"mode": "success"}                       │
│    state = self.server.provider_states["3"]                   │
│    state.update({"mode": "success"})                          │
│      ↓ Acquires lock                                          │
│      ↓ Sets state.mode = "success"                            │
│      ↓ Releases lock                                          │
│                                                               │
│ 3. Return response:                                           │
│    HTTP 200 {"status": "ok"}                                  │
└────────────┬───────────────────────────────────────────────────┘
             │
             │ HTTP 200 Response
             ↓
┌─────────────────────────────────────────────┐
│  Test                                       │
│  ✓ Scenario updated!                        │
│                                             │
│  Now ready to test router behavior with:    │
│  - Provider 1: rate-limited                 │
│  - Provider 2: down                         │
│  - Provider 3: healthy                      │
└─────────────────────────────────────────────┘
```

---

### Flow 7: Full End-to-End Test Scenario

```
START: Test wants to verify failover behavior

┌──────────────────────────────────────────────────────────────┐
│ Step 1: Configure Scenario                                  │
└──────────────────────────────────────────────────────────────┘

Test calls: sim_control.set_scenario({
    1: "rate_limit",
    2: "success",
    3: "success"
})
    ↓
ControlHandler receives POST /scenario
    ↓
Updates provider states:
- Provider 1: mode = "rate_limit"
- Provider 2: mode = "success"
- Provider 3: mode = "success"
    ↓
Returns: {"status": "ok"}

┌──────────────────────────────────────────────────────────────┐
│ Step 2: Test Calls Router                                   │
└──────────────────────────────────────────────────────────────┘

Test sends: POST /
  Body: {"method": "eth_blockNumber"}
    ↓
Router receives request
    ↓
Router logic: "I have 3 providers, let me find the best one"

┌──────────────────────────────────────────────────────────────┐
│ Step 3: Router Tries Provider 1 (port 18545)               │
└──────────────────────────────────────────────────────────────┘

Router calls: provider-simulator:18545/
    ↓
JSONRPCHandler[1].do_POST()
    ↓
Reads state: {"mode": "rate_limit"}
    ↓
Checks conditions:
  - mode == "down"? NO
  - latency_ms > 0? NO
  - mode == "rate_limit"? YES!
    ↓
Returns: HTTP 429 (Too Many Requests)

┌──────────────────────────────────────────────────────────────┐
│ Step 4: Router Receives 429, Tries Provider 2               │
└──────────────────────────────────────────────────────────────┘

Router: "Provider 1 returned 429, that's bad. Try provider 2"
    ↓
Router calls: provider-simulator:18546/
    ↓
JSONRPCHandler[2].do_POST()
    ↓
Reads state: {"mode": "success"}
    ↓
Checks all conditions: all pass ✓
    ↓
Returns: HTTP 200 {"jsonrpc":"2.0","result":"0x1"}

┌──────────────────────────────────────────────────────────────┐
│ Step 5: Router Returns Success to Test                      │
└──────────────────────────────────────────────────────────────┘

Router: "Got success from provider 2"
    ↓
Router returns to test: HTTP 200 {"result":"0x1"}

┌──────────────────────────────────────────────────────────────┐
│ Step 6: Test Verifies Result                                │
└──────────────────────────────────────────────────────────────┘

Test receives: {"result":"0x1"}
    ↓
Test asserts:
  - Response code is 200 ✓
  - Response has result ✓
  - Result is "0x1" ✓
    ↓
Test passes! ✓

┌──────────────────────────────────────────────────────────────┐
│ Step 7: Test Cleanup                                        │
└──────────────────────────────────────────────────────────────┘

Test calls: sim_control.reset()
    ↓
ControlHandler receives POST /reset
    ↓
Resets all providers to healthy state
    ↓
Returns: {"status": "reset"}
    ↓
Test complete ✓
```

---

## Complete Request Cycles

### Cycle 1: From Test's Perspective

```python
# Test code (smart_router_automation/tests/simulator/test_router_routing.py)

from tests.simulator.sim_control import SimulatorControl, ProviderConfig

def test_failover(sim_control, sim_router_url, http_client):
    # Cycle Step 1: Configure simulator
    sim_control.set_scenario({
        1: "rate_limit",
        2: "down",
        3: "success"
    })
    # HTTP POST /scenario
    # ← Control API updates ProviderState
    # ← HTTP 200 {"status": "ok"}
    
    # Cycle Step 2: Call router
    response = http_client.post(sim_router_url, json={
        "method": "eth_blockNumber"
    })
    # HTTP POST to router
    # → Router calls providers via our simulator
    # ← Eventually gets success from provider 3
    # ← HTTP 200 {"result":"0x1"}
    
    # Cycle Step 3: Verify
    assert response.status_code == 200
    assert "result" in response.json()
    
    # Cycle Step 4: Cleanup
    sim_control.reset()
    # HTTP POST /reset
    # ← All providers reset to healthy
    # ← HTTP 200 {"status": "reset"}
```

### Cycle 2: From Simulator's Perspective

```
Timeline: 0ms - 1000ms

0ms:  Test sets scenario (1=rate_limit, 2=down, 3=success)
      ↓
      ControlHandler.do_POST()
      ↓
      Updates ProviderState[1].mode = "rate_limit"
      Updates ProviderState[2].mode = "down"
      Updates ProviderState[3].mode = "success"
      ↓
      Returns {"status": "ok"}

100ms: Router calls provider 1
       ↓
       JSONRPCHandler[1].do_POST() runs
       ↓
       Reads ProviderState[1].snapshot() → {mode: "rate_limit"}
       ↓
       Checks: mode == "rate_limit"? YES
       ↓
       Returns HTTP 429

150ms: Router calls provider 2
       ↓
       JSONRPCHandler[2].do_POST() runs
       ↓
       Reads ProviderState[2].snapshot() → {mode: "down"}
       ↓
       Checks: mode == "down"? YES
       ↓
       Returns HTTP 503

200ms: Router calls provider 3
       ↓
       JSONRPCHandler[3].do_POST() runs
       ↓
       Reads ProviderState[3].snapshot() → {mode: "success"}
       ↓
       All checks pass
       ↓
       Returns HTTP 200 with result

250ms: Test calls reset
       ↓
       ControlHandler.do_POST()
       ↓
       For each ProviderState: call reset()
       ↓
       Returns {"status": "reset"}

1000ms: Test ends
```

---

## State Transitions

### State Machine for One Provider

```
                    ┌─────────────────┐
                    │    success      │ ← Default state
                    │                 │
                    │ Returns HTTP 200│
                    │ with response   │
                    └────────┬────────┘
                             │
                    ┌────────┴────────┐
                    │                 │
                    ↓                 ↓
            ┌─────────────┐   ┌──────────────┐
            │ rate_limit  │   │     down     │
            │             │   │              │
            │ Returns 429 │   │ Returns 503  │
            └──────┬──────┘   └──────┬───────┘
                   │                 │
                   └────────┬────────┘
                            │
                    ┌───────┴────────┐
                    │                │
                    ↓                ↓
            ┌──────────────┐  ┌───────────┐
            │    error     │  │success    │
            │              │  │with delay │
            │ Returns 200  │  │(latency_ms)
            │ with error   │  │           │
            └──────┬───────┘  └─────┬─────┘
                   │                │
                   └────────┬───────┘
                            │
                            ↓
                    ┌──────────────┐
                    │ + error_      │
                    │ probability   │
                    │              │
                    │ Random error │
                    │ on X% of     │
                    │ requests     │
                    └──────────────┘
```

### Transition Example

```
Initial: success

Test calls: sim_control.set_scenario({1: "rate_limit"})
  ↓
ProviderState.update({"mode": "rate_limit"})
  ↓
Transition: success → rate_limit

Requests to provider 1 now return 429

Test calls: sim_control.set_scenario({1: "down"})
  ↓
ProviderState.update({"mode": "down"})
  ↓
Transition: rate_limit → down

Requests to provider 1 now return 503

Test calls: sim_control.reset()
  ↓
ProviderState.reset()
  ↓
Transition: down → success

Requests to provider 1 now return 200 with result
```

---

## Thread Interaction

### Scenario: Concurrent Requests During State Update

```
Time    Main Thread         ControlHandler              JSONRPCHandler[1]
────    ────────────────    ──────────────────          ──────────────────
0ms                         ControlHandler.do_POST()
                            Reading request body
1ms                         state.update() starts
                            Acquires lock
                            ProviderState.mode = "down"
                                                        JSONRPCHandler.do_POST() starts
2ms                         Still holding lock...
                                                        Waits for lock... (blocked)
3ms                         Releases lock
                            Returns 200 OK
                                                        Acquires lock! ✓
4ms                                                     Reads state: {mode: "down"}
                                                        Releases lock
5ms                                                     Returns 503
```

**Key Point:** The lock ensures JSONRPCHandler always reads a consistent state, even if ControlHandler is modifying it.

---

## Common Scenarios

### Scenario 1: Testing Failover

**Goal:** Verify router fails over from bad provider to good provider

```python
def test_failover():
    # Setup: Provider 1 fails, Provider 2 succeeds
    sim_control.set_scenario({
        1: "down",
        2: "success"
    })
    
    # Test: Send request
    response = make_request_to_router()
    
    # Verify: Got response from provider 2
    assert response.status_code == 200
    assert response.json()["result"] == "0x1"
    
    # Cleanup
    sim_control.reset()
```

**Data flow:**
```
Router: "Call provider 1"
  ↓ JSONRPCHandler[1] returns 503
Router: "Provider 1 is down. Call provider 2"
  ↓ JSONRPCHandler[2] returns 200
Router: "Got success!"
Test: ✓ Failover works!
```

---

### Scenario 2: Testing Rate Limit Handling

**Goal:** Verify router doesn't overload when provider is rate-limited

```python
def test_rate_limit_handling():
    # Setup: Provider 1 rate-limited
    sim_control.set_scenario({
        1: "rate_limit"
    })
    
    # Test: Send request
    response = make_request_to_router()
    
    # Verify: Still got response (from another provider or retry)
    assert response.status_code == 200
    
    # Cleanup
    sim_control.reset()
```

**Data flow:**
```
Router: "Call provider 1"
  ↓ JSONRPCHandler[1] returns 429
Router: "Provider 1 is rate-limited. Avoid it."
  ↓ Uses Provider 2 or 3 instead
Router: "Got response from healthy provider"
Test: ✓ Rate limit handling works!
```

---

### Scenario 3: Testing Latency Impact

**Goal:** Verify router handles slow providers correctly

```python
def test_slow_provider():
    # Setup: Provider 1 is slow, Provider 2 is fast
    sim_control.set_scenario({
        1: ProviderConfig(mode="success", latency_ms=2000),  # 2 second delay
        2: "success"
    })
    
    # Test: Send request
    start = time.time()
    response = make_request_to_router()
    elapsed = time.time() - start
    
    # Verify: Got fast response (from provider 2, not 1)
    assert elapsed < 1.0  # Should be fast
    assert response.status_code == 200
    
    # Cleanup
    sim_control.reset()
```

**Data flow:**
```
Time 0: Router decides "Call provider 1"
  ↓ JSONRPCHandler[1].do_POST() runs
  ↓ Sleeps for 2 seconds (latency_ms=2000)
Time 2: JSONRPCHandler[1] returns 200
  ↓ But took 2 seconds!

Meanwhile, Router's timeout might trigger:
  "Provider 1 is taking too long"
  ↓ Calls provider 2
  ↓ Gets response immediately

Test: ✓ Router correctly handles slow providers!
```

---

### Scenario 4: Testing Flaky Providers

**Goal:** Verify router retries when provider is unreliable

```python
def test_flaky_provider():
    # Setup: Provider 1 fails 50% of the time
    sim_control.set_scenario({
        1: ProviderConfig(mode="success", error_probability=0.5),
        2: "success"
    })
    
    # Test: Send 10 requests
    responses = [make_request_to_router() for _ in range(10)]
    
    # Verify: Most succeeded despite flakiness
    success_count = sum(1 for r in responses if r.status_code == 200)
    assert success_count >= 8  # Allow 2 failures, but expect retry success
    
    # Cleanup
    sim_control.reset()
```

**Data flow:**
```
Request 1:
  Router calls Provider 1
  JSONRPCHandler[1].do_POST() runs
  Checks: random.random() < 0.5?
    If YES (50% chance): Return error
    If NO (50% chance): Return success
  Either returns success or error, router retries on error

Request 2-10:
  (Same process, random outcome each time)

Test counts successes:
  Even though provider fails 50% of the time,
  Router's retry logic gets success most of the time
```

---

## Summary

### Request Types
- **Control**: Tests configure simulator (port 19000)
- **Router**: Router gets blockchain data (ports 18545-18547)

### Data Flow Pattern
```
Test sets scenario → ControlHandler updates ProviderState
                                          ↓
                      Router calls JSONRPCHandler
                      JSONRPCHandler reads ProviderState
                                          ↓
                      JSONRPCHandler returns response
                                          ↓
                      Router processes response
                                          ↓
                      Test verifies behavior
```

### Thread Safety
- Each ProviderState has a lock
- ControlHandler holds lock while updating
- JSONRPCHandler holds lock while reading
- Ensures consistent state reads even during concurrent updates

### Common Test Patterns
1. Set scenario → Make request → Verify → Reset
2. Test failover, rate limiting, latency, flakiness
3. Always reset at end (via fixture or explicit call)

---

**You now understand how data flows through the entire system!** 🎉

