#!/bin/sh
set -eu

source_sha=${1:-}
digest=${2:-}

printf '%s' "$source_sha" | grep -Eq '^[0-9a-f]{40}$'
printf '%s' "$digest" | grep -Eq '^sha256:[0-9a-f]{64}$'
jq -e --arg sha "$source_sha" '."org.opencontainers.image.revision" == $sha' >/dev/null
