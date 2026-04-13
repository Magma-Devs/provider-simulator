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
- Step 1 — clone `provider-simulator`
- Step 2 — deploy simulator pod
- Step 3 — verify simulator pod is healthy
- Step 4 — copy `values_sim.yml` into `smart-router-standalone`
- Step 5 — verify chart version
- Step 6 — run `helm upgrade` with simulator values
- Step 7 — smoke test public simulator route

If this server also needs **clock injection / score reset via debug domain**, continue to **Appendix A** after step 7.

---

## 0. Find this server's base domain

The base domain is set in `smart-router-standalone` — check it before doing anything else:

```bash
grep base_domain ~/smart-router-standalone/values/core/values.yml
# → base_domain: "victoria.magmadevs.com"
```

You'll need this value in steps 2 and 6. Keep it in mind or note it down.

---

## 1. Repository access (public repo)

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

## 2. Set domain, deploy

**Set the base domain before deploying** — `scripts/deploy.sh` reads this file to build hostnames.
Replace the value with the domain you found in step 0:

```bash
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

## 3. Verify pod is running

```bash
kubectl get pods -n lava-infra -l app=provider-simulator
```

Expected: `1/1 Running`

> During rollout you may briefly see two pods — one `Terminating` (old) and one `Running` (new). This is normal. Wait a few seconds and recheck.

> ⚠️ Do NOT test the `sim-control` URL yet — the TLS cert won't cover it until step 6.

---

## 4. Create `values/simulator/values_sim.yml`

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

> ⚠️ Use `routers:` (not `chains:`) and `nodes:` (not `providers:`) — chart 4.0.0 schema.
> Using old keys produces no error — Helm silently ignores them and nothing gets created.

---

## 5. Verify chart version

```bash
grep -i helm_chart_version ~/smart-router-standalone/scripts/utils/common.sh
```

Expected: `export HELM_CHART_VERSION="4.0.0"` — update if it shows `3.1.0`.

---

## 6. Helm upgrade — point smart router at simulator

```bash
cd ~/smart-router-standalone
source scripts/utils/common.sh
echo "$HELM_REGISTRY_TOKEN" | helm registry login ghcr.io \
  --username "$HELM_REGISTRY_USERNAME" --password-stdin

helm upgrade smart-router \
  "oci://ghcr.io/magma-devs/smart-router-helm-chart/smart-router" \
  --namespace lava-infra \
  --version "$HELM_CHART_VERSION" \
  --values values/core/values.yml \
  --values values/simulator/values_sim.yml \
  --wait --timeout 5m

bash scripts/install_gateway_api_tls_certificate.sh
```

Verify (use the domain from step 0):

```bash
curl -s https://sim-control.<YOUR_DOMAIN>/health
# Expected: {"status": "ok"}
```

---

## 7. Smoke test

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

### 522 on sim-control after step 6

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

