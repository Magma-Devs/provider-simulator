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
    error_code: int = -32000
    error_message: str = "Internal error"
    http_status: int = 200
    responses: Dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAX), repr=False)
    total_calls: int = 0
    calls_by_status: Dict[str, int] = field(default_factory=dict, repr=False)
```

### Instance Variables Explained

| Variable | Type | Default | Purpose |
|----------|------|---------|---------|
| `mode` | str | `"success"` | Provider behaviour: `"success"`, `"error"`, `"rate_limit"`, `"down"` |
| `latency_ms` | int | `0` | Milliseconds to sleep before responding |
| `error_probability` | float | `0.0` | Probability of error when `mode="success"` (0.0 = never, 1.0 = always) |
| `error_code` | int | `-32000` | JSON-RPC error code when `mode="error"` or `error_probability` fires |
| `error_message` | str | `"Internal error"` | JSON-RPC error message paired with `error_code` |
| `http_status` | int | `200` | HTTP status of error responses (200 = JSON-RPC body error; 400/500 = HTTP-level) |
| `responses` | dict | `{}` | Per-method custom result overrides (`{method_name: {result: ...}}`) |
| `lock` | Lock | new Lock() | Field-level lock used by every accessor method |
| `history` | deque | `deque(maxlen=HISTORY_MAX)` | Per-provider ring buffer of recent calls; oldest is dropped past `HISTORY_MAX` entries (2000 by default, `SIM_HISTORY_MAX` env var overrides) |
| `total_calls` | int | `0` | All-time call count; never reset by ring rollover, only by `clear_history()` |
| `calls_by_status` | dict | `{}` | All-time per-status counters (`success`/`error`/`rate_limit`/`down`) |

### Methods

#### **1. snapshot() - Read Current State Safely**

```python
def snapshot(self) -> dict:
    with self.lock:
        return {
            "mode":              self.mode,
            "latency_ms":        self.latency_ms,
            "error_probability": self.error_probability,
            "error_code":        self.error_code,
            "error_message":     self.error_message,
            "http_status":       self.http_status,
        }
```

**What it does:**
- Returns a **copy** of the six mutable scenario-config fields
- Uses the lock so the copy is internally consistent even if a writer is running concurrently
- Does NOT include `responses` (returned only via `state.responses` under the lock when the success path actually needs it) or `history`/counters (those have dedicated accessors `get_history()`/`stats()`)

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
# Output: {"mode": "rate_limit", "latency_ms": 100, "error_probability": 0.0,
#          "error_code": -32000, "error_message": "Internal error", "http_status": 200}
```

---

#### **2. update(cfg) - Change the State**

```python
def update(self, cfg: dict) -> None:
    with self.lock:
        self.mode              = cfg.get("mode",              self.mode)
        self.latency_ms        = cfg.get("latency_ms",        self.latency_ms)
        self.error_probability = cfg.get("error_probability", self.error_probability)
        self.error_code        = cfg.get("error_code",        self.error_code)
        self.error_message     = cfg.get("error_message",     self.error_message)
        self.http_status       = cfg.get("http_status",       self.http_status)
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

#### **3. reset_scenario() - Return Scenario Config to Healthy Defaults**

```python
def reset_scenario(self) -> None:
    with self.lock:
        self.mode              = "success"
        self.latency_ms        = 0
        self.error_probability = 0.0
        self.error_code        = -32000
        self.error_message     = "Internal error"
        self.http_status       = 200
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

provider_state.reset_scenario()
# State: success, fast (back to healthy)
```

**Used by:** 
- ControlHandler when test sends `/reset` request
- The reset_simulator fixture in pytest (runs after each test)

---

#### **4. clear_history() - Wipe Call Log and Counters**

```python
def clear_history(self) -> None:
    with self.lock:
        self.history.clear()
        self.total_calls       = 0
        self.calls_by_status   = {}
```

**What it does:**
- Drops every entry from the in-memory ring-buffer
- Resets the all-time `total_calls` and `calls_by_status` counters to zero
- Does NOT touch scenario config — mode, latency, responses, etc. all stay as they were

**Why this matters:**
`/history/clear` is load-bearing for tests that need a clean call log on a still-configured provider — e.g. set `mode="rate_limit"`, clear history, send a request, confirm it was rate-limited without prior background traffic. Pairing it with the inverse `reset_scenario()` (which keeps history) is what makes the three reset routes meaningfully different: `/reset` (scenario only), `/history/clear` (history only), `/reset/all` (both).

**Used by:**
- ControlHandler when test sends `POST /history/clear` or `POST /reset/all`

---

#### **5. push_call_to_buffer() - Record a Completed Call**

```python
def push_call_to_buffer(self, method: str, status: str, latency_ms: int,
                        request_id: object = None, lava_headers: dict = None) -> None:
    now = time.time()
    with self.lock:
        self.history.append({
            "ts":           now,
            "time":         <UTC-formatted "YYYY-MM-DD HH:MM:SS.mmm UTC">,
            "request_id":   request_id,
            "method":       method,
            "status":       status,
            "latency_ms":   latency_ms,
            "lava_headers": lava_headers or {},
        })
        self.total_calls += 1
        self.calls_by_status[status] = self.calls_by_status.get(status, 0) + 1
```

**What it does:**
- Appends one entry to the per-provider ring buffer (capped at `HISTORY_MAX`, 2000 by default; oldest is dropped on overflow)
- Increments the all-time `total_calls` counter (never reset by ring rollover)
- Bumps the per-status counter in `calls_by_status`

**Called by:** `JSONRPCHandler.do_POST` on **every** branch — success, error, rate_limit, and down — so history is the source of truth for what actually happened.

**Special values:**
- `method="*"` is used for `mode="down"` calls, where the body is never parsed. Filter with `?method=*` to find rejected requests.
- `request_id=None` for those same down-mode calls (no body → no `id` field to echo).
- `lava_headers` is the dict of all `lava-*` request headers captured by the handler.

---

#### **6. stats() - All-Time Counters**

```python
def stats(self) -> dict:
    with self.lock:
        return {
            "total_requests_all_time":     self.total_calls,
            "total_calls":                 self.total_calls,
            "requests_by_status_all_time": dict(self.calls_by_status),
            "calls_by_status":             dict(self.calls_by_status),
            "history_ring_buffer_entries": len(self.history),
        }
```

**What it does:**
- Returns a thread-safe snapshot of the all-time counters for this provider
- Counters survive ring-buffer rollover — only `clear_history()` resets them
- Includes both `total_requests_all_time` / `total_calls` and `requests_by_status_all_time` / `calls_by_status` as aliases (legacy name plus shorter alias for convenience)
- `history_ring_buffer_entries` is the current buffer occupancy, capped at `HISTORY_MAX`

**Used by:** `ControlHandler.do_GET` to build the `GET /stats` response.

---

#### **7. get_history() - Snapshot of the Ring Buffer**

```python
def get_history(self) -> list:
    with self.lock:
        return list(self.history)
```

**What it does:**
- Returns a list copy of the current ring-buffer contents (oldest → newest order)
- Mutations to the returned list do not affect the live buffer

**Used by:** `ControlHandler.do_GET` to build the `GET /history` response. After collecting one list per provider, the handler merges them, sorts by `ts`, and assigns derived `correlation_group` / `call_order` fields before returning.

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
    method = body.get("method", "unknown")

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
        self._reply(snap["http_status"], {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": snap["error_code"], "message": snap["error_message"]}
        })
        return

    # Step 6: Success - return configured response
    with state.lock:
        method_cfg = state.responses.get(method) or state.responses.get("default", {})
    result = method_cfg.get("result", METHOD_DEFAULTS.get(method, "0x1"))

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
- `snap` is now a dict with all six scenario-config fields: `{"mode": "down", "latency_ms": 0, "error_probability": 0.0, "error_code": -32000, "error_message": "Internal error", "http_status": 200}`

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
    method = body.get("method", "unknown")
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
result = method_cfg.get("result", METHOD_DEFAULTS.get(method, "0x1"))

self._reply(200, {"jsonrpc": "2.0", "id": req_id, "result": result})
```
- Look up custom response for this method
- If not found, use "default" response
- If neither found, use a method-specific default from `METHOD_DEFAULTS`
- Example: `eth_blockNumber` → `"0x1312D00"`; `eth_getBlockByNumber` → block object
- Return HTTP 200 with the result

---

#### **_reply() - Send Response Helper**

```python
def _reply(self, status: int, data: dict):
    body = json.dumps(data).encode()
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-store")
    self.end_headers()
    self.wfile.write(body)
```

**What it does:**
- Converts the response data to JSON
- Sends HTTP headers (status, content-type, content-length, `Cache-Control: no-store` so routers/caches do not retain simulated responses)
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
10. Use `METHOD_DEFAULTS["eth_blockNumber"]` → `"0x1312D00"`
11. Call _reply(200, {...})

Response:
HTTP/1.1 200 OK
Content-Type: application/json
Content-Length: 59

{"jsonrpc":"2.0","id":1,"result":"0x1312D00"}
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
        # Reset scenario config only
        for state in self.server.provider_states.values():
            state.reset_scenario()
        self._reply(200, {"status": "scenario reset"})

    elif self.path == "/history/clear":
        # Clear history/counters only
        for state in self.server.provider_states.values():
            state.clear_history()
        self._reply(200, {"status": "history cleared"})

    elif self.path == "/reset/all":
        # Reset scenario config and clear history
        for state in self.server.provider_states.values():
            state.reset_scenario()
            state.clear_history()
        self._reply(200, {"status": "scenario reset and history cleared"})

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
- Calls `reset_scenario()` on all provider states
- Resets scenario config only

**Example:**
```python
# Request body: {} (empty)

# Runs:
for state in [state1, state2, state3]:
    state.reset_scenario()

# Returns:
{"status": "scenario reset"}
```

**Case 3: POST /history/clear**
- Calls `clear_history()` on all provider states
- Wipes history/counters only

**Case 4: POST /reset/all**
- Resets scenario config and clears history in one call

---

#### **do_GET() - Read State, Health, Stats, and History**

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

    if self.path == "/stats":
        self._reply(200, {"providers": {pid: s.stats()
                          for pid, s in self.server.provider_states.items()}})
        return

    if self.path == "/history" or self.path.startswith("/history?"):
        # merges history from all providers and supports filters
        ...
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

**Case 3: GET /stats**
- Returns all-time counters per provider
- Useful to see how many calls each provider handled by status

**Case 4: GET /history**
- Returns merged, ordered call history across all providers
- Supports query params: `last`, `from`, `to`, `provider`, `method`, `status`, `request_id`, and the dynamic-prefix `lava_header_<name>=<value>` (underscores in `<name>` become hyphens — e.g. `?lava_header_lava_stateful_api=true` matches header `lava-stateful-api: true`; multiple `lava_header_*` filters AND together)

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
3. For all providers, call state.reset_scenario()
4. Call _reply(200, {"status": "scenario reset"})

Response:
HTTP/1.1 200 OK
Content-Type: application/json

{"status": "scenario reset"}
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
    # snap = {"mode": "rate_limit", "latency_ms": 0, "error_probability": 0.0,
    #         "error_code": -32000, "error_message": "Internal error", "http_status": 200}
    
    if snap["mode"] == "down":  # ✗ False
        ...
    
    if snap["latency_ms"] > 0:  # ✗ False
        ...
    
    length = int(self.headers.get("Content-Length", 0))
    body = json.loads(self.rfile.read(length))
    # body = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
    req_id = body.get("id", 1)  # req_id = 1
    method = body.get("method", "unknown")  # method = "eth_blockNumber"
    
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

