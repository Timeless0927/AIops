#!/usr/bin/env bash
set -euo pipefail

export HOME="${HOME:-/root}"
export AIOPS_DATA_DIR="${AIOPS_DATA_DIR:-/data/aiops}"
export AIOPS_MODEL_NAME="${AIOPS_MODEL_NAME:-gpt-5.4}"
export AIOPS_MODEL_PROVIDER="${AIOPS_MODEL_PROVIDER:-custom}"
export AIOPS_AGENT_MAX_TURNS="${AIOPS_AGENT_MAX_TURNS:-90}"
export AIOPS_WEBHOOK_HOST="${AIOPS_WEBHOOK_HOST:-0.0.0.0}"
export AIOPS_WEBHOOK_PORT="${AIOPS_WEBHOOK_PORT:-8765}"
export FEISHU_GROUP_POLICY="${FEISHU_GROUP_POLICY:-open}"
export FEISHU_ALLOWED_USERS="${FEISHU_ALLOWED_USERS:-}"
export AIOPS_APPROVAL_ALLOW_SELF_APPROVAL_LOW_RISK="${AIOPS_APPROVAL_ALLOW_SELF_APPROVAL_LOW_RISK:-false}"
export AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_EXEC="${AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_EXEC:-true}"
export AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_DANGEROUS="${AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_DANGEROUS:-true}"
if [[ -n "${HERMES_CONFIG:-}" && -z "${HERMES_HOME:-}" ]]; then
  export HERMES_HOME="$(dirname "$HERMES_CONFIG")"
fi
export HERMES_HOME="${HERMES_HOME:-/data/hermes}"
export HERMES_CONFIG="${HERMES_CONFIG:-${HERMES_HOME}/config.yaml}"

required_bins=(python3 kubectl hermes)
for bin_name in "${required_bins[@]}"; do
  if ! command -v "$bin_name" >/dev/null 2>&1; then
    echo "missing required binary: $bin_name" >&2
    exit 1
  fi
done

required_envs=(
  FEISHU_APP_ID
  FEISHU_APP_SECRET
  FEISHU_MAIN_CHAT_ID
  AIOPS_MODEL_BASE_URL
  AIOPS_MODEL_API_KEY
  AIOPS_SRE_ADMIN_NAME
  AIOPS_SRE_ADMIN_OPEN_ID
  AIOPS_SRE_OPERATOR_NAME
  AIOPS_SRE_OPERATOR_OPEN_ID
)
for env_name in "${required_envs[@]}"; do
  if [[ -z "${!env_name:-}" ]]; then
    echo "missing required env: $env_name" >&2
    exit 1
  fi
done

mkdir -p "$AIOPS_DATA_DIR" "$HERMES_HOME"

python3 - <<'PY'
import os
from pathlib import Path

template = Path("deploy/hermes-config.template.yaml").read_text(encoding="utf-8")
keys = [
    "AIOPS_MODEL_NAME",
    "AIOPS_MODEL_PROVIDER",
    "AIOPS_MODEL_BASE_URL",
    "AIOPS_MODEL_API_KEY",
    "AIOPS_AGENT_MAX_TURNS",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_VERIFICATION_TOKEN",
    "FEISHU_ENCRYPT_KEY",
    "FEISHU_MAIN_CHAT_ID",
    "FEISHU_GROUP_POLICY",
    "AIOPS_SRE_ADMIN_NAME",
    "AIOPS_SRE_ADMIN_OPEN_ID",
    "AIOPS_SRE_OPERATOR_NAME",
    "AIOPS_SRE_OPERATOR_OPEN_ID",
    "AIOPS_APPROVAL_ALLOW_SELF_APPROVAL_LOW_RISK",
    "AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_EXEC",
    "AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_DANGEROUS",
]
for key in keys:
    template = template.replace("${" + key + "}", os.getenv(key, ""))

config_path = Path(os.environ["HERMES_CONFIG"])
config_path.parent.mkdir(parents=True, exist_ok=True)
config_path.write_text(template, encoding="utf-8")
PY

if [[ "${AIOPS_WEBHOOK_ONLY:-0}" == "1" ]]; then
  exec python3 -m hooks.alert_webhook_server --host "$AIOPS_WEBHOOK_HOST" --port "$AIOPS_WEBHOOK_PORT"
fi

python3 -m hooks.alert_webhook_server --host "$AIOPS_WEBHOOK_HOST" --port "$AIOPS_WEBHOOK_PORT" &
webhook_pid=$!

python3 -m runtime.hermes_gateway &
gateway_pid=$!

term_handler() {
  kill "$webhook_pid" "$gateway_pid" 2>/dev/null || true
}

trap term_handler TERM INT
wait -n "$webhook_pid" "$gateway_pid"
