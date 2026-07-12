#!/bin/sh
set -eu

previous_console_sha=${1:-}
gateway_sha=${2:-}
for sha in "$previous_console_sha" "$gateway_sha"; do
  printf '%s' "$sha" | grep -Eq '^[0-9a-f]{40}$'
  git cat-file -e "${sha}^{commit}"
done

current_sha=$(git rev-parse HEAD)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/previous" "$tmp/current"
git archive "$previous_console_sha" | tar -x -C "$tmp/previous"
git archive "$current_sha" | tar -x -C "$tmp/current"

gateway_spec="$tmp/deployed-gateway-v1.json"
current_spec="$tmp/current-gateway-v1.json"
git show "${gateway_sha}:api/openapi/gateway-v1.json" > "$gateway_spec"
cp "$tmp/current/api/openapi/gateway-v1.json" "$current_spec"

cd "$tmp/current/apps/aiops_console_web"
npm ci
cp "$gateway_spec" "$tmp/current/api/openapi/gateway-v1.json"
npm run generate:api
npm run build

cd "$tmp/previous/apps/aiops_console_web"
npm ci
cp "$current_spec" "$tmp/previous/api/openapi/gateway-v1.json"
npm run generate:api
npm run build
