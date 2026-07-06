#!/usr/bin/env bash
set -euo pipefail

exec /app/deploy/entrypoint-diagnosis.sh "$@"
