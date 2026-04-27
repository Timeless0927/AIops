# Kubernetes Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Kubernetes-ready runtime package for the AIOps Feishu SRE Agent with container image, non-interactive Hermes bootstrap, and deployable Kubernetes manifests.

**Architecture:** Keep the current application architecture intact and add a deployment layer around it. A single image runs both `hermes gateway` and `hooks.alert_webhook_server`, bootstraps `~/.hermes/config.yaml` from environment variables, persists SQLite under `/data`, and uses in-cluster `ServiceAccount` credentials with bundled `kubectl`.

**Tech Stack:** Python 3.11, Hermes CLI, Bash entrypoint, Docker, Kubernetes YAML, ConfigMap, Secret, ServiceAccount, RBAC, PVC, pytest.

---

## File Structure

- Create: `deploy/entrypoint.sh` — container bootstrap script that validates env, renders `~/.hermes/config.yaml`, ensures data directories, and starts both long-running processes.
- Create: `deploy/hermes-config.template.yaml` — template used by entrypoint to generate `~/.hermes/config.yaml` from environment variables.
- Create: `Dockerfile.aiops` — project image definition bundling app code, Hermes runtime, and `kubectl`.
- Create: `deploy/k8s/namespace.yaml` — optional isolated namespace manifest.
- Create: `deploy/k8s/configmap.yaml` — non-secret runtime configuration for gateway/webhook/model settings.
- Create: `deploy/k8s/secret.example.yaml` — documented secret shape for Feishu and model credentials.
- Create: `deploy/k8s/serviceaccount.yaml` — workload identity for in-cluster API access.
- Create: `deploy/k8s/rbac.yaml` — least-privilege RBAC starter policy for read/write/exec operations.
- Create: `deploy/k8s/pvc.yaml` — persistent volume claim for SQLite state.
- Create: `deploy/k8s/deployment.yaml` — single-replica deployment running the dual-process container.
- Create: `deploy/k8s/service.yaml` — ClusterIP service exposing webhook port `8765`.
- Create: `deploy/k8s/README.md` — operator guide for build, secret creation, deploy, verification, and Alertmanager endpoint wiring.
- Modify: `toolsets/incident_store.py` — move default SQLite path to `/data` when `AIOPS_DATA_DIR` is set, keeping repo-local fallback for tests/dev.
- Modify: `toolsets/message_delivery.py` — same `AIOPS_DATA_DIR` support.
- Modify: `toolsets/approval_async.py` — same `AIOPS_DATA_DIR` support.
- Modify: `toolsets/system_mode.py` — same `AIOPS_DATA_DIR` support.
- Modify: `toolsets/audit_log.py` — same `AIOPS_DATA_DIR` support.
- Modify: `toolsets/operation_lock.py` — same `AIOPS_DATA_DIR` support.
- Test: `tests/test_data_dir_env.py` — verify `AIOPS_DATA_DIR` redirects SQLite file locations.

### Task 1: Add data directory environment override

**Files:**
- Create: `tests/test_data_dir_env.py`
- Modify: `toolsets/incident_store.py`
- Modify: `toolsets/message_delivery.py`
- Modify: `toolsets/approval_async.py`
- Modify: `toolsets/system_mode.py`
- Modify: `toolsets/audit_log.py`
- Modify: `toolsets/operation_lock.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_data_dir_env.py` with:

```python
from __future__ import annotations

import importlib
from pathlib import Path


def test_default_db_paths_follow_aiops_data_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AIOPS_DATA_DIR", str(tmp_path))

    modules = {
        "incident_store": importlib.import_module("toolsets.incident_store"),
        "message_delivery": importlib.import_module("toolsets.message_delivery"),
        "approval_async": importlib.import_module("toolsets.approval_async"),
        "system_mode": importlib.import_module("toolsets.system_mode"),
        "audit_log": importlib.import_module("toolsets.audit_log"),
        "operation_lock": importlib.import_module("toolsets.operation_lock"),
    }

    importlib.reload(modules["incident_store"])
    importlib.reload(modules["message_delivery"])
    importlib.reload(modules["approval_async"])
    importlib.reload(modules["system_mode"])
    importlib.reload(modules["audit_log"])
    importlib.reload(modules["operation_lock"])

    assert modules["incident_store"]._default_db_path() == tmp_path / "incidents.db"
    assert modules["message_delivery"]._default_db_path() == tmp_path / "message_delivery.db"
    assert modules["approval_async"]._default_db_path() == tmp_path / "approvals.db"
    assert modules["system_mode"]._default_db_path() == tmp_path / "system_mode.db"
    assert modules["audit_log"]._default_db_path() == tmp_path / "audit_log.db"
    assert modules["operation_lock"]._default_db_path() == tmp_path / "operation_lock.db"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk pytest tests/test_data_dir_env.py -q`
Expected: FAIL because `_default_db_path()` still points to project-local `data/`.

- [ ] **Step 3: Implement `AIOPS_DATA_DIR` override**

In each of the six tool modules, change `_default_db_path()` to this shape:

```python
def _default_db_path() -> Path:
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "incidents.db"
    return _project_root() / "data" / "incidents.db"
```

Apply the correct filename per module:

- `incident_store.py` → `incidents.db`
- `message_delivery.py` → `message_delivery.db`
- `approval_async.py` → `approvals.db`
- `system_mode.py` → `system_mode.db`
- `audit_log.py` → `audit_log.db`
- `operation_lock.py` → `operation_lock.db`

Also add `import os` where missing.

- [ ] **Step 4: Run the focused test**

Run: `rtk pytest tests/test_data_dir_env.py -q`
Expected: PASS.

- [ ] **Step 5: Run adjacent storage tests**

Run: `rtk pytest tests/test_incident_store.py tests/test_approval_async.py tests/test_message_delivery.py tests/test_health_check.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
rtk git add tests/test_data_dir_env.py toolsets/incident_store.py toolsets/message_delivery.py toolsets/approval_async.py toolsets/system_mode.py toolsets/audit_log.py toolsets/operation_lock.py
rtk git commit -m "feat: support configurable aiops data dir"
```

### Task 2: Add non-interactive container bootstrap

**Files:**
- Create: `deploy/hermes-config.template.yaml`
- Create: `deploy/entrypoint.sh`

- [ ] **Step 1: Write the failing shell validation test**

Create `tests/test_deploy_entrypoint.py` with:

```python
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_entrypoint_renders_config(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "AIOPS_DATA_DIR": str(tmp_path / "data"),
            "FEISHU_APP_ID": "cli_app",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_MAIN_CHAT_ID": "oc_main",
            "AIOPS_MODEL_PROVIDER": "custom",
            "AIOPS_MODEL_BASE_URL": "http://model.local/v1",
            "AIOPS_MODEL_API_KEY": "token",
            "AIOPS_WEBHOOK_ONLY": "1",
        }
    )

    subprocess.run(
        ["bash", "deploy/entrypoint.sh"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        timeout=10,
    )

    config_text = (tmp_path / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    assert "main_chat_id: \"oc_main\"" in config_text
    assert "base_url: \"http://model.local/v1\"" in config_text
    assert "toolsets:" in config_text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk pytest tests/test_deploy_entrypoint.py -q`
Expected: FAIL because `deploy/entrypoint.sh` does not exist.

- [ ] **Step 3: Add config template**

Create `deploy/hermes-config.template.yaml` with:

```yaml
model:
  default: "${AIOPS_MODEL_NAME}"
  provider: "${AIOPS_MODEL_PROVIDER}"
  base_url: "${AIOPS_MODEL_BASE_URL}"
  api_key: "${AIOPS_MODEL_API_KEY}"

providers: {}
fallback_providers: []

toolsets:
  - hermes-cli
  - sre
  - k8s

platform_toolsets:
  feishu:
    - hermes-feishu
    - sre
    - k8s

agent:
  max_turns: ${AIOPS_AGENT_MAX_TURNS}

terminal:
  backend: local
  cwd: .
  timeout: 180
  persistent_shell: true

sre:
  project_root: "/app"
  toolsets_dir: "/app/toolsets"
  hooks_dir: "/app/hooks"
  skills_dir: "/app/skills"
  dedup_key_version: "v1"
  raw_output_ttl_hours: 24
  system_mode_store: "sqlite"

platforms:
  feishu:
    app_id: "${FEISHU_APP_ID}"
    app_secret: "${FEISHU_APP_SECRET}"
    verification_token: "${FEISHU_VERIFICATION_TOKEN}"
    encrypt_key: "${FEISHU_ENCRYPT_KEY}"
    main_chat_id: "${FEISHU_MAIN_CHAT_ID}"
```

- [ ] **Step 4: Add entrypoint script**

Create `deploy/entrypoint.sh` with:

```bash
#!/usr/bin/env bash
set -euo pipefail

export HOME="${HOME:-/root}"
export AIOPS_DATA_DIR="${AIOPS_DATA_DIR:-/data}"
export AIOPS_MODEL_NAME="${AIOPS_MODEL_NAME:-gpt-5.4}"
export AIOPS_MODEL_PROVIDER="${AIOPS_MODEL_PROVIDER:-custom}"
export AIOPS_AGENT_MAX_TURNS="${AIOPS_AGENT_MAX_TURNS:-90}"
export AIOPS_WEBHOOK_HOST="${AIOPS_WEBHOOK_HOST:-0.0.0.0}"
export AIOPS_WEBHOOK_PORT="${AIOPS_WEBHOOK_PORT:-8765}"

required_bins=(hermes python3 kubectl)
for bin_name in "${required_bins[@]}"; do
  command -v "$bin_name" >/dev/null 2>&1 || {
    echo "missing required binary: $bin_name" >&2
    exit 1
  }
done

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
  exec python3 -m hooks.alert_webhook_server --host "$AIOPS_WEBHOOK_HOST" --port "$AIOPS_WEBHOOK_PORT"
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
```

- [ ] **Step 5: Run entrypoint test**

Run: `rtk pytest tests/test_deploy_entrypoint.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
rtk git add deploy/hermes-config.template.yaml deploy/entrypoint.sh tests/test_deploy_entrypoint.py
rtk git commit -m "feat: add kubernetes runtime bootstrap"
```

### Task 3: Build a deployable image definition

**Files:**
- Create: `Dockerfile.aiops`

- [ ] **Step 1: Write the failing image structure check**

Append to `tests/test_deploy_entrypoint.py`:

```python
def test_dockerfile_aiops_contains_runtime_dependencies() -> None:
    dockerfile = Path("Dockerfile.aiops").read_text(encoding="utf-8")
    assert "kubectl" in dockerfile
    assert "deploy/entrypoint.sh" in dockerfile
    assert "ENTRYPOINT [\"/app/deploy/entrypoint.sh\"]" in dockerfile
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk pytest tests/test_deploy_entrypoint.py::test_dockerfile_aiops_contains_runtime_dependencies -q`
Expected: FAIL because `Dockerfile.aiops` does not exist.

- [ ] **Step 3: Create image definition**

Create `Dockerfile.aiops` with:

```dockerfile
FROM python:3.11-slim

ARG KUBECTL_VERSION=v1.33.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/root \
    AIOPS_DATA_DIR=/data

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates bash gettext-base git \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && rm -rf /var/lib/apt/lists/*

COPY hermes-agent /tmp/hermes-agent
RUN pip install /tmp/hermes-agent

COPY . /app
RUN chmod +x /app/deploy/entrypoint.sh

VOLUME ["/data"]

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
```

- [ ] **Step 4: Run the focused test**

Run: `rtk pytest tests/test_deploy_entrypoint.py::test_dockerfile_aiops_contains_runtime_dependencies -q`
Expected: PASS.

- [ ] **Step 5: Smoke-parse the Dockerfile**

Run: `rtk docker build -f Dockerfile.aiops --target does-not-exist .`
Expected: Docker reads the file and fails only with `target stage does-not-exist could not be found`, not syntax errors.

- [ ] **Step 6: Commit**

```bash
rtk git add Dockerfile.aiops tests/test_deploy_entrypoint.py
rtk git commit -m "feat: add aiops container image definition"
```

### Task 4: Add Kubernetes manifests

**Files:**
- Create: `deploy/k8s/namespace.yaml`
- Create: `deploy/k8s/configmap.yaml`
- Create: `deploy/k8s/secret.example.yaml`
- Create: `deploy/k8s/serviceaccount.yaml`
- Create: `deploy/k8s/rbac.yaml`
- Create: `deploy/k8s/pvc.yaml`
- Create: `deploy/k8s/deployment.yaml`
- Create: `deploy/k8s/service.yaml`

- [ ] **Step 1: Write the failing manifest check**

Create `tests/test_k8s_manifests.py` with:

```python
from pathlib import Path


def test_deployment_manifest_references_required_runtime_components() -> None:
    deployment = Path("deploy/k8s/deployment.yaml").read_text(encoding="utf-8")
    assert "serviceAccountName: aiops-agent" in deployment
    assert "claimName: aiops-agent-data" in deployment
    assert "name: FEISHU_MAIN_CHAT_ID" in deployment
    assert "image: aiops-agent:latest" in deployment


def test_service_manifest_exposes_webhook_port() -> None:
    service = Path("deploy/k8s/service.yaml").read_text(encoding="utf-8")
    assert "port: 8765" in service
    assert "targetPort: 8765" in service
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk pytest tests/test_k8s_manifests.py -q`
Expected: FAIL because manifests do not exist.

- [ ] **Step 3: Add namespace manifest**

Create `deploy/k8s/namespace.yaml` with:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: aiops
```

- [ ] **Step 4: Add config and secret templates**

Create `deploy/k8s/configmap.yaml` with:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: aiops-agent-config
  namespace: aiops
data:
  FEISHU_MAIN_CHAT_ID: "oc_replace_me"
  AIOPS_MODEL_PROVIDER: "custom"
  AIOPS_MODEL_NAME: "gpt-5.4"
  AIOPS_MODEL_BASE_URL: "http://model-service.default.svc.cluster.local/v1"
  AIOPS_AGENT_MAX_TURNS: "90"
  AIOPS_WEBHOOK_HOST: "0.0.0.0"
  AIOPS_WEBHOOK_PORT: "8765"
  AIOPS_DATA_DIR: "/data"
```
```

Create `deploy/k8s/secret.example.yaml` with:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: aiops-agent-secret
  namespace: aiops
type: Opaque
stringData:
  FEISHU_APP_ID: "replace-me"
  FEISHU_APP_SECRET: "replace-me"
  FEISHU_VERIFICATION_TOKEN: ""
  FEISHU_ENCRYPT_KEY: ""
  AIOPS_MODEL_API_KEY: "replace-me"
```
```

- [ ] **Step 5: Add identity, storage, and service manifests**

Create `deploy/k8s/serviceaccount.yaml` with:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: aiops-agent
  namespace: aiops
```

Create `deploy/k8s/pvc.yaml` with:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: aiops-agent-data
  namespace: aiops
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 5Gi
```

Create `deploy/k8s/service.yaml` with:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: aiops-agent-webhook
  namespace: aiops
spec:
  selector:
    app: aiops-agent
  ports:
    - name: webhook
      port: 8765
      targetPort: 8765
```
```

- [ ] **Step 6: Add RBAC and deployment manifests**

Create `deploy/k8s/rbac.yaml` with:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: aiops-agent
  namespace: aiops
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log", "events", "services", "configmaps"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["pods/exec", "pods/attach"]
    verbs: ["create"]
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets", "daemonsets", "replicasets"]
    verbs: ["get", "list", "watch", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: aiops-agent
  namespace: aiops
subjects:
  - kind: ServiceAccount
    name: aiops-agent
    namespace: aiops
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: aiops-agent
```

Create `deploy/k8s/deployment.yaml` with:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aiops-agent
  namespace: aiops
spec:
  replicas: 1
  selector:
    matchLabels:
      app: aiops-agent
  template:
    metadata:
      labels:
        app: aiops-agent
    spec:
      serviceAccountName: aiops-agent
      containers:
        - name: aiops-agent
          image: aiops-agent:latest
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8765
              name: webhook
          envFrom:
            - configMapRef:
                name: aiops-agent-config
            - secretRef:
                name: aiops-agent-secret
          env:
            - name: FEISHU_MAIN_CHAT_ID
              valueFrom:
                configMapKeyRef:
                  name: aiops-agent-config
                  key: FEISHU_MAIN_CHAT_ID
          volumeMounts:
            - name: data
              mountPath: /data
          readinessProbe:
            tcpSocket:
              port: 8765
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            tcpSocket:
              port: 8765
            initialDelaySeconds: 15
            periodSeconds: 20
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: aiops-agent-data
```

- [ ] **Step 7: Run manifest tests**

Run: `rtk pytest tests/test_k8s_manifests.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
rtk git add deploy/k8s/namespace.yaml deploy/k8s/configmap.yaml deploy/k8s/secret.example.yaml deploy/k8s/serviceaccount.yaml deploy/k8s/rbac.yaml deploy/k8s/pvc.yaml deploy/k8s/deployment.yaml deploy/k8s/service.yaml tests/test_k8s_manifests.py
rtk git commit -m "feat: add kubernetes deployment manifests"
```

### Task 5: Add operator deployment guide

**Files:**
- Create: `deploy/k8s/README.md`

- [ ] **Step 1: Write the failing documentation check**

Append to `tests/test_k8s_manifests.py`:

```python
def test_k8s_readme_mentions_build_apply_and_alertmanager_url() -> None:
    readme = Path("deploy/k8s/README.md").read_text(encoding="utf-8")
    assert "docker build -f Dockerfile.aiops" in readme
    assert "kubectl apply -f deploy/k8s" in readme
    assert "/webhooks/alertmanager" in readme
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk pytest tests/test_k8s_manifests.py::test_k8s_readme_mentions_build_apply_and_alertmanager_url -q`
Expected: FAIL because `deploy/k8s/README.md` does not exist.

- [ ] **Step 3: Write the operator guide**

Create `deploy/k8s/README.md` with:

```markdown
# AIOps Agent Kubernetes Deployment

## Build image

```bash
docker build -f Dockerfile.aiops -t aiops-agent:latest .
```

## Prepare secrets

Copy `deploy/k8s/secret.example.yaml` to a real secret manifest and replace all placeholder values.

## Apply manifests

```bash
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/secret.yaml
kubectl apply -f deploy/k8s/serviceaccount.yaml
kubectl apply -f deploy/k8s/rbac.yaml
kubectl apply -f deploy/k8s/pvc.yaml
kubectl apply -f deploy/k8s/deployment.yaml
kubectl apply -f deploy/k8s/service.yaml
```

You can also apply the directory after creating `deploy/k8s/secret.yaml`:

```bash
kubectl apply -f deploy/k8s
```

## Verify runtime

```bash
kubectl -n aiops get pods
kubectl -n aiops logs deploy/aiops-agent
kubectl -n aiops exec deploy/aiops-agent -- kubectl auth can-i get pods
```

## Alertmanager target

Point Alertmanager to the webhook service URL ending with `/webhooks/alertmanager`, for example:

```text
http://aiops-agent-webhook.aiops.svc.cluster.local:8765/webhooks/alertmanager
```
```

- [ ] **Step 4: Run the documentation check**

Run: `rtk pytest tests/test_k8s_manifests.py::test_k8s_readme_mentions_build_apply_and_alertmanager_url -q`
Expected: PASS.

- [ ] **Step 5: Run all deployment-focused tests**

Run: `rtk pytest tests/test_data_dir_env.py tests/test_deploy_entrypoint.py tests/test_k8s_manifests.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
rtk git add deploy/k8s/README.md tests/test_k8s_manifests.py
rtk git commit -m "docs: add kubernetes deployment guide"
```

### Task 6: Final verification and handoff

**Files:**
- Modify: `docs/superpowers/specs/2026-04-24-k8s-deployment-design.md`
- Modify: `docs/superpowers/plans/2026-04-24-k8s-deployment-implementation.md`

- [ ] **Step 1: Reconcile spec and implementation**

Update the spec with one short “Implemented by” note under the relevant sections if file names or resource names changed during implementation.

- [ ] **Step 2: Run the full focused verification set**

Run:

```bash
rtk pytest tests/test_data_dir_env.py tests/test_deploy_entrypoint.py tests/test_k8s_manifests.py tests/test_alert_webhook.py tests/test_alert_webhook_server.py tests/test_feishu_conversation.py tests/test_incident_store.py tests/test_approval_async.py tests/test_message_delivery.py tests/test_health_check.py -q
```

Expected: PASS.

- [ ] **Step 3: Check git status is clean except intended files**

Run: `rtk git status --short`
Expected: only the deployment files and any intentional doc updates remain, or nothing remains after final commit.

- [ ] **Step 4: Commit final integration**

```bash
rtk git add Dockerfile.aiops deploy/ docs/superpowers/specs/2026-04-24-k8s-deployment-design.md docs/superpowers/plans/2026-04-24-k8s-deployment-implementation.md tests/test_data_dir_env.py tests/test_deploy_entrypoint.py tests/test_k8s_manifests.py toolsets/incident_store.py toolsets/message_delivery.py toolsets/approval_async.py toolsets/system_mode.py toolsets/audit_log.py toolsets/operation_lock.py
rtk git commit -m "feat: package aiops agent for kubernetes deployment"
```
