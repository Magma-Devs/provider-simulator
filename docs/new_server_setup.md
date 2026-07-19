# New Server Setup — Provider Simulator

> **Run everything in this guide as root.**
> ```bash
> sudo -s   # or log in directly as root
> ```
> All paths (`~/…`) resolve to `/root/` when running as root.

## What this guide covers

Use this document for a **fresh server** where provider simulator must work end-to-end with smart_router_automation.

It has two parts:

1. **Main setup flow** — required to make simulator traffic work.
2. **Appendix** — optional debug-server setup and troubleshooting.

### Fresh-server happy path

- Step 0 — find this server's base domain
- Prerequisites — tools the deploy server needs (`curl`, `grpcurl`)
- Step 2 — clone `provider-simulator`
- Step 3 — deploy simulator pod
- Step 4 — verify simulator pod is healthy
- Step 5 — verify all simulator surfaces are up
- Step 6 — copy `values_sim.yml` into `smart-router-standalone`
- Step 7 — install/upgrade smart router (auto-layers simulator values)
- Step 8 — smoke test public simulator route

If this server also needs **clock injection / score reset via debug domain**, continue to **Appendix A** after step 8.

---

## 0. Find this server's base domain

The base domain is set in `smart-router-standalone` — check it before doing anything else:

```bash
grep base_domain ~/smart-router-standalone/values/core/values.yml
# → base_domain: "victoria.magmadevs.com"
```

You'll need this value in steps 3 and 7. Keep it in mind or note it down.

---

## Prerequisites — tools the deploy server needs

These tools are used by `scripts/deploy.sh` and the smoke / verification steps that follow. Install them once per server before running Step 3.

### `curl` (required)

Used by Step 7 and Step 8 to hit `sim-control` and the simulator's HTTP-RPC surface.

```bash
which curl >/dev/null 2>&1 || sudo apt-get update && sudo apt-get install -y curl
```

### `grpcurl` (required for gRPC simulator surface, MAG-1780+)

Used to test the gRPC simulator endpoints added by MAG-1780. Two install paths — pick whichever fits the server.

**Via Go** (server already has Go installed for smart-router builds):

```bash
go install github.com/fullstorydev/grpcurl/cmd/grpcurl@latest
export PATH="$PATH:$(go env GOPATH)/bin"
```

**Via release binary** (no Go required):

```bash
curl -sL https://github.com/fullstorydev/grpcurl/releases/download/v1.9.1/grpcurl_1.9.1_linux_x86_64.tar.gz | sudo tar xz -C /usr/local/bin grpcurl
```

Verify:

```bash
grpcurl --version
```

### `docker` + `microk8s` (assumed already present on the magma deploy server)

If this is a fresh server and these aren't there yet, run the global server bootstrap first (out of scope for this guide).

---

## 2. Repository access (public repo)

`Magma-Devs/provider_simulator` is now public, so **deploy keys are not required for read-only clone/pull**.

Use HTTPS by default:

```bash
git clone https://github.com/Magma-Devs/provider_simulator.git ~/provider-simulator
cd ~/provider-simulator
```

If you need to push from this server, you can still use SSH keys (personal key or deploy key) and clone with:

```bash
git clone git@github.com:Magma-Devs/provider_simulator.git ~/provider-simulator
cd ~/provider-simulator
```

---

## 3. Set domain, deploy

**Set the base domain before deploying** — `scripts/deploy.sh` reads `config/base-domain.env`, which is **per-server and untracked**. Create it from the template and set the domain you found in step 0:

```bash
cp config/base-domain.env.example config/base-domain.env
vi config/base-domain.env
# Set: BASE_DOMAIN="<YOUR_DOMAIN>"   ← use the domain from step 0
```

Then deploy:

```bash
bash scripts/deploy.sh
```

`deploy.sh` builds the image, imports it into MicroK8s, applies k8s manifests, and runs
`kubectl rollout restart` automatically — no manual restart needed.

---

## 4. Verify pod is running

```bash
kubectl get pods -n lava-infra -l app=provider-simulator
```

Expected: `1/1 Running`

> During rollout you may briefly see two pods — one `Terminating` (old) and one `Running` (new). This is normal. Wait a few seconds and recheck.

> ⚠️ Do NOT test the `sim-control` URL yet — the TLS cert won't cover it until step 8.

---

## 5. Verify all simulator surfaces are up

The simulator runs **7 listeners** in a single pod. Pod `Running` only tells you the process started — not that every listener bound its port. Confirm each surface:

### 7 Service ports are exposed

```bash
kubectl describe svc provider-simulator -n lava-infra | grep -E 'Port:|TargetPort'
```

Expected output — 7 entries:

- `provider-1` → 18545
- `provider-2` → 18546
- `provider-3` → 18547
- `control` → 19000
- `grpc-sim-1` → 18548
- `grpc-sim-2` → 18549
- `grpc-sim-3` → 18550

Missing entries → manifest wasn't applied; re-run the deploy step.

### JSON-RPC providers respond (ports 18545 / 18546 / 18547)

From the pod:

```bash
for port in 18545 18546 18547; do kubectl exec -n lava-infra deployment/provider-simulator -- sh -c "wget -qO- --post-data='{\"jsonrpc\":\"2.0\",\"method\":\"eth_blockNumber\",\"params\":[],\"id\":1}' --header=Content-Type:application/json http://localhost:$port" | grep -q '"result":' && echo "port $port: OK" || echo "port $port: FAIL"; done
```

Expected: three `OK` lines.

### Control API responds (port 19000)

```bash
kubectl exec -n lava-infra deployment/provider-simulator -- wget -qO- http://localhost:19000/health
```

Expected: `{"status": "ok"}`.

### gRPC providers respond (ports 18548 / 18549 / 18550)

Requires `grpcurl` from the Prerequisites section. From the deploy server:

```bash
for port in 18548 18549 18550; do kubectl port-forward -n lava-infra deployment/provider-simulator $port:$port >/dev/null 2>&1 & PF=$!; sleep 1; grpcurl -plaintext localhost:$port list 2>/dev/null | grep -q "cosmos.base.tendermint" && echo "port $port: OK" || echo "port $port: FAIL"; kill $PF 2>/dev/null; wait $PF 2>/dev/null; done
```

Expected: three `OK` lines.

Then sanity-test one real gRPC call:

```bash
kubectl port-forward -n lava-infra deployment/provider-simulator 18548:18548 >/dev/null 2>&1 & PF=$!; sleep 1; grpcurl -plaintext -d '{}' localhost:18548 cosmos.base.tendermint.v1beta1.Service/GetLatestBlock; kill $PF 2>/dev/null
```

Expected: a JSON `block` object with `header.height` matching the simulator's current block.

### If any surface fails

Check pod logs for startup errors:

```bash
kubectl logs -n lava-infra deployment/provider-simulator | tail -100
```

Look for tracebacks at boot, port-bind errors, or missing-module errors. If logs are clean but a port isn't responding, the corresponding listener didn't start — re-run the deploy step.

---

## 6. Create `values/simulator/values_sim.yml`

This file lives in `smart-router-standalone` — **not** in this repo.

```bash
mkdir -p ~/smart-router-standalone/values/simulator
```

`provider-simulator/config/values_sim.yml` already contains the full router list used on the working server (`base`, `eth`, `eth-sim`).
That file is the source of truth for simulator wiring in this repo.
Copy it as-is:

```bash
cp ~/provider-simulator/config/values_sim.yml ~/smart-router-standalone/values/simulator/values_sim.yml

# quick sanity check: should print base, eth, eth-sim
grep -n '^  - id:' ~/smart-router-standalone/values/simulator/values_sim.yml
```

Final file structure must be:
```yaml
routers:
  - id: "base"
    ...
  - id: "eth"
    ...
  - id: "eth-sim"
    network: "eth1"
    nodes:
      - name: "SimProvider1"
        endpoints:
          - url: "http://provider-simulator.lava-infra.svc.cluster.local:18545"
            interface: "jsonrpc"
            addons: ["archive", "trace", "debug"]
      ...
```
> Source of truth: `provider-simulator/config/values_sim.yml`

> ⚠️ Use `routers:` (not `chains:`) and `nodes:` (not `providers:`) — current chart 5.x schema.
> Using old keys produces no error — Helm silently ignores them and nothing gets created.

---

## 7. Install / upgrade smart router (use `INTERNAL=1` to layer simulator values)

For a fresh server that needs simulator traffic, run the install via `make` with `INTERNAL=1`:

```bash
cd ~/smart-router-standalone
make install_smart_router INTERNAL=1
bash scripts/install_gateway_api_tls_certificate.sh
```

The Makefile target dispatches to `scripts/install_smart_router.sh --internal` (see `Makefile` lines 66-68 + 139-140). `INTERNAL=1` is the dev/test path — without it, the script uses customer-facing values and **does not** layer `values_sim.yml`, so the `eth-sim` router never gets created.

What `INTERNAL=1` does:
- Picks **`values/core/values_internal.yml`** as the base values file (not the customer-facing `values/core/values.yml`).
- Layers `values/simulator/values_sim.yml` on top *if* it exists (placed in step 6).
- Reads the chart version from `scripts/utils/common.sh` (`HELM_CHART_VERSION`).
- Authenticates to GHCR using `HELM_REGISTRY_TOKEN` from `scripts/utils/common.sh`.

Then `install_gateway_api_tls_certificate.sh` refreshes the TLS cert so `sim-control.<YOUR_DOMAIN>` is covered.

Verify (use the domain from step 0):

```bash
curl -s https://sim-control.<YOUR_DOMAIN>/health
# Expected: {"status": "ok"}
```

> **Why opt-in instead of auto-detect?** Helm replaces list values wholesale — when `values_sim.yml` is layered, its `routers:` list completely replaces the one in the base values file. That's safe in internal mode (`values_internal.yml` is a dev-only fixture and `values_sim.yml` is a superset). On a customer install you almost certainly don't want simulator routers in the cluster, so the merge is opt-in. See `install_smart_router.sh` around lines 265-272.

> **Truly-fresh server?** If smart-router and the observability stack are not yet installed at all, run `make wizard` first to lay down the full stack (observability, Loki, gateway, TLS, smart-router from customer values). Then re-run `make install_smart_router INTERNAL=1` to switch to internal-mode + values_sim.yml.

> **Want to confirm chart version manually?** `grep -i helm_chart_version scripts/utils/common.sh` — expected `5.0.2` (or whichever 5.x is the current published release). The `routers:` / `nodes:` schema is the long-standing format used by 5.x.

---

## 8. Smoke test

```bash
curl -s -X POST https://eth-sim-jsonrpc.<YOUR_DOMAIN> \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'
```

Expected: `{"jsonrpc":"2.0","id":1,"result":"0x1312D00"}`

---

## Future deploys

```bash
cd ~/provider-simulator && git pull origin develop && bash scripts/deploy.sh
```

`scripts/deploy.sh` handles the rollout restart automatically — no extra commands needed.

If you are testing a feature branch (example: `fix_request_id`) before merge:

```bash
cd ~/provider-simulator
git fetch origin
git checkout fix_request_id
git pull origin fix_request_id
bash scripts/deploy.sh
```

After the fix is merged, switch back to `develop` for normal deploys.

---

# Appendix

## Appendix A — Debug server (dedicated setup)

This is the one-time setup needed on a new server so debug endpoints are reachable via domain.

### A.1 Enable debug flags in `values/core/values.yml`

```bash
yq eval -i '.miscellaneous.devMode.enabled = true' ~/smart-router-standalone/values/core/values.yml
yq eval -i '.miscellaneous.routers.additionalFlags += ["--debug-address", ":9999"]' ~/smart-router-standalone/values/core/values.yml
yq eval -i '.miscellaneous.routers.additionalFlags |= unique' ~/smart-router-standalone/values/core/values.yml
```

### A.2 Helm upgrade with both values files

```bash
cd ~/smart-router-standalone
source scripts/utils/common.sh

helm upgrade smart-router \
  "oci://ghcr.io/magma-devs/smart-router-helm-chart/smart-router" \
  --namespace lava-infra \
  --version "$HELM_CHART_VERSION" \
  --values values/core/values.yml \
  --values values/simulator/values_sim.yml \
  --wait --timeout 5m
```

### A.3 Expose debug server via domain (one-time)

Use base domain from step 0:

```bash
# works with both quoted and unquoted YAML values
BASE_DOMAIN=$(grep -E '^\s*base_domain\s*:' ~/smart-router-standalone/values/core/values.yml | head -1 | sed -E 's/.*base_domain\s*:\s*"?([^" ]+)"?.*/\1/')
echo "$BASE_DOMAIN"
```

If both objects already exist, skip the create blocks below:

```bash
kubectl get service eth-router-debug -n lava-infra
kubectl get httproute eth-router-debug-httproute -n lava-infra
```

Create Service:

```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: eth-router-debug
  namespace: lava-infra
spec:
  selector:
    app.lavanet.io/router: eth
  ports:
    - port: 9999
      targetPort: 9999
EOF
```

Create HTTPRoute:

```bash
cat <<EOF | kubectl apply -f -
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: eth-router-debug-httproute
  namespace: lava-infra
spec:
  parentRefs:
    - name: sr-gateway
      namespace: lava-infra
  hostnames:
    - "debug.${BASE_DOMAIN}"
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /debug/
      backendRefs:
        - name: eth-router-debug
          port: 9999
EOF
```

Refresh TLS cert so the new hostname is covered:

```bash
cd ~/smart-router-standalone
bash scripts/install_gateway_api_tls_certificate.sh
```

### A.4 Verify debug server is reachable

```bash
kubectl get service eth-router-debug -n lava-infra
kubectl get httproute eth-router-debug-httproute -n lava-infra

ETH_POD=$(kubectl get pods -n lava-infra -l app.lavanet.io/router=eth -o jsonpath='{.items[0].metadata.name}')
kubectl logs -n lava-infra "$ETH_POD" | grep "Debug HTTP server started"

curl -s "https://debug.${BASE_DOMAIN}/debug/time" | python3 -m json.tool
```

Clock shift / reset examples:

```bash
curl -s -X POST "https://debug.${BASE_DOMAIN}/debug/time-warp" \
  -H "Content-Type: application/json" \
  -d '{"offset_seconds": 3600}'

curl -s -X POST "https://debug.${BASE_DOMAIN}/debug/time-warp" \
  -H "Content-Type: application/json" \
  -d '{"offset_seconds": 0}'
```

---

## Appendix B — Troubleshooting

### `eth-sim-router` stuck in CrashLoopBackOff

**Cause:** Router calls `eth_getBlockByNumber` on startup and expects a JSON block object.
If `server.py` returns `"0x1"` for every method, the router panics:
```
"message":"[-] verify failed to parse result"
```

**Fix:** Add `METHOD_DEFAULTS` to `server.py` so `eth_getBlockByNumber` returns a proper block object:
```python
# Before (broken):
result = method_cfg.get("result", "0x1")

# After (correct):
result = method_cfg.get("result", METHOD_DEFAULTS.get(method, "0x1"))
```

After editing, commit, push, then on the server (replace `develop` with your test branch if not merged yet):
```bash
cd ~/provider-simulator && git pull origin develop && bash scripts/deploy.sh
```

The `eth-sim-router` pods recover automatically within ~30 seconds.

---

### 522 on sim-control after step 8

TLS cert may not cover `sim-control` subdomain yet. Re-run:
```bash
cd ~/smart-router-standalone && bash scripts/install_gateway_api_tls_certificate.sh
```

---

### Debug URL returns 404/502

Check that debug route objects exist:

```bash
kubectl get service eth-router-debug -n lava-infra
kubectl get httproute eth-router-debug-httproute -n lava-infra
```

Check debug server started in router pod:

```bash
ETH_POD=$(kubectl get pods -n lava-infra -l app.lavanet.io/router=eth -o jsonpath='{.items[0].metadata.name}')
kubectl logs -n lava-infra "$ETH_POD" | grep "Debug HTTP server started"
```

If route exists but TLS fails, re-run certificate install:

```bash
cd ~/smart-router-standalone
bash scripts/install_gateway_api_tls_certificate.sh
```

