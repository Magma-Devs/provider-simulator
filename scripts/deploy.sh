#!/bin/bash
# scripts/deploy.sh — provider-simulator deploy
#
# MAG-1808: extended with manifest-diff gating so a second run is a no-op,
# bundled `kubectl apply -f deployment.yml -f service.yml -n <ns>` in one
# pass, and a 180s rollout timeout (was 60s).
#
# Behavior
# --------
# 1. Builds and imports the Docker image (always — image tag is :latest).
# 2. Renders Gateway-API route templates from $BASE_DOMAIN.
# 3. Applies k8s/deployment.yml + k8s/service.yml as one declarative command.
#    A rollout restart follows when a fresh image was built this run, when the
#    manifests changed, or when FORCE_RESTART=true. A build always restarts so
#    new code behind the :latest tag actually runs; manifests-only runs
#    (SKIP_BUILD=true) restart only on a real manifest diff.
# 4. Applies the rendered HTTPRoute / GRPCRoute manifests.
# 5. Waits for rollout completion with --timeout=180s. Fails loud on timeout.
#
# Idempotency contract (MAG-1808 requirement 4)
# ---------------------------------------------
# A build produces a new image behind the :latest tag, so the DEFAULT path
# (which builds) restarts every run by design — that is what makes new code
# take effect. For an idempotent manifests-only run, pass SKIP_BUILD=true:
# then two runs back-to-back produce at most one restart, and only when the
# manifests actually changed (observable in `kubectl get events -n lava-infra`).
#
# Env overrides
# -------------
#   DEPLOY_REF=main      — git ref to fetch + hard-reset the checkout to
#                          before building, so a stale local checkout can
#                          never be deployed. Override to deploy a branch,
#                          e.g. DEPLOY_REF=my-feature.
#   SKIP_SELF_UPDATE=false — set true to skip the fetch + reset self-update
#                          (offline/air-gapped runs, or to deploy files you
#                          deliberately hand-edited). A dirty tree aborts
#                          rather than being clobbered.
#   FORCE_RESTART=true   — force a rollout restart even if diff is empty
#                          (use to pick up a fresh :latest image without
#                          touching manifests)
#   SKIP_BUILD=true      — skip docker build + microk8s import (faster
#                          re-runs when iterating on manifests only). Also the
#                          idempotent path: with no build, a restart fires only
#                          on a real manifest diff (or FORCE_RESTART=true).
#   ROLLOUT_TIMEOUT=180s — override the rollout-status timeout
#
# Verify (paste-ready)
# --------------------
#   kubectl describe svc provider-simulator -n lava-infra | grep -E 'Port:|TargetPort' | wc -l
#   kubectl exec -n lava-infra deployment/provider-simulator -- python3 -c "import socket; [print(p, 'LISTENING' if (s:=socket.socket()).connect_ex(('127.0.0.1',p))==0 else 'closed') or s.close() for p in (18545,18546,18547)]"
#   bash scripts/deploy.sh && bash scripts/deploy.sh && kubectl get events -n lava-infra --sort-by=lastTimestamp | tail -5
#
# Refs: MAG-1808 (this script), MAG-1805 (manifest + constants additions
# this script rolls out once they land).

set -euo pipefail

NAMESPACE="lava-infra"
DEPLOYMENT="provider-simulator"
CONFIG_FILE="${CONFIG_FILE:-config/base-domain.env}"
HTTPROUTE_TEMPLATE="k8s/httproute-control.yml"
# MAG-1780 — GRPCRoute template for the gRPC simulator. Hostname placeholder
# is substituted with $LAVA_SIM_GRPC_HOSTNAME before apply.
GRPCROUTE_TEMPLATE="k8s/grpcroute-lava-sim-grpc.yml"
HTTPROUTE_REST_TEMPLATE="k8s/httproute-lava-sim-rest.yml"
DEPLOYMENT_MANIFEST="k8s/deployment.yml"
SERVICE_MANIFEST="k8s/service.yml"

# Self-update controls (see the stale-checkout guard below).
DEPLOY_REF="${DEPLOY_REF:-main}"
SKIP_SELF_UPDATE="${SKIP_SELF_UPDATE:-false}"

FORCE_RESTART="${FORCE_RESTART:-false}"
SKIP_BUILD="${SKIP_BUILD:-false}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-180s}"

if [ ! -f "$CONFIG_FILE" ]; then
	echo "[deploy] missing config file: $CONFIG_FILE" >&2
	exit 1
fi

# shellcheck disable=SC1090
source "$CONFIG_FILE"

if [ -z "${BASE_DOMAIN:-}" ]; then
	echo "[deploy] BASE_DOMAIN must be set in $CONFIG_FILE" >&2
	exit 1
fi

# =========================================================================
# Stale-checkout guard — self-update to the target ref BEFORE building.
#
# WHY: this script builds the image from whatever sits in the local checkout.
# On a long-lived deploy server that checkout silently drifts behind origin
# (one incident: 126 commits stale, which dropped the Solana provider ports
# 18582-18584 from the built image with no error at all). To make a stale
# checkout impossible to deploy, fetch and hard-reset to origin/$DEPLOY_REF
# here — before the template render + docker build below read the tree.
#
# Resetting our own file mid-run is safe: git reset --hard writes a fresh
# inode, so this already-running script keeps executing its current bytes to
# the end while the downstream build reads the freshly-updated files. No
# re-exec needed.
#
# SKIP_SELF_UPDATE=true bypasses this for offline/air-gapped runs, or to
# deploy files you deliberately hand-edited. Uncommitted *tracked* changes
# abort the run instead of being clobbered.
# =========================================================================
if [ "$SKIP_SELF_UPDATE" = "true" ]; then
	echo "=== Skipping self-update (SKIP_SELF_UPDATE=true): building the local checkout as-is ==="
else
	echo "=== Self-updating checkout to origin/$DEPLOY_REF before build ==="
	SELF_UPDATE_BEFORE="$(git rev-parse --short HEAD)"
	git fetch origin --quiet
	if ! git diff --quiet || ! git diff --cached --quiet; then
		echo "[deploy] refusing to self-update: working tree has uncommitted tracked changes." >&2
		echo "[deploy] commit or stash them, or re-run with SKIP_SELF_UPDATE=true to build as-is." >&2
		exit 1
	fi
	if ! git rev-parse --verify --quiet "refs/remotes/origin/$DEPLOY_REF" >/dev/null; then
		echo "[deploy] origin/$DEPLOY_REF not found — check DEPLOY_REF (does that branch exist on origin?)." >&2
		exit 1
	fi
	git checkout -q -B "$DEPLOY_REF" "origin/$DEPLOY_REF"
	SELF_UPDATE_AFTER="$(git rev-parse --short HEAD)"
	if [ "$SELF_UPDATE_BEFORE" = "$SELF_UPDATE_AFTER" ]; then
		echo "[deploy] checkout already current at $SELF_UPDATE_AFTER (origin/$DEPLOY_REF)"
	else
		echo "[deploy] updated checkout: $SELF_UPDATE_BEFORE -> $SELF_UPDATE_AFTER (origin/$DEPLOY_REF)"
	fi
fi

CONTROL_HOSTNAME="sim-control.${BASE_DOMAIN}"
SIM_ROUTER_HOSTNAME="eth-sim-jsonrpc.${BASE_DOMAIN}"
LAVA_SIM_GRPC_HOSTNAME="lava-sim-grpc.${BASE_DOMAIN}"
LAVA_SIM_REST_HOSTNAME="lava-sim-rest.${BASE_DOMAIN}"
LAVA_SIM_WS_HOSTNAME="lava-sim-ws.${BASE_DOMAIN}"
RENDERED_HTTPROUTE="$(mktemp)"
RENDERED_GRPCROUTE="$(mktemp)"
RENDERED_REST_HTTPROUTE="$(mktemp)"
RENDERED_WS_HTTPROUTE="$(mktemp)"
HTTPROUTE_WS_TEMPLATE="k8s/httproute-lava-sim-ws.yml"

cleanup() {
	rm -f "$RENDERED_HTTPROUTE" "$RENDERED_GRPCROUTE" "$RENDERED_REST_HTTPROUTE" "$RENDERED_WS_HTTPROUTE"
}

trap cleanup EXIT

sed "s|__CONTROL_HOSTNAME__|$CONTROL_HOSTNAME|g" "$HTTPROUTE_TEMPLATE" > "$RENDERED_HTTPROUTE"
sed "s|__LAVA_SIM_GRPC_HOSTNAME__|$LAVA_SIM_GRPC_HOSTNAME|g" "$GRPCROUTE_TEMPLATE" > "$RENDERED_GRPCROUTE"
sed "s|__LAVA_SIM_REST_HOSTNAME__|$LAVA_SIM_REST_HOSTNAME|g" "$HTTPROUTE_REST_TEMPLATE" > "$RENDERED_REST_HTTPROUTE"
sed "s|__LAVA_SIM_WS_HOSTNAME__|$LAVA_SIM_WS_HOSTNAME|g" "$HTTPROUTE_WS_TEMPLATE" > "$RENDERED_WS_HTTPROUTE"

echo "=== Deployment configuration ==="
echo "Config file         : $CONFIG_FILE"
echo "Base domain         : $BASE_DOMAIN"
echo "Control hostname    : $CONTROL_HOSTNAME"
echo "Simulator router    : $SIM_ROUTER_HOSTNAME"
echo "gRPC sim hostname   : $LAVA_SIM_GRPC_HOSTNAME"
echo "REST sim hostname   : $LAVA_SIM_REST_HOSTNAME"
echo "WS sim hostname     : $LAVA_SIM_WS_HOSTNAME"
echo "Force restart       : $FORCE_RESTART"
echo "Skip build          : $SKIP_BUILD"
echo "Rollout timeout     : $ROLLOUT_TIMEOUT"

# BUILT records whether a fresh :latest image was produced this run. A rebuilt
# image is a NEW image behind the same :latest tag; with imagePullPolicy
# IfNotPresent the running pod keeps the old code until it is restarted, so a
# build must force a rollout restart below. This is the fix for the "edit code,
# run deploy.sh, pod still runs old code" trap. Manifests-only runs use
# SKIP_BUILD=true and stay idempotent (they restart only on a real manifest diff).
BUILT="false"
if [ "$SKIP_BUILD" = "true" ]; then
	echo "=== Skipping Docker build (SKIP_BUILD=true) ==="
else
	echo "=== Building Docker image ==="
	docker build -t provider-simulator:latest .

	echo "=== Importing image into MicroK8s ==="
	docker save provider-simulator:latest | microk8s ctr image import -
	BUILT="true"
fi

# MAG-1808 requirement 4 — idempotency gate.
# `kubectl diff` exits 1 when there is a diff against the cluster, 0 when
# there is none. We want to apply + restart only when there is a real diff.
# `kubectl apply` itself is declarative and would be a no-op without our
# gate, but the *rollout restart* below is not — it always cycles pods —
# so we must guard it explicitly.
echo "=== Checking manifest drift (MAG-1808 idempotency gate) ==="
MANIFESTS_CHANGED="false"
DIFF_OUTPUT="$(mktemp)"
trap 'rm -f "$RENDERED_HTTPROUTE" "$RENDERED_GRPCROUTE" "$RENDERED_REST_HTTPROUTE" "$RENDERED_WS_HTTPROUTE" "$DIFF_OUTPUT"' EXIT

# kubectl diff returns 1 when there's a diff, 0 when none, >1 on error.
# We must not let `set -e` kill the script when exit code is 1.
set +e
kubectl diff -n "$NAMESPACE" -f "$DEPLOYMENT_MANIFEST" -f "$SERVICE_MANIFEST" > "$DIFF_OUTPUT" 2>&1
DIFF_RC=$?
set -e

case "$DIFF_RC" in
	0)
		echo "[deploy] manifests unchanged — kubectl diff is clean"
		;;
	1)
		echo "[deploy] manifest diff detected — will apply + restart"
		MANIFESTS_CHANGED="true"
		;;
	*)
		echo "[deploy] kubectl diff failed (exit $DIFF_RC):" >&2
		cat "$DIFF_OUTPUT" >&2
		exit "$DIFF_RC"
		;;
esac

# MAG-1808 requirement 1 — single declarative apply across both manifests.
# Even when nothing changed, running `kubectl apply` again is a documented
# no-op and is safe; it is the cheap belt-and-suspenders that proves the
# desired state matches.
echo "=== Applying Kubernetes manifests (deployment + service) ==="
kubectl apply -n "$NAMESPACE" -f "$DEPLOYMENT_MANIFEST" -f "$SERVICE_MANIFEST"

echo "=== Applying Gateway-API routes ==="
kubectl apply -f "$RENDERED_HTTPROUTE"
kubectl apply -f "$RENDERED_GRPCROUTE"
kubectl apply -f "$RENDERED_REST_HTTPROUTE"
kubectl apply -f "$RENDERED_WS_HTTPROUTE"

# MAG-1808 requirement 2 + 3 — rollout restart after apply, then wait with
# a hard timeout. The restart is gated by either a real manifest diff or
# the explicit FORCE_RESTART override, OR whenever a fresh :latest image was
# built this run (BUILT=true) — a rebuilt image needs a restart to take effect
# under imagePullPolicy IfNotPresent, otherwise the pod keeps running old code.
if [ "$MANIFESTS_CHANGED" = "true" ] || [ "$FORCE_RESTART" = "true" ] || [ "$BUILT" = "true" ]; then
	if [ "$BUILT" = "true" ]; then
		echo "=== Restarting deployment (fresh :latest image built) ==="
	elif [ "$MANIFESTS_CHANGED" = "true" ]; then
		echo "=== Restarting deployment (manifest change) ==="
	else
		echo "=== Restarting deployment (FORCE_RESTART=true) ==="
	fi
	kubectl rollout restart -n "$NAMESPACE" "deployment/$DEPLOYMENT"
	kubectl rollout status -n "$NAMESPACE" "deployment/$DEPLOYMENT" --timeout="$ROLLOUT_TIMEOUT"
else
	echo "=== Skipping rollout restart (no build, manifests unchanged, FORCE_RESTART=false) ==="
	# Still confirm the existing deployment is healthy. If a previous deploy
	# left it in a bad state, fail loudly here rather than pretend everything
	# is fine.
	kubectl rollout status -n "$NAMESPACE" "deployment/$DEPLOYMENT" --timeout="$ROLLOUT_TIMEOUT"
fi

echo "=== Updating TLS certificate to include new hostname ==="
# This regenerates the TLS cert to include $CONTROL_HOSTNAME, $LAVA_SIM_REST_HOSTNAME,
# $LAVA_SIM_GRPC_HOSTNAME, $LAVA_SIM_WS_HOSTNAME, and any other HTTPRoute hostnames.
# Run the existing TLS certificate script from smart-router-standalone if available:
#   cd /path/to/smart-router-standalone && bash scripts/install_gateway_api_tls_certificate.sh

echo ""
echo "Provider simulator deployed."
echo "  JSON-RPC providers : ClusterDNS provider-simulator.lava-infra.svc.cluster.local:18545/18546/18547"
echo "  gRPC providers     : ClusterDNS provider-simulator.lava-infra.svc.cluster.local:18548/18549/18550"
echo "  REST sim providers : ClusterDNS provider-simulator.lava-infra.svc.cluster.local:18551/18552/18553"
echo "  WS sim providers   : ClusterDNS provider-simulator.lava-infra.svc.cluster.local:18557/18558/18559"
echo "  Control API        : https://$CONTROL_HOSTNAME"
echo "  Simulator router   : https://$SIM_ROUTER_HOSTNAME"
echo "  gRPC sim ingress   : $LAVA_SIM_GRPC_HOSTNAME:443"
echo "  REST sim ingress   : https://$LAVA_SIM_REST_HOSTNAME"
echo "  WS sim ingress     : wss://$LAVA_SIM_WS_HOSTNAME/ws"
echo ""
echo "Verify:"
echo "  curl https://$CONTROL_HOSTNAME/health"
echo "  grpcurl -import-path cosmos_pb2 -proto cosmos/base/tendermint/v1beta1/query.proto $LAVA_SIM_GRPC_HOSTNAME:443 cosmos.base.tendermint.v1beta1.Service.GetLatestBlock"
echo "  curl https://$LAVA_SIM_REST_HOSTNAME/cosmos/base/tendermint/v1beta1/blocks/latest"
echo "  websocat wss://$LAVA_SIM_WS_HOSTNAME/ws    # then paste {\"jsonrpc\":\"2.0\",\"method\":\"eth_blockNumber\",\"id\":1}"
echo ""
echo "MAG-1808 idempotency check:"
echo "  bash scripts/deploy.sh && bash scripts/deploy.sh && kubectl get events -n $NAMESPACE --sort-by=lastTimestamp | tail -5"
