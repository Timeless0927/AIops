#!/usr/bin/env bash
set -euo pipefail

export AIOPS_LOKI_MCP_HOST="${AIOPS_LOKI_MCP_HOST:-0.0.0.0}"
export AIOPS_LOKI_MCP_PORT="${AIOPS_LOKI_MCP_PORT:-8084}"

if [[ "$#" -gt 0 ]]; then
  exec "$@"
fi

exec python3 -m apps.mcp_loki.main \
  --host "$AIOPS_LOKI_MCP_HOST" \
  --port "$AIOPS_LOKI_MCP_PORT"
