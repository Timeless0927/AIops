# Feishu Approval Runtime Overlay Design

> **For agentic workers:** 这份文档是后续自动开发的事实源。实现时不得修改 `hermes-agent` 上游源码；不得把审批通过直接接到真实 Kubernetes 写操作。

## Summary

目标是在不 fork、不修改 `hermes-agent` 的前提下，让飞书文本审批回复进入父项目审批状态机。当前实现采用父项目 runtime overlay：容器启动时先安装 Feishu adapter patch，再启动 Hermes gateway。

当前能力：

- 识别精确文本审批命令：`批准 <approval_id>` / `拒绝 <approval_id> <reason>`。
- 使用 Feishu sender identity 作为 approver。
- 调用 `hooks/approval_reply.py` 变更 approval 状态。
- 将处理结果回复到同一 chat/thread。
- 已处理审批文本不进入 LLM/session batching。
- 普通文本继续进入 Hermes 原始消息流。

明确边界：审批通过只改变状态，不执行 `k8s_write` / `k8s_exec`。

## Architecture

```text
deploy/entrypoint.sh
  |
  |- hooks.alert_webhook_server
  `- runtime.hermes_gateway
       |
       |- add hermes-agent source path
       |- runtime.feishu_approval_overlay.install()
       `- hermes_cli.gateway.run_gateway(replace=True)

Feishu inbound message
  |
  v
Hermes FeishuAdapter._process_inbound_message
  |
  v
AIOps overlay patched method
  |
  |- approval text?
  |    |
  |    |- require approver identity
  |    |- hooks.approval_reply.handle_approval_reply()
  |    |- self.send(response, same chat/thread)
  |    `- return None
  |
  `- original Hermes method
```

Why runtime overlay instead of fork:

- Approval semantics belong to the parent AIOps product, not generic Hermes.
- Hermes upgrades remain possible without maintaining a long-lived business fork.
- The compatibility surface is intentionally narrow: one adapter method.
- Fail-fast tests detect upstream shape changes.

## Interfaces

### Input Text

Accepted commands are exact whitespace-split commands:

- `批准 <approval_id>`
- `拒绝 <approval_id> <reason>`

Non-goals:

- No fuzzy matching.
- No partial natural language approval.
- No Feishu card callback in this step.

### Approver Identity

Required identity order:

1. `sender_id.open_id`
2. `sender_id.user_id` as fallback only

If neither exists, overlay replies `审批处理失败：无法识别审批人身份` and does not call `handle_approval_reply()`.

### State Mutation

Only `hooks/approval_reply.py` mutates approval state. Overlay responsibilities are limited to:

- Extract text.
- Detect approval command.
- Extract approver identity.
- Call approval reply handler.
- Convert handler result to user-facing reply.
- Preserve Feishu chat/thread reply metadata.

### Output Text

- Approved: `审批已批准：<approval_id>`
- Denied: `审批已拒绝：<approval_id>`
- Failed: `审批处理失败：<reason>`

## Failure Modes

- Hermes adapter method missing: overlay install raises immediately; deployment should fail fast.
- Approval handler raises: overlay logs warning and replies `审批处理失败：<exception>`。
- Unknown approval ID: handler returns `审批记录不存在`; overlay replies failure text.
- Missing approver identity: overlay refuses before state mutation.
- Send response fails: current process surfaces the adapter send exception; future production hardening may add retry or message_delivery integration.
- Normal text misdetected: tests must prove ordinary Feishu text reaches original Hermes flow.

## Upgrade Checklist

Run before accepting a Hermes upgrade:

```bash
rtk pytest tests/test_feishu_approval_overlay.py tests/test_deploy_entrypoint.py -q
rtk pytest tests/test_approval_reply.py tests/test_voice_context.py -q
```

Manual review checklist:

- `gateway.platforms.feishu.FeishuAdapter` still exists.
- `_process_inbound_message` still exists.
- Patched method arguments are still compatible: `data`, `message`, `sender_id`, `chat_type`, `message_id`。
- `message.content`, `message.chat_id`, `message.thread_id` are still available or have a documented replacement.
- `self.send(chat_id=..., content=..., reply_to=..., metadata=...)` is still valid.
- Entrypoint still starts `python3 -m runtime.hermes_gateway` rather than bare `hermes gateway`.

Rollback options:

- Revert Hermes pointer/version.
- Temporarily run `AIOPS_WEBHOOK_ONLY=1` to keep Alertmanager ingestion alive while Feishu gateway is repaired.
- Do not patch `hermes-agent` business logic as a hotfix unless the change is suitable for upstream contribution.

## Executable Roadmap

### 1. Overlay Production Hardening

Goal: make current overlay operationally maintainable.

Implementation requirements:

- Document startup path and wrapper requirement.
- Keep fail-fast behavior when Hermes adapter shape changes.
- Add or maintain tests proving normal messages pass through.
- Add explicit troubleshooting notes for missing sender identity and unknown approval IDs.

Acceptance:

- Focused overlay and entrypoint tests pass.
- Docs clearly state that direct Hermes gateway startup bypasses approval interception.
- `hermes-agent` submodule has no business-code diff.

### 2. Approver Authorization

Goal: require a real authorized approver, not just any Feishu identity.

Implementation requirements:

- Map Feishu `open_id` to operator profile.
- Require `can_approve` or matching approval rule.
- Check namespace, operation_type, and risk_level.
- Unauthorized replies must not mutate approval state.
- Unauthorized attempts should create an audit or timeline event.

Tests:

- Admin can approve.
- Non-admin cannot approve high-risk action.
- Unknown Feishu user is refused.
- Missing identity still refuses before authorization lookup.

### 3. Automatic Execution Design

Goal: design the execution bridge before writing execution code.

Required design elements:

- Execution coordinator or queue; do not execute inside Feishu reply handler.
- Idempotency key per approval/action.
- Re-check authorization before execution.
- Server-side dry-run, with explicit fallback policy.
- Operation lock acquisition and release semantics.
- Audit record shape.
- Health check criteria.
- Rollback or manual escalation path.

Acceptance:

- State machine diagram exists.
- Failure-mode table exists.
- Test matrix exists.
- `k8s_exec` is explicitly out of initial automatic execution scope.

### 4. Automatic Execution Implementation

Goal: implement only after step 3 is approved.

Initial scope:

- Low-risk allowlisted `k8s_write` actions only.
- No automatic `k8s_exec`。
- No broad free-form kubectl command execution.

Acceptance:

- Approval cannot execute twice.
- Dry-run failure prevents execution.
- Lock conflict prevents execution and informs user.
- Execution writes audit + timeline.
- Health check result is visible in incident timeline.

## Development Specification

完整自动执行闭环的后续开发以 `docs/superpowers/specs/2026-05-07-approval-remediation-execution-complete-design.md` 为主规格；本节保留 runtime overlay 相关的阶段摘要和约束。

后续开发必须按下面顺序推进。每个阶段完成后都要保持前一阶段测试绿色，不允许把执行逻辑直接塞进 Feishu overlay 或 `hooks/approval_reply.py`。

### Phase A: Approver Authorization

Goal: 只有被授权的 Feishu 用户可以批准或拒绝 approval。

#### Proposed Modules

- `hooks/approval_authorization.py`
  - 负责把 Feishu approver identity 映射到 operator profile。
  - 负责校验 approval context 中的 namespace / operation_type / risk_level。
  - 不写 approval 状态。
- `hooks/approval_reply.py`
  - 保持唯一状态变更入口。
  - 在调用 `approval_async.resolve_approval()` 前调用 authorization。
- `hooks/identity.py`
  - 复用已有 operator / approval_rules 读取能力，不新增第二套权限配置格式。
- `toolsets/audit_log.py` 或 `incident_store.add_event()`
  - 记录未授权审批尝试。

#### Authorization Input

`authorize_approval_reply()` 接收：

```python
authorize_approval_reply(
    *,
    approval: dict[str, Any],
    approver_id: str,
    decision: Literal["approved", "denied"],
) -> dict[str, Any]
```

`approval` 来自 `approval_async.check_approval(approval_id)`，至少需要：

- `approval_id` / `id`
- `status`
- `operation_type`
- `namespace`
- `risk_level`
- `requester`
- `context`
- `incident_id`

#### Authorization Rules

按顺序判定，任一步失败都不得修改 approval 状态：

1. `approver_id` 必须存在。
2. `approver_id` 必须能映射到 `sre_permissions.operators` 中的 operator。
3. operator 必须 `can_approve=true`，或匹配一条允许该 role/name 审批的 rule。
4. operator namespace 范围必须覆盖 approval namespace，`*` 表示全局。
5. 高风险操作必须由 admin/can_approve 用户审批。
6. requester 不允许审批自己的高风险操作；低风险是否允许自批由配置决定，默认不允许。
7. approval 状态必须仍是 `pending`。

默认配置补充：

```yaml
sre_permissions:
  approval_policy:
    allow_self_approval_low_risk: false
    require_admin_for_exec: true
    require_admin_for_dangerous: true
```

如果配置不存在，按上面的安全默认值处理。

#### Authorization Output

允许：

```python
{"ok": True, "operator": {...}}
```

拒绝：

```python
{
  "ok": False,
  "message": "审批人无权审批该操作",
  "reason_code": "approver_not_authorized",
}
```

建议 reason_code：

- `missing_approver_id`
- `unknown_approver`
- `approver_not_allowed`
- `namespace_not_allowed`
- `self_approval_denied`
- `approval_not_pending`

#### User-Facing Replies

- 未识别身份：`审批处理失败：无法识别审批人身份`
- 未配置用户：`审批处理失败：审批人未授权`
- 越权 namespace：`审批处理失败：审批人无权审批该命名空间`
- 自批被拒：`审批处理失败：不能审批自己发起的高风险操作`
- 非 pending：`审批处理失败：审批已处理或已过期`

#### Audit / Timeline

未授权尝试需要留痕，但不能污染 approval 成功/失败状态：

- 有 `incident_id`：写 `incident_store.add_event(incident_id, "approval_unauthorized", "approval_reply", approval_id, approver_id, metadata)`。
- 无 `incident_id`：写 `audit_log`，如果 audit 不可用则至少记录 warning log。

metadata 建议：

```json
{
  "approval_id": "ap-1",
  "approver_id": "ou_xxx",
  "decision": "approved",
  "reason_code": "namespace_not_allowed",
  "operation_type": "k8s_write",
  "namespace": "prod"
}
```

#### Tests

Add/extend `tests/test_approval_reply.py`:

- Admin approver can approve pending approval.
- Admin approver can deny pending approval and reason is preserved.
- Unknown Feishu user is refused and `resolve_approval()` is not called.
- Non-admin cannot approve dangerous/high-risk operation.
- Namespace-scoped approver cannot approve another namespace.
- Self-approval high-risk operation is refused by default.
- Already resolved/expired approval returns clear failure and does not add duplicate success timeline.

Add `tests/test_approval_authorization.py`:

- Operator lookup by Feishu `open_id`.
- `*` namespace grants all namespaces.
- Missing optional `approval_policy` uses safe defaults.
- Reason codes are stable.

### Phase B: Execution Coordinator Design

Goal: 在 approval 被批准后，由独立 coordinator 推进安全执行；不要在 Feishu reply handler 内执行命令。

#### Proposed Modules

- `toolsets/approval_execution.py`
  - 扫描或接收 approved approvals。
  - 维护 execution idempotency。
  - 调用 dry-run、operation lock、执行、审计、健康检查。
- `toolsets/remediation_health.py`
  - 根据 incident/operation context 做执行后健康检查。
- `toolsets/remediation_plan.py`（可选）
  - 将 approval context 规范化为 allowlisted action，不接受自由文本命令直接执行。

`hooks/approval_reply.py` 不直接 import 这些模块。

#### Execution Trigger

初期推荐 pull 模式，降低飞书回调路径风险：

```text
approved approval
  |
  v
approval_execution.process_pending_executions(limit=N)
  |- find approved but not executed approvals
  |- create execution record / idempotency key
  `- process one execution at a time
```

后续可加后台 worker 或 cron，但第一版可以由显式 tool/test 调用驱动。

#### Execution State

新增 execution 子状态，不升级为 incident 主状态：

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

建议新增表 `approval_executions`：

```sql
CREATE TABLE approval_executions (
  id TEXT PRIMARY KEY,
  approval_id TEXT NOT NULL UNIQUE,
  incident_id TEXT,
  action_signature TEXT NOT NULL,
  operation_type TEXT NOT NULL,
  namespace TEXT NOT NULL,
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
```

`approval_id` 唯一，确保同一个 approval 不会执行两次。

#### Allowed Operation Shape

第一版只允许结构化 action，不允许从自然语言拼接 kubectl：

```json
{
  "action_type": "scale_deployment",
  "cluster": "prod-a",
  "namespace": "default",
  "resource_kind": "deployment",
  "resource_name": "nginx",
  "parameters": {"replicas": 3}
}
```

第一版 allowlist：

- `scale_deployment`
- `restart_deployment` only if implemented as controlled rollout restart
- `patch_resource_limit` only after separate design review

Explicitly out of initial execution:

- `k8s_exec`
- `kubectl delete`
- namespace/node/PV/CRD operations
- free-form shell commands
- multi-resource batch writes

#### Execution Flow

```text
process approved approval
  |
  |- load approval + incident
  |- verify approval.status == approved
  |- verify execution does not already exist
  |- normalize context into allowlisted action
  |- re-run authorization snapshot checks
  |- create approval_execution(status=queued)
  |- run dry-run
  |    |- failed -> status=dry_run_failed, timeline, user reply/notification, stop
  |- acquire operation_lock(lock_key)
  |    |- failed -> status=lock_waiting or failed, timeline, stop
  |- update incident status executing
  |- execute action through safe tool API
  |- write audit_log
  |- run health check
  |    |- healthy -> status=succeeded, incident verifying/resolved as appropriate
  |    `- unhealthy -> status=rollback_required or failed, notify human
  `- release lock
```

#### Dry-Run Policy

- Prefer server-side dry-run when Kubernetes supports it.
- If only client-side dry-run is possible, execution requires `risk_level=low` and explicit config `allow_client_dry_run_execution=true`.
- If dry-run cannot be performed, do not execute automatically.
- Dry-run result must be saved in `approval_executions.dry_run_result_json`.

#### Lock Policy

Lock key format:

```text
{cluster}:{namespace}:{resource_kind}/{resource_name}
```

Rules:

- Lock is acquired after dry-run and before real execution.
- Lock conflict prevents execution.
- Lock conflict does not revert approval status; it records execution failure/conflict.
- Lock must be released in `finally` unless process crashes; stale lock cleanup remains handled by `operation_lock` TTL.

#### Health Check Policy

First version health checks are conservative and Kubernetes-read-only:

- Deployment desired/available replicas match expected state.
- Pods for the workload become Ready within timeout.
- Restart count does not continue increasing during observation window.
- Recent events do not show new Failed/BackOff/OOMKilled for the workload.

Health check result shape:

```json
{
  "ok": true,
  "checks": [
    {"name": "available_replicas", "ok": true, "summary": "3/3 available"}
  ],
  "observed_at": 1234567890.0
}
```

#### Rollback Policy

Do not implement automatic rollback in the first execution implementation unless snapshot and restore are fully designed for the action type.

For first execution implementation:

- If health check fails, mark `rollback_required` and notify human.
- Only actions with deterministic inverse may later support automatic rollback.
- Rollback action must have its own audit record.

#### User-Facing Messages

- Dry-run failed: `审批已批准，但 dry-run 失败，未执行：<reason>`
- Lock conflict: `审批已批准，但资源正在被其他操作占用，未执行：<lock_key>`
- Execution success: `审批动作已执行并通过健康检查：<approval_id>`
- Health failed: `审批动作已执行，但健康检查失败，需要人工确认：<approval_id>`
- Duplicate execution: `审批动作已处理过，未重复执行：<approval_id>`

#### Tests

Add `tests/test_approval_execution.py`:

- Approved approval creates one execution record.
- Running coordinator twice does not execute twice.
- Non-approved approval is ignored.
- Free-form/unrecognized action is rejected.
- Dry-run failure prevents execution.
- Lock conflict prevents execution.
- Successful execution writes audit and timeline.
- Health check failure marks `rollback_required` and does not claim resolved.

Add/extend `tests/test_operation_lock.py`:

- Execution lock key format is stable.
- Stale locks do not permanently block future execution.

Add/extend `tests/test_alert_webhook.py`:

- Approval context contains structured action fields needed by execution coordinator.
- Approval context includes `action_signature` idempotency key.

### Phase C: Feishu Card Buttons

Goal: improve UX without changing approval state semantics.

Rules:

- Card callbacks must call the same authorization + approval reply service path.
- Card callback payload carries `approval_id`, `decision`, and optional reason.
- Text approval remains supported as fallback.
- Card approval must not bypass approver identity or self-approval checks.

Initial implementation can stay out of scope until text approval + authorization + execution design are stable.

## Test Plan

Current coverage to keep green:

- `tests/test_feishu_approval_overlay.py`
  - Approval text is intercepted.
  - Deny reason is preserved.
  - Normal text reaches original Hermes flow.
  - Missing sender identity refuses safely.
  - Adapter shape change fails fast.
  - Gateway wrapper installs overlay before runner.
- `tests/test_deploy_entrypoint.py`
  - Normal mode starts wrapper.
  - Webhook-only mode skips gateway wrapper.
- `tests/test_approval_reply.py`
  - Approve/deny parse and resolve.
  - Unknown approval returns clear error.
- `tests/test_voice_context.py`
  - Voice/thread context remains incident enrichment only.

Verification command:

```bash
rtk pytest tests/test_feishu_approval_overlay.py tests/test_deploy_entrypoint.py tests/test_approval_reply.py tests/test_voice_context.py -q
```

## Assumptions

- Text approval is the MVP interaction.
- `hermes-agent` remains upstream-owned.
- Parent project runtime overlay is acceptable as a narrow compatibility layer.
- Approval reply state mutation remains centralized in `hooks/approval_reply.py`.
- Automatic Kubernetes execution is a separate phase and requires explicit design approval.
