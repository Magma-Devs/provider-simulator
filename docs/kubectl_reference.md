# kubectl Reference — Provider Simulator

**Namespace:** `lava-infra`  
**Deployment:** `provider-simulator`

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
```

---

## Deploy

```bash
# full deploy from scratch (build → import → apply → restart)
cd ~/provider-simulator && git pull origin develop && bash scripts/deploy.sh

# force restart without rebuilding (picks up already-imported image)
kubectl rollout restart deployment/provider-simulator -n lava-infra

# wait for rollout to finish
kubectl rollout status deployment/provider-simulator -n lava-infra --timeout=60s
```

---

## Verify new image is running

```bash
# shows image SHA — compare with what docker build produced
kubectl describe pod -n lava-infra -l app=provider-simulator | grep Image

# hit health endpoint — quickest sanity check
curl -s https://sim-control.victoria.magmadevs.com/health
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
```

---

## Quick reference

| Situation | Command |
|---|---|
| Is it running? | `kubectl get pods -n lava-infra -l app=provider-simulator` |
| See logs | `kubectl logs -n lava-infra -l app=provider-simulator --tail=30` |
| Waiting for termination | `kubectl get pods -n lava-infra -l app=provider-simulator -w` |
| Deploy new version | `git pull origin develop && bash scripts/deploy.sh` |
| Force restart | `kubectl rollout restart deployment/provider-simulator -n lava-infra` |
| Pod crashing | `kubectl logs ... --previous` + `kubectl describe pod ...` |
| Verify new code is live | `curl -s https://sim-control.victoria.magmadevs.com/health` |

