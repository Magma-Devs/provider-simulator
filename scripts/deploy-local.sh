#!/usr/bin/env bash
set -euo pipefail

# Local wrapper for server-specific deploys.
# Uses config/base-domain.local.env so config/base-domain.env can stay unchanged in git.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCAL_CONFIG="$REPO_ROOT/config/base-domain.local.env"

if [ ! -f "$LOCAL_CONFIG" ]; then
  cat <<EOF
Missing local config: $LOCAL_CONFIG

Create it once per server, for example:
  cat > $LOCAL_CONFIG <<'EOC'
  BASE_DOMAIN="nadav.magmadevs.com"
  EOC

Then run:
  bash scripts/deploy-local.sh
EOF
  exit 1
fi

# shellcheck disable=SC1090
source "$LOCAL_CONFIG"

if [ -z "${BASE_DOMAIN:-}" ]; then
  echo "BASE_DOMAIN must be set in $LOCAL_CONFIG"
  exit 1
fi

echo "Using BASE_DOMAIN from local config: $BASE_DOMAIN"
CONFIG_FILE="$LOCAL_CONFIG" bash "$REPO_ROOT/scripts/deploy.sh"

