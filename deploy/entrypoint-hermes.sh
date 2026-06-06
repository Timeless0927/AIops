#!/usr/bin/env bash
set -euo pipefail

export AIOPS_HERMES_HOST="${AIOPS_HERMES_HOST:-0.0.0.0}"
export AIOPS_HERMES_PORT="${AIOPS_HERMES_PORT:-8082}"
export AIOPS_GATEWAY_URL="${AIOPS_GATEWAY_URL:-http://gateway:8080}"

if [[ "$#" -gt 0 ]]; then
  exec "$@"
fi

if [[ "${AIOPS_HERMES_RUNTIME:-health}" == "gateway" ]]; then
  exec python3 -m runtime.hermes_gateway
fi

exec python3 -m hermes.service_main \
  --host "$AIOPS_HERMES_HOST" \
  --port "$AIOPS_HERMES_PORT"
