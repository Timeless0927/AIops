# Phase 3 Alert Diagnosis and Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Phase 2 已经可用的 Alertmanager incident 诊断链路推进到 Phase 3 的最小闭环：告警去重后自动诊断，诊断建议能创建非阻塞审批，并且审批结果能回写 incident timeline。

**Architecture:** 复用现有 `hooks/alert_webhook.py`、`toolsets/alert_dedup.py`、`toolsets/approval_async.py`、`toolsets/operation_lock.py` 和 `toolsets/incident_store.py`，只补缺少的接线层。第一版只让 webhook 从分析结果生成一个待审批的建议动作，不执行真实写操作；执行、dry-run、rollback、cron 巡检另列后续阶段。

**Tech Stack:** Python 3.11, aiohttp, SQLite WAL, pytest, existing Hermes tool registry, Feishu thread workflow, current `rtk` shell wrapper.

---

## Scope Boundaries

### In Scope

- Alertmanager webhook 继续使用现有 HMAC 校验、dedup、incident 复用和 Feishu thread summary。
- 从 persisted analysis 的 `next_best_actions` 生成一个明确的 approval candidate。
- 创建 `approval_async` pending record，绑定 `incident_id`、`operation_type`、`risk_level`、`namespace`、`requester` 和上下文。
- 在 incident timeline 中记录 `approval_requested`、`approval_approved`、`approval_denied`、`approval_expired` 事件。
- 新增一个 webhook/handler 入口处理 Feishu 审批回复文本：`批准 <approval_id>` / `拒绝 <approval_id> <reason>`。
- `sre_metrics` 暴露 pending approval baseline，帮助 Phase 3 验收是否产生积压。
- 测试覆盖 webhook 自动建审批、审批回写 incident、重复审批幂等、过期审批回写、metrics。

### Explicitly Out of Scope

- 不接 cron 定时巡检。
- 不执行真实 `k8s_write` / `k8s_exec`。
- 不做 server-side dry-run、执行后健康检查、自动 rollback。
- 不做 Feishu 交互卡片按钮，只支持文本审批回复。
- 不引入新的 planner、memory、queue 或多 agent 调度框架。
- 不重写 `alert_dedup.py` 的内存态去重实现；Phase 3 只补接线与可观测性。

## What Already Exists

- `hooks/alert_webhook.py` 已完成 Alertmanager payload 提取、HMAC 校验、dedup 调用、incident 创建/复用、targeted evidence 采集、analysis 持久化、similar case recall 和 Feishu thread summary。
- `toolsets/approval_async.py` 已有 SQLite approval lifecycle：request/check/resolve/execute/expire，且支持 `incident_id` 和 `approval_message_id` 字段。
- `toolsets/operation_lock.py` 已有资源锁基础能力，但本计划第一版不执行写操作，因此只在 approval context 中记录目标 resource key，不获取锁。
- `toolsets/incident_store.py` 已有 timeline、analysis、evidence、case profile、Feishu binding 与 incident status 字段。
- `toolsets/sre_metrics.py` 已能读取 incidents 和 approvals，用于追加 pending approval 指标。

## Data Flow

```text
Alertmanager POST
  |
  v
hooks/alert_webhook.py
  |- verify HMAC
  |- alert_dedup.should_process(alert)
  |- create/reuse incident
  |- collect targeted read-only evidence
  |- upsert persisted analysis
  |- render Feishu thread summary
  `- maybe_request_phase3_approval()
       |- pick one action from analysis.next_best_actions
       |- classify operation_type + risk_level
       |- approval_async.request_approval(..., incident_id=incident_id)
       |- incident_store.add_event(approval_requested)
       `- append approval instructions to thread summary

Feishu text reply
  |
  v
approval reply handler
  |- parse: 批准 <approval_id> / 拒绝 <approval_id> <reason>
  |- approval_async.resolve_approval(...)
  |- incident_store.add_event(approval_approved|approval_denied)
  `- return human-readable reply text
```

## Failure Modes

- Analysis has no actionable next step: no approval is created; timeline records `approval_skipped` with reason `no_action`.
- Same incident is reprocessed by dedup/retry: no duplicate pending approval for the same incident/action signature.
- Feishu reply references missing approval ID: return a clear not-found message and do not mutate incident state.
- Approval was already resolved/expired: return current status and do not add duplicate timeline events.
- `approval_async` database is locked: existing WAL retry path handles transient lock; test covers request path through monkeypatch.

## File Structure

- Modify: `hooks/alert_webhook.py` — add approval candidate generation and call it after persisted analysis is written.
- Modify: `hooks/voice_context.py` or create `hooks/approval_reply.py` — parse Feishu approval replies and resolve approvals. Prefer creating `hooks/approval_reply.py` if existing message hook boundaries are unclear.
- Modify: `toolsets/approval_async.py` — add lookup helpers needed for idempotency and incident-bound approval resolution if missing.
- Modify: `toolsets/incident_store.py` — add small timeline wrapper helpers only if current `add_event()` is too awkward for approval events.
- Modify: `toolsets/sre_metrics.py` — add `pending_approval_count` and `approval_backlog_rate`.
- Modify: `tests/test_alert_webhook.py` — cover webhook-created approval and duplicate prevention.
- Create or modify: `tests/test_approval_reply.py` — cover approve/deny/not-found/already-resolved reply handling.
- Modify: `tests/test_approval_async.py` — cover new idempotency lookup helpers.
- Modify: `tests/test_sre_metrics.py` — cover pending approval baseline.

## Task 1: Add Approval Idempotency Helpers

**Files:**
- Modify: `tests/test_approval_async.py`
- Modify: `toolsets/approval_async.py`

- [ ] **Step 1: Write the failing idempotency lookup test**

Add to `tests/test_approval_async.py`:

```python
@pytest.mark.asyncio
async def test_find_pending_by_incident_and_signature(tmp_path: Path, **_: object) -> None:
    """同一 incident/action signature 应复用已有 pending approval。"""
    module = _load_module(tmp_path)
    approval_id = await module.request_approval(
        "k8s_write",
        "检查并重启 deployment/nginx",
        {"action_signature": "restart:default:deployment/nginx"},
        "default",
        "alert_webhook",
        "standard",
        incident_id="inc-1",
    )

    found = await module.find_pending_approval(
        incident_id="inc-1",
        action_signature="restart:default:deployment/nginx",
    )

    assert found is not None
    assert found["approval_id"] == approval_id
    assert found["status"] == "pending"
```

- [ ] **Step 2: Run the focused test to verify RED**

Run:

```bash
rtk pytest tests/test_approval_async.py::test_find_pending_by_incident_and_signature -q
```

Expected: FAIL with `AttributeError` because `find_pending_approval` does not exist.

- [ ] **Step 3: Add the helper**

Add async wrapper and DB method:

```python
def find_pending_approval(self, incident_id: str, action_signature: str) -> Dict[str, Any] | None:
    with self._lock:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE incident_id = ? AND status = 'pending' ORDER BY created_at DESC",
            (incident_id,),
        ).fetchall()
    for row in rows:
        context = json.loads(row["context_json"] or "{}")
        if context.get("action_signature") == action_signature:
            return self.check_approval(row["id"])
    return None


async def find_pending_approval(incident_id: str, action_signature: str) -> Dict[str, Any] | None:
    return await asyncio.to_thread(_DB.find_pending_approval, incident_id, action_signature)
```

- [ ] **Step 4: Run approval tests**

Run:

```bash
rtk pytest tests/test_approval_async.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
rtk git add tests/test_approval_async.py toolsets/approval_async.py
rtk git commit -m "feat: reuse pending incident approvals"
```

## Task 2: Create Approval Candidate From Webhook Analysis

**Files:**
- Modify: `tests/test_alert_webhook.py`
- Modify: `hooks/alert_webhook.py`

- [ ] **Step 1: Write the failing webhook approval test**

Add a test that monkeypatches `module.approval_async.request_approval` and sends a firing Alertmanager payload whose analysis contains `next_best_actions=["重启 deployment/nginx"]`.

Assert:

```python
assert approval_call["operation_type"] == "k8s_write"
assert approval_call["namespace"] == "default"
assert approval_call["incident_id"] == result["incident_id"]
assert approval_call["context"]["action_signature"] == "k8s_write:default:重启 deployment/nginx"
assert any(event["event_type"] == "approval_requested" for event in timeline)
```

- [ ] **Step 2: Run the focused test to verify RED**

Run:

```bash
rtk pytest tests/test_alert_webhook.py::test_webhook_requests_approval_for_next_best_action -q
```

Expected: FAIL because webhook does not create approvals.

- [ ] **Step 3: Load `approval_async` in `hooks/alert_webhook.py`**

Follow the module-loading pattern already used for `incident_store`:

```python
def _load_approval_async_module():
    module_name = "aiops_approval_async"
    if module_name in sys.modules:
        return sys.modules[module_name]
    module_path = _project_root() / "toolsets" / "approval_async.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 approval_async: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


approval_async = _load_approval_async_module()
```

- [ ] **Step 4: Add candidate helpers**

Add small helpers near `_initial_analysis()`:

```python
def _pick_approval_action(analysis: dict[str, Any]) -> str | None:
    actions = analysis.get("next_best_actions")
    if not isinstance(actions, list):
        return None
    for action in actions:
        if isinstance(action, str) and action.strip():
            return action.strip()
    return None


def _approval_operation_type(action: str) -> str:
    lowered = action.lower()
    if "exec" in lowered or "进入" in action:
        return "k8s_exec"
    return "k8s_write"


def _approval_risk_level(operation_type: str, action: str) -> str:
    if operation_type == "k8s_exec":
        return "elevated"
    dangerous_words = ("删除", "delete", "namespace", "node", "pv")
    if any(word in action.lower() for word in dangerous_words):
        return "dangerous"
    return "standard"
```

- [ ] **Step 5: Add `maybe_request_phase3_approval()`**

```python
async def _maybe_request_phase3_approval(incident_id: str, alert: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any] | None:
    action = _pick_approval_action(analysis)
    namespace = str(alert.get("namespace") or "default")
    if not action:
        await incident_store.add_event(incident_id, "approval_skipped", "alert_webhook", "", "no_action")
        return None

    operation_type = _approval_operation_type(action)
    risk_level = _approval_risk_level(operation_type, action)
    action_signature = f"{operation_type}:{namespace}:{action}"
    existing = await approval_async.find_pending_approval(incident_id, action_signature)
    if existing:
        return existing

    approval_id = await approval_async.request_approval(
        operation_type,
        action,
        {
            "action_signature": action_signature,
            "alertname": alert.get("alertname"),
            "namespace": namespace,
            "cluster": alert.get("cluster"),
            "source": "alert_webhook",
        },
        namespace,
        "alert_webhook",
        risk_level,
        incident_id=incident_id,
    )
    await incident_store.add_event(incident_id, "approval_requested", "alert_webhook", action, approval_id)
    return await approval_async.check_approval(approval_id)
```

- [ ] **Step 6: Call it after analysis persistence**

In the firing-alert path after `incident_store.upsert_analysis(...)`, call:

```python
approval = await _maybe_request_phase3_approval(incident_id, enriched_alert, analysis)
if approval:
    analysis.setdefault("next_best_actions", []).append(f"审批待处理：{approval['approval_id']}")
```

- [ ] **Step 7: Run webhook tests**

Run:

```bash
rtk pytest tests/test_alert_webhook.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
rtk git add tests/test_alert_webhook.py hooks/alert_webhook.py
rtk git commit -m "feat: request approvals from alert analysis"
```

## Task 3: Add Feishu Text Approval Reply Handler

**Files:**
- Create: `hooks/approval_reply.py`
- Create: `tests/test_approval_reply.py`

- [ ] **Step 1: Write failing parser tests**

Create `tests/test_approval_reply.py` with tests for:

```python
def test_parse_approve_reply() -> None:
    parsed = module.parse_approval_reply("批准 abc-123")
    assert parsed == {"decision": "approved", "approval_id": "abc-123", "reason": None}


def test_parse_deny_reply_with_reason() -> None:
    parsed = module.parse_approval_reply("拒绝 abc-123 风险过高")
    assert parsed == {"decision": "denied", "approval_id": "abc-123", "reason": "风险过高"}


def test_parse_non_approval_reply() -> None:
    assert module.parse_approval_reply("看一下 nginx") is None
```

- [ ] **Step 2: Run parser tests to verify RED**

Run:

```bash
rtk pytest tests/test_approval_reply.py -q
```

Expected: FAIL because file/module does not exist.

- [ ] **Step 3: Implement parser and handler skeleton**

Create `hooks/approval_reply.py`:

```python
"""飞书文本审批回复处理。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_tool_module(file_name: str, module_name: str):
    if module_name in sys.modules:
        return sys.modules[module_name]
    module_path = _project_root() / "toolsets" / f"{file_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {file_name}: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


approval_async = _load_tool_module("approval_async", "aiops_approval_async")
incident_store = _load_tool_module("incident_store", "aiops_incident_store")


def parse_approval_reply(text: str) -> dict[str, str | None] | None:
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 2:
        return None
    verb = parts[0]
    if verb not in {"批准", "拒绝"}:
        return None
    return {
        "decision": "approved" if verb == "批准" else "denied",
        "approval_id": parts[1],
        "reason": parts[2].strip() if len(parts) > 2 and parts[2].strip() else None,
    }


async def handle_approval_reply(text: str, approver: str) -> dict[str, Any]:
    parsed = parse_approval_reply(text)
    if parsed is None:
        return {"handled": False}
    result = await approval_async.resolve_approval(
        str(parsed["approval_id"]),
        str(parsed["decision"]),
        approver,
        parsed.get("reason"),
    )
    if not result.get("ok"):
        return {"handled": True, "ok": False, "message": result.get("message"), "approval_id": parsed["approval_id"]}
    approval = await approval_async.check_approval(str(parsed["approval_id"]))
    incident_id = approval.get("incident_id")
    if incident_id:
        event_type = "approval_approved" if parsed["decision"] == "approved" else "approval_denied"
        await incident_store.add_event(str(incident_id), event_type, "approval_reply", str(parsed["approval_id"]), approver)
    return {"handled": True, "ok": True, "approval_id": parsed["approval_id"], "status": result.get("status")}
```

- [ ] **Step 4: Add lifecycle tests**

Add async tests that monkeypatch `approval_async.resolve_approval`, `approval_async.check_approval`, and `incident_store.add_event`.

Assert approved and denied replies add the correct timeline event.

- [ ] **Step 5: Run tests**

Run:

```bash
rtk pytest tests/test_approval_reply.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
rtk git add hooks/approval_reply.py tests/test_approval_reply.py
rtk git commit -m "feat: handle text approval replies"
```

## Task 4: Add Expired Approval Timeline Reconciliation

**Files:**
- Modify: `tests/test_recovery.py`
- Modify: `hooks/recovery.py`

- [ ] **Step 1: Write failing recovery test**

Add a test where `approval_async.expire_stale()` returns expired approval rows:

```python
{"expired": 1, "approvals": [{"approval_id": "ap-1", "incident_id": "inc-1"}]}
```

Assert `incident_store.add_event("inc-1", "approval_expired", "recovery", "ap-1", "")` is called.

- [ ] **Step 2: Run focused test to verify RED**

Run:

```bash
rtk pytest tests/test_recovery.py::test_recovery_records_expired_approval_timeline -q
```

Expected: FAIL because recovery currently only returns counts.

- [ ] **Step 3: Extend `approval_async.expire_stale()` return shape if needed**

If the current function only returns counts, update it to include expired approval IDs and incident IDs while preserving existing keys:

```python
return {"ok": True, "expired": len(expired_rows), "approvals": expired_rows}
```

- [ ] **Step 4: Reconcile in recovery hook**

After expiring approvals, add timeline events for rows with `incident_id`.

- [ ] **Step 5: Run recovery and approval tests**

Run:

```bash
rtk pytest tests/test_recovery.py tests/test_approval_async.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
rtk git add tests/test_recovery.py hooks/recovery.py toolsets/approval_async.py
rtk git commit -m "feat: record expired approval events"
```

## Task 5: Extend Metrics With Approval Backlog

**Files:**
- Modify: `tests/test_sre_metrics.py`
- Modify: `toolsets/sre_metrics.py`

- [ ] **Step 1: Write failing metrics test**

Add a test that stubs approval stats as pending=3 and total=10.

Assert:

```python
assert result["pending_approval_count"] == 3
assert result["approval_backlog_rate"] == 0.3
```

- [ ] **Step 2: Run focused test to verify RED**

Run:

```bash
rtk pytest tests/test_sre_metrics.py::test_compute_metrics_includes_approval_backlog -q
```

Expected: FAIL because metrics do not expose backlog fields.

- [ ] **Step 3: Add metrics fields**

Read from approval stats helper already used by `sre_metrics.py`, then return:

```python
"pending_approval_count": pending_approvals,
"approval_backlog_rate": (pending_approvals / total_approvals) if total_approvals else None,
```

- [ ] **Step 4: Run metrics tests**

Run:

```bash
rtk pytest tests/test_sre_metrics.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
rtk git add tests/test_sre_metrics.py toolsets/sre_metrics.py
rtk git commit -m "feat: expose approval backlog metrics"
```

## Task 6: Final Phase 3 Regression

**Files:**
- No source changes unless a regression is found.

- [ ] **Step 1: Run focused Phase 3 suite**

Run:

```bash
rtk pytest \
  tests/test_alert_webhook.py \
  tests/test_approval_async.py \
  tests/test_approval_reply.py \
  tests/test_recovery.py \
  tests/test_sre_metrics.py \
  tests/test_operation_lock.py -q
```

Expected: PASS.

- [ ] **Step 2: Run full suite**

Run:

```bash
rtk pytest -q
```

Expected: PASS.

- [ ] **Step 3: Confirm scope did not expand**

Run:

```bash
rtk git diff --stat main...
```

Expected: diffs are limited to approval/webhook/recovery/metrics tests and modules. No cron, rollback, deployment, or planner files.

- [ ] **Step 4: Write PR/handoff summary**

Use this summary:

```markdown
Phase 3 MVP only adds the approval bridge after automatic alert diagnosis:
1. Webhook diagnosis can create one deduplicated pending approval from next_best_actions.
2. Feishu text replies can approve/deny that approval and update incident timeline.
3. Recovery and metrics expose expired/pending approval state.

Explicitly not included: cron巡检、真实执行、dry-run、自动 rollback、Feishu card buttons。
```

## NOT in Scope

- Cron health checks: useful, but independent from alert-to-approval MVP and should ship as its own workstream.
- Real `k8s_write` / `k8s_exec` execution: higher blast radius; needs dry-run, locks, audit, and rollback first.
- Auto rollback: depends on snapshots and post-execution health checks, so it belongs after execution is wired.
- Feishu card buttons: better UX, but text approval is enough to validate the state machine and storage contract.
- Persistent dedup store: valuable after multi-process deployment is confirmed; current in-memory dedup is already used by webhook tests.

## Parallelization Strategy

Sequential implementation. Tasks share `approval_async`, `alert_webhook`, and incident timeline contracts, so parallel worktrees would create avoidable merge and behavior conflicts.

## Self-Review

### Spec Coverage

- Alertmanager automatic diagnosis is already present from Phase 2 and reused by Task 2.
- Non-blocking approval creation is covered by Task 1 and Task 2.
- Human approval reply handling is covered by Task 3.
- Approval timeout visibility is covered by Task 4.
- Operational visibility is covered by Task 5.

### Placeholder Scan

- No `TBD`, `TODO`, or `similar to Task N` placeholders remain.
- Each task names exact files, commands, expected failures, and expected pass conditions.

### Type Consistency

- `approval_id` is always a string.
- `incident_id` is always passed through `approval_async.request_approval(..., incident_id=...)`.
- `action_signature` lives in `approval.context` and is used only for idempotency.
- Timeline event types use explicit strings: `approval_requested`, `approval_approved`, `approval_denied`, `approval_expired`, `approval_skipped`.
