#!/usr/bin/env bash
set -euo pipefail

export AIOPS_TOPOLOGY_MCP_HOST="${AIOPS_TOPOLOGY_MCP_HOST:-0.0.0.0}"
export AIOPS_TOPOLOGY_MCP_PORT="${AIOPS_TOPOLOGY_MCP_PORT:-8085}"

if [[ "$#" -gt 0 ]]; then
  exec "$@"
fi

exec python3 -m apps.mcp_topology.main \
  --host "$AIOPS_TOPOLOGY_MCP_HOST" \
  --port "$AIOPS_TOPOLOGY_MCP_PORT"
