#!/usr/bin/env bash
set -euo pipefail

export AIOPS_PROMETHEUS_MCP_HOST="${AIOPS_PROMETHEUS_MCP_HOST:-0.0.0.0}"
export AIOPS_PROMETHEUS_MCP_PORT="${AIOPS_PROMETHEUS_MCP_PORT:-8083}"

if [[ "$#" -gt 0 ]]; then
  exec "$@"
fi

exec python3 -m apps.mcp_prometheus.main \
  --host "$AIOPS_PROMETHEUS_MCP_HOST" \
  --port "$AIOPS_PROMETHEUS_MCP_PORT"
