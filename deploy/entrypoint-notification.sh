#!/usr/bin/env bash
set -euo pipefail

export AIOPS_NOTIFICATION_HOST="${AIOPS_NOTIFICATION_HOST:-0.0.0.0}"
export AIOPS_NOTIFICATION_PORT="${AIOPS_NOTIFICATION_PORT:-8086}"

if [[ "$#" -gt 0 ]]; then
  exec "$@"
fi

exec python3 -m notification_service \
  --host "$AIOPS_NOTIFICATION_HOST" \
  --port "$AIOPS_NOTIFICATION_PORT"
