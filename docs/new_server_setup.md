# New Server Setup — Provider Simulator

## 1. GitHub deploy key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_github -N "" -C "$(hostname)"
cat ~/.ssh/id_github.pub
```

→ Add the printed key to GitHub:  
`github.com/Magma-Devs/provider_simulator → Settings → Deploy keys → Add (read-only)`

```bash
cat > ~/.ssh/config << 'EOF'
Host github.com
  User git
  IdentityFile ~/.ssh/id_github
EOF
chmod 600 ~/.ssh/config
ssh -T git@github.com   # must say: Hi Magma-Devs/provider_simulator!
```

---

## 2. Clone and deploy

```bash
git clone git@github.com:Magma-Devs/provider_simulator.git ~/provider-simulator
cd ~/provider-simulator && bash scripts/deploy.sh
```

---

## 3. Verify

```bash
kubectl get pods -n lava-infra -l app=provider-simulator
curl -s https://sim-control.<YOUR_DOMAIN>/health
```

Expected: `1/1 Running` and `{"status": "ok"}`

---

## 4. Create `values/simulator/values_sim.yml`

This file lives in `smart-router-standalone` — **not** in this repo. It must be created manually.

```bash
mkdir -p ~/smart-router-standalone/values/simulator
```

The `eth-sim` block is in `provider-simulator/config/values_sim.yml` — the simulator endpoints never change (internal cluster DNS). **You must prepend the `base` and `eth` router blocks from `values/core/values.yml`** — Helm replaces lists entirely so all three routers must be in one file.

```bash
# copy eth-sim block from provider-simulator repo
cat ~/provider-simulator/config/values_sim.yml

# open the final file and paste base + eth blocks from values/core/values.yml above eth-sim
vi ~/smart-router-standalone/values/simulator/values_sim.yml
```

Final file structure must be:
```
routers:
  - id: "base"     ← copy from values/core/values.yml
    ...
  - id: "eth"      ← copy from values/core/values.yml
    ...
  - id: "eth-sim"  ← from config/values_sim.yml (no changes needed)
    ...
```

---

## 5. Helm upgrade — point smart router at simulator

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

---

## 6. Smoke test

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

