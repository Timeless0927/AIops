#!/usr/bin/env bash
set -euo pipefail

export AIOPS_GATEWAY_HOST="${AIOPS_GATEWAY_HOST:-0.0.0.0}"
export AIOPS_GATEWAY_PORT="${AIOPS_GATEWAY_PORT:-8080}"
export AIOPS_CONNECTOR_URL="${AIOPS_CONNECTOR_URL:-http://connector:8081}"

if [[ "$#" -gt 0 ]]; then
  exec "$@"
fi

exec python3 -m apps.aiops_k8s_gateway.main \
  --host "$AIOPS_GATEWAY_HOST" \
  --port "$AIOPS_GATEWAY_PORT"
