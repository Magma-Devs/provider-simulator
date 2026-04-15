#!/usr/bin/env bash
set -euo pipefail

# Experimental only.
# This script wraps the existing simulator deploy flow and can also orchestrate the
# smart-router-standalone side: base_domain check/sync, values_sim.yml copy,
# helm upgrade, TLS refresh, and smoke checks.
#
# It is intentionally additive and does NOT replace scripts/deploy.sh.

usage() {
  cat <<'EOF'
Usage:
  bash scripts/deploy_experimental_full.sh [options]

Options:
  --smart-router-dir PATH   Path to smart-router-standalone
                            (default: ~/smart-router-standalone)
  --sync-base-domain        If provider-simulator/config/base-domain.env does not
                            match smart-router-standalone/values/core/values.yml,
                            update it automatically.
  --skip-simulator          Skip running scripts/deploy.sh
  --skip-router             Skip copying values_sim.yml, Helm upgrade, and TLS
  --skip-tls                Skip TLS refresh after Helm upgrade
  --skip-verify             Skip curl smoke checks at the end
  --dry-run                 Print actions without executing them
  -h, --help                Show this help text

Examples:
  bash scripts/deploy_experimental_full.sh --sync-base-domain
  bash scripts/deploy_experimental_full.sh --smart-router-dir /root/smart-router-standalone
  bash scripts/deploy_experimental_full.sh --skip-router
EOF
}

log() {
  printf '[experimental-deploy] %s\n' "$*"
}

fail() {
  printf '[experimental-deploy] ERROR: %s\n' "$*" >&2
  exit 1
}

run() {
  log "+ $*"
  if [ "$DRY_RUN" -eq 0 ]; then
    "$@"
  fi
}

run_in_repo() {
  log "+ (cd $1 && ${*:2})"
  if [ "$DRY_RUN" -eq 0 ]; then
    (
      cd "$1"
      "${@:2}"
    )
  fi
}

require_file() {
  [ -f "$1" ] || fail "Missing file: $1"
}

require_dir() {
  [ -d "$1" ] || fail "Missing directory: $1"
}

extract_router_base_domain() {
  local value
  value="$(sed -nE 's/^[[:space:]]*base_domain[[:space:]]*:[[:space:]]*"?([^"[:space:]]+)"?[[:space:]]*$/\1/p' "$ROUTER_CORE_VALUES" | head -1)"
  [ -n "$value" ] || fail "Could not extract base_domain from $ROUTER_CORE_VALUES"
  printf '%s\n' "$value"
}

extract_local_base_domain() {
  require_file "$CONFIG_FILE"
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  [ -n "${BASE_DOMAIN:-}" ] || fail "BASE_DOMAIN is empty in $CONFIG_FILE"
  printf '%s\n' "$BASE_DOMAIN"
}

update_local_base_domain() {
  local new_domain="$1"

  if [ "$DRY_RUN" -eq 1 ]; then
    log "Would update $CONFIG_FILE to BASE_DOMAIN=\"$new_domain\""
    return 0
  fi

  python3 - "$CONFIG_FILE" "$new_domain" <<'PY'
from pathlib import Path
import sys

config_path = Path(sys.argv[1])
new_domain = sys.argv[2]
lines = config_path.read_text().splitlines()
updated = False
new_lines = []
for line in lines:
    if line.startswith("BASE_DOMAIN="):
        new_lines.append(f'BASE_DOMAIN="{new_domain}"')
        updated = True
    else:
        new_lines.append(line)
if not updated:
    if new_lines and new_lines[-1] != "":
        new_lines.append("")
    new_lines.append(f'BASE_DOMAIN="{new_domain}"')
config_path.write_text("\n".join(new_lines) + "\n")
PY
}

copy_values_sim() {
  run mkdir -p "$ROUTER_SIM_VALUES_DIR"
  run cp "$VALUES_SIM_SOURCE" "$ROUTER_SIM_VALUES"
  log "Copied $VALUES_SIM_SOURCE -> $ROUTER_SIM_VALUES"
}

run_router_upgrade() {
  require_file "$ROUTER_COMMON_SH"

  log "Running Helm upgrade in $SMART_ROUTER_DIR"
  if [ "$DRY_RUN" -eq 1 ]; then
    cat <<EOF
[experimental-deploy] + (cd $SMART_ROUTER_DIR && source scripts/utils/common.sh && \
  echo "\$HELM_REGISTRY_TOKEN" | helm registry login ghcr.io --username "\$HELM_REGISTRY_USERNAME" --password-stdin && \
  helm upgrade smart-router \
    "oci://ghcr.io/magma-devs/smart-router-helm-chart/smart-router" \
    --namespace lava-infra \
    --version "\$HELM_CHART_VERSION" \
    --values values/core/values.yml \
    --values values/simulator/values_sim.yml \
    --wait --timeout 5m)
EOF
    return 0
  fi

  (
    cd "$SMART_ROUTER_DIR"
    # shellcheck disable=SC1090
    source "$ROUTER_COMMON_SH"

    [ -n "${HELM_REGISTRY_USERNAME:-}" ] || fail "HELM_REGISTRY_USERNAME is missing after sourcing $ROUTER_COMMON_SH"
    [ -n "${HELM_REGISTRY_TOKEN:-}" ] || fail "HELM_REGISTRY_TOKEN is missing after sourcing $ROUTER_COMMON_SH"
    [ -n "${HELM_CHART_VERSION:-}" ] || fail "HELM_CHART_VERSION is missing after sourcing $ROUTER_COMMON_SH"

    printf '%s' "$HELM_REGISTRY_TOKEN" | helm registry login ghcr.io \
      --username "$HELM_REGISTRY_USERNAME" --password-stdin

    helm upgrade smart-router \
      "oci://ghcr.io/magma-devs/smart-router-helm-chart/smart-router" \
      --namespace lava-infra \
      --version "$HELM_CHART_VERSION" \
      --values values/core/values.yml \
      --values values/simulator/values_sim.yml \
      --wait --timeout 5m
  )
}

run_tls_refresh() {
  require_file "$ROUTER_TLS_SCRIPT"
  run_in_repo "$SMART_ROUTER_DIR" bash scripts/install_gateway_api_tls_certificate.sh
}

run_smoke_checks() {
  local domain="$1"

  log "Running smoke checks against $domain"
  if [ "$DRY_RUN" -eq 1 ]; then
    cat <<EOF
[experimental-deploy] + curl -fsS https://sim-control.$domain/health
[experimental-deploy] + curl -fsS -X POST https://eth-sim-jsonrpc.$domain -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'
EOF
    return 0
  fi

  curl -fsS "https://sim-control.$domain/health"
  echo

  curl -fsS -X POST "https://eth-sim-jsonrpc.$domain" \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'
  echo
}

SYNC_BASE_DOMAIN=0
SKIP_SIMULATOR=0
SKIP_ROUTER=0
SKIP_TLS=0
SKIP_VERIFY=0
DRY_RUN=0
SMART_ROUTER_DIR="${SMART_ROUTER_DIR:-$HOME/smart-router-standalone}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --smart-router-dir)
      [ "$#" -ge 2 ] || fail "--smart-router-dir requires a path"
      SMART_ROUTER_DIR="$2"
      shift 2
      ;;
    --sync-base-domain)
      SYNC_BASE_DOMAIN=1
      shift
      ;;
    --skip-simulator)
      SKIP_SIMULATOR=1
      shift
      ;;
    --skip-router)
      SKIP_ROUTER=1
      shift
      ;;
    --skip-tls)
      SKIP_TLS=1
      shift
      ;;
    --skip-verify)
      SKIP_VERIFY=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$REPO_ROOT/config/base-domain.env"
VALUES_SIM_SOURCE="$REPO_ROOT/config/values_sim.yml"
SIM_DEPLOY_SCRIPT="$REPO_ROOT/scripts/deploy.sh"
ROUTER_CORE_VALUES="$SMART_ROUTER_DIR/values/core/values.yml"
ROUTER_SIM_VALUES_DIR="$SMART_ROUTER_DIR/values/simulator"
ROUTER_SIM_VALUES="$ROUTER_SIM_VALUES_DIR/values_sim.yml"
ROUTER_COMMON_SH="$SMART_ROUTER_DIR/scripts/utils/common.sh"
ROUTER_TLS_SCRIPT="$SMART_ROUTER_DIR/scripts/install_gateway_api_tls_certificate.sh"

log "Experimental deploy wrapper — optional, not official"
log "Repo root           : $REPO_ROOT"
log "Smart router dir    : $SMART_ROUTER_DIR"
log "Dry run             : $DRY_RUN"

require_file "$CONFIG_FILE"
require_file "$VALUES_SIM_SOURCE"
require_file "$SIM_DEPLOY_SCRIPT"

LOCAL_BASE_DOMAIN="$(extract_local_base_domain)"
TARGET_BASE_DOMAIN="$LOCAL_BASE_DOMAIN"

if [ "$SKIP_ROUTER" -eq 0 ]; then
  require_dir "$SMART_ROUTER_DIR"
  require_file "$ROUTER_CORE_VALUES"

  ROUTER_BASE_DOMAIN="$(extract_router_base_domain)"
  log "Router base_domain  : $ROUTER_BASE_DOMAIN"
  log "Local BASE_DOMAIN   : $LOCAL_BASE_DOMAIN"

  if [ "$LOCAL_BASE_DOMAIN" != "$ROUTER_BASE_DOMAIN" ]; then
    if [ "$SYNC_BASE_DOMAIN" -eq 1 ]; then
      log "BASE_DOMAIN mismatch detected — syncing $CONFIG_FILE to $ROUTER_BASE_DOMAIN"
      update_local_base_domain "$ROUTER_BASE_DOMAIN"
      TARGET_BASE_DOMAIN="$ROUTER_BASE_DOMAIN"
    else
      fail "BASE_DOMAIN mismatch: local=$LOCAL_BASE_DOMAIN router=$ROUTER_BASE_DOMAIN (re-run with --sync-base-domain to auto-fix)"
    fi
  fi

  copy_values_sim
fi

if [ "$SKIP_SIMULATOR" -eq 0 ]; then
  run_in_repo "$REPO_ROOT" bash scripts/deploy.sh
fi

if [ "$SKIP_ROUTER" -eq 0 ]; then
  run_router_upgrade

  if [ "$SKIP_TLS" -eq 0 ]; then
    run_tls_refresh
  else
    log "Skipping TLS refresh"
  fi
fi

if [ "$SKIP_VERIFY" -eq 0 ]; then
  run_smoke_checks "$TARGET_BASE_DOMAIN"
else
  log "Skipping smoke checks"
fi

log "Done"

