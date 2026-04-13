# Helm Upgrade Steps — Add eth-sim Chain to Smart Router

---

> **Current recommendation:** for a fresh server, use `docs/new_server_setup.md` first.
> This document is still useful as a deep-dive explanation of the Helm / Kubernetes
> pieces, but the shortest current operator path lives in `new_server_setup.md`.

## Before you start — understanding the landscape

### What is the smart router?

The smart router is a running application on the server `victoria.magmadevs.com`.
It receives blockchain RPC requests from the outside world and routes them to real
blockchain nodes (Google, QuickNode). It is deployed inside **Kubernetes**.

### What is Kubernetes?

Kubernetes (also called "k8s") is a system that runs and manages containerised
applications on a server. Instead of running `python server.py` directly, you
describe what you want in a YAML file ("run 2 copies of this container, restart if
it crashes, expose it on this port") and Kubernetes makes it happen and keeps it that
way.

The key things Kubernetes manages:

| Term | What it is |
|---|---|
| **Pod** | The smallest unit. One running container (or a small group). Like a running process. |
| **Deployment** | A rule: "always keep N copies of this pod running". If a pod crashes, Kubernetes starts a new one. |
| **Service** | A stable network address (DNS name + port) that points to a set of pods. Pods come and go; the Service address never changes. |
| **HTTPRoute** | A rule for the Gateway: "requests to hostname X should go to Service Y". |
| **Namespace** | A folder/group inside the cluster. All our stuff lives in the `lava-infra` namespace. |

### What is kubectl?

`kubectl` is the command-line tool for talking to Kubernetes. It is like a remote
control for the cluster. Examples:
- `kubectl get pods -n lava-infra` → list all running pods in the `lava-infra` namespace
- `kubectl logs <pod-name> -n lava-infra` → read what a pod printed to its console
- `kubectl describe pod <pod-name> -n lava-infra` → detailed status and events for a pod

### What is Helm?

Helm is a package manager for Kubernetes — think of it like `apt` (Ubuntu) or `brew`
(Mac) but for Kubernetes applications. Instead of manually writing 10 YAML files and
applying them one by one, you use a **Helm chart**: a template package that generates
all the YAML for you based on a **values file** you provide.

The smart router is deployed as a Helm **release** named `smart-router`.

### What is a Helm chart?

A chart is a collection of templates. You give it a values file (your config), and it
generates the Kubernetes YAML (Deployments, Services, HTTPRoutes, etc.) for you.
The smart router chart lives at:
```
oci://ghcr.io/magma-devs/smart-router-helm-chart/smart-router
```
`oci://` means it is stored in a container registry (same place as Docker images).
`ghcr.io` is GitHub Container Registry — a private registry owned by Magma Devs.

### What is a Helm release?

When you run `helm install` or `helm upgrade`, Helm creates a **release** — a named
deployment of a chart on the cluster. The smart router release is named `smart-router`.
Every time you run `helm upgrade`, the release gets a new **revision number**
(1, 2, 3...). You can roll back to any previous revision.

### What is a values file?

A values file is a YAML file you pass to Helm that fills in the blanks in the chart
templates. Think of the chart as a form and the values file as your answers.

The smart router has two values files:
- `values/core/values.yml` — the main config: domain, routers (chains), gateway
  settings, resource limits, dashboard. **This is the source of truth** for what is
  running.
- `values/simulator/values_sim.yml` — the simulator override: adds `eth-sim` to the
  routers list alongside `base` and `eth`.

### What is `smart-router-standalone`?

This is the Git repository on the server at `~/smart-router-standalone`. It contains
the values files and scripts used to deploy and configure the smart router. It does
NOT contain the router source code — just the configuration for deploying it via Helm.

---

## ⚠️ Schema changed in chart 4.0.0 — read before touching any values file

The original implementation guide was written when the chart was version `3.1.0`.
The server now runs `4.0.0`. The YAML key names changed between versions.

Using the old keys produces **no error** — Helm silently stores them and ignores them.
This is the most confusing failure mode: everything looks like it worked, but no new
pods or routes appear.

| Topic | Original guide (chart 3.1.0) | Actual (chart 4.0.0) |
|---|---|---|
| Chart version | `3.1.0` | `4.0.0` |
| Top-level routers key | `chains:` | `routers:` |
| Provider list key | `providers:` | `nodes:` |
| Separate override file | only `eth-sim` in second file | ❌ Must include ALL routers — Helm replaces lists |

HOW TO ALWAYS KNOW THE CORRECT SCHEMA: run `cat values/core/values.yml` and read the
key names used for the already-working `base` and `eth` routers. Whatever keys are
used there are the correct keys for the current chart version.

---

## Step 1 — Go to the correct directory

```bash
cd ~/smart-router-standalone
```

WHY THIS MATTERS: Every command in the steps below uses **relative paths** — paths
that are relative to wherever you are right now. For example, Step 4 references
`values/core/values.yml`. That is not an absolute path like `/root/smart-router-standalone/values/core/values.yml` — it is a shorthand that only works if you are
already inside `smart-router-standalone`.

If you are in the wrong directory, every command will fail with:
```
Error: open values/core/values.yml: no such file or directory
```

Rule of thumb: before running ANY command in this guide, check where you are:
```bash
pwd
```
The output must be `/root/smart-router-standalone`.

---

## Step 2 — Load the environment variables

```bash
source scripts/utils/common.sh
```

### What is `scripts/utils/common.sh`?

It is a shell script file that lives inside the `smart-router-standalone` repo at
`smart-router-standalone/scripts/utils/common.sh`. It contains a list of `export`
statements — shell variable definitions that subsequent commands will need.

If you opened the file, you would see something like:
```bash
export NAMESPACE="lava-infra"
export HELM_CHART_VERSION="4.0.0"
export HELM_REGISTRY_USERNAME="some-github-user"
export HELM_REGISTRY_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxx"
```

After running `source scripts/utils/common.sh`, those variables are available in
your terminal session. When you later type `$NAMESPACE` in a command, the shell
replaces it with `"lava-infra"`.

### What is `HELM_CHART_VERSION`?

It is the version number of the Helm chart to use. The smart router chart has
multiple published versions (3.1.0, 4.0.0, etc.), each with different features and
schemas. `HELM_CHART_VERSION` tells the `helm upgrade` command which exact version to
download from the registry.

### Why `source` and not `bash`?

`source` runs the script **in your current shell**. The variables it sets stay alive
in your terminal for as long as the session is open.

`bash scripts/utils/common.sh` creates a **child shell**, runs the script there, and
then the child shell exits — taking all the variables with it. Your terminal never
sees them.

Think of it like this: `bash` is like reading a recipe in a separate room and then
walking back. `source` is like reading the recipe in the same room — the knowledge
stays with you.

### Verify the chart version

```bash
grep -i helm_chart_version scripts/utils/common.sh
```

`grep` searches a file for lines matching a pattern. `-i` means case-insensitive.
Expected output:
```
export HELM_CHART_VERSION="4.0.0"
```

If it says `3.1.0`, the upgrade will apply the wrong chart version. Update the file
to `4.0.0` before continuing.

---

## Step 3 — Log in to the Helm chart registry

```bash
echo "$HELM_REGISTRY_TOKEN" | helm registry login ghcr.io \
  --username "$HELM_REGISTRY_USERNAME" \
  --password-stdin
```

### What is `ghcr.io`?

GitHub Container Registry. It is a private registry where Magma Devs stores both
Docker images and Helm charts. Before Helm can download the smart router chart, it
must authenticate — just like you need to log in to pull a private Docker image.

### What is `$HELM_REGISTRY_TOKEN`?

A GitHub Personal Access Token (PAT) — a string that proves Helm has permission to
access the private registry. It was loaded from `common.sh` in Step 2.

### Why `--password-stdin`?

Instead of `--password "$HELM_REGISTRY_TOKEN"`, which would expose the token in the
process list (visible to anyone running `ps aux`), `--password-stdin` reads the
password from stdin (piped from `echo`). The token never appears in shell history or
process listings.

Expected output: `Login Succeeded`

---

## Step 4 — Create `values/simulator/values_sim.yml`

### What does this file do?

This file is the Helm values override for the simulator deployment. When passed to
`helm upgrade` alongside `values/core/values.yml`, it tells the chart to also create
router resources for `eth-sim` — a new chain that routes to your simulator pod
instead of real blockchain nodes.

### ⚠️ Critical Helm behaviour: lists are REPLACED, not merged

Helm merges two values files like this:
- **Scalar values** (strings, numbers, booleans): the second file wins
- **Maps** (nested key-value blocks): keys are merged recursively
- **Lists** (items starting with `-`): the **second file's list replaces the first entirely**

This means: if `values_sim.yml` only contains `eth-sim` under `routers:`, Helm will
replace the entire `routers:` list with just `eth-sim` — silently deleting `base` and
`eth`. The existing routers would stop working.

**The fix: `values_sim.yml` must contain ALL routers** (`base`, `eth`, `eth-sim`).

Create the directory and file:

```bash
mkdir -p ~/smart-router-standalone/values/simulator
```

Current repo state is simpler: `provider-simulator/config/values_sim.yml` already contains
the full router list used on the working server (`base`, `eth`, `eth-sim`).

Copy it as-is:

```bash
cp ~/provider-simulator/config/values_sim.yml ~/smart-router-standalone/values/simulator/values_sim.yml

# sanity check — should print base, eth, eth-sim
grep -n '^  - id:' ~/smart-router-standalone/values/simulator/values_sim.yml
```

Reference structure:

```yaml
routers:
  - id: "base"
    network: "base"
    nodes:
      - name: "Lava1"
        endpoints:
          - url: "https://base.lava.build:443/"
            interface: "jsonrpc"
            addons: ["archive", "trace", "debug"]
      - name: "Lava2"
        endpoints:
          - url: "https://base.lava.build:443/"
            interface: "jsonrpc"
            addons: ["archive", "trace", "debug"]
      - name: "Lava3"
        endpoints:
          - url: "https://base.lava.build:443/"
            interface: "jsonrpc"
            addons: ["archive", "trace", "debug"]
  - id: "eth"
    network: "eth1"
    nodes:
      - name: "Google1"
        endpoints:
          - url: "https://YOUR_ETH_RPC_PROVIDER_HOST?key=..."
            interface: "jsonrpc"
            addons: ["archive", "trace", "debug"]
      - name: "Google2"
        endpoints:
          - url: "https://YOUR_ETH_RPC_PROVIDER_HOST?key=..."
            interface: "jsonrpc"
            addons: ["archive", "trace", "debug"]
      - name: "QuickNode1"
        endpoints:
          - url: "https://wispy-distinguished-glitter.quiknode.pro/..."
            interface: "jsonrpc"
            addons: ["archive", "trace", "debug"]
      - name: "QuickNode2"
        endpoints:
          - url: "https://wispy-distinguished-glitter.quiknode.pro/..."
            interface: "jsonrpc"
            addons: ["archive", "trace", "debug"]
  - id: "eth-sim"
    network: "eth1"
    nodes:
      - name: "SimProvider1"
        endpoints:
          - url: "http://provider-simulator.lava-infra.svc.cluster.local:18545"
            interface: "jsonrpc"
            addons: ["archive", "trace", "debug"]
      - name: "SimProvider2"
        endpoints:
          - url: "http://provider-simulator.lava-infra.svc.cluster.local:18546"
            interface: "jsonrpc"
            addons: ["archive", "trace", "debug"]
      - name: "SimProvider3"
        endpoints:
          - url: "http://provider-simulator.lava-infra.svc.cluster.local:18547"
            interface: "jsonrpc"
            addons: ["archive", "trace", "debug"]
```

Source of truth:
```bash
cat ~/provider-simulator/config/values_sim.yml
```

### What is `routers:` + `nodes:`?

These are the YAML key names that chart 4.0.0 expects. The original guide used
`chains:` + `providers:` (chart 3.1.0 schema) — those keys are accepted silently
but ignored, so no pods get created. Always verify by reading `values/core/values.yml`.

### What is `provider-simulator.lava-infra.svc.cluster.local`?

This is a Kubernetes internal DNS name. It follows the pattern:
```
<service-name>.<namespace>.svc.cluster.local
```
Kubernetes automatically creates this DNS entry when a Service named
`provider-simulator` exists in the `lava-infra` namespace. It only resolves inside
the cluster — you cannot curl it from your laptop or browser.

### Why three ports (18545 / 18546 / 18547)?

The simulator pod runs three independent fake JSON-RPC servers, one per port. The
router sees them as three separate providers. This is what allows WRS (Weighted
Round-robin Scoring) to score them independently — essential for testing failover,
latency scoring, and rate-limit avoidance.

---

## Step 5 — Run the Helm upgrade

```bash
helm upgrade smart-router \
  "oci://ghcr.io/magma-devs/smart-router-helm-chart/smart-router" \
  --namespace lava-infra \
  --version "$HELM_CHART_VERSION" \
  --values values/core/values.yml \
  --values values/simulator/values_sim.yml \
  --wait --timeout 5m
```

### Breaking down every argument

`helm upgrade smart-router`
→ Update the existing Helm release named `smart-router`. Not reinstalling — just
  applying a new configuration to the already-running router.

`oci://ghcr.io/magma-devs/smart-router-helm-chart/smart-router`
→ Where to download the chart from. `oci://` = stored in a container registry.

`--namespace lava-infra`
→ The Kubernetes namespace where the release lives. All smart router pods, services,
  and routes are in this namespace.

`--version 4.0.0`
→ Use exactly this chart version. Pinning prevents accidentally pulling a newer
  version with schema changes you are not ready for.

`--values values/core/values.yml`
→ The base configuration (domain, gateway, dashboard, resource limits).

`--values values/simulator/values_sim.yml`
→ The simulator override: the full routers list including `eth-sim`. Applied on top
  of core values. For lists, this file wins entirely — see Step 4 warning.

`--wait --timeout 5m`
→ Helm will wait up to 5 minutes for all new pods to reach `Ready` state before
  returning. If the simulator pod is not deployed yet, the eth-sim-router pods
  will not become `Ready` and Helm will hang until timeout.
  **Safe to press `Ctrl+C`** — the resources are already applied regardless.

### What Helm does after this command

1. Downloads the chart from `ghcr.io`
2. Merges `values/core/values.yml` + `values/simulator/values_sim.yml`
3. Generates Kubernetes YAML from the chart templates
4. Applies the diff to the cluster (only creates/updates what changed)
5. Creates: `eth-sim-router` Deployment, Service, and HTTPRoute

Expected output:
```
Pulled: ghcr.io/magma-devs/smart-router-helm-chart/smart-router:4.0.0
Release "smart-router" has been upgraded. Happy Helming!
STATUS: deployed
REVISION: 3   ← increments on every upgrade; exact number does not matter
```

---

## Step 6 — Update the TLS certificate

```bash
bash scripts/install_gateway_api_tls_certificate.sh
```

### What is TLS?

TLS (Transport Layer Security) is the encryption protocol behind HTTPS. When a
browser or test makes a request to `https://eth-sim-jsonrpc.victoria.magmadevs.com`,
the server must present a **certificate** that proves it is the legitimate owner of
that hostname.

### What is a TLS certificate in this context?

The Gateway (Envoy) holds a TLS certificate stored as a Kubernetes Secret named
`router-tls`. The certificate lists all the hostnames it covers. When Helm creates
the new HTTPRoute for `eth-sim-jsonrpc.victoria.magmadevs.com`, that hostname is not
yet in the certificate — HTTPS requests would fail with a certificate error.

### What does this script do?

It reads all existing HTTPRoutes in `lava-infra`, collects every hostname, and
regenerates the `router-tls` secret to cover all of them. It then restarts
`envoy-gateway` so it picks up the updated certificate.

Expected output:
```
secret/router-tls configured
TLS secret router-tls updated with all hostnames
⚠️  Using self-signed certificate. For production, replace with a valid certificate.
deployment.apps/envoy-gateway restarted
```

---

## Step 7 — Verify HTTPRoute and pods

```bash
kubectl get httproute -n lava-infra | grep eth-sim
kubectl get pods -n lava-infra | grep eth-sim
```

### What is `kubectl get`?

`kubectl get <resource-type> -n <namespace>` lists all resources of that type in
the namespace. `| grep eth-sim` pipes the output to `grep`, which filters to only
lines containing "eth-sim".

### What is an HTTPRoute?

An HTTPRoute is a Kubernetes resource (part of the Gateway API) that tells the
Envoy Gateway: "when a request arrives for hostname X, forward it to Service Y".
The Helm chart creates one automatically for each router based on its `id` and your
`base_domain`. For `id: eth-sim` + `base_domain: victoria.magmadevs.com` the chart
creates: `eth-sim-jsonrpc.victoria.magmadevs.com`.

Expected output:
```
smart-router-eth-sim-jsonrpc-httproute   ["eth-sim-jsonrpc.victoria.magmadevs.com"]   2m
eth-sim-router-xxxxxxxxxx-xxxxx          0/1   CrashLoopBackOff   4 (40s ago)   2m
```

### What does `0/1 CrashLoopBackOff` mean?

- `0/1` → 0 out of 1 containers are Ready
- `CrashLoopBackOff` → the container starts, crashes, Kubernetes waits a bit, restarts it, it crashes again, repeat

This is **expected at this point** and is NOT a config error. The router process
starts correctly, reads the config, and logs all 3 simulator provider URLs — but
then crashes because `provider-simulator.lava-infra.svc.cluster.local` does not
resolve yet (the simulator pod and its Service do not exist).

✅ HTTPRoute present = the values file schema was correct, Helm worked.
⚠️ CrashLoopBackOff = the backend (simulator pod) is missing. Fix is Step 8.

To confirm the crash is only about the missing backend (not a config mistake):
```bash
kubectl logs -n lava-infra <eth-sim-router-pod-name> -c router | head -50 | grep -i "Direct RPC Endpoint:"
```

You should see `"Direct RPC Endpoint:"` lines for all 3 simulator URLs and
`"created direct RPC connection"` — the config is correct, only the backend is missing.

---

## Step 8 — Deploy the provider-simulator pod

### Why is this a separate step?

The simulator pod is owned by the automation team and lives in the `provider-simulator`
repository — separate from the router repo. The router Helm chart knows the simulator's
DNS address but does not deploy it. You deploy it manually.

### Why do the eth-sim-router pods crash without the simulator?

When the router pod starts, it tries to resolve
`provider-simulator.lava-infra.svc.cluster.local`. Kubernetes DNS only creates that
entry when a **Service** named `provider-simulator` exists in `lava-infra`. Without
the simulator pod deployed (and its Service), the DNS lookup fails and the router
process eventually crashes.

### Get the repo onto the server

`Magma-Devs/provider_simulator` is now public, so read-only clone/pull does **not** require a deploy key.

Recommended option:

```bash
git clone https://github.com/Magma-Devs/provider_simulator.git ~/provider-simulator
```

Alternative options:

**Option A — Copy from your Mac (fastest, one-time):**

Run this on your **Mac** terminal:
```bash
cd /Users/victoria && tar czf - provider-simulator \
  --exclude=provider-simulator/.git \
  --exclude=provider-simulator/untracked \
  --exclude=provider-simulator/.DS_Store | \
  ssh victoria@64.176.170.39 "sudo tar xzf - -C /root/"
```

`tar czf -` creates a compressed archive and writes it to stdout (`-`).
The `|` pipes it directly into `ssh`, which runs `tar xzf -` on the server,
extracting it to `/root/`. No temporary file is created anywhere.

macOS tar warnings about `LIBARCHIVE.xattr.com.apple.provenance` are harmless —
macOS extended attributes that Linux tar does not understand but safely ignores.

**Option B — GitHub deploy key or personal SSH key (only if you want SSH-based git access):**

A deploy key is an SSH key pair where the public key is registered on a specific
GitHub repo, granting read access to whoever has the private key.

On the server:
```bash
ssh-keygen -t ed25519 -C "victoria-smart-router" -f ~/.ssh/id_github -N ""
cat ~/.ssh/id_github.pub
```

`ssh-keygen` generates a key pair. `-t ed25519` = key type. `-f ~/.ssh/id_github` =
where to save the private key. `-N ""` = no passphrase (needed for automation).

Copy the printed public key. Go to:
`github.com/Magma-Devs/provider_simulator` → **Settings → Deploy keys → Add deploy key**
Paste the key. Read-only — do not tick "Allow write access".

After adding the key to GitHub, configure SSH to always use it for `github.com`
(without this, `git pull` will fail with "Permission denied (publickey)" even though
the key exists on the server):

```bash
cat >> ~/.ssh/config << 'EOF'

Host github.com
  IdentityFile ~/.ssh/id_github
  User git
EOF
chmod 600 ~/.ssh/config
```

Test the connection before cloning:
```bash
ssh -T git@github.com
```
Expected output:
```
Hi Magma-Devs/provider_simulator! You've successfully authenticated, but GitHub does not provide shell access.
```
If you see `Permission denied (publickey)` — the public key was not added to GitHub
correctly. Go back to **Settings → Deploy keys** and verify the key is there.

Clone (only needed the first time — the SSH config handles key selection automatically):
```bash
git clone git@github.com:Magma-Devs/provider_simulator.git ~/provider-simulator
```

For future updates after the initial clone:
```bash
cd ~/provider-simulator && git pull origin develop
```

WHY not use `HELM_REGISTRY_TOKEN`? That token only has permission to access the Helm
chart registry (`ghcr.io`). It does not have permission to read the `provider_simulator`
source code repository. A deploy key grants exactly the read access needed for this
one repo.

### Run the deploy script

```bash
cd ~/provider-simulator
bash scripts/deploy.sh
```

### What `deploy.sh` does now

`deploy.sh` already performs the rollout restart for you. No extra manual restart is needed after it finishes.

`scripts/deploy.sh` does four things in order:

**1. Builds the Docker image:**
```
docker build -t provider-simulator:latest .
```
Reads `Dockerfile` and `server.py`, creates a local Docker image named
`provider-simulator:latest`.

**2. Imports the image into MicroK8s:**
```
docker save provider-simulator:latest | microk8s ctr image import -
```
MicroK8s uses `containerd` as its container runtime — a completely separate image
store from Docker. Even though Docker can see the image, Kubernetes cannot pull it
unless it is imported into MicroK8s. This step is required on every re-deploy if
`server.py` has changed.

**3. Applies the Kubernetes manifests:**
```
kubectl apply -f k8s/deployment.yml
kubectl apply -f k8s/service.yml
kubectl apply -f <rendered httproute using BASE_DOMAIN>
```
- `deployment.yml` → tells Kubernetes to run 1 replica of the simulator container
- `service.yml` → creates the Service that makes the DNS name resolve
- rendered `httproute-control.yml` → exposes the control API at `sim-control.<BASE_DOMAIN>`

**4. Restarts the deployment and waits for readiness:**

`deploy.sh` runs `kubectl rollout restart deployment/provider-simulator -n lava-infra`
and waits until the new pod is ready.

---

## Step 9 — Verify simulator is running

```bash
kubectl get pods -n lava-infra | grep provider-simulator
curl https://sim-control.<YOUR_DOMAIN>/health
```

### What does `1/1 Running` mean?

`1/1` → 1 out of 1 containers are Ready. `Running` → the container is alive and
its readiness probe is passing. The simulator's readiness probe is a `GET /health`
request to port 19000 — it passes once the Python HTTP servers are listening.

### Why do the eth-sim-router pods recover automatically?

Kubernetes never gives up on a `CrashLoopBackOff` pod — it keeps restarting it with
increasing delays (10s, 20s, 40s...). When you deploy the simulator, the DNS name
`provider-simulator.lava-infra.svc.cluster.local` starts resolving. The next time
Kubernetes restarts the eth-sim-router pod, the router process starts successfully and
stays running. No manual action needed.

Expected:
```
provider-simulator-xxxxx   1/1   Running
{"status": "ok"}
```

The eth-sim-router pods go `Running` within ~30 seconds of the simulator starting.

---

## Step 10 — Final smoke test

```bash
curl -s -X POST https://eth-sim-jsonrpc.<YOUR_DOMAIN> \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'
```

### What does this command do?

`curl` is a command-line HTTP client. This sends a POST request to the public
`eth-sim` router endpoint with a JSON-RPC body asking for the current block number.

The full request path:
```
curl
  → Cloudflare (DNS / DDoS protection)
    → Envoy Gateway (<YOUR_DOMAIN>:443)
      → HTTPRoute: eth-sim-jsonrpc.<YOUR_DOMAIN> → eth-sim-router Service
        → eth-sim-router pod (WRS scoring, picks a provider)
          → provider-simulator pod :18545 or :18546 or :18547
            → returns fake block number "0x1312D00"
```

Expected response:
```json
{"jsonrpc":"2.0","id":1,"result":"0x1312D00"}
```

`0x1312D00` is the current default stubbed result for `eth_blockNumber`. If you see this response,
the entire stack is working end-to-end.

If you get a `503` or connection error:
- Check Step 9 — the simulator pod must be `Running`
- Check `kubectl get pods -n lava-infra | grep eth-sim` — router pods must be `Running`
- Check `kubectl get httproute -n lava-infra | grep eth-sim` — route must exist

---

## Troubleshooting — eth-sim-router stuck in CrashLoopBackOff after simulator is Running

### Symptom

`kubectl get pods -n lava-infra | grep eth-sim` shows `CrashLoopBackOff` even though
`kubectl get pods -n lava-infra | grep provider-simulator` shows `1/1 Running`.

### How to read the crash logs

```bash
kubectl logs -n lava-infra \
  $(kubectl get pods -n lava-infra | grep eth-sim-router | head -1 | awk '{print $1}') \
  --previous 2>/dev/null | grep -iE "error|fatal|panic|fail|refused|invalid"
```

### Root cause — older simulator returned wrong type for `eth_getBlockByNumber`

On startup the router sends an `eth_getBlockByNumber` RPC call to each provider.
This is a **pruning verification** — the router checks whether the node is full or
archive by inspecting the block object. It expects the `result` field to be a
**JSON object** (a block with fields like `number`, `hash`, `transactions`, etc.).

An older `server.py` returned a bare hex string `"0x1"` as the default result
for **every** JSON-RPC method regardless of what was called. The router tried to
parse `"0x1"` as a block object, failed, and panicked — producing this log line:

```
"message":"[-] verify failed to parse result"
```

### Fix — use current `server.py` with `METHOD_DEFAULTS`

`server.py` needs per-method default responses so `eth_getBlockByNumber` returns a
proper block object. The fix adds a `METHOD_DEFAULTS` dict and uses it as the
fallback instead of the hardcoded `"0x1"`:

```python
# Before (broken — returns "0x1" for every method):
result = method_cfg.get("result", "0x1")

# After (correct — returns method-appropriate defaults):
result = method_cfg.get("result", METHOD_DEFAULTS.get(method, "0x1"))
```

`METHOD_DEFAULTS` contains realistic responses for all methods the router calls
during startup verification, including the full block object for `eth_getBlockByNumber`.

If you still hit this on another server, make sure it is running the current repo version. After editing `server.py` on your Mac, commit and push:
```bash
cd /Users/victoria/provider-simulator
git add server.py
git commit -m "fix: return proper block object for eth_getBlockByNumber"
git push origin develop
```

On the server, pull and redeploy:
```bash
cd ~/provider-simulator && git pull origin develop && bash scripts/deploy.sh
```

The eth-sim-router pods recover automatically within ~30 seconds once the simulator
returns a valid block object on startup.

