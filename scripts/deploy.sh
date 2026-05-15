#!/bin/bash
set -euo pipefail

NAMESPACE="lava-infra"
CONFIG_FILE="${CONFIG_FILE:-config/base-domain.env}"
HTTPROUTE_TEMPLATE="k8s/httproute-control.yml"
# MAG-1780 — GRPCRoute template for the gRPC simulator. Hostname placeholder
# is substituted with $LAVA_SIM_GRPC_HOSTNAME before apply.
GRPCROUTE_TEMPLATE="k8s/grpcroute-lava-sim-grpc.yml"

if [ ! -f "$CONFIG_FILE" ]; then
	echo "Missing config file: $CONFIG_FILE"
	exit 1
fi

source "$CONFIG_FILE"

if [ -z "${BASE_DOMAIN:-}" ]; then
	echo "BASE_DOMAIN must be set in $CONFIG_FILE"
	exit 1
fi

CONTROL_HOSTNAME="sim-control.${BASE_DOMAIN}"
SIM_ROUTER_HOSTNAME="eth-sim-jsonrpc.${BASE_DOMAIN}"
LAVA_SIM_GRPC_HOSTNAME="lava-sim-grpc.${BASE_DOMAIN}"
LAVA_SIM_REST_HOSTNAME="lava-sim-rest.${BASE_DOMAIN}"
RENDERED_HTTPROUTE="$(mktemp)"
RENDERED_GRPCROUTE="$(mktemp)"
RENDERED_REST_HTTPROUTE="$(mktemp)"
HTTPROUTE_REST_TEMPLATE="k8s/httproute-lava-sim-rest.yml"

cleanup() {
	rm -f "$RENDERED_HTTPROUTE" "$RENDERED_GRPCROUTE" "$RENDERED_REST_HTTPROUTE"
}

trap cleanup EXIT

sed "s|__CONTROL_HOSTNAME__|$CONTROL_HOSTNAME|g" "$HTTPROUTE_TEMPLATE" > "$RENDERED_HTTPROUTE"
sed "s|__LAVA_SIM_GRPC_HOSTNAME__|$LAVA_SIM_GRPC_HOSTNAME|g" "$GRPCROUTE_TEMPLATE" > "$RENDERED_GRPCROUTE"
sed "s|__LAVA_SIM_REST_HOSTNAME__|$LAVA_SIM_REST_HOSTNAME|g" "$HTTPROUTE_REST_TEMPLATE" > "$RENDERED_REST_HTTPROUTE"

echo "=== Deployment configuration ==="
echo "Config file         : $CONFIG_FILE"
echo "Base domain         : $BASE_DOMAIN"
echo "Control hostname    : $CONTROL_HOSTNAME"
echo "Simulator router    : $SIM_ROUTER_HOSTNAME"
echo "gRPC sim hostname   : $LAVA_SIM_GRPC_HOSTNAME"
echo "REST sim hostname   : $LAVA_SIM_REST_HOSTNAME"

echo "=== Building Docker image ==="
docker build -t provider-simulator:latest .

echo "=== Importing image into MicroK8s ==="
docker save provider-simulator:latest | microk8s ctr image import -

echo "=== Applying Kubernetes manifests ==="
kubectl apply -f k8s/deployment.yml
kubectl apply -f k8s/service.yml
kubectl apply -f "$RENDERED_HTTPROUTE"
kubectl apply -f "$RENDERED_GRPCROUTE"
kubectl apply -f "$RENDERED_REST_HTTPROUTE"

echo "=== Waiting for pod to be ready ==="
kubectl rollout status deployment/provider-simulator -n "$NAMESPACE" --timeout=60s

echo "=== Restarting pod to pick up new image ==="
# The image tag is always 'latest' — Kubernetes will not replace the running pod
# automatically (imagePullPolicy: IfNotPresent). An explicit rollout restart is
# required so the new image imported above is actually used.
kubectl rollout restart deployment/provider-simulator -n "$NAMESPACE"
kubectl rollout status deployment/provider-simulator -n "$NAMESPACE" --timeout=60s

echo "=== Updating TLS certificate to include new hostname ==="
# This regenerates the TLS cert to include $CONTROL_HOSTNAME and any other HTTPRoute hostnames.
# Run the existing TLS certificate script from smart-router-standalone if available:
#   cd /path/to/smart-router-standalone && bash scripts/install_gateway_api_tls_certificate.sh

echo ""
echo "Provider simulator deployed."
echo "  JSON-RPC providers : ClusterDNS provider-simulator.lava-infra.svc.cluster.local:18545/18546/18547"
echo "  gRPC providers     : ClusterDNS provider-simulator.lava-infra.svc.cluster.local:18548/18549/18550"
echo "  REST sim providers : ClusterDNS provider-simulator.lava-infra.svc.cluster.local:18551/18552/18553"
echo "  Control API        : https://$CONTROL_HOSTNAME"
echo "  Simulator router   : https://$SIM_ROUTER_HOSTNAME"
echo "  gRPC sim ingress   : $LAVA_SIM_GRPC_HOSTNAME:443"
echo "  REST sim ingress   : https://$LAVA_SIM_REST_HOSTNAME"
echo ""
echo "Verify:"
echo "  curl https://$CONTROL_HOSTNAME/health"
echo "  grpcurl -import-path cosmos_pb2 -proto cosmos/base/tendermint/v1beta1/query.proto \\"
echo "    $LAVA_SIM_GRPC_HOSTNAME:443 cosmos.base.tendermint.v1beta1.Service.GetLatestBlock"
echo "  curl https://$LAVA_SIM_REST_HOSTNAME/cosmos/base/tendermint/v1beta1/blocks/latest"

