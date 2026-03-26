# Provider Simulator - Class & Module Reference Guide

## Table of Contents
1. [ProviderState Class](#providerstate-class)
2. [JSONRPCHandler Class](#jsonrpchandler-class)
3. [ControlHandler Class](#controlhandler-class)
4. [Main Function](#main-function)
5. [Module-by-Module Breakdown](#module-by-module-breakdown)
6. [Complete Code Walkthrough](#complete-code-walkthrough)

---

## ProviderState Class

Public simulator URLs in this guide are derived from `BASE_DOMAIN`, which is stored once in `config/base-domain.env`.

### Purpose
Holds the **state** of one blockchain provider. Each provider (1, 2, 3) has its own instance.

### What It Does
- Stores the current configuration for a single provider
- Makes sure multiple threads can safely read/write the state
- Provides methods to read, update, and reset the state

### Code Structure

```python
@dataclass
class ProviderState:
    mode: str = "success"
    latency_ms: int = 0
    error_probability: float = 0.0
    responses: Dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
```

### Instance Variables Explained

| Variable | Type | Default | Purpose |
|----------|------|---------|---------|
| `mode` | str | "success" | What this provider does: "success", "error", "rate_limit", "down" |
| `latency_ms` | int | 0 | How many milliseconds to delay responses |
| `error_probability` | float | 0.0 | Chance of error (0.0 = never, 1.0 = always) |
| `responses` | dict | {} | Custom responses for specific methods |
| `lock` | Lock | new Lock() | Thread-safety mechanism |

### Methods

#### **1. snapshot() - Read Current State Safely**

```python
def snapshot(self) -> dict:
    with self.lock:
        return {
            "mode":              self.mode,
            "latency_ms":        self.latency_ms,
            "error_probability": self.error_probability,
        }
```

**What it does:**
- Returns a **copy** of the current state
- Uses the lock to ensure the copy is consistent
- Doesn't include `responses` (only exposed via control API separately)

**Why this is needed:**
- Without the lock, if thread A is updating `mode` while thread B is reading it, thread B might get a corrupted state
- `with self.lock:` ensures only one thread can access the state at a time

**Example:**
```python
provider_state = ProviderState()
provider_state.mode = "rate_limit"
provider_state.latency_ms = 100

snap = provider_state.snapshot()
print(snap)
# Output: {"mode": "rate_limit", "latency_ms": 100, "error_probability": 0.0}
```

---

#### **2. update(cfg) - Change the State**

```python
def update(self, cfg: dict) -> None:
    with self.lock:
        self.mode              = cfg.get("mode",              self.mode)
        self.latency_ms        = cfg.get("latency_ms",        self.latency_ms)
        self.error_probability = cfg.get("error_probability", self.error_probability)
        if "responses" in cfg:
            self.responses = cfg["responses"]
```

**What it does:**
- Takes a configuration dictionary
- Updates any fields that are present in the dictionary
- Leaves other fields unchanged if not specified
- Holds the lock while updating

**Example:**
```python
provider_state = ProviderState()  # Starts as: mode="success", latency_ms=0

# Update only mode
provider_state.update({"mode": "rate_limit"})
# Now: mode="rate_limit", latency_ms=0 (latency_ms unchanged!)

# Update mode AND latency
provider_state.update({"mode": "success", "latency_ms": 500})
# Now: mode="success", latency_ms=500
```

**Used by:** ControlHandler when test sends `/scenario` request

---

#### **3. reset() - Return to Healthy Defaults**

```python
def reset(self) -> None:
    with self.lock:
        self.mode              = "success"
        self.latency_ms        = 0
        self.error_probability = 0.0
        self.responses         = {}
```

**What it does:**
- Clears all custom settings
- Returns provider to "healthy" state
- All responses will be successful

**Example:**
```python
provider_state = ProviderState()
provider_state.update({"mode": "down", "latency_ms": 1000})
# State: down, slow

provider_state.reset()
# State: success, fast (back to healthy)
```

**Used by:** 
- ControlHandler when test sends `/reset` request
- The reset_simulator fixture in pytest (runs after each test)

---

### Thread Safety Explained

**Problem without lock:**
```
Thread A (ControlHandler):      Thread B (JSONRPCHandler):
1. Read mode ("success")        
2. Set mode = "down"            1. Read mode (gets "down"? or "success"?)
3. Read latency (0)             2. Set latency = 100
4. Set latency = 0              3. Read error_prob (0.0? or 0.5?)
```

The threads could step on each other's toes!

**Solution with lock:**
```
Thread A (ControlHandler):                  Thread B (JSONRPCHandler):
1. Acquire lock
2. Read mode, set mode, read latency, set latency
3. Release lock                            1. Acquire lock (was waiting!)
                                           2. Read all values consistently
                                           3. Release lock
```

Lock ensures one thread at a time.

---

## JSONRPCHandler Class

### Purpose
Receives JSON-RPC requests from the router and returns fake blockchain responses.

### What It Does
- Listens for HTTP POST requests on ports 18545, 18546, or 18547
- Reads the current ProviderState
- Simulates the provider's behavior (success, error, rate-limit, down)
- Returns appropriate HTTP responses

### Code Structure

```python
class JSONRPCHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Handles HTTP POST requests
        ...
    
    def _reply(self, status: int, data: dict):
        # Sends a response back
        ...
    
    def log_message(self, *_):
        pass  # Disable logging
```

### Methods

#### **do_POST() - Main Request Handler**

This is the **most important method**. When a request arrives, this runs.

```python
def do_POST(self):
    state: ProviderState = self.server.state
    snap = state.snapshot()

    # Step 1: Check if provider is down
    if snap["mode"] == "down":
        self.send_response(503)
        self.end_headers()
        return

    # Step 2: Inject latency if configured
    if snap["latency_ms"] > 0:
        time.sleep(snap["latency_ms"] / 1000.0)

    # Step 3: Parse the incoming request
    length = int(self.headers.get("Content-Length", 0))
    body   = json.loads(self.rfile.read(length)) if length else {}
    req_id = body.get("id", 1)
    method = body.get("method", "default")

    # Step 4: Check for rate limiting
    if snap["mode"] == "rate_limit":
        self._reply(429, {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": 429, "message": "Too many requests"}
        })
        return

    # Step 5: Check for probabilistic errors
    if snap["mode"] == "error" or random.random() < snap["error_probability"]:
        self._reply(200, {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32000, "message": "Internal error"}
        })
        return

    # Step 6: Success - return configured response
    with state.lock:
        method_cfg = state.responses.get(method) or state.responses.get("default", {})
    result = method_cfg.get("result", "0x1")

    self._reply(200, {"jsonrpc": "2.0", "id": req_id, "result": result})
```

**Detailed Step-by-Step Walkthrough:**

**Step 1: Get State**
```python
state: ProviderState = self.server.state
snap = state.snapshot()
```
- Gets the ProviderState object for this provider
- Takes a thread-safe snapshot of the state
- `snap` is now a dict like: `{"mode": "down", "latency_ms": 0, "error_probability": 0.0}`

**Step 2: Check if Provider is Down**
```python
if snap["mode"] == "down":
    self.send_response(503)
    self.end_headers()
    return
```
- If mode is "down", return HTTP 503 (Service Unavailable)
- Router interprets 503 as "this provider is offline"
- Exit early - don't process the request further

**Step 3: Inject Latency**
```python
if snap["latency_ms"] > 0:
    time.sleep(snap["latency_ms"] / 1000.0)
```
- If configured to be slow, sleep for that many milliseconds
- Example: latency_ms=100 → sleep for 0.1 seconds
- Useful for testing timeout behavior

**Step 4: Parse the Request**
```python
length = int(self.headers.get("Content-Length", 0))
body   = json.loads(self.rfile.read(length)) if length else {}
req_id = body.get("id", 1)
method = body.get("method", "default")
```
- Read the HTTP body
- Parse the JSON
- Extract the "id" (request ID for matching response)
- Extract the "method" (like "eth_blockNumber")

**Example request:**
```json
{
  "jsonrpc": "2.0",
  "id": 123,
  "method": "eth_blockNumber",
  "params": []
}
```
After parsing: `req_id=123`, `method="eth_blockNumber"`

**Step 5: Check for Rate Limiting**
```python
if snap["mode"] == "rate_limit":
    self._reply(429, {...})
    return
```
- If mode is "rate_limit", return HTTP 429 (Too Many Requests)
- Router interprets 429 as "this provider is overwhelmed"

**Step 6: Check for Probabilistic Errors**
```python
if snap["mode"] == "error" or random.random() < snap["error_probability"]:
    self._reply(200, {"jsonrpc": "2.0", "id": req_id, "error": {...}})
    return
```
- If mode is "error", always return error
- OR if error_probability is set, randomly return error
- Example: error_probability=0.3 means 30% of requests error
- `random.random()` returns value from 0.0 to 1.0
- If random value < 0.3, return error

**Step 7: Return Success**
```python
with state.lock:
    method_cfg = state.responses.get(method) or state.responses.get("default", {})
result = method_cfg.get("result", "0x1")

self._reply(200, {"jsonrpc": "2.0", "id": req_id, "result": result})
```
- Look up custom response for this method
- If not found, use "default" response
- If neither found, use "0x1"
- Return HTTP 200 with the result

---

#### **_reply() - Send Response Helper**

```python
def _reply(self, status: int, data: dict):
    body = json.dumps(data).encode()
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)
```

**What it does:**
- Converts the response data to JSON
- Sends HTTP headers (status, content-type, content-length)
- Sends the body

**Example:**
```python
self._reply(200, {"jsonrpc": "2.0", "id": 1, "result": "0xABC"})

# Sends:
# HTTP/1.1 200 OK
# Content-Type: application/json
# Content-Length: 43
#
# {"jsonrpc":"2.0","id":1,"result":"0xABC"}
```

---

#### **log_message() - Disable Logging**

```python
def log_message(self, *_):
    pass
```

**What it does:**
- BaseHTTPRequestHandler normally logs every request to console
- This overrides it to do nothing
- Keeps logs cleaner

---

### Example Request-Response Cycle

```
Incoming request:
POST / HTTP/1.1
Content-Type: application/json
Content-Length: 67

{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}

↓
JSONRPCHandler.do_POST() runs:

1. state = ProviderState(mode="success", latency_ms=100)
2. snap = {"mode": "success", "latency_ms": 100, ...}
3. Check mode == "down"? NO
4. Check latency_ms > 0? YES → sleep(0.1)
5. Parse body: req_id=1, method="eth_blockNumber"
6. Check mode == "rate_limit"? NO
7. Check mode == "error"? NO
8. Look up responses["eth_blockNumber"] → not found
9. Use responses["default"] → not found
10. Use default result: "0x1"
11. Call _reply(200, {...})

Response:
HTTP/1.1 200 OK
Content-Type: application/json
Content-Length: 51

{"jsonrpc":"2.0","id":1,"result":"0x1"}
```

---

## ControlHandler Class

### Purpose
Receives control commands from tests and updates the ProviderState objects.

### What It Does
- Listens on port 19000
- Handles requests to configure providers
- Handles requests to reset to healthy state
- Handles requests to read current state
- Handles health checks

### Code Structure

```python
class ControlHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Handles POST requests (/scenario, /reset)
        ...
    
    def do_GET(self):
        # Handles GET requests (/scenario, /health)
        ...
    
    def _reply(self, status: int, data: dict):
        # Sends a response back
        ...
    
    def log_message(self, *_):
        pass  # Disable logging
```

### Methods

#### **do_POST() - Handle Configuration Updates**

```python
def do_POST(self):
    length = int(self.headers.get("Content-Length", 0))
    body   = json.loads(self.rfile.read(length)) if length else {}

    if self.path == "/scenario":
        # Update provider configurations
        for pid, cfg in body.get("providers", {}).items():
            state = self.server.provider_states.get(str(pid))
            if state:
                state.update(cfg)
        self._reply(200, {"status": "ok"})

    elif self.path == "/reset":
        # Reset all providers to healthy
        for state in self.server.provider_states.values():
            state.reset()
        self._reply(200, {"status": "reset"})

    else:
        self._reply(404, {"error": "unknown path"})
```

**What it does:**

**Case 1: POST /scenario**
- Parses incoming configuration
- For each provider ID and config, updates the corresponding ProviderState
- Returns success

**Example:**
```python
# Request body:
{
  "providers": {
    "1": {"mode": "rate_limit"},
    "2": {"mode": "down"},
    "3": {"mode": "success", "latency_ms": 100}
  }
}

# Runs:
state["1"].update({"mode": "rate_limit"})
state["2"].update({"mode": "down"})
state["3"].update({"mode": "success", "latency_ms": 100})

# Returns:
{"status": "ok"}
```

**Case 2: POST /reset**
- Calls `reset()` on all provider states
- Returns to healthy defaults

**Example:**
```python
# Request body: {} (empty)

# Runs:
for state in [state1, state2, state3]:
    state.reset()

# Returns:
{"status": "reset"}
```

---

#### **do_GET() - Read State and Health Check**

```python
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
```

**What it does:**

**Case 1: GET /health**
- Returns immediate success
- Kubernetes uses this to check if pod is alive

**Response:**
```json
{"status": "ok"}
```

**Case 2: GET /scenario**
- Returns current state of all providers
- Takes snapshots to ensure thread-safe reads

**Response example:**
```json
{
  "providers": {
    "1": {"mode": "rate_limit", "latency_ms": 0, "error_probability": 0.0},
    "2": {"mode": "down", "latency_ms": 0, "error_probability": 0.0},
    "3": {"mode": "success", "latency_ms": 100, "error_probability": 0.0}
  }
}
```

---

#### **_reply() - Send Response Helper**

Same as JSONRPCHandler's `_reply()` method.

---

### Example Control API Request-Response

```
Request 1:
POST /scenario HTTP/1.1
Content-Type: application/json

{"providers": {"1": {"mode": "rate_limit"}}}

↓
ControlHandler.do_POST() runs:
1. Parse body
2. Check path == "/scenario"? YES
3. For provider "1", call state.update({"mode": "rate_limit"})
4. Call _reply(200, {"status": "ok"})

Response:
HTTP/1.1 200 OK
Content-Type: application/json

{"status": "ok"}

---

Request 2:
GET /scenario HTTP/1.1

↓
ControlHandler.do_GET() runs:
1. Check path == "/scenario"? YES
2. For each provider state, get snapshot
3. Call _reply(200, {"providers": {...}})

Response:
HTTP/1.1 200 OK
Content-Type: application/json

{
  "providers": {
    "1": {"mode": "rate_limit", "latency_ms": 0, "error_probability": 0.0},
    ...
  }
}

---

Request 3:
POST /reset HTTP/1.1

↓
ControlHandler.do_POST() runs:
1. Parse body
2. Check path == "/reset"? YES
3. For all providers, call state.reset()
4. Call _reply(200, {"status": "reset"})

Response:
HTTP/1.1 200 OK
Content-Type: application/json

{"status": "reset"}
```

---

## Main Function

### Purpose
Orchestrates the entire application - creates states, starts servers, manages threads.

### Code

```python
def main():
    # 1. Create provider states
    states = {pid: ProviderState() for pid in PROVIDER_PORTS}

    # 2. Start JSON-RPC servers (ports 18545-18547)
    servers = []
    for pid, port in PROVIDER_PORTS.items():
        srv = HTTPServer(("0.0.0.0", port), JSONRPCHandler)
        srv.state = states[pid]
        servers.append(srv)

    # 3. Start control API server (port 19000)
    ctrl = HTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    ctrl.provider_states = states
    servers.append(ctrl)

    # 4. Print startup message
    print("Provider simulator started")
    for pid, port in PROVIDER_PORTS.items():
        print(f"  provider {pid} → :{port}")
    print(f"  control API  → :{CONTROL_PORT}")

    # 5. Start each server in its own thread
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()

    # 6. Wait for threads (or until Ctrl+C)
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        for s in servers:
            s.shutdown()
```

**Step-by-Step:**

**Step 1: Create States**
```python
states = {pid: ProviderState() for pid in PROVIDER_PORTS}
# Creates: states = {"1": ProviderState(), "2": ProviderState(), "3": ProviderState()}
```

**Step 2: Start JSON-RPC Servers**
```python
for pid, port in PROVIDER_PORTS.items():
    srv = HTTPServer(("0.0.0.0", port), JSONRPCHandler)
    srv.state = states[pid]
    servers.append(srv)
```
- Creates 3 HTTPServer instances
- Each listens on a different port
- Each gets its own ProviderState
- **"0.0.0.0"** means "listen on all network interfaces"

**Step 3: Start Control Server**
```python
ctrl = HTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
ctrl.provider_states = states
servers.append(ctrl)
```
- Creates 1 HTTPServer for the control API
- Gives it access to ALL provider states
- So control API can update any provider

**Step 4: Print Startup Message**
```python
print("Provider simulator started")
print("  provider 1 → :18545")
print("  provider 2 → :18546")
print("  provider 3 → :18547")
print("  control API  → :19000")
```

**Step 5: Start Threads**
```python
threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
for t in threads:
    t.start()
```
- Creates 4 threads (one per server)
- Each thread runs `serve_forever()` in the background
- **daemon=True** means threads stop when main program stops
- Returns immediately - servers run in background

**Step 6: Wait for Threads**
```python
try:
    for t in threads:
        t.join()  # Wait for thread to finish
except KeyboardInterrupt:
    for s in servers:
        s.shutdown()  # Gracefully shutdown servers on Ctrl+C
```
- **join()** blocks until thread finishes
- **KeyboardInterrupt** catches Ctrl+C
- Gracefully shuts down all servers

---

## Module-by-Module Breakdown

### Constants

```python
PROVIDER_PORTS = {"1": 18545, "2": 18546, "3": 18547}
CONTROL_PORT   = 19000
```

Maps provider IDs to their ports.

### Imports

```python
import json          # Parse/generate JSON
import random        # For random error probability
import threading     # For running multiple servers concurrently
import time          # For sleep() in latency injection
from dataclasses import dataclass, field  # For ProviderState
from http.server import BaseHTTPRequestHandler, HTTPServer  # HTTP server
from typing import Dict, Any  # Type hints
```

Each import serves a specific purpose:
- **json**: JSON-RPC is JSON format
- **random**: Probabilistic error injection
- **threading**: Run 4 servers at the same time
- **time**: Inject latency by sleeping
- **dataclasses**: Clean way to define ProviderState
- **http.server**: Python's built-in HTTP server
- **typing**: Type annotations for clarity

---

## Complete Code Walkthrough

### Full Request Flow

**Scenario: Test sets provider 1 to "rate_limit" then requests from provider 1**

```
┌─────────────────────────────────────────────────────────────────────┐
│ Step 1: Test sends control request                                 │
└─────────────────────────────────────────────────────────────────────┘

POST https://sim-control.${BASE_DOMAIN}/scenario
Content-Type: application/json

{
  "providers": {
    "1": {"mode": "rate_limit"}
  }
}

↓

┌─────────────────────────────────────────────────────────────────────┐
│ Step 2: ControlHandler.do_POST() executes                          │
└─────────────────────────────────────────────────────────────────────┘

def do_POST(self):
    length = int(self.headers.get("Content-Length", 0))
    body   = json.loads(self.rfile.read(length))
    # body = {"providers": {"1": {"mode": "rate_limit"}}}
    
    if self.path == "/scenario":  # ✓ This is true
        for pid, cfg in body.get("providers", {}).items():
            # pid = "1", cfg = {"mode": "rate_limit"}
            state = self.server.provider_states.get(str(pid))
            # state = ProviderState (for provider 1)
            if state:
                state.update(cfg)
                # ProviderState.update({"mode": "rate_limit"})
        self._reply(200, {"status": "ok"})

┌─────────────────────────────────────────────────────────────────────┐
│ Step 3: ProviderState.update() executes                            │
└─────────────────────────────────────────────────────────────────────┘

def update(self, cfg: dict) -> None:
    with self.lock:  # Acquire lock (thread-safety)
        self.mode = cfg.get("mode", self.mode)
        # self.mode = "rate_limit"
        # (other fields unchanged since not in cfg)
    # Release lock

Result: Provider 1 now has mode = "rate_limit"

┌─────────────────────────────────────────────────────────────────────┐
│ Step 4: ControlHandler returns response                            │
└─────────────────────────────────────────────────────────────────────┘

HTTP/1.1 200 OK
Content-Type: application/json
Content-Length: 18

{"status": "ok"}

↓

┌─────────────────────────────────────────────────────────────────────┐
│ Step 5: Test sends router request                                  │
└─────────────────────────────────────────────────────────────────────┘

POST https://eth-sim-jsonrpc.${BASE_DOMAIN}
Content-Type: application/json

{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "eth_blockNumber",
  "params": []
}

(Router routes this to provider-simulator:18545)

↓

┌─────────────────────────────────────────────────────────────────────┐
│ Step 6: JSONRPCHandler (port 18545) receives request              │
└─────────────────────────────────────────────────────────────────────┘

def do_POST(self):
    state: ProviderState = self.server.state  # Provider 1's state
    snap = state.snapshot()
    # snap = {"mode": "rate_limit", "latency_ms": 0, "error_probability": 0.0}
    
    if snap["mode"] == "down":  # ✗ False
        ...
    
    if snap["latency_ms"] > 0:  # ✗ False
        ...
    
    length = int(self.headers.get("Content-Length", 0))
    body = json.loads(self.rfile.read(length))
    # body = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
    req_id = body.get("id", 1)  # req_id = 1
    method = body.get("method", "default")  # method = "eth_blockNumber"
    
    if snap["mode"] == "rate_limit":  # ✓ TRUE!
        self._reply(429, {
            "jsonrpc": "2.0",
            "id": req_id,  # 1
            "error": {"code": 429, "message": "Too many requests"}
        })
        return  # Exit here - don't process further

┌─────────────────────────────────────────────────────────────────────┐
│ Step 7: JSONRPCHandler._reply() sends response                     │
└─────────────────────────────────────────────────────────────────────┘

def _reply(self, status: int, data: dict):
    body = json.dumps(data).encode()
    # body = b'{"jsonrpc":"2.0","id":1,"error":{"code":429,"message":"Too many requests"}}'
    self.send_response(status)  # HTTP 429
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)  # Send JSON body

┌─────────────────────────────────────────────────────────────────────┐
│ Step 8: Test receives response                                     │
└─────────────────────────────────────────────────────────────────────┘

HTTP/1.1 429 Too Many Requests
Content-Type: application/json
Content-Length: 85

{"jsonrpc":"2.0","id":1,"error":{"code":429,"message":"Too many requests"}}

↓

Router receives 429 error
↓
Router: "Provider 1 is rate-limited, try provider 2"
↓
Router tries provider 2 (which is healthy)
↓
Gets success response
↓
Test verifies: "Router correctly handled rate-limited provider"
✓ TEST PASSED
```

---

## Class Interaction Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                          main()                             │
│                                                             │
│  1. Create states = {                                      │
│       "1": ProviderState(),                                │
│       "2": ProviderState(),                                │
│       "3": ProviderState()                                 │
│     }                                                      │
│                                                             │
│  2. Create JSONRPCHandler servers [port 18545-18547]      │
│     ├─ srv.state = states["1"]                            │
│     ├─ srv.state = states["2"]                            │
│     └─ srv.state = states["3"]                            │
│                                                             │
│  3. Create ControlHandler server [port 19000]             │
│     └─ ctrl.provider_states = states (all 3)              │
│                                                             │
│  4. Start 4 threads                                        │
└─────────────────────────────────────────────────────────────┘
           ↓
   ┌───────┼───────┬────────┐
   │       │       │        │
   ↓       ↓       ↓        ↓

Thread 1:              Thread 2:              Thread 3:              Thread 4:
JSONRPCHandler        JSONRPCHandler        JSONRPCHandler        ControlHandler
(port 18545)          (port 18546)          (port 18547)          (port 19000)

Accesses:             Accesses:             Accesses:             Accesses:
states["1"]           states["2"]           states["3"]           states (all 3)

do_POST():            do_POST():            do_POST():            do_POST():
├─ state.snapshot()   ├─ state.snapshot()   ├─ state.snapshot()   ├─ Iterate states
├─ Check mode         ├─ Check mode         ├─ Check mode         ├─ Call update()
├─ Check latency      ├─ Check latency      ├─ Check latency      └─ Return 200
├─ Check error_prob   ├─ Check error_prob   ├─ Check error_prob
└─ Return response    └─ Return response    └─ Return response    do_GET():
                                                                   ├─ Get snapshots
                                                                   └─ Return state
```

---

## Summary Table

| Class | Port | Purpose | Main Method | Uses |
|-------|------|---------|-------------|------|
| **ProviderState** | N/A | Hold provider state | `update()` | Thread lock |
| **JSONRPCHandler** | 18545, 18546, 18547 | Serve fake responses | `do_POST()` | ProviderState |
| **ControlHandler** | 19000 | Configure providers | `do_POST()`, `do_GET()` | ProviderState (all) |
| **main()** | N/A | Orchestrate all | `main()` | All classes |

---

**You now have complete understanding of every class and module!** 🎉

