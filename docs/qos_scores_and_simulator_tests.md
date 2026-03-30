# rpcconsumer QoS Scores — Findings & Implications for Simulator Tests

> Sources verified against `/Users/victoria/lava` source code, March 2026.
> Key files:
> - `protocol/provideroptimizer/provider_optimizer.go`
> - `utils/score/score_store.go`
> - `utils/score/score_config.go`
> - `protocol/rpcconsumer/rpcconsumer_server.go`
> - `protocol/relaycore/results_manager.go`
> - `protocol/relaycore/relay_processor.go`
> - `protocol/relaycore/selection.go`
> - `protocol/lavasession/consumer_session_manager.go`
> - `protocol/statetracker/updaters/pairing_updater.go`
> - `protocol/chainlib/chainproxy/rpcInterfaceMessages/jsonRPCMessage.go`
> - `protocol/common/endpoints.go`

---

## Glossary — Lava Protocol Quick Reference

> Plain-language definitions for anyone new to the project.
> Full explanations with context are in section 0.

| Term | Definition |
|---|---|
| **AppendRelayData** | An internal function called after a relay succeeds. It tells the scoring system "this provider just worked" and nudges that provider's reliability score upward. |
| **AppendRelayFailure** | An internal function called after a relay fails. It tells the scoring system "this provider just failed" and pushes that provider's reliability score down. |
| **ConsumerSessionManager (CSM)** | The part of the rpcconsumer that manages active "conversations" with each provider. Each conversation (session) is signed and time-limited so providers can verify the request is legitimate and track usage. |
| **Cross-Validation** | An optional mode where the router sends the same question to several providers at once and only trusts the answer if most of them agree — like asking multiple people and only believing the majority. Not used in our simulator tests. |
| **CU (Compute Unit)** | Lava's unit for measuring how expensive a request is. Simple calls like `eth_blockNumber` cost a few CUs; complex ones like `eth_getLogs` cost more. Providers get paid based on CUs served, so the router tracks CU spend per session. |
| **DefaultHalfLifeTime** | A hardcoded 1-hour setting that controls how fast a provider's score fades. After 1 hour, old observations count for half as much. After 2 hours, a quarter. This means a provider that was bad an hour ago but is now healthy will gradually recover. |
| **Epoch** | A roughly 30-minute time window defined by the Lava blockchain. Think of it as a "shift": at the start of each epoch, the router downloads a fresh list of available providers. Sessions from the previous epoch can't be reused. |
| **Extension / Addon** | Extra capabilities some providers support beyond the standard Ethereum API. For example, `archive` means the provider can answer questions about very old blockchain states. You can request a specific extension using the `lava-extension` header. |
| **InitialDataStaleness** | A trick for brand-new providers: their initial score is timestamped 24 hours in the past, making it effectively weightless. The very first real relay immediately becomes the actual score — no unfair advantage from the placeholder default. |
| **`lava-provider-address`** | A response header the router adds to every reply, telling you which provider(s) were involved. Single name = first provider worked fine. Comma-separated list = retries happened, all attempted providers are listed (in random order — you can't tell from position who actually won). |
| **`Lava-Retries`** | A response header showing how many extra attempts were needed. `0` or absent = first provider worked. `1` = first provider failed, a second was tried. `2` = two failures before success. |
| **MaxHalfTime** | The maximum half-life the scoring system will use: 3 hours. Normally scores decay with a 1-hour half-life, but for providers that haven't been contacted recently the half-life stretches — up to this cap. |
| **Node Error** | The provider answered, but with an error inside the response — e.g. `{"error": "block not found"}`. The provider was reachable; the blockchain call itself failed. The router counts this as a failure and retries with a different provider. |
| **`OnSessionDone`** | A function that runs automatically when a relay completes successfully. It triggers `AppendRelayData` to update the provider's score upward. |
| **`OnSessionFailure`** | A function that runs automatically when a relay fails. It triggers `AppendRelayFailure` to update the provider's score downward. |
| **Protocol Error** | The provider couldn't be reached at all — connection refused, HTTP 503/429, or timeout. The router never got any response back. Treated as a hard failure for scoring and triggers a retry. |
| **`ProviderOptimizer`** | The brain of the routing system. It keeps a score for every provider and uses those scores to decide who to send requests to. Good providers get picked more often; bad ones less often. It also handles the occasional "try someone new" to keep scores fresh. |
| **`providersStorage`** | The in-memory store where all provider scores live. It's a simple key → value map: provider name → score data. Lives in RAM, so it's completely wiped when the rpcconsumer pod restarts. |
| **`RelayCountOnNodeError`** | How many retries are allowed after a node error: `2`. This means the router will try up to 3 providers total before giving up and returning an error to you. |
| **rpcconsumer** | The main Lava routing process your tests talk to. You send it a normal JSON-RPC call; it picks a provider, forwards the request, retries on failure, and returns the result. It's the only component in the stack you interact with directly. |
| **Relay** | One complete round-trip from the rpcconsumer to a provider. It's not just forwarding your HTTP request — the rpcconsumer rebuilds it as a signed gRPC message with authentication and usage tracking attached. See section 0 for the full flow diagram. |
| **Session** | A short-lived signed contract between the router and a specific provider. It proves the request is legitimate and tracks how many CUs have been used. Sessions expire automatically and are replaced with new ones. |
| **`usedProviders`** | A temporary list built per-request of providers already tried. On each retry the router skips everyone on this list — so a bad provider is never tried twice for the same request. |
| **WRS (Weighted Random Selection)** | The algorithm the router uses to pick a provider. It doesn't always pick the best one — it picks randomly, but providers with better scores get a higher chance. This means good providers win most of the time while others still get occasional attempts, keeping their scores up to date. |

---

## 0. Lava Protocol — Key Concepts and Request Headers

Before reading the rest of this document it helps to know what the Lava-specific
terms mean.

### Core concepts

**Relay**
A relay is one complete round-trip from the rpcconsumer to a provider.

A common misconception is that the rpcconsumer just takes your HTTP request,
adds some headers, and forwards it. That's not what happens. The rpcconsumer
**completely rebuilds** your request into a different protocol (gRPC) and wraps
it with authentication and usage tracking before sending it on. On the way back
it adds its own diagnostic headers to the response it returns to you — the
backend (simulator or real blockchain node) never sees those headers.

```
You                 rpcconsumer            lavap provider        Backend
────────────────────────────────────────────────────────────────────────
POST eth_blockNumber
──────────────────→
                    picks a provider,
                    rebuilds request as
                    signed gRPC relay
                    (session auth + CU)
                    ───────────────────→
                                          unwraps relay,
                                          makes new plain
                                          HTTP JSON-RPC call
                                          ─────────────────→
                                                               {"result":"0x..."}
                                          ←─────────────────
                    ←───────────────────
                    adds response headers
                    (lava-provider-address,
                    Lava-Retries, etc.)
←──────────────────
response + headers
```

Two things to take away from this:
1. The headers in `src/constants/project.py` like `lava-provider-address` and
   `Lava-Retries` are **added by the rpcconsumer on the way back to you** — the
   provider and backend know nothing about them.
2. When the test logs say `retries=1` it means **two relays** were sent — the
   first to a provider that failed, the second to a different one.



**rpcconsumer**
The Lava component that sits between your application and the blockchain
providers. It receives plain JSON-RPC calls (e.g. `eth_blockNumber`), applies
WRS/QoS to select a provider, sends the relay, handles retries on failure, and
returns the result. Our simulator tests call the rpcconsumer at
`https://eth-sim-jsonrpc.victoria.magmadevs.com`.

**Provider**
A node operator running both a `lavap provider` process (Lava protocol
authentication + session management) and the actual blockchain endpoint. In our
simulator, `simprovider1`, `simprovider2`, `simprovider3` are fake providers
that mimic real ones. Each has a unique name used as its score key.

**WRS — Weighted Random Selection**
The algorithm the rpcconsumer uses to pick a provider. Each provider is
assigned a weight derived from its composite QoS score. Higher score = higher
selection probability, but all providers with positive scores retain some
chance of being chosen (exploration). After repeated failures a provider's
weight drops toward zero but never disappears entirely.

**QoS Score**
A composite number per provider, calculated from three dimensions: availability
(fraction of relays that succeeded), latency (round-trip time), and sync (how
far behind the latest block the provider is). Stored in-memory in
`ProviderOptimizer.providersStorage`. See sections 1–4 for full detail.

**Node Error vs Protocol Error**
- *Node error*: the provider responded but with a JSON-RPC error body
  (`{"error": {...}}`). The provider was reachable; the blockchain request
  itself failed. The rpcconsumer retries with a different provider.
- *Protocol error*: the provider was unreachable at the Lava session level
  (HTTP timeout, TCP refused, 503 from the pod). The session is marked as a
  failure and the provider's availability score is penalised.

**Session**
A time-bounded interaction ticket between the rpcconsumer and a specific
provider, managed by `ConsumerSessionManager`. Each session tracks how many
CU have been used, is signed, and has an epoch number. A new session is
opened automatically when the current one expires or is exhausted.

**usedProviders**
An in-memory set built per-request that tracks which providers have already
been tried. On each retry the rpcconsumer excludes `usedProviders` from WRS
so the same failing provider is never retried for the same request.

**Cross-Validation (mode)**
An optional mode where the rpcconsumer sends the **same request to multiple
providers simultaneously** and compares their responses. If enough providers
agree (≥ `agreement-threshold`) the result is accepted as trustworthy. Used
for high-stakes or security-sensitive queries where data integrity matters
more than latency. Our simulator tests run in **standard (non-cross-validation)
mode** — one provider at a time, retry on failure, no result comparison.

**Extension / Addon**
Lava providers can expose optional capabilities beyond the base JSON-RPC spec:
- `archive` — full historical state (blocks before the pruning window)
- `trace` — execution trace methods (`debug_traceTransaction` etc.)
- `debug` — debug-level RPC methods

The `lava-extension` request header lets a consumer explicitly request a
provider that supports a specific extension for that one call.

**Epoch**
A time window defined by the Lava blockchain (typically ~30 minutes). Provider
pairings, stakes, and session keys are valid for one epoch. At epoch change the
rpcconsumer fetches a new pairing list from the chain.

---

### Request headers (sent by the consumer TO the rpcconsumer)

These are **directive headers** — they are consumed by the rpcconsumer and
never forwarded to providers.

| Header | Constant | What it does |
|---|---|---|
| `lava-force-cache-refresh` | `FORCE_CACHE_REFRESH_HEADER_NAME` | Skip the router cache and force a real provider call. See section 14 for full detail. |
| `lava-providers-block` | `BLOCK_PROVIDERS_ADDRESSES_HEADER_NAME` | Comma-separated list of provider names to **exclude** from WRS for this request. Use when you know a specific provider gave bad data. |
| `lava-relay-timeout` | `RELAY_TIMEOUT_HEADER_NAME` | Override the default relay timeout (seconds) for this one request. Useful for known slow operations (e.g. `debug_traceBlockByNumber`). |
| `lava-extension` | `EXTENSION_OVERRIDE_HEADER_NAME` | Force selection of a provider that supports this extension (`archive`, `trace`, `debug`). Without it the rpcconsumer infers the required extension from the method name. |
| `lava-stickiness` | `STICKINESS_HEADER_NAME` | All requests carrying the same stickiness value are routed to the **same provider**. Critical for stateful flows: `eth_sendRawTransaction` and the subsequent `eth_getTransactionReceipt` must hit the same provider or the pending transaction is not visible. |
| `lava-lb-unique-id` | `LAVA_LB_UNIQUE_ID_HEADER` | When multiple rpcconsumer pods are behind a load balancer, this header is used as a sticky-session key at the LB level, ensuring all requests in one logical flow reach the same rpcconsumer pod (and therefore the same QoS history). |
| `lava-debug-relay` | `LAVA_DEBUG_RELAY` | Enable debug mode for this relay. The response includes extra diagnostic info: which provider was chosen, latency breakdown, QoS score at decision time. Use in production to diagnose routing issues without changing config. |

Cross-validation request headers (only relevant when using cross-validation mode):

| Header | Constant | What it does |
|---|---|---|
| `lava-cross-validation-max-participants` | `CROSS_VALIDATION_HEADER_MAX_PARTICIPANTS` | How many providers to query simultaneously (e.g. `3`). Higher = more trustworthy, higher latency. |
| `lava-cross-validation-agreement-threshold` | `CROSS_VALIDATION_HEADER_AGREEMENT_THRESHOLD` | Fraction of providers that must return the same result for it to be accepted (e.g. `0.6` = 60%). |

---

### Response headers (sent by the rpcconsumer TO the consumer)

| Header | Constant | What it tells you |
|---|---|---|
| `lava-provider-address` | `ROUTING_HEADER_PROVIDER_ADDRESS` | Who answered. Single name when no retries; unordered comma-separated set when retries happened. See section 7 for the full behaviour. |
| `Lava-Retries` | `ROUTING_HEADER_RETRIES` | How many retries happened beyond the first attempt. Absent or `0` = first provider succeeded. `1` = one retry, etc. |
| `provider-latest-block` | `ROUTING_HEADER_PROVIDER_LATEST_BLOCK` | The latest block height the winning provider reported. |
| `lava-guid` | `ROUTING_HEADER_REQUEST_GUID` | Unique correlation ID for this request. Use to trace a specific request across rpcconsumer logs. |

Cross-validation response headers:

| Header | Constant | What it tells you |
|---|---|---|
| `lava-cross-validation-all-providers` | `CROSS_VALIDATION_ALL_PROVIDERS_HEADER_NAME` | All providers that were queried (sorted alphabetically). |
| `lava-cross-validation-status` | `CROSS_VALIDATION_STATUS_HEADER_NAME` | Outcome: `agreed`, `disagreed`, or `insufficient` (not enough providers responded). |
| `lava-cross-validation-agreeing-providers` | `CROSS_VALIDATION_AGREEING_PROVIDERS_HEADER` | Which providers agreed on the winning response (sorted alphabetically). |

---

### Internal gRPC trailers (NOT visible in HTTP responses)

These values travel on the **gRPC layer** between the `lavap provider` process
and the `rpcconsumer`. They are consumed internally and never forwarded to
your HTTP client — you will never see them in a `curl -i` output.

| Trailer | Constant | What it does |
|---|---|---|
| `Lava-Provider-Unique-Id` | `chainlib.RpcProviderUniqueIdHeader` | A random `uint64` generated once at provider process startup. Sent back on every relay response. The rpcconsumer stores it on first contact and verifies it matches on every subsequent relay in the same session — a session integrity guard. See **section 19** for full detail. |
| `Lava-Provider-Load-Rate` | `chainlib.RpcProviderLoadRateHeader` | The provider's current load rate, appended to every relay response trailer. Used by the consumer's optimizer to factor in provider load when making selection decisions. |

---

## 1. What QoS Scores Are

The `rpcconsumer` (`lavap rpcconsumer`) maintains an **in-memory** score for
every provider it has ever contacted. These scores drive **Weighted Random
Selection (WRS)**: providers with higher scores get chosen more often.

Each provider has three independent score dimensions:

| Dimension    | Default value  | What it measures |
|---|---|---|
| Availability | `1.0` (100%)   | Fraction of requests that succeeded (not error, not timeout) |
| Latency      | `0.01` (10 ms) | Round-trip time to the provider |
| Sync         | `0.1` (100 ms) | How far behind the provider's block height is |

All three are combined into a single composite score used for tier placement
and WRS selection.

---

## 2. How Initial Scores Are Set

**Verified against: `utils/score/score_store.go` — `NewScoreStore()`**

### Scores are fractions, not single numbers

Before getting into the defaults it helps to know how a score is actually
stored. Each dimension's score is kept as a **fraction**: a numerator (`Num`)
divided by a denominator (`Denom`). The actual score you'd see in a report is
`Num / Denom`. This fraction structure is what makes the decaying weighted
average in section 3 work cleanly.

### What the defaults are

When a provider is contacted for the very first time, Lava creates a fresh
score record for it. The starting values are:

```go
// utils/score/score_store.go — lines 316–318
DefaultAvailabilityNum float64 = 1      // availability score starts at 1/1 = 1.0  (100%)
DefaultLatencyNum      float64 = 0.01  // latency score starts at 0.01/1 = 10ms
DefaultSyncNum         float64 = 0.1   // sync score starts at 0.1/1 = 100ms behind latest
```

All three start with `Denom = 1`, so the initial resolved score for each
dimension is just the numerator value listed above.

In plain language: a brand-new provider enters the system looking perfect —
100% availability, 10ms latency, 100ms sync lag. This is not real data; it
is a placeholder that gives new providers the benefit of the doubt until real
data arrives.

### Why those initial values are immediately overridden

The initial score record is not just created with optimistic numbers — it is
also given a **backdated timestamp**:

```go
// utils/score/score_store.go — NewScoreStore()
time.Now().Add(-InitialDataStaleness)   // where InitialDataStaleness = 24 * time.Hour
```

The timestamp is set **24 hours in the past**. This matters because the decay
formula (section 3) weights every observation by how old it is. When the first
real relay arrives and the score updates, the initial values are weighted as
if they were 24 hours old:

```
weight of the initial record = 2^(-24h / 1h half-life) = 2^-24 ≈ 0.000006%
```

That tiny weight means the initial values contribute essentially nothing. The
very first real relay observation — whether it succeeds or fails — overwhelms
the starting value and becomes the effective score.

**Practical meaning:** "all providers start equal" is only true in the sense
that none of them has real history yet. After a single relay, the provider's
score already reflects that real experience.

### Why "perfect" defaults and not "neutral"?

Starting availability at 1.0 (perfect) rather than 0.5 (neutral) means a
brand-new provider gets a **high initial WRS weight**. This maximises
exploration: the rpcconsumer is willing to try a provider it has never seen
before. The 24h staleness ensures this high starting weight vanishes the
moment real data arrives, so a bad new provider can't hide behind its
optimistic default for long.

---

## 3. Score Decay

**Verified against: `utils/score/score_config.go` and `score_store.go:Update()`**

### The idea in plain English

Think of the score as a running average where recent data matters more than
old data. Every time you get a new observation (a relay succeeded or failed),
you blend it with the existing average. But as time passes, the existing
average automatically loses influence — it "decays". The older an observation,
the less it counts toward the current score.

The speed of that decay is controlled by the **half-life**: the time it takes
for an old observation to lose half of its influence. Lava uses a **1-hour
default half-life**.

### The constants

```go
// utils/score/score_config.go
DefaultHalfLifeTime = time.Hour      // default: scores halve in weight every 1 hour
MaxHalfTime         = 3 * time.Hour  // the half-life can stretch, but never past 3 hours
```

### The full update formula

Each score dimension stores a fraction: `Num / Denom`. Both parts are
updated on every relay, verified from `score_store.go:Update()`:

```
// Step 1: how much should the existing score shrink?
decay = exp(-ln(2) × timeSinceLast / halfLife)
      = 2^(-timeSinceLast / halfLife)     ← equivalent form

// Step 2: blend new sample into both parts of the fraction
new_Num   = old_Num   × decay + sample × weight
new_Denom = old_Denom × decay + weight

// Step 3: the actual score you see in a report
score = new_Num / new_Denom
```

`weight` for real relays is `RelayUpdateWeight = 1.0` (from `score_config.go`).
`sample` is:
- `1.0` for a successful relay (availability dimension)  
- `0.0` for a failed relay (availability dimension)
- The round-trip time in seconds for the latency dimension
- The sync lag in seconds for the sync dimension

### How quickly old data fades — worked examples

Using `halfLife = 1 hour`:

| Time since the observation | How much it still influences the score |
|---|---|
| 1 minute | 98.8% — barely faded |
| 10 minutes | 89% |
| 30 minutes | 71% |
| **1 hour** | **50%** — half-life: exactly half the influence gone |
| 2 hours | 25% |
| 3 hours | 12.5% |
| 6 hours | 1.5% |
| 24 hours | ~0% |

**Practical consequence:** if a provider starts failing after running perfectly
for an hour, it takes another ~2–3 hours of consecutive failures to push its
score below a healthy provider. The rpcconsumer does not forget good history
instantly.

### The half-life is adaptive — not always 1 hour

The code does NOT always use `DefaultHalfLifeTime = 1h` directly. The actual
half-life used for each relay is calculated by `calculateHalfTime()` in
`provider_optimizer.go`:

```go
func (po *ProviderOptimizer) calculateHalfTime(provider string, sampleTime time.Time) time.Duration {
    halfTime := score.DefaultHalfLifeTime          // start at 1 hour
    relaysHalfTime := po.getRelayStatsTimeDiff(provider, sampleTime)  // time since median relay
    if relaysHalfTime > halfTime {
        halfTime = relaysHalfTime                  // stretch if provider not seen recently
    }
    if halfTime > score.MaxHalfTime {
        halfTime = score.MaxHalfTime               // never exceed 3 hours
    }
    return halfTime
}
```

If a provider has not been contacted for a long time, its effective half-life
stretches up to 3 hours. The intent: if you haven't heard from a provider
recently, its old score is still your best data — don't let it decay away
before you have new data to replace it with.

**In simulator tests** (rapid requests, all recent): the median relay
timestamp is always seconds ago, so `relaysHalfTime ≈ a few seconds < 1 hour`.
The half-life stays at `DefaultHalfLifeTime = 1 hour` for all practical
purposes in our test runs.

---

## 4. What Resets Scores — and What Does NOT

**Verified against: `provider_optimizer.go` — `providersStorage` is a Ristretto in-memory cache.**

### The short answer

The rpcconsumer stores QoS scores in RAM. There is **no API to wipe them**.
The only way to start fresh is to restart the process (or kill the pod).
Everything else — resetting the simulator, changing scenarios, waiting a few
seconds — leaves the scores exactly as they were.

### Full reference table

| Action | Resets QoS scores? | Why |
|---|---|---|
| `POST /reset` on simulator control API | **NO** | Only changes what the fake providers return. The rpcconsumer doesn't know this happened. |
| `POST /scenario` on simulator control API | **NO** | Same — simulator config change, rpcconsumer is unaware. |
| Calling `sim_control.reset()` in Python | **NO** | Same as above — it just calls `POST /reset`. |
| Restarting the `rpcconsumer` pod | **YES** | The Ristretto in-memory cache (`providersStorage`) is created fresh at startup. |
| Helm upgrade that recreates the pod | **YES** | Pod restart → same as above. |
| Natural decay over time | **Partial** | Old observations lose influence but the entry is never deleted from the cache. A provider that has been silent for 3+ hours will have near-zero weight scores, but the entry still exists. |

### Why this matters for tests — a concrete example

Imagine two tests run one after the other:

**Test 1 — `test_routes_to_only_healthy_provider`:**  
Scenario: P1=rate_limit, P2=down, P3=success.  
The test sends 60+ warmup requests. P3 answers all of them. By the end,
P3 has an availability score close to 1.0 built from ~60 relays. P1 and P2
have scores close to 0. All three scores are now live in the rpcconsumer's RAM.

**`sim_control.reset()` fires between tests:**  
The simulator backends go back to "success" mode. The rpcconsumer scores are
**not touched**. P3 still has a score close to 1.0. P1 and P2 still have
scores close to 0.

**Test 2 — `test_router_succeeds_despite_flaky_providers`:**  
Scenario: P1=70% error, P2=90% error, P3=20% error.  
The rpcconsumer enters this test thinking P1 and P2 are nearly dead (scores
≈ 0 from test 1) and P3 is perfect (score ≈ 1.0 from test 1). This is
actually the right situation for the test! But now consider the opposite
order:

**Test 2 runs first, then Test 1:**  
After Test 2 (300 requests with flaky providers), P3 has a score around 0.80
and P1/P2 have scores around 0.25–0.30 (not zero — they succeeded ~30% of
the time). Now Test 1 starts. The scenario says P1=rate_limit, P2=down. But
the rpcconsumer doesn't know that yet — its scores say P1 and P2 are still
somewhat viable. It will try P1 and P2 first, observe them failing, update
their scores, and eventually stop using them. But it takes more requests to
converge than if scores had been clean.

**This is exactly why the warmup phase exists** — to burn through the stale
history from previous tests before we start asserting anything.

### There is no reset button because Lava was not designed for test isolation

The `providersStorage` cache was built to give the rpcconsumer a long memory
of provider reliability — that's the whole point of the 1-hour half-life.
There is no `/reset-scores` admin API because in production you would never
want to throw away accumulated quality data. For tests, the warmup phase
solves the problem by simply accumulating enough new data to dominate the
old scores.

---

## 5. How QoS Scores Are Keyed — Per Provider Name, Not Per Pod

**Source: `protocol/statetracker/updaters/pairing_updater.go:266`**

For static providers (configured by name in `values.yml`), the score key is
the provider's **`name`** field from the config, which becomes `PublicLavaAddress`:

```go
providerEntry := lavasession.NewConsumerSessionWithProvider(
    provider.Name,    // ← "simprovider1", "simprovider2", "simprovider3"
    endpoints,
    ...
)
```

**Running on the same pod does not merge scores.** `simprovider1`,
`simprovider2`, and `simprovider3` each have their own independent score entry
even though all three run on the same Kubernetes pod on different ports
(18545, 18546, 18547).

### Effect of same-pod deployment on score dimensions

Because all three providers share the same machine:

- **Latency scores** are nearly identical — network round-trip is the same.
- **Sync scores** are also identical if all providers query the same blockchain node.
- **Availability scores** are the **only meaningful differentiator**.

For `TestFlaky` this is beneficial: `error_probability` is the only variable
we control, and QoS convergence is purely availability-driven with no latency
noise distorting the signal.

---

## 6. How `error_probability` Is Applied

### What the simulator does

```python
# simulator do_POST handler — simulator_implementation_guide.md:169
if snap["mode"] == "error" or random.random() < snap["error_probability"]:
    self._reply(200, {
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": -32000, "message": "Internal error"}
    })
    return
```

The simulator **always returns HTTP 200**. The error is carried in the
JSON-RPC body, not in the HTTP status code.

### What the rpcconsumer does with it

**Source: `protocol/chainlib/chainproxy/rpcInterfaceMessages/jsonRPCMessage.go:57`**

`CheckResponseError()` parses the response body and detects the `"error"` field:

```go
func (jm JsonrpcMessage) CheckResponseError(data []byte, httpStatusCode int) (hasError bool, errorMessage string) {
    var result struct {
        Error *rpcclient.JsonError `json:"error,omitempty"`
    }
    json.Unmarshal(data, &result)
    if result.Error == nil {
        return false, ""
    }
    return result.Error.Message != "", result.Error.Message
}
```

`foundError = true` → classified as a **node error**, not a protocol error.

**Source: `protocol/relaycore/selection.go`**

```go
var RelayCountOnNodeError = 2   // retry up to 2 times on node error
```

On node error the rpcconsumer retries up to **2 times** with **different
providers** — already-used providers are excluded via `usedProviders`.

**Source: `protocol/lavasession/consumer_session_manager.go` — `OnSessionFailure`**

```go
go csm.providerOptimizer.AppendRelayFailure(consumerSession.Parent.PublicLavaAddress)
```

`AppendRelayFailure` records `availability = 0` for the failing provider.
**QoS scoring correctly learns from JSON-RPC errors in HTTP 200 responses.**

### Full request lifecycle when P2 (90% error) is tried first

```
1. WRS selects P2
2. Simulator rolls random() < 0.9 → returns HTTP 200 + {"error": ...}
3. rpcconsumer: CheckResponseError → node error detected
4. OnSessionFailure → AppendRelayFailure(P2) → P2 availability drops
5. Retry 1: WRS selects P1 (P2 excluded via usedProviders)
6. P1 may fail again (70% chance) → AppendRelayFailure(P1) → P1 availability drops
7. Retry 2: WRS selects P3 → P3 likely succeeds (80% chance)
8. OnSessionDone → AppendRelayData(P3) → P3 availability rises
```

The response headers after this 3-attempt chain:

```
Lava-Retries: 2
lava-provider-address: simprovider1,simprovider3,simprovider2
                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                       Unordered set — all 3 participants collected into a
                       Go map and joined in non-deterministic iteration order.
                       P3 (the winner) is in the MIDDLE here, not last.
                       On a different run it could appear first or last.
                       Position carries NO information about who won.
```

**The winner (P3) is NOT identifiable from the header when `Lava-Retries > 0`.**
The only reliable way to know a winner is when `Lava-Retries = 0` — a single
entry meaning the first provider succeeded with no retries.

### Why the worst provider keeps winning in early requests — scoring was correct all along

During investigation, we observed P2 (90% error probability) appearing in
winner counts early in the test run and wondered whether the QoS scoring
system was actually penalising JSON-RPC errors wrapped in HTTP 200 responses.
The answer: **QoS scoring works correctly for these errors.**

The `CheckResponseError()` path correctly classifies HTTP 200 + JSON-RPC error
body as a node error. `OnSessionFailure` fires. `AppendRelayFailure` is called.
The availability score drops. Retries happen. The scoring system was never broken.

**Why P2 still wins early:** the rpcconsumer enters each test carrying QoS
scores from the previous test (1-hour half-life, no reset API, no pod restart).
If P2 had a high availability score from a previous all-success scenario, the
decaying-average score for P2 starts the test at, say, 0.85 (from N prior
successes). After a handful of failures the score drops — but those few new
failure observations are weighted against many old success observations. WRS
still assigns P2 significant weight until enough failures have accumulated to
statistically dominate the decaying average. This is score persistence, not a
scoring bug. The warmup phase exists precisely to burn through the stale
high scores before the measured window begins.

---

## 7. Response Headers — What They Mean and Their Limitations

**Source: `protocol/common/endpoints.go`, `protocol/rpcconsumer/rpcconsumer_server.go`**

| Header | Our constant | Value |
|---|---|---|
| `lava-provider-address` | `ROUTING_HEADER_PROVIDER_ADDRESS` | See below — behaviour changes based on retry count |
| `Lava-Retries` | `ROUTING_HEADER_RETRIES` | Integer — number of retries beyond the first attempt |
| `provider-latest-block` | `ROUTING_HEADER_PROVIDER_LATEST_BLOCK` | Block height from the winning provider |
| `lava-guid` | `ROUTING_HEADER_REQUEST_GUID` | Unique request correlation ID |
| `lava-force-cache-refresh` *(request)* | `ROUTING_HEADER_SKIP_CACHE` | Set `"true"` to bypass router cache |

### `lava-provider-address` — two completely different behaviours

**When `Lava-Retries = 0` (no retries, first provider succeeded):**

```go
// rpcconsumer_server.go
providerAddress := relayResult.GetProvider()   // the single winner
metadataReply = append(metadataReply, pairingtypes.Metadata{
    Name:  common.PROVIDER_ADDRESS_HEADER_NAME,
    Value: providerAddress,                    // single value, unambiguous
})
```

Result:
```
lava-provider-address: simprovider3    ← definitive winner
Lava-Retries: (absent)
```

**When `Lava-Retries > 0` (retries happened):**

The original single-winner value is **overwritten** with a comma-separated set
built from a **Go `map`** which has **non-deterministic iteration order**:

```go
allProvidersMap := make(map[string]bool)    // Go map — random iteration order
// Add winner + all providers from nodeErrorResults + protocolErrorResults
for provider := range allProvidersMap {     // ← random order, not retry sequence
    allProvidersList = append(allProvidersList, provider)
}
allProvidersString := strings.Join(allProvidersList, ",")
// This OVERWRITES the single-winner header value set earlier
metadataReply[i].Value = allProvidersString
```

Result:
```
lava-provider-address: simprovider1,simprovider3    ← arbitrary order
Lava-Retries: 1
```

**The comma-separated list is an unordered set of all participants.**
The winner's position within the list is random. It may appear first, last,
or anywhere in the middle. The list does NOT represent the retry sequence.

### Correct parsing rules

| `Lava-Retries` | `lava-provider-address` | What we can determine |
|---|---|---|
| `0` or absent | Single entry | **Definitive winner** — use `attempts[0]` |
| `> 0` | Comma-separated set | **All attempted providers** in arbitrary order — **winner not identifiable from position** |

### Cross-validation path sorts; the regular path does not

The sorting asymmetry between the two paths is the key reason the unordered
behaviour is surprising. Looking at the Lava source side by side:

**Cross-validation path** (`rpcconsumer_server.go`, around line 1879):
```go
sort.Strings(allProvidersList)   // ← explicitly sorted before joining
allProvidersString := strings.Join(allProvidersList, ",")
```
Cross-validation response headers always list providers **alphabetically**.
This is why the header table in section 0 says "sorted alphabetically" for
`lava-cross-validation-all-providers` and `lava-cross-validation-agreeing-providers`.

**Regular (non-cross-validation) path** (`results_manager.go` / `rpcconsumer_server.go`):
```go
allProvidersMap := make(map[string]bool)   // Go map
// ... providers added ...
for provider := range allProvidersMap {    // ← map range — NO sort
    allProvidersList = append(allProvidersList, provider)
}
allProvidersString := strings.Join(allProvidersList, ",")
```
No `sort.Strings` call. Go map iteration order is **intentionally randomised
by the runtime on every execution**. The same retry chain
(`P2 failed → P1 failed → P3 won`) can produce:

```
simprovider3,simprovider1,simprovider2   ← one run
simprovider2,simprovider3,simprovider1   ← another run, same event
simprovider1,simprovider2,simprovider3   ← yet another run
```

**All three are equally likely. Position carries zero information about
who was tried first, who was tried last, or who ultimately answered.**

This asymmetry — sorted in cross-validation, unsorted in the regular path —
is the root cause of the counter-intuitive header behaviour. See
`docs/lava_dev_question_header_ordering.md` for the open question sent to the
Lava team about whether this asymmetry is intentional.

---

## 8. How `lava-provider-address` Is Parsed in Test Code

Section 7 explains *why* the header behaves differently depending on retry
count. This section documents *how* the test code reads it correctly.

### The three-branch parsing pattern

Every request in the measurement loop runs this block:

```python
raw     = resp.headers.get(ROUTING_HEADER_PROVIDER_ADDRESS, "")
retries = int(resp.headers.get(ROUTING_HEADER_RETRIES, "0") or "0")

# Split and strip regardless of retry count — produces the participant list.
attempts = [p.strip() for p in raw.split(",") if p.strip()]

if raw.lower() == "cached":
    # Router served from cache — no provider was contacted, no routing decision made.
    # Log a warning; this should not happen when lava-force-cache-refresh is set.
    logger.warning("  [req %03d]  ⚠️  chosen=Cached — skip-cache header may not be working", i)
    winner_counter["Cached"] += 1

elif not attempts:
    # Header present but empty ("", "  ", ",,") — log and skip attribution.
    # Does not happen with a healthy rpcconsumer but guards against proxy stripping
    # or future router behaviour changes.
    logger.warning("  [req %03d]  provider header empty (raw=%r) — skipping", i, raw)

elif retries == 0:
    # No retries → single entry → definitive first-try winner.
    winner = attempts[0]                   # safe: attempts is guaranteed non-empty here
    winner_counter[winner]    += 1
    attempt_counter[winner]   += 1
    if i <= N_REQUESTS // 2:
        first_half_winners[winner]  += 1
    else:
        second_half_winners[winner] += 1

else:
    # Retries happened → header is an unordered set of all participants.
    # Record every provider as attempted; winner cannot be identified from position.
    # See section 7 for why the Go map makes position meaningless.
    for p in attempts:
        attempt_counter[p] += 1
    chain = " / ".join(attempts)
    logger.info("  [req %03d]  attempted=[%s]  retries=%d  (winner unknown)", i, chain, retries)
```

### Why the three branches are ordered the way they are

| Branch | Guard | Purpose |
|---|---|---|
| `raw.lower() == "cached"` | First | Short-circuit before the split — "Cached" would pass the `attempts` filter |
| `not attempts` | Second | Defensive — `attempts[0]` below would crash on an empty list |
| `retries == 0` | Third | Only safe place to call `attempts[0]` as the definitive winner |
| `else` (retries > 0) | Last | Unordered participant set — log only |

### Why `winner_counter` totals are less than `N_REQUESTS`

`winner_counter` only increments in the `retries == 0` branch — requests where
we are certain who the router chose as its primary selection. Requests that
involved retries (`retries > 0`) contribute only to `attempt_counter`. This is
intentional: including retry-chain requests would introduce Go map ordering
ambiguity into the convergence data.

---

## 9. How `TestFlaky` Works With All of the Above

```
conftest.py reset_simulator():
  POST /reset         → simulator modes → "success" (no error_probability)
                        rpcconsumer QoS  → UNCHANGED, persists from previous tests

test body:
  POST /scenario      → P1=70% error, P2=90% error, P3=20% error
                        rpcconsumer QoS  → still holds scores from previous tests

Warmup phase (N_WARMUP = 100 requests, not counted):
  → rpcconsumer tries providers, observes P1/P2 returning node errors
  → OnSessionFailure → AppendRelayFailure → P1/P2 availability scores drop
  → OnSessionDone    → AppendRelayData    → P3 availability score rises
  → After ~100 requests scores have been updated to reflect the new scenario

time.sleep(1):
  → minor pause to let async score updates (goroutines) propagate

Measurement phase (N_REQUESTS = 300 requests):
  retries == 0 → winner identified  → winner_counter + convergence halves
  retries >  0 → participants logged → attempt_counter only, no winner attributed
```

### What a healthy convergence table looks like

```
provider          first 50%   last 50%   trend
simprovider1              8          2   ↓ router learned to avoid this provider
simprovider2              4          1   ↓ router learned to avoid this provider
simprovider3             28         37   ↑ router learned to prefer this provider
```

> Totals are less than 150 per half because retry-chain requests don't count.

### What a broken convergence table looks like

```
provider          first 50%   last 50%   trend
simprovider1             13         14   → no change
simprovider2             12         12   → no change
simprovider3             15         14   → no change
```

Possible causes: QoS not feeding back into selection, error probabilities too
low for signal, or warmup phase too short.

---

## 10. Constants in `src/constants/project.py`

These constants are the Python names we use to refer to the actual HTTP header
strings. Using named constants instead of raw strings everywhere means that if
Lava ever renames a header, there is one place to change it.

### Response headers — what the rpcconsumer tells us after each request

**`ROUTING_HEADER_PROVIDER_ADDRESS = "lava-provider-address"`**  
The most important routing header. After a request completes, this tells you
which provider(s) were involved. When there were no retries it is a single
provider name (the definitive winner). When retries happened it becomes an
unordered comma-separated list of all providers that were tried. See section 7
for the full details on why the ordering cannot be trusted.

**`ROUTING_HEADER_RETRIES = "Lava-Retries"`**  
How many retry attempts happened beyond the first try. Absent or `"0"` means
the first provider succeeded on the first attempt. `"1"` means there was one
retry (two providers were tried total). This is the key to interpreting
`lava-provider-address` correctly — section 8 explains the three-branch
parsing pattern built around it.

**`ROUTING_HEADER_PROVIDER_LATEST_BLOCK = "provider-latest-block"`**  
The block height that the winning provider reported. Useful for diagnosing
sync issues — if this number is far below the current chain tip, the provider
is behind. Not used directly in routing logic by our tests but visible in logs.

**`ROUTING_HEADER_REQUEST_GUID = "lava-guid"`**  
A unique ID the rpcconsumer assigns to each request. If something looks wrong
in the router logs, you can grep for this ID to trace the exact request across
rpcconsumer log lines. Not used in assertions but helpful when debugging a
specific failing request.

**`ROUTING_HEADER_REQUEST_TYPE = "lava-user-request-type"`**  
Tells you how the rpcconsumer classified this request (e.g. `"regular"`,
`"archive"`, `"debug"`). Useful for confirming that extension inference is
working correctly.

**`ROUTING_HEADER_CF_CACHE_STATUS = "cf-cache-status"`**  
Added by Cloudflare (not the rpcconsumer). Values like `HIT`, `MISS`,
`BYPASS`, `DYNAMIC` tell you what the Cloudflare cache layer did with the
request before it ever reached the rpcconsumer. If this is `HIT`, the
rpcconsumer was never contacted at all — Cloudflare answered from its own
cache. Relevant for diagnosing why `lava-provider-address` might be absent.

### Request header — what we send to control routing behaviour

**`ROUTING_HEADER_SKIP_CACHE = "lava-force-cache-refresh"`**  
This is a **request** header (sent by us, not by the router). Setting it to
`"true"` tells the rpcconsumer to skip its response cache and always contact
a real provider. Without this, methods like `eth_blockNumber` are served from
cache and `lava-provider-address` comes back as `"Cached"` — meaning no
routing decision was made and no test scenario was exercised. Every simulator
test sets this header. See section 17 for the full explanation of why it is
set in two places (fixture default and explicit call-site).

---

## 11. What the Test Log Now Shows

```
─── Warmup phase — 15 requests (not counted in results) ──────────────────────
  [warmup 01]  provider=simprovider3                   retries=0  HTTP=200
  [warmup 02]  provider=simprovider2,simprovider3      retries=1  HTTP=200
  [warmup 03]  provider=simprovider1                   retries=0  HTTP=200

─── Per-request routing trace ────────────────────────────────────────────────
  [req 001]  chosen=simprovider3           retries=0  outcome=✅ success
  [req 002]  attempted=[simprovider2 / simprovider1 / simprovider3]
             retries=2  outcome=✅ success  (winner unknown — Go map order not deterministic)
  [req 003]  chosen=simprovider3           retries=0  outcome=✅ success

─── Final winner distribution (who ultimately answered) ──────────────────────
  winner  simprovider3    55x  ███████████████████████████████████████
  winner  simprovider1     4x  ████
  winner  simprovider2     1x  █

─── Total attempts per provider (incl. failed retries) ───────────────────────
  attempts  simprovider3   70x total  (55 won, 15 failed/retried)
  attempts  simprovider1   58x total  ( 4 won, 54 failed/retried)
  attempts  simprovider2   42x total  ( 1 won, 41 failed/retried)

─── QoS convergence — first half vs second half (each half = 60 requests) ────
  provider          first 50%   last 50%   trend
  simprovider1              8          2   ↓ router learned to avoid this provider
  simprovider2              4          1   ↓ router learned to avoid this provider
  simprovider3             28         37   ↑ router learned to prefer this provider
```

---

## 12. Defensive Guard — Empty `lava-provider-address`

The header splitting step produces an empty list when the header value is `""`,
`"  "`, or `",,"`:

```python
attempts = [p.strip() for p in raw.split(",") if p.strip()]
# "" → []    "  " → []    ",," → []
```

Calling `attempts[0]` without a guard would raise `IndexError` and abort the
entire measurement loop mid-run, discarding all counters collected so far.

The guard is placed as the first content branch (after the `"Cached"` check),
before the `retries == 0` branch that accesses `attempts[0]`:

```python
if raw.lower() == "cached":
    ...                         # no provider contacted — see section 17
elif not attempts:
    logger.warning(             # ← guard fires here
        "  [req %03d]  provider header empty (raw=%r) — skipping attribution",
        i, raw,
    )
elif retries == 0:
    winner = attempts[0]        # safe — list guaranteed non-empty at this point
    ...
else:
    ...                         # retries > 0 → unordered set
```

An empty header should not occur with a healthy rpcconsumer. Scenarios where it
can appear:
- A reverse proxy strips or truncates the header.
- A future Lava version changes when the header is emitted (e.g., only emits it
  on success, not on total failure with no provider reached).
- A test is run against a router that is not the expected version.

When the warning fires, the request is skipped for attribution purposes — the
measurement loop continues and collects all subsequent requests normally.

---

## 13. `_warmup_until_stable()` — Adaptive vs Fixed Warmup

The original design used a fixed warmup count (`N_WARMUP = 15`). This was
replaced with two distinct strategies once the difference between *deterministic*
and *probabilistic* failure scenarios became clear.

### Why a fixed count is unreliable for deterministic scenarios

If P1 is **always** `rate_limit` and P2 is **always** `down`, you would expect
15 requests to be enough to push their scores to the floor. But the
rpcconsumer's QoS scores persist from the previous test with the Lava
1-hour half-life. If P1 and P2 had *perfect* scores going into the test (from a
`test_routes_to_only_healthy_provider` run where all providers were healthy),
those high scores decay very slowly. Sending 15 requests with failures drives
the scores down — but whether that is enough depends entirely on how good those
prior scores were. A provider that answered 300 requests successfully (as
`TestFlaky` produces) needs far more than 15 failures to score below a fresh
provider.

### Strategy 1 — `_warmup_until_stable()` for deterministic failures

Used when at least one provider will **always** fail (mode `down`, `rate_limit`,
or `error_probability = 1.0`) and at least one provider will **always** succeed.

```python
def _warmup_until_stable(
    http_client, router_url, *,
    request_payload, extra_headers,
    target_providers: set,
    min_consecutive: int = 3,
    max_attempts:    int = 60,
) -> int:
```

**Stop condition:** the router must select a provider from `target_providers`
with `Lava-Retries = 0` for `min_consecutive` requests in a row. Three
consecutive clean wins at `retries = 0` means the router chose that provider
*first try* every time — its WRS weight is now clearly the highest.

**Why `retries = 0` matters here:** if the router still occasionally picks a
bad provider first (causing a retry), it means the bad provider's score has not
dropped far enough yet. `retries = 0` is the only observable proof that the
target provider dominates WRS selection.

**Streak reset rules:**

| Outcome | Streak |
|---|---|
| `retries = 0` and provider ∈ `target_providers` | `+1` |
| `retries = 0` and provider ∉ `target_providers` | `reset to 0` — a non-target provider won clean, scores not right yet |
| `retries > 0` | `reset to 0` — router still trying bad providers first; every retry still helps score them down |
| response header is `"Cached"` | skip — no routing decision was made |

**Max attempts guard:** if the router fails to converge within `max_attempts`
(default 60), a warning is logged and the test proceeds. This prevents infinite
loops in CI if the environment is degraded. The test may be flaky in that case,
but it will not hang.

### Strategy 2 — Fixed count for probabilistic failures

Used in `TestFlaky` where every provider can both succeed and fail:

```python
N_WARMUP = 100   # enough requests to statistically dominate the score update
```

There is no "definitive winner" to wait for when `error_probability` is between
0.0 and 1.0: P3 (20% error) can fail, and P1 (70% error) can succeed on any
individual request. The `_warmup_until_stable()` stop condition — which looks
for a clean win — would be satisfied by P1 or P2 getting lucky and answering
cleanly, which is not the same as the router having *learned* to prefer P3.

Instead, 100 requests ensure that the router has accumulated enough score
observations to statistically differentiate P1 (70% fail) and P2 (90% fail)
from P3 (20% fail). With the 1-hour half-life decay:

```
weight of a 5-minute-old score relative to a brand-new score:
  2^(-5min / 60min) = 2^-0.083 ≈ 0.944   → barely decayed
```

After 100 requests, P1 will have registered ~70 failures and ~30 successes.
The decaying-average availability score for P1 will be close to 0.30,
while P3's availability will be close to 0.80. The WRS weight difference is
large enough for measurable convergence in the 300-request measurement window.

### Comparison table

| Property | `_warmup_until_stable()` | Fixed N_WARMUP = 100 |
|---|---|---|
| Used when | ≥1 provider always fails, ≥1 always succeeds | All providers are probabilistic |
| Stop condition | min_consecutive clean wins from target set | Fixed request count |
| Failure convergence proof | Observable from headers | Statistical — verified by convergence table |
| Flakiness risk | Low — adaptive | Moderate — count may be too small in edge cases |
| Max run time | Up to `max_attempts` requests | Exactly 100 requests |

### Which tests use which strategy

| Test | Strategy | `target_providers` / count |
|---|---|---|
| `test_routes_to_only_healthy_provider` | `_warmup_until_stable()` | `{"simprovider3"}`, min=3 |
| `test_all_providers_down_returns_error_not_crash` | None (no warmup needed — all fail deterministically) | — |
| `test_router_recovers_when_provider_comes_back` | None (single-shot assertion each phase) | — |
| `test_router_handles_all_providers_rate_limited` | None | — |
| `test_router_avoids_rate_limited_provider` | None (success threshold ≥8/10 tolerates stale scores) | — |
| `test_router_handles_slow_providers` | None | — |
| `test_router_succeeds_despite_flaky_providers` | Fixed N_WARMUP = 100 | — |
| `test_custom_response_content_is_forwarded` | `_warmup_until_stable()` | `{"simprovider3"}`, min=3 |
| `test_error_response_body_is_structured_json` | None | — |
| `test_partial_recovery_one_of_two_down_providers_comes_back` | None | — |
| `test_mixed_down_and_rate_limited_with_one_healthy` | `_warmup_until_stable()` | `{"simprovider3"}`, min=3 |

---

## 14. Simulator Modes — What Each Mode Does and How the rpcconsumer Reacts

**Source: `provider-simulator/server.py` — `JSONRPCHandler.do_POST()`**

The simulator has four discrete modes plus a continuous `error_probability`
modifier. They are applied in this priority order in the request handler:

```
1. mode == "down"                → HTTP 503, no body
2. latency_ms > 0                → sleep before responding
3. mode == "rate_limit"          → HTTP 429 + JSON error body
4. mode == "error"               → HTTP 200 + JSON-RPC error body
   OR random() < error_probability
5. (default)                     → HTTP 200 + JSON-RPC success body
```

### Mode: `down`

```python
if snap["mode"] == "down":
    self.send_response(503)
    self.end_headers()
    return
```

The simulator returns HTTP 503 with no body. The `lavap provider` process
treats a 503 as a protocol-level failure (not a node error): the session is
marked failed, `OnSessionFailure` fires, and the provider's availability
score drops. The rpcconsumer retries with a different provider.

**rpcconsumer reaction:** protocol error → `AppendRelayFailure` → availability ↓
**`Lava-Retries`:** incremented for each down-provider attempt.

### Mode: `rate_limit`

```python
if snap["mode"] == "rate_limit":
    self._reply(429, {
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": 429, "message": "Too many requests"}
    })
```

HTTP 429 with a JSON error body. The rpcconsumer treats this as a protocol
error (non-200 HTTP status), marks the session failed, and retries.

**rpcconsumer reaction:** protocol error → `AppendRelayFailure` → availability ↓
**Difference from `down`:** body is present and parseable; HTTP status is 429
not 503. From the rpcconsumer perspective both are non-200 and trigger the same
`OnSessionFailure` path.

### Mode: `error` (and `error_probability`)

```python
if snap["mode"] == "error" or random.random() < snap["error_probability"]:
    self._reply(200, {
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": -32000, "message": "Internal error"}
    })
```

HTTP 200 with a JSON-RPC error body. The HTTP transport succeeded — the
provider was reachable — but the blockchain call failed. `CheckResponseError()`
in `jsonRPCMessage.go` detects the `"error"` field and classifies this as a
**node error**, not a protocol error.

**rpcconsumer reaction:** node error → `AppendRelayFailure` → availability ↓
**Key difference from `down`/`rate_limit`:** classified as node error, not
protocol error. The Lava source defines `RelayCountOnNodeError = 2` (up to
2 retries on node errors). Protocol errors may have a different retry budget.
**`Lava-Retries`:** incremented for each error-response attempt.

### Mode: `success` (default)

```python
self._reply(200, {"jsonrpc": "2.0", "id": req_id, "result": result})
```

HTTP 200 with a JSON-RPC success body. `result` is looked up from
`state.responses[method]` if configured, or falls back to `"0x1"`.

**rpcconsumer reaction:** `OnSessionDone` → `AppendRelayData` → availability ↑

### `latency_ms` — latency injection (orthogonal to mode)

```python
if snap["latency_ms"] > 0:
    time.sleep(snap["latency_ms"] / 1000.0)
```

Applied before the mode check (except `down`, which bypasses it). A provider
with `latency_ms = 3000` and `mode = "success"` sleeps 3 seconds then returns
HTTP 200. The rpcconsumer records the full round-trip time as the latency
observation for that provider, which drives the latency dimension of the QoS
score down.

### `responses` — per-method custom result values

```python
# ProviderConfig(responses={"eth_blockNumber": {"result": "0xABC123"}})
with state.lock:
    method_cfg = state.responses.get(method) or state.responses.get("default", {})
result = method_cfg.get("result", "0x1")
```

Lets tests assert that the router forwarded *exactly* the provider's response
without substituting a cached or synthesised value. Used in
`test_custom_response_content_is_forwarded` and
`test_routes_to_only_healthy_provider`.

### Mode summary table

| Mode | HTTP status | Body | rpcconsumer classification | Score effect |
|---|---|---|---|---|
| `down` | 503 | empty | Protocol error | availability ↓ |
| `rate_limit` | 429 | JSON error | Protocol error | availability ↓ |
| `error` | 200 | JSON-RPC error | Node error | availability ↓ |
| `error_probability` | 200 | JSON-RPC error | Node error | availability ↓ |
| `success` | 200 | JSON-RPC result | — | availability ↑ |

---

## 15. Test Class Inventory and Design Rationale

### `TestFailover`

| Test | Scenario | What is asserted |
|---|---|---|
| `test_routes_to_only_healthy_provider` | P1=rate_limit, P2=down, P3=success (result=0xABC123) | HTTP 200; result == "0xABC123" |
| `test_all_providers_down_returns_error_not_crash` | P1=down, P2=down, P3=down | Status in {200, 400, 500, 503}; no crash |
| `test_router_recovers_when_provider_comes_back` | Phase 1: P1+P2 down, P3 success → Phase 2: all success | Both phases return `result` or `error` |

**Design principle:** Failover tests use deterministic failure modes (`down`,
`rate_limit`). The router must *always* fail on P1/P2 in these scenarios —
there is no probability. This makes the assertions simple and deterministic:
if the router returns `result`, it must have reached P3.

### `TestRateLimit`

| Test | Scenario | What is asserted |
|---|---|---|
| `test_router_handles_all_providers_rate_limited` | P1+P2+P3=rate_limit | Status in {200, 429, 500, 503}; no crash |
| `test_router_avoids_rate_limited_provider` | P1=rate_limit, P2+P3=success | ≥8/10 requests succeed |

**Design principle:** rate-limit tests focus on resilience (no crash) and
degraded-mode success rate. The ≥8/10 threshold tolerates the router
occasionally trying the rate-limited P1 first (when its old QoS score is
still high) before falling back. No warmup is needed for the 8/10 test because
two healthy providers make success likely regardless of score history.

### `TestLatency`

| Test | Scenario | What is asserted |
|---|---|---|
| `test_router_handles_slow_providers` | P1=3000ms, P2=3000ms, P3=10ms | Response contains `result` or `error`; client timeout = 15s |

**Design principle:** latency tests verify that the router doesn't time out
client connections even when some providers are very slow. The assertion is
minimal — it only checks that the router *returned* without a timeout, not
which provider answered. Latency convergence (P3 wins more over time) is
left to `TestFlaky` because latency-only convergence requires many more
requests than a single test should run.

### `TestFlaky`

| Test | Scenario | What is asserted |
|---|---|---|
| `test_router_succeeds_despite_flaky_providers` | P1=70% error, P2=90% error, P3=20% error | ≥75% of 300 requests succeed; convergence table logged |

**Design principle:** This is the only test that validates **QoS learning over
time**. It does not assert convergence numerically — it logs a convergence
table and asserts only a success rate. The convergence table is manual evidence
for engineers reviewing logs, not an automated assertion. Asserting convergence
numerically would be brittle because WRS is probabilistic: P1 and P2 never
reach zero weight, so their win counts in the second half will never drop to
zero.

### `TestEdgeCases`

| Test | Scenario | What is asserted |
|---|---|---|
| `test_custom_response_content_is_forwarded` | P1+P2=down, P3=success (result=0xDEADBEEF) | `result == "0xDEADBEEF"` exactly |
| `test_error_response_body_is_structured_json` | P1+P2+P3=down | Body is valid JSON; `"error"` key present and non-empty |
| `test_partial_recovery_one_of_two_down_providers_comes_back` | Phase 1: P1+P2=down, P3=success → Phase 2: P1=success, P2=down, P3=success | Phase 2: ≥4/5 succeed |
| `test_mixed_down_and_rate_limited_with_one_healthy` | P1=down, P2=rate_limit, P3=success | ≥4/5 succeed |

**Design principle:** Edge-case tests cover the corners — exact response
content fidelity, error body structure (no crash pages), mixed failure types,
and partial recovery. These guard against regressions where the router:
- Substitutes a cached or synthesised result instead of forwarding the provider's exact value
- Returns an HTML error page or empty body instead of a JSON error
- Gets stuck on a half-recovered pairing list

---

## 16. Fixture Architecture — `tests/simulator/conftest.py`

### Fixture scoping decisions

```python
@pytest.fixture(scope="session")
def sim_control() -> SimulatorControl:
    ...

@pytest.fixture(scope="session")
def sim_router_url() -> str:
    ...

@pytest.fixture(autouse=True)
def reset_simulator(sim_control, request):
    ...
    sim_control.reset()
    yield
    # no teardown

@pytest.fixture
def http_client():
    ...
    session.headers.update({ROUTING_HEADER_SKIP_CACHE: "true"})
    yield session
    session.close()
```

**`sim_control` — session-scoped:**
The `SimulatorControl` instance is cheap to create but the health check at
creation time (one HTTP GET to `/health`) takes ~100 ms. More importantly,
session scope ensures the connectivity check runs exactly once per test run —
if the simulator is unreachable, `pytest.skip()` fires once and the entire
session is skipped cleanly. A function-scoped fixture would fire the skip for
every individual test, creating noise in the output.

**`sim_router_url` — session-scoped:**
The URL is a constant derived from an environment variable. It never changes
during a run. Session scope avoids rebuilding the same string for every test.

**`reset_simulator` — autouse=True, function-scoped, no teardown:**
`autouse=True` means it runs before every test in `tests/simulator/` without
needing to be listed in each test's parameters. The fixture calls `reset()` in
the setup phase (before `yield`) and deliberately omits a teardown call. The
reason: if teardown also called `reset()`, there would be two back-to-back
`POST /reset` requests between every pair of tests — one at the end of test N
and one at the start of test N+1. The second reset is redundant. Omitting the
teardown halves the number of control API calls without any correctness impact.

**`http_client` — function-scoped:**
A new `requests.Session` is created for every test. Session scope would be
correct from a performance standpoint (sessions are reusable), but function
scope avoids any state leak: connection pools, SSL sessions, and any
session-level state from one test cannot bleed into the next. The session
sets `lava-force-cache-refresh: true` by default — see section 17.

### Why `sim_control` is required even though tests call `sim_control` directly

Fixtures in pytest are resolved by name from the fixture pool. `sim_control`
is a session fixture that creates one `SimulatorControl` and shares it across
all tests. Tests that take `sim_control` as an argument receive the *same*
instance. This means the health-check skip fires exactly once, and there is
no risk of two tests racing to call `/reset` simultaneously (though that is
unlikely in practice with a sequential test run).

---

## 17. Cache Bypass — Why `lava-force-cache-refresh` Is Required on Every Request

### What the router cache does

The `rpcconsumer` / Cloudflare layer caches responses for methods that are
classified as **safe** (read-only and deterministic for a given block). Methods
like `eth_blockNumber`, `eth_getBlockByNumber`, and `eth_call` on a specific
past block are cacheable.

When a cached response is served, the router does **not** contact any provider.
The `lava-provider-address` response header is set to `"Cached"` (or omitted,
depending on the router version). No WRS selection happens, no QoS scores are
updated, and no failover or retry logic is exercised.

### The test problem

The simulator tests exist to verify **routing behaviour** — which provider was
selected, how the router handles failures, whether retries work. If requests
hit the cache, none of that is tested. The test assertions would either:
- Pass vacuously (cached `result` matches the expected value because it was
  correct the last time a real request was made)
- Fail with confusing errors (`lava-provider-address: Cached`, provider not
  found in expected set)

### The fix — two layers of protection

**Layer 1 — `http_client` fixture always sets the header:**

```python
session.headers.update({
    "Content-Type":             "application/json",
    ROUTING_HEADER_SKIP_CACHE: "true",   # force real routing decision on every request
})
```

Every request made through the `http_client` fixture automatically carries
`lava-force-cache-refresh: true`. Tests that forget to pass `SKIP_CACHE`
explicitly are still protected.

**Layer 2 — `SKIP_CACHE` passed explicitly at the call site:**

```python
SKIP_CACHE = {ROUTING_HEADER_SKIP_CACHE: "true"}

resp = http_client.post(sim_router_url, json=ETH_BLOCK_NUMBER, headers=SKIP_CACHE, timeout=10)
```

The `SKIP_CACHE` dict is merged with the session's default headers by
`requests`. The explicit pass is intentional: it makes the intent visible
at the call site so a future reader does not wonder "why isn't this cached?"
The comment in the constant definition explains the rationale.

### What happens if `lava-force-cache-refresh` is omitted

```
request → router → Cloudflare cache hit
lava-provider-address: Cached     ← or header absent
Lava-Retries: (absent)

test code:
  raw = resp.headers.get(ROUTING_HEADER_PROVIDER_ADDRESS, "unknown")
  → raw = "Cached"
  raw.lower() == "cached" → True
  winner_counter["Cached"] += 1    ← counted separately, no provider attributed
```

In `TestFlaky`, this would mean the 300-request measurement window sees mostly
`"Cached"` responses. The `winner_counter` for real providers would be near
zero, `success_count` would be high (cached = correct result), and the test
would pass — while actually testing nothing. The cache-detection branch in the
measurement loop logs a warning for exactly this reason:

```python
if raw.lower() == "cached":
    logger.warning(
        "  [req %03d]  ⚠️  chosen=Cached — router served from cache despite "
        "skip-cache header; this request did NOT exercise provider selection",
        i,
    )
```

This warning appearing in CI logs is a signal that the cache bypass is not
working as expected (header stripped by a proxy, router version change, etc.).

---

## 18. SimulatorControl API Reference

`SimulatorControl` is the Python client that wraps the provider simulator's
HTTP control API. Tests import it from `tests/simulator/sim_control.py`.

### Control API endpoints

| Method | Endpoint | What it does |
|---|---|---|
| `POST` | `/scenario` | Set per-provider behaviour for the current test |
| `POST` | `/reset` | Reset scenario config only (mode, latency, responses → defaults). Does **not** clear history. |
| `POST` | `/history/clear` | Wipe call history and counters only. Does **not** touch scenario config. |
| `POST` | `/reset/all` | Reset scenario config AND clear history — full clean slate. |
| `GET` | `/scenario` | Return the current state of all providers as JSON |
| `GET` | `/health` | Returns `{"status": "ok"}` when the simulator is reachable |
| `GET` | `/stats` | Per-provider call counts and status breakdown (all-time counters, never reset) |
| `GET` | `/history` | Timestamped ring-buffer of the last 200 calls per provider, with optional filters and `call_order`. See below. |

Default control URL: `https://sim-control.victoria.magmadevs.com`  
Override: set the `SIM_CONTROL_URL` environment variable.

---

### `/history` — response format and `call_order`

`GET /history` merges the call logs from all three provider servers, sorts
them by wall-clock time (`ts`), and stamps each entry with a **`call_order`**
integer (1-based). `call_order: 1` is the provider the router hit **first**,
`call_order: 2` is the second attempt, and so on.

**Supported query parameters** (all optional, combinable):

| Parameter | Example | What it does |
|---|---|---|
| `last=<seconds>` | `?last=30` | Only calls in the last N seconds |
| `from=<unix_ts>` | `?from=1774534600` | Only calls at or after this timestamp |
| `to=<unix_ts>` | `?to=1774534700` | Only calls at or before this timestamp |
| `provider=<id>` | `?provider=2` | Only calls to provider 1, 2, or 3 |
| `method=<name>` | `?method=eth_blockNumber` | Only calls for this JSON-RPC method |
| `status=<name>` | `?status=error` | Only calls with this status (`success`, `error`, `rate_limit`, `down`) |

**Response fields per entry:**

| Field | Type | Description |
|---|---|---|
| `call_order` | int | 1-based position in the time-sorted merged list. `1` = first provider the router tried. |
| `provider` | string | Which simulator provider answered: `"1"`, `"2"`, or `"3"` |
| `method` | string | JSON-RPC method name, e.g. `"eth_blockNumber"` |
| `status` | string | Outcome: `success`, `error`, `rate_limit`, or `down` |
| `ts` | float | Wall-clock Unix timestamp of when the call arrived (full float precision) |
| `time` | string | Human-readable UTC time with milliseconds: `"YYYY-MM-DD HH:MM:SS.mmm UTC"` |
| `latency_ms` | int | Simulated latency injected before the response (from `latency_ms` config) |

**Example — P1 rate-limited, P2 down, P3 succeeds (one failover chain):**

```json
{
  "count": 3,
  "history": [
    { "call_order": 1, "provider": "1", "method": "eth_blockNumber", "status": "rate_limit", "ts": 1743300001.164, "time": "2026-03-30 10:12:40.164 UTC", "latency_ms": 2 },
    { "call_order": 2, "provider": "2", "method": "eth_blockNumber", "status": "down",       "ts": 1743300001.331, "time": "2026-03-30 10:12:40.331 UTC", "latency_ms": 0 },
    { "call_order": 3, "provider": "3", "method": "eth_blockNumber", "status": "success",    "ts": 1743300001.512, "time": "2026-03-30 10:12:40.512 UTC", "latency_ms": 8 }
  ]
}
```

Reading it: the router tried Provider 1 first (rate-limited → retry), then
Provider 2 (down → retry), then Provider 3 which succeeded. `call_order`
makes the failover sequence immediately readable without comparing raw `ts`
floats.

> **`call_order` is relative to the filtered result.**  
> If you use `?provider=3`, the single returned entry will have `call_order: 1`
> even though Provider 3 was the third attempt globally. Use unfiltered
> `/history?last=<N>` to see the true attempt sequence.

---

### `ProviderConfig` dataclass

```python
@dataclass
class ProviderConfig:
    mode:              str                = "success"  # success | error | rate_limit | down
    latency_ms:        int                = 0          # added delay before responding
    error_probability: float              = 0.0        # fraction of requests that return JSON-RPC error
    responses:         Dict[str, dict]    = field(default_factory=dict)
    # responses: {"eth_blockNumber": {"result": "0x1"}, "default": {"result": "0x0"}}
```

All fields are optional. The minimum useful config is setting `mode`.

### `SimulatorControl.set_scenario(providers)`

```python
sim_control.set_scenario({
    1: "rate_limit",                        # shorthand string
    2: "down",                              # shorthand string
    3: ProviderConfig(                      # full config
        mode="success",
        latency_ms=200,
        responses={"eth_blockNumber": {"result": "0xABC123"}},
    ),
})
```

Keys are provider numbers (1, 2, 3). Values are either a shorthand mode string
(`"success"`, `"error"`, `"rate_limit"`, `"down"`) or a full `ProviderConfig`.

**What this call does and does NOT do:**

| Affected | Not affected |
|---|---|
| What the simulator backends return | rpcconsumer in-memory QoS scores |
| Simulator `mode`, `latency_ms`, `error_probability`, `responses` | Provider pairing list |
| Takes effect immediately (synchronous HTTP POST) | WRS weights |

### `SimulatorControl.reset()`

```python
sim_control.reset()
```

Calls `POST /reset` — resets scenario config only (mode, latency, responses → defaults).
Does **not** clear history. Called automatically before every test by the `reset_simulator`
autouse fixture in `conftest.py`.

### `SimulatorControl.clear_history()`

```python
sim_control.clear_history()
```

Calls `POST /history/clear` — wipes the call buffer and counters only.
Does **not** touch scenario config. Use this before sending a specific request
you want to isolate in history.

### `SimulatorControl.get_scenario()`

```python
state = sim_control.get_scenario()
# Returns e.g.:
# {
#   "1": {"mode": "rate_limit", "latency_ms": 0, "error_probability": 0.0},
#   "2": {"mode": "down",       "latency_ms": 0, "error_probability": 0.0},
#   "3": {"mode": "success",    "latency_ms": 0, "error_probability": 0.0},
# }
```

Useful in debugging — call it after `set_scenario` to confirm the simulator
received and applied the payload correctly.

### `SimulatorControl.health()`

```python
ok = sim_control.health()   # True if reachable, False otherwise
```

Called once at session start by the `sim_control` fixture. If it returns
`False`, `pytest.skip()` fires and all simulator tests are skipped with a
single message rather than failing with a connection error on every test.

### Environment variables

| Variable | Default | What it controls |
|---|---|---|
| `SIM_CONTROL_URL` | `https://sim-control.victoria.magmadevs.com` | Control API base URL |
| `SIM_ROUTER_URL` | `https://eth-sim-jsonrpc.victoria.magmadevs.com` | Router endpoint for JSON-RPC requests |

Both are read in `tests/simulator/conftest.py` at session scope. Override them
to point at a local or staging simulator deployment without modifying any code.

---

## 19. `Lava-Provider-Unique-Id` — What It Is and What It Does

> Sources: `protocol/rpcprovider/rpcprovider.go`, `protocol/rpcprovider/rpcprovider_server.go`,
> `protocol/lavasession/single_consumer_session.go`,
> `protocol/rpcconsumer/rpcconsumer_server.go`,
> `protocol/rpcsmartrouter/rpcsmartrouter_server.go`,
> `protocol/chainlib/common.go`, `utils/uniqueIdentifier.go`

### What it is

`Lava-Provider-Unique-Id` is a **gRPC trailer** — a key/value pair appended
to the gRPC response *after* the body. It is **not** an HTTP header and is
never visible to your HTTP client (`curl -i` will not show it).

The constant lives in:

```go
// protocol/chainlib/common.go
RpcProviderUniqueIdHeader = "Lava-Provider-Unique-Id"
```

### Where the value comes from

When a `lavap provider` process starts up, it generates a random 64-bit
integer once and stores it for its entire lifetime:

```go
// protocol/rpcprovider/rpcprovider.go
rpcp.providerUniqueId = strconv.FormatUint(utils.GenerateUniqueIdentifier(), 10)

// utils/uniqueIdentifier.go
func GenerateUniqueIdentifier() uint64 {
    return rand.Uint64()
}
```

The value is:
- **Random** — no deterministic component, no address or hostname in it.
- **Process-scoped** — the same for every relay this process sends during its
  lifetime.
- **Regenerated on restart** — a pod restart or binary re-launch produces a
  completely different ID.

On every relay response the provider appends the ID to the gRPC trailer:

```go
// protocol/rpcprovider/rpcprovider_server.go
trailerMd := metadata.Pairs(chainlib.RpcProviderUniqueIdHeader, rpcps.providerUniqueId)
trailer.Append(chainlib.RpcProviderUniqueIdHeader, rpcps.providerUniqueId)
```

### What the consumer does with it

The rpcconsumer (and rpcsmartrouter) reads the trailer after each relay:

```go
// protocol/rpcconsumer/rpcconsumer_server.go
providerUniqueId := relayResult.ProviderTrailer.Get(chainlib.RpcProviderUniqueIdHeader)
```

It then calls `VerifyProviderUniqueIdAndStoreIfFirstTime()` on the
`SingleConsumerSession`:

```go
// protocol/lavasession/single_consumer_session.go
func (scs *SingleConsumerSession) VerifyProviderUniqueIdAndStoreIfFirstTime(
    providerUniqueId string,
) bool {
    if scs.providerUniqueId == "" {
        // First relay on this session — store the ID, return true (ok)
        scs.providerUniqueId = providerUniqueId
        return true
    }
    // Subsequent relays — ID must match what was stored on first contact
    return providerUniqueId == scs.providerUniqueId
}
```

**Logic in plain English:**

| Situation | What happens |
|---|---|
| First relay on this session | ID is stored. No verification yet. |
| Subsequent relays, ID matches | Session is healthy. Normal flow continues. |
| Subsequent relays, ID **does not** match | Consumer logs a warning. The session is treated as if it hit an unexpected provider — likely the pod was restarted mid-session. |
| Trailer is absent (`""`) | Silently ignored (old provider version that doesn't set the trailer). |

### What it is NOT

| Common misconception | Reality |
|---|---|
| "It tells me which provider was chosen" | No — `lava-provider-address` (HTTP response header) does that. |
| "It is the provider's Lava wallet address" | No — it is a random runtime integer, not related to the on-chain address. |
| "I can read it with curl" | No — it is a gRPC trailer, invisible to HTTP clients. |

### Why it matters for simulator tests

The simulator providers (`simprovider1/2/3`) run as real `lavap provider`
processes, so each one generates and returns its own `Lava-Provider-Unique-Id`.
The rpcconsumer verifies it normally. You don't need to configure or stub
this — it works transparently. The only time it becomes visible is if you
restart a provider pod mid-test: the first relay after the restart will store
a new ID, and any pre-existing session will get a mismatch warning in the
rpcconsumer logs.

---

## 20. `/history` `call_order` — How It Is Calculated

> Source: `server.py` — `ControlHandler.do_GET()`, `/history` branch.

### The short answer

`call_order` is a **1-based sequence number** stamped on each history entry
at **query time**, not at call time. It reflects position in the
**time-sorted merged list** of all provider calls within the requested window.

### Step-by-step

```
1. Collect entries from each provider's ring-buffer (provider 1, 2, 3).
   Each entry has: ts (float wall-clock), method, status, latency_ms.

2. Tag each entry with its provider number:
       {"provider": "1", "ts": 1743300001.1, "method": "eth_blockNumber", ...}
       {"provider": "2", "ts": 1743300001.3, ...}
       {"provider": "3", "ts": 1743300001.5, ...}

3. Apply any filters (?last=, ?provider=, ?method=, ?status=).

4. Sort the merged list by ts ascending (earliest first).

5. Enumerate from 1:
       entry[0]["call_order"] = 1   ← provider the router hit FIRST
       entry[1]["call_order"] = 2   ← second attempt
       entry[2]["call_order"] = 3   ← third attempt (or the winner)
```

### Why `ts` ordering equals attempt ordering

Each simulator provider runs as a **separate HTTP server** on its own port.
When the Lava rpcconsumer tries providers sequentially (the standard retry
flow), it contacts Provider 1 first, waits for the response, then — on
failure — moves to Provider 2, etc. Because there is real elapsed time
between each attempt, Provider 1's `ts` is always less than Provider 2's
`ts`, which is always less than Provider 3's `ts`. Sorting by `ts` therefore
naturally produces the attempt order.

> **Edge case:** if the rpcconsumer ever sends concurrent relays to multiple
> providers (as in cross-validation mode), two entries could have nearly
> identical `ts` values and their relative `call_order` would be arbitrary.
> For standard (non-cross-validation) tests, sequential ordering is reliable.

### What `call_order` does and does NOT tell you

| It DOES tell you | It does NOT tell you |
|---|---|
| Which provider was tried first / second / third | Which provider the router *intended* to try first (WRS picks randomly) |
| The sequence within the **filtered** result set | Global position if you used `?provider=` or `?status=` filters |
| Relative attempt order within the query window | Which provider ultimately responded to the client (check `status=success`) |

### Example

```bash
curl -s "https://sim-control.victoria.magmadevs.com/history?last=5" | python3 -m json.tool
```

```json
{
    "count": 3,
    "history": [
        {
            "call_order": 1,
            "provider": "1",
            "method": "eth_blockNumber",
            "status": "rate_limit",
            "ts": 1743300001.164,
            "time": "2026-03-30 10:12:40.164 UTC",
            "latency_ms": 2
        },
        {
            "call_order": 2,
            "provider": "2",
            "method": "eth_blockNumber",
            "status": "down",
            "ts": 1743300001.331,
            "time": "2026-03-30 10:12:40.331 UTC",
            "latency_ms": 0
        },
        {
            "call_order": 3,
            "provider": "3",
            "method": "eth_blockNumber",
            "status": "success",
            "ts": 1743300001.512,
            "time": "2026-03-30 10:12:40.512 UTC",
            "latency_ms": 8
        }
    ]
}
```

Reading this: the router tried Provider 1 first (rate-limited), then Provider
2 (down), then Provider 3 which succeeded. `call_order` makes the failover
sequence immediately obvious without needing to compare raw `ts` floats.
