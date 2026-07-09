# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-transport JSON-RPC / BTC RPC / REST / gRPC / WebSocket simulator used to test smart-router behavior. Top-level Python — no subpackages — with two C-extension deps (`grpcio`, `protobuf`) required only for the gRPC transport. Runs many `HTTPServer` instances plus three asyncio gRPC servers in daemon threads from a single process, all sharing one in-memory state map:

- ETH JSON-RPC providers on `18545`/`18546`/`18547` (dispatch always handlers_eth).
- BTC JSON-RPC providers on `18575`/`18576`/`18577` (dispatch always handlers_btc — MAG-2089 gave BTC its own dedicated listener pool, the handler is port-derived).
- LN JSON-RPC providers on `18578`/`18579`/`18580` (dispatch always handlers_lnd — MAG-2089 ditto for LN).
- gRPC providers on `18548`/`18549`/`18550` (chain_family="grpc"; cosmos-tendermint servicer in handlers_grpc).
- REST providers on `18551`/`18552`/`18553` (chain_family="rest"; handlers_rest).
- WebSocket providers on `18557`/`18558`/`18559` (chain_family="ws"; handlers_ws — supports subscribe/unsubscribe lifecycle plus request/response over a single frame; delegates non-subscription methods back to handlers_eth/handlers_btc).
- Control API on `19000` (scenario config, reset, history, /ws/emit, /ws/subscriptions).

## Common commands

```bash
# Run locally (no deps to install)
python -u run.py

# Run the test suite — boots all four servers in-process on test ports 28545-28547 / 29000
pytest tests/test_simulator.py -v

# Run a single test class or test
pytest tests/test_simulator.py::TestHistory -v
pytest tests/test_simulator.py::TestHistory::test_filter_method_returns_matching_only -v

# Deploy to the MicroK8s server (builds image, imports, applies manifests, rollout-restarts)
bash scripts/deploy.sh
```

There is no linter or formatter configured. The Dockerfile copies only `*.py` from the repo root, so any new runtime module must live at the top level (not inside a subpackage).

## Architecture

The whole simulator is intentionally one process sharing one in-memory state map.

- **`server.py`** — `main()` constructs `states = {pid: ProviderState() for pid in PROVIDER_PORTS}` once, then attaches the same dict to every server: each `JSONRPCHandler` server gets a single `state` plus a `provider_id`, while the `ControlHandler` server gets the full `provider_states` dict. Control API mutations are therefore immediately visible to JSON-RPC handlers without any IPC.
- **`ProviderState`** — owns a `threading.Lock`. All field reads/writes go through `snapshot()` / `update()` / `reset_scenario()` / `clear_history()` / `push_call_to_buffer()` so handlers always operate on a stable snapshot even if a request mutates state mid-flight. The lock is also why `field(default_factory=...)` is used for `lock` and `history` (they must not be shared across instances).
- **`JSONRPCHandler.do_POST`** — fixed decision order: `down` (503, body never parsed) → `latency_ms` sleep → `rate_limit` (429) → forced/probabilistic `error` → custom per-method `responses` → `METHOD_DEFAULTS` from `stubs.py`. Every branch ends with `push_call_to_buffer()`, so history is the source of truth for what happened.
- **`ControlHandler`** — four POST routes (`/scenario`, `/reset`, `/history/clear`, `/reset/all`) and four GET routes (`/health`, `/scenario`, `/stats`, `/history`). The reset/clear split is load-bearing: `/reset` zeros scenario config but keeps history; `/history/clear` does the inverse; `/reset/all` does both. Tests rely on this distinction.
- **`WsHandler` (handlers_ws.py)** — RFC 6455 transport on ports 18557/18558/18559. Each connection runs a reader thread (parses frames, dispatches subscribe/unsubscribe/non-subscription JSON-RPC) and a writer thread (drains an outbound queue → `sendall`). A module-level `_WS_SUBSCRIPTIONS` registry maps `sub_id → SubscriptionHandle`; `POST /ws/emit` on the control server pushes wrapped event frames into the right connection's queue.
- **`/history`** — merges all three providers' ring buffers, sorts by `ts`, then assigns two derived fields per entry: `correlation_group` (groups calls sharing `(request_id, method)` within a 50ms window — i.e. one router relay) and `call_order` (1-based position in the merged timeline). Filters: `from`/`to`/`last`/`provider`/`method`/`status`/`request_id`/`lava_header_*` (the last is a dynamic prefix — `?lava_header_lava_stateful_api=true` matches the `lava-stateful-api` header).
- **`HISTORY_MAX`** in `constants.py` is the per-provider ring-buffer cap — `2000` by default, overridable via the `SIM_HISTORY_MAX` env var. All-time counters in `ProviderState.stats()` are separate and never reset by the ring rollover; only `clear_history()` resets them.
- **`stubs.py`** — `METHOD_DEFAULTS` provides default JSON-RPC results. `eth_getBlockByNumber` is special: `JSONRPCHandler.do_POST` rewrites `result["number"]` from `params[0]` (with `latest`/`earliest`/`pending`/`safe`/`finalized` mapped to fixed hex blocks) so the router's pruning verification sees a matching block number.

## Deployment

`scripts/deploy.sh` reads `BASE_DOMAIN` from `config/base-domain.env`, renders `k8s/httproute-control.yml` (substituting `__CONTROL_HOSTNAME__`), then `docker build` → `microk8s ctr image import` → `kubectl apply` → `kubectl rollout restart`. The restart is required because the image tag is always `latest` with `imagePullPolicy: IfNotPresent` — without it, the pod keeps the old image. The Kubernetes deployment is intentionally one replica (history and counters are in-memory; multiple pods would split state).

`config/values_sim.yml` is a separate artifact: it is the smart-router Helm values file that wires the simulator's three ports into the router as `eth-sim` nodes. It is not consumed by anything in this repo — it is meant to be copied to the smart-router repo.

## Tests

`tests/test_simulator.py` boots the same `JSONRPCHandler` and `ControlHandler` classes against an isolated `_PROVIDER_PORTS = {"1": 28545, "2": 28546, "3": 28547}` / `_CONTROL_PORT = 29000`. A module-scoped `sim` fixture starts the servers; an autouse `clean_state` fixture calls `/reset/all` before and after every test. There is no background traffic in tests, so `/history` returns exactly the calls a test made — unlike the deployed environment, where router scoring/pruning continuously fills the buffer with `eth_blockNumber` / `eth_getBlockByNumber` and method-filtering becomes the primary isolation tool.