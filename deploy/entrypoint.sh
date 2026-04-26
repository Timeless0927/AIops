#!/usr/bin/env bash
set -euo pipefail

export HOME="${HOME:-/root}"
export AIOPS_DATA_DIR="${AIOPS_DATA_DIR:-/data}"
export AIOPS_MODEL_NAME="${AIOPS_MODEL_NAME:-gpt-5.4}"
export AIOPS_MODEL_PROVIDER="${AIOPS_MODEL_PROVIDER:-custom}"
export AIOPS_AGENT_MAX_TURNS="${AIOPS_AGENT_MAX_TURNS:-90}"
export AIOPS_WEBHOOK_HOST="${AIOPS_WEBHOOK_HOST:-0.0.0.0}"
export AIOPS_WEBHOOK_PORT="${AIOPS_WEBHOOK_PORT:-8765}"

required_envs=(FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_MAIN_CHAT_ID AIOPS_MODEL_BASE_URL AIOPS_MODEL_API_KEY)
for env_name in "${required_envs[@]}"; do
  if [[ -z "${!env_name:-}" ]]; then
    echo "missing required env: $env_name" >&2
    exit 1
  fi
done

mkdir -p "$HOME/.hermes" "$AIOPS_DATA_DIR"

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
]
for key in keys:
    template = template.replace("${" + key + "}", os.getenv(key, ""))

home = Path(os.environ["HOME"]) / ".hermes"
home.mkdir(parents=True, exist_ok=True)
(home / "config.yaml").write_text(template, encoding="utf-8")
PY

if [[ "${AIOPS_WEBHOOK_ONLY:-0}" == "1" ]]; then
  exit 0
fi

python3 -m hooks.alert_webhook_server --host "$AIOPS_WEBHOOK_HOST" --port "$AIOPS_WEBHOOK_PORT" &
webhook_pid=$!

hermes gateway &
gateway_pid=$!

term_handler() {
  kill "$webhook_pid" "$gateway_pid" 2>/dev/null || true
}

trap term_handler TERM INT
wait -n "$webhook_pid" "$gateway_pid"
