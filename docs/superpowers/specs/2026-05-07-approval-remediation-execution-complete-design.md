# Approval Remediation Execution Complete Design

> **For agentic workers:** 这是审批授权到安全自动执行闭环的完整开发规格。后续开发按本文档顺序推进；不要从自然语言直接拼 kubectl，不要在 Feishu reply handler 内执行修复，不要修改 `hermes-agent` 上游代码。

## Summary

当前系统已经完成 Alertmanager -> incident -> analysis -> pending approval -> Feishu text approval -> approval/timeline 状态更新。完整目标是在此基础上补齐安全自动修复闭环：

```text
approved approval
  -> authorized approver
  -> structured remediation action
  -> dry-run
  -> operation lock
  -> safe execution API
  -> audit + timeline
  -> health check
  -> rollback_required / rollback
  -> Feishu notification
```

核心约束：

- `hooks/approval_reply.py` 只负责审批状态变更。
- 自动执行由 `toolsets/approval_execution.py` 负责。
- 第一版执行只支持低风险 allowlist action。
- `k8s_exec`、delete、node/PV/CRD、自由 shell、多资源批量写不进入第一版自动执行。
- 第一版健康检查失败只标记 `rollback_required` 并通知人工；自动 rollback 作为单独阶段。

## Phase Order

1. Approver authorization.
2. Remediation action schema and generation.
3. Execution coordinator persistence and idempotency.
4. Dry-run adapter.
5. Safe execution API.
6. Health check.
7. Rollback-required notification.
8. Deterministic rollback for selected action types.
9. Feishu card buttons.

每个 phase 必须有 focused tests，且前一阶段测试保持绿色。

## 1. Approver Authorization

### Goal

只有被授权的 Feishu 用户可以批准或拒绝 approval。授权失败不得修改 approval 状态。

### Modules

- `hooks/approval_authorization.py`
  - `authorize_approval_reply(...)`
  - operator lookup
  - namespace/risk/operation/self-approval checks
- `hooks/approval_reply.py`
  - 在 `resolve_approval()` 前调用授权
- `hooks/identity.py`
  - 复用 `load_operators()` / `load_approval_rules()` / `match_approval_rule()` 能力
- `toolsets/audit_log.py` 或 `incident_store.add_event()`
  - 记录 `approval_unauthorized`

### API

```python
async def authorize_approval_reply(
    *,
    approval: dict[str, Any],
    approver_id: str,
    decision: str,
) -> dict[str, Any]:
    ...
```

Success:

```python
{"ok": True, "operator": operator_profile}
```

Failure:

```python
{
  "ok": False,
  "message": "审批人无权审批该操作",
  "reason_code": "approver_not_authorized",
}
```

### Required Approval Fields

`approval_async.check_approval()` must return enough fields for authorization:

- `approval_id` / `id`
- `status`
- `operation_type`
- `namespace`
- `risk_level`
- `requester`
- `context`
- `incident_id`

If any critical field is missing, authorization fails closed with `approval_context_incomplete`.

### Rules

1. `approver_id` must be non-empty.
2. `approver_id` must map to a configured operator.
3. Approval must be `pending`.
4. Operator namespace list must include approval namespace or `*`.
5. `k8s_exec`, `dangerous`, namespace/node/PV/CRD operations require admin/can_approve.
6. High-risk self-approval is denied by default.
7. Low-risk self-approval is denied unless `allow_self_approval_low_risk=true`.
8. Deny requires the same authorization as approve; unauthorized users cannot mutate approval by denying it.

Safe defaults:

```yaml
sre_permissions:
  approval_policy:
    allow_self_approval_low_risk: false
    require_admin_for_exec: true
    require_admin_for_dangerous: true
```

### Reason Codes

- `missing_approver_id`
- `unknown_approver`
- `approval_context_incomplete`
- `approval_not_pending`
- `approver_not_allowed`
- `namespace_not_allowed`
- `self_approval_denied`

### Tests

- Admin approves pending approval.
- Admin denies pending approval and reason is preserved.
- Unknown approver is refused and `resolve_approval()` is not called.
- Namespace-scoped approver cannot approve outside namespace.
- High-risk self-approval is denied.
- Already resolved approval is refused before resolve.
- Missing context fails closed.

## 2. Remediation Action Schema

### Goal

`approval.context` must carry structured action data. Execution must never parse free-form natural language as a command.

### Schema

```json
{
  "action_schema_version": "remediation.action.v1",
  "action_signature": "scale_deployment:prod-a:default:deployment/nginx:replicas=3",
  "action_type": "scale_deployment",
  "cluster": "prod-a",
  "namespace": "default",
  "resource_kind": "deployment",
  "resource_name": "nginx",
  "parameters": {
    "replicas": 3
  },
  "source": {
    "incident_id": "inc-1",
    "alertname": "KubeDeploymentReplicasMismatch",
    "analysis_action": "扩容 deployment/nginx 到 3 副本"
  },
  "risk": {
    "risk_level": "low",
    "operation_type": "k8s_write"
  }
}
```

Required fields:

- `action_schema_version`
- `action_signature`
- `action_type`
- `cluster`
- `namespace`
- `resource_kind`
- `resource_name`
- `parameters`

### Allowlist V1

`scale_deployment`:

```json
{
  "action_type": "scale_deployment",
  "resource_kind": "deployment",
  "parameters": {"replicas": 3}
}
```

Validation:

- `replicas` integer, `0 <= replicas <= max_replicas`.
- default `max_replicas=20` unless config overrides.
- namespace must not be empty.
- resource_name must be DNS-label compatible.

`restart_deployment`:

```json
{
  "action_type": "restart_deployment",
  "resource_kind": "deployment",
  "parameters": {"strategy": "rollout_restart"}
}
```

Validation:

- strategy must equal `rollout_restart`.
- no arbitrary annotations from LLM.

Optional future action, not first implementation:

- `patch_resource_limit`

### Action Generation

`hooks/alert_webhook.py` may still choose a human-readable next action, but it must convert it to structured action only if it matches a deterministic pattern.

Allowed deterministic mappings:

- `扩容 deployment/<name> 到 <n> 副本` -> `scale_deployment`.
- `重启 deployment/<name>` -> `restart_deployment`.

If mapping fails:

- Create approval with human-readable suggestion only.
- Mark `executable=false` in context.
- Execution coordinator must ignore it.

### Context Shape in Approval

```json
{
  "action_signature": "...",
  "executable": true,
  "remediation_action": {...}
}
```

### Tests

- Known scale suggestion creates structured action.
- Unknown suggestion creates non-executable approval.
- Invalid replicas fail validation.
- `action_signature` is stable and deterministic.
- Execution ignores `executable=false` approvals.

## 3. Execution Coordinator

### Goal

Process approved executable approvals safely and idempotently.

### Module

`toolsets/approval_execution.py`

Public APIs:

```python
async def process_pending_executions(limit: int = 10) -> dict[str, Any]: ...
async def process_approval_execution(approval_id: str) -> dict[str, Any]: ...
async def check_execution(approval_id: str) -> dict[str, Any] | None: ...
```

### Persistence

Add `approval_executions` table in `toolsets/approval_async.py` or a new `toolsets/approval_execution.py` DB. Prefer colocating with approvals if it avoids cross-database transaction ambiguity.

```sql
CREATE TABLE IF NOT EXISTS approval_executions (
  id TEXT PRIMARY KEY,
  approval_id TEXT NOT NULL UNIQUE,
  incident_id TEXT,
  action_signature TEXT NOT NULL,
  action_schema_version TEXT NOT NULL,
  action_type TEXT NOT NULL,
  cluster TEXT,
  namespace TEXT NOT NULL,
  resource_kind TEXT NOT NULL,
  resource_name TEXT NOT NULL,
  status TEXT NOT NULL,
  dry_run_result_json TEXT,
  lock_key TEXT,
  audit_id INTEGER,
  health_result_json TEXT,
  rollback_result_json TEXT,
  error_message TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  completed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_approval_executions_status
ON approval_executions(status, updated_at);
```

### Statuses

- `queued`
- `dry_run_running`
- `dry_run_failed`
- `lock_waiting`
- `executing`
- `health_checking`
- `succeeded`
- `failed`
- `rollback_required`
- `rolled_back`
- `cancelled`

### Idempotency

- `approval_id` is UNIQUE.
- If execution exists in terminal status, return it and do not execute.
- If execution exists in non-terminal status, return `already_processing` unless explicit retry is supported.
- `action_signature` must match approval context; mismatch fails closed.

### Flow

```text
load approval
  |- status != approved -> ignored
  |- context.executable != true -> ignored_non_executable
  |- existing execution -> return existing
  |- validate remediation_action
  |- create execution queued
  |- run dry-run
  |- acquire lock
  |- execute action
  |- write audit
  |- run health check
  |- update timeline + notify
  `- release lock
```

### Tests

- Approved executable approval executes once.
- Approved non-executable approval is ignored with reason.
- Pending/denied/expired approval is ignored.
- Duplicate coordinator run does not execute twice.
- Context mutation after execution creation is detected and rejected.

## 4. Dry-Run Adapter

### Goal

Every automatic execution must have a dry-run result before acquiring the lock.

### Module

`toolsets/remediation_dry_run.py`

API:

```python
async def dry_run_action(action: dict[str, Any]) -> dict[str, Any]: ...
```

Result:

```json
{
  "ok": true,
  "mode": "server",
  "command_preview": "kubectl scale deployment/nginx --replicas=3 -n default --dry-run=server",
  "summary": "server dry-run accepted",
  "warnings": [],
  "raw_result_ref": null
}
```

Failure:

```json
{
  "ok": false,
  "mode": "server",
  "reason_code": "dry_run_failed",
  "summary": "deployment not found",
  "stderr": "...redacted..."
}
```

### Per-Action Dry-Run

`scale_deployment`:

- Preferred command preview: `kubectl scale deployment/<name> --replicas=<n> -n <namespace> --dry-run=server`.
- If server dry-run unsupported and `allow_client_dry_run_execution=true`, use client dry-run only for `risk_level=low`.

`restart_deployment`:

- Use `kubectl rollout restart deployment/<name> -n <namespace> --dry-run=server` if supported.
- If unsupported, do not auto-execute unless a deterministic patch dry-run is implemented.

### Tests

- Server dry-run success permits next step.
- Server dry-run failure prevents execution.
- Client fallback requires config and low risk.
- Unsupported action returns `dry_run_unsupported`.
- Output is redacted before persistence.

## 5. Safe Execution API

### Goal

Execution must call structured Python APIs, not arbitrary `execute_approved(command)` with LLM text.

### Module

`toolsets/remediation_actions.py`

API:

```python
async def execute_action(action: dict[str, Any]) -> dict[str, Any]: ...
```

Result:

```json
{
  "ok": true,
  "action_type": "scale_deployment",
  "command_preview": "kubectl scale deployment/nginx --replicas=3 -n default",
  "summary": "deployment.apps/nginx scaled",
  "stdout": "...redacted...",
  "stderr": "",
  "exit_code": 0
}
```

### Implementation Rules

- Build kubectl argv from validated structured fields.
- Do not use shell=True.
- Do not accept command strings from approval context.
- Run through existing `_run_kubectl` only after command is generated by trusted code.
- Redact output before storing in execution/audit/timeline.

### Per-Action Execution

`scale_deployment`:

```text
kubectl scale deployment/<resource_name> --replicas=<replicas> -n <namespace>
```

`restart_deployment`:

```text
kubectl rollout restart deployment/<resource_name> -n <namespace>
```

### Tests

- Generated command for scale is stable.
- Generated command for restart is stable.
- Invalid action_type rejected.
- Invalid resource_name rejected.
- No shell metacharacter injection is possible through resource_name/namespace.
- Output is redacted.

## 6. Health Check

### Goal

Confirm symptoms improved after execution before claiming success.

### Module

`toolsets/remediation_health.py`

API:

```python
async def check_action_health(
    action: dict[str, Any],
    *,
    timeout_seconds: int = 180,
    interval_seconds: int = 10,
) -> dict[str, Any]: ...
```

Result:

```json
{
  "ok": true,
  "checks": [
    {"name": "deployment_available", "ok": true, "summary": "3/3 available"},
    {"name": "pods_ready", "ok": true, "summary": "all selected pods Ready"},
    {"name": "no_new_warning_events", "ok": true, "summary": "no new warning events"}
  ],
  "observed_at": 1234567890.0
}
```

### Check Strategy

For deployment actions:

1. Read deployment desired/available/updated replicas.
2. Resolve selector from deployment spec.
3. Read matching pods.
4. Confirm pods Ready.
5. Compare restart count at beginning/end of observation window when possible.
6. Read recent warning events for involved pods/deployment.

### Timeout Policy

- Default timeout 180 seconds.
- Default interval 10 seconds.
- On timeout, return `ok=false`, `reason_code=health_check_timeout`.
- Health check failure does not automatically rollback in first implementation.

### Tests

- Healthy deployment returns ok.
- unavailable replicas fail.
- pod not Ready fails.
- warning event fails.
- timeout returns stable reason_code.
- check uses read-only Kubernetes calls only.

## 7. Rollback Strategy

### Goal

Introduce rollback safely and only for deterministic actions.

### Phase 1 Behavior

No automatic rollback. Health check failure sets execution status `rollback_required`, writes timeline, and notifies human.

### Rollback Phase Preconditions

Before automatic rollback is allowed for an action type:

- The action has a deterministic inverse.
- Pre-change snapshot is stored.
- Rollback dry-run exists.
- Rollback has audit record.
- Rollback failure behavior is defined.
- Product decision exists on whether rollback needs second approval.

### Rollback Eligibility

`scale_deployment` can be rollback-eligible if pre-change replicas are captured:

```json
{
  "action_type": "scale_deployment",
  "before": {"replicas": 2},
  "after": {"replicas": 3}
}
```

Rollback command:

```text
kubectl scale deployment/<name> --replicas=<before.replicas> -n <namespace>
```

`restart_deployment` is not rollback-eligible by default.

### Rollback API

```python
async def rollback_execution(execution_id: str, *, approver_id: str | None = None) -> dict[str, Any]: ...
```

### Tests

- `rollback_required` does not auto-run rollback.
- Scale rollback requires captured previous replicas.
- Restart rollback is refused.
- Rollback writes separate audit record.
- Rollback failure is visible in timeline.

## 8. Feishu Card Buttons

### Goal

Improve approval UX while preserving same backend semantics.

### Rules

- Text approval remains supported.
- Card callback uses same authorization and resolve path.
- Card callback cannot bypass approver identity.
- Card callback payload must include `approval_id`, `decision`, optional `reason`.
- Card actions must be idempotent.

### Callback Normalization

Convert card callback to the same service-level input as text:

```python
handle_approval_decision(
    approval_id="ap-1",
    decision="approved",
    reason=None,
    approver_id="ou_admin",
    source="feishu_card",
)
```

Text replies should also eventually call this normalized service, leaving parsing outside state mutation.

### Card States

- pending: approve/deny buttons enabled.
- approved: disabled, shows approver and time.
- denied: disabled, shows approver, reason, time.
- expired: disabled.
- unauthorized attempt: card state unchanged, ephemeral/user-visible failure reply.

### Tests

- Card approve calls same decision service.
- Card deny preserves reason.
- Duplicate card callback is idempotent.
- Unauthorized card approver cannot mutate state.
- Text approval remains working.

## 9. End-to-End Acceptance Matrix

| Flow | Expected |
|---|---|
| Alert creates executable scale approval | approval context contains `remediation_action` and `action_signature` |
| Admin approves | approval becomes approved, timeline records approval_approved |
| Unauthorized user approves | approval remains pending, unauthorized event recorded |
| Coordinator processes approved scale | dry-run, lock, execute, audit, health check, timeline |
| Coordinator reruns same approval | no duplicate execution |
| Dry-run fails | no lock, no execution, user notified |
| Lock conflict | no execution, conflict visible |
| Health check fails | status `rollback_required`, no resolved claim |
| Card approve duplicate callback | idempotent, no duplicate timeline |

## 10. NOT In Scope Until Explicitly Approved

- Free-form kubectl execution.
- Automatic `k8s_exec`.
- Delete operations.
- Node/PV/CRD operations.
- Multi-resource batch changes.
- Fully automatic rollback for non-deterministic actions.
- Multi-instance worker scheduling.
- External queue infrastructure.
