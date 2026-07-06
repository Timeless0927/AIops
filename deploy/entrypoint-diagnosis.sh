#!/usr/bin/env bash
set -euo pipefail

export AIOPS_DIAGNOSIS_HOST="${AIOPS_DIAGNOSIS_HOST:-${AIOPS_HERMES_HOST:-0.0.0.0}}"
export AIOPS_DIAGNOSIS_PORT="${AIOPS_DIAGNOSIS_PORT:-${AIOPS_HERMES_PORT:-8082}}"
export AIOPS_GATEWAY_URL="${AIOPS_GATEWAY_URL:-http://gateway:8080}"

if [[ "$#" -gt 0 ]]; then
  exec "$@"
fi

exec python3 -m diagnosis_service.service_main \
  --host "$AIOPS_DIAGNOSIS_HOST" \
  --port "$AIOPS_DIAGNOSIS_PORT"
