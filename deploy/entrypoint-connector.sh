#!/usr/bin/env bash
set -euo pipefail

export AIOPS_CONNECTOR_HOST="${AIOPS_CONNECTOR_HOST:-0.0.0.0}"
export AIOPS_CONNECTOR_PORT="${AIOPS_CONNECTOR_PORT:-8081}"
export AIOPS_CONNECTOR_ID="${AIOPS_CONNECTOR_ID:-connector-local}"
export AIOPS_CLUSTER_ID="${AIOPS_CLUSTER_ID:-cluster-local}"
export AIOPS_NAMESPACE_SCOPE="${AIOPS_NAMESPACE_SCOPE:-default}"
export AIOPS_CONNECTOR_CAPABILITIES="${AIOPS_CONNECTOR_CAPABILITIES:-health,validate}"
export AIOPS_GATEWAY_URL="${AIOPS_GATEWAY_URL:-}"

if [[ "$#" -gt 0 ]]; then
  exec "$@"
fi

exec python3 -m apps.cluster_connector.main \
  --host "$AIOPS_CONNECTOR_HOST" \
  --port "$AIOPS_CONNECTOR_PORT"
