# kubectl Reference — Provider Simulator

**Namespace:** `lava-infra`  
**Deployment:** `provider-simulator`  
**Ports:** providers `:18545 / :18546 / :18547` · control `:19000`

> **Examples below are from Victoria's setup** — server IP `64.176.170.39`, SSH user `victoria`, domain `victoria.magmadevs.com`.
> Each teammate has their own server. Replace these with your own server's IP, SSH user, and `BASE_DOMAIN` (set in `config/base-domain.env`) when running any of the commands below.

---

## Connect to the server

```bash
ssh 64.176.170.39        # connects as victoria, drops straight into root shell
```

`~/.ssh/config` entry that makes this work:
```
Host 64.176.170.39
  User victoria
  IdentityFile ~/.ssh/id_ed25519
  ServerAliveInterval 60
  RemoteCommand sudo -s
  RequestTTY yes
```

---

## Status

```bash
# is the pod running?
kubectl get pods -n lava-infra -l app=provider-simulator

# full deployment status
kubectl get deployment provider-simulator -n lava-infra

# watch rollout in real time
kubectl get pods -n lava-infra -l app=provider-simulator -w

# show all resources for this app at once
kubectl get deployment,service,pod -n lava-infra -l app=provider-simulator
```

---

## Logs

```bash
# last 30 lines
kubectl logs -n lava-infra -l app=provider-simulator --tail=30

# follow live
kubectl logs -n lava-infra -l app=provider-simulator -f

# previous crashed pod (if pod is in CrashLoopBackOff)
kubectl logs -n lava-infra -l app=provider-simulator --previous

# logs by pod name (use this if --previous doesn't work with a label selector)
POD=$(kubectl get pod -n lava-infra -l app=provider-simulator -o jsonpath='{.items[0].metadata.name}')
kubectl logs -n lava-infra "$POD" --previous
```

---

## Deploy

```bash
# full deploy from scratch (build → import → apply → restart)
cd ~/provider-simulator && git pull origin develop && bash scripts/deploy.sh

# deploy a specific branch
cd ~/provider-simulator && git fetch origin && git checkout <branch> && bash scripts/deploy.sh

# optional: force restart without rebuilding (only if image is already present)
kubectl rollout restart deployment/provider-simulator -n lava-infra

# wait for rollout to finish
kubectl rollout status deployment/provider-simulator -n lava-infra --timeout=60s
```

`scripts/deploy.sh` already does the rollout restart for the normal deploy flow.

---

## Verify new image is running

```bash
# shows image SHA — compare with what docker build produced
kubectl describe pod -n lava-infra -l app=provider-simulator | grep Image

# hit health endpoint — quickest sanity check
curl -s https://sim-control.victoria.magmadevs.com/health

# test a provider JSON-RPC port directly (port-forward first — see below)
curl -s http://localhost:18545 \
  -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' \
  -H 'Content-Type: application/json'
```

---

## Exec into the pod

```bash
# open an interactive shell inside the running container
kubectl exec -it -n lava-infra \
  "$(kubectl get pod -n lava-infra -l app=provider-simulator -o jsonpath='{.items[0].metadata.name}')" \
  -- /bin/sh

# run a one-off command without an interactive shell
kubectl exec -n lava-infra \
  "$(kubectl get pod -n lava-infra -l app=provider-simulator -o jsonpath='{.items[0].metadata.name}')" \
  -- curl -s localhost:19000/health
```

---

## Port-forward (local testing)

Bypass the Gateway and hit the pod directly from your laptop.

```bash
# control API on localhost:19000
kubectl port-forward -n lava-infra svc/provider-simulator 19000:19000

# provider 1 on localhost:18545
kubectl port-forward -n lava-infra svc/provider-simulator 18545:18545

# all four ports at once
kubectl port-forward -n lava-infra svc/provider-simulator \
  18545:18545 18546:18546 18547:18547 19000:19000

# then in another terminal:
curl http://localhost:19000/health
curl http://localhost:19000/stats
```

---

## Rollout history

```bash
# view rollout history (shows revision numbers)
kubectl rollout history deployment/provider-simulator -n lava-infra

# see what changed in a specific revision
kubectl rollout history deployment/provider-simulator -n lava-infra --revision=2

# roll back to the previous version
kubectl rollout undo deployment/provider-simulator -n lava-infra

# roll back to a specific revision
kubectl rollout undo deployment/provider-simulator -n lava-infra --to-revision=1
```

---

## Network / Gateway

```bash
# check the HTTPRoute for the control endpoint
kubectl get httproute -n lava-infra sim-control-httproute

# see full HTTPRoute config + status conditions
kubectl describe httproute -n lava-infra sim-control-httproute

# check the gateway
kubectl get gateway -n lava-infra sr-gateway

# verify service endpoints are populated (empty = pod not ready or label mismatch)
kubectl get endpoints provider-simulator -n lava-infra

# check the service ports
kubectl get svc provider-simulator -n lava-infra
```

---

## Events (first stop for crash / scheduling issues)

```bash
# all events in the namespace, newest last
kubectl get events -n lava-infra --sort-by='.lastTimestamp'

# only events for the provider-simulator pod
kubectl get events -n lava-infra \
  --field-selector involvedObject.name="$(kubectl get pod -n lava-infra \
    -l app=provider-simulator -o jsonpath='{.items[0].metadata.name}')"
```

---

## MicroK8s image management

```bash
# list images imported into MicroK8s containerd
microk8s ctr images list | grep provider-simulator

# manually reimport without running the full deploy script
docker build -t provider-simulator:latest . && \
  docker save provider-simulator:latest | microk8s ctr image import -

# remove old image (forces a fresh import on the next restart)
microk8s ctr images rm docker.io/library/provider-simulator:latest
```

---

## Troubleshoot

```bash
# pod not starting — see events and probe failures
kubectl describe pod -n lava-infra -l app=provider-simulator

# check readiness probe (hits GET /health on port 19000)
kubectl get pod -n lava-infra -l app=provider-simulator -o wide

# resource usage
kubectl top pod -n lava-infra -l app=provider-simulator

# check configured resource requests/limits
kubectl get deployment provider-simulator -n lava-infra \
  -o jsonpath='{.spec.template.spec.containers[0].resources}' | python3 -m json.tool

# dump full pod spec — verify env vars, volume mounts, probe config
kubectl get pod -n lava-infra -l app=provider-simulator -o yaml
```

---

## Quick reference

| Situation | Command |
|---|---|
| Is it running? | `kubectl get pods -n lava-infra -l app=provider-simulator` |
| See logs | `kubectl logs -n lava-infra -l app=provider-simulator --tail=30` |
| Watch rollout | `kubectl get pods -n lava-infra -l app=provider-simulator -w` |
| Deploy new version | `git pull origin develop && bash scripts/deploy.sh` |
| Force restart | `kubectl rollout restart deployment/provider-simulator -n lava-infra` |
| Pod crashing | `kubectl logs ... --previous` + `kubectl describe pod ...` |
| Verify new code is live | `curl -s https://sim-control.victoria.magmadevs.com/health` |
| Open shell in pod | `kubectl exec -it -n lava-infra <pod-name> -- /bin/sh` |
| Local port-forward | `kubectl port-forward -n lava-infra svc/provider-simulator 19000:19000` |
| Check network route | `kubectl describe httproute -n lava-infra sim-control-httproute` |
| Check service endpoints | `kubectl get endpoints provider-simulator -n lava-infra` |
| Rollback | `kubectl rollout undo deployment/provider-simulator -n lava-infra` |
| Debug scheduling | `kubectl get events -n lava-infra --sort-by='.lastTimestamp'` |
| Check MicroK8s images | `microk8s ctr images list \| grep provider-simulator` |
