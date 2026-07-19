# Deploy After Push (Simulator)

Use this guide after you push new code from this repo.

Run all commands in this guide on the deployment server (not your local machine).

## Quick decision

- If you changed simulator code/manifests (for example: `server.py`, `stubs.py`, `constants.py`, `k8s/*`): deploy/restart **`provider-simulator` only**.
- If you changed router Helm/chart/config (outside this simulator repo flow): run a **router Helm upgrade**.

## Simulator-only change (most common)

On the deployment server, run from `provider-simulator` repo:

```zsh
cd /path/to/provider-simulator
bash scripts/deploy.sh   # self-updates to origin/$DEPLOY_REF first; no manual git pull needed
```

What this script already does (`scripts/deploy.sh`):
- self-updates first: fetches and checks out `origin/$DEPLOY_REF` (a branch; default `main`) via `git checkout -B` before building, so a stale checkout can't be deployed — override `DEPLOY_REF` to deploy another branch, or set `SKIP_SELF_UPDATE=true` to skip (offline runs / deliberately hand-edited files; a dirty tree aborts rather than being clobbered)
- builds `provider-simulator:latest` (unless `SKIP_BUILD=true`)
- imports image into MicroK8s
- applies `k8s/deployment.yml` + `k8s/service.yml` in one pass
- applies the Gateway-API routes: `httproute-control.yml`, `grpcroute-lava-sim-grpc.yml`, `httproute-lava-sim-rest.yml`, `httproute-lava-sim-ws.yml`
- restarts `deployment/provider-simulator` in namespace `lava-infra` — but only when a fresh image was built this run, the manifests actually changed (`kubectl diff`), or `FORCE_RESTART=true` is set. A no-op re-run (`SKIP_BUILD=true`, nothing changed) skips the restart and just confirms the deployment is healthy.
- waits for rollout to finish (`--timeout=180s`, override with `ROLLOUT_TIMEOUT`)
- prints a reminder to regenerate the TLS certificate for the current hostnames — this step is NOT automatic; if a route hostname is new, run the certificate script from `smart-router-standalone` yourself (`bash scripts/install_gateway_api_tls_certificate.sh`), or HTTPS requests to that hostname fail with a certificate error

## If image is already present and you only need restart

Run these `kubectl` commands on the deployment server.

```zsh
kubectl rollout restart deployment/provider-simulator -n lava-infra
kubectl rollout status deployment/provider-simulator -n lava-infra --timeout=60s
```

## When to restart router

Do **not** restart router for simulator-only code changes.

Restart/upgrade router only if router config/chart changed (typically in `smart-router-standalone`):

```zsh
helm upgrade smart-router ... -n lava-infra
```

## Verify after deploy

```zsh
kubectl get pods -n lava-infra -l app=provider-simulator
curl -s https://sim-control.<BASE_DOMAIN>/health

# for example :

curl https://sim-control.victoria.magmadevs.com/health
```

Expected:
- simulator pod is `Running`
- `/health` returns `{"status":"ok"}`

