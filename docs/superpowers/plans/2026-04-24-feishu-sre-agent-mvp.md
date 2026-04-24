# Feishu SRE Agent MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first production-shaped Feishu AIOps SRE Agent loop from Alertmanager firing alert to incident timeline, Feishu binding metadata, approval association, delivery retry state, and recoverable close/reopen semantics.

**Architecture:** Extend the current SQLite-backed SRE tool modules incrementally instead of introducing a workflow engine. `incident_store.py` remains the incident truth source, `approval_async.py` gains incident/message linkage, a new `message_delivery.py` owns Feishu delivery compensation state, and `alert_webhook.py` orchestrates alert ingestion by calling these storage APIs.

**Tech Stack:** Python 3.11, SQLite WAL, aiohttp webhook tests, pytest/pytest-asyncio, existing Hermes tool registry patterns.

---

## File Structure

- Modify: `toolsets/incident_store.py` — extend incident schema, add status constants, dedup/reopen helpers, Feishu binding APIs, and backward-compatible migrations.
- Modify: `tests/test_incident_store.py` — cover schema migration, status transition validation, dedup lookup, reopen, close, and Feishu metadata updates.
- Modify: `toolsets/approval_async.py` — add `incident_id` and `approval_message_id` to approval records and API output.
- Modify: `tests/test_approval_async.py` — cover incident/message association and backward-compatible request calls.
- Create: `toolsets/message_delivery.py` — persist delivery target status, retry attempts, payload hash, errors, and idempotent upserts.
- Create: `tests/test_message_delivery.py` — cover create/update/retry/pending queries and idempotent payload handling.
- Modify: `hooks/alert_webhook.py` — create/reuse incidents, write `alert_fired`, return incident-aware response, and prepare Feishu delivery records without directly depending on a real Feishu API.
- Modify: `tests/test_alert_webhook.py` — update expectations from prompt-only behavior to incident lifecycle behavior.
- Modify: `config.yaml` — add `reopen_window_seconds`, `storm_threshold_per_minute`, `dedup_key_version`, `raw_output_ttl_hours`, and `system_mode_store` defaults.
- Modify: `docs/feishu-sre-agent-detailed-design.md` — mark each implemented MVP piece as mapped to code after implementation.

---

### Task 1: Extend Incident Schema

**Files:**
- Modify: `toolsets/incident_store.py`
- Modify: `tests/test_incident_store.py`

- [ ] **Step 1: Write failing test for extended incident fields**

Append this test to `tests/test_incident_store.py`:

```python
@pytest.mark.asyncio
async def test_create_incident_stores_dedup_and_feishu_fields(tmp_path: Path, **_: object) -> None:
    """新建 incident 应保存 dedup 与飞书绑定字段。"""
    module, store = _load_module(tmp_path)

    incident_id = await module.create_incident(
        "PodCrashLooping",
        "default",
        "prod-a",
        "pod 重启次数持续增加",
        platform="feishu",
        chat_id="oc_ops",
        root_message_id="om_root",
        thread_id="omt_thread",
        status_card_message_id="om_card",
        dedup_key="PodCrashLooping|default|prod-a",
        dedup_key_version="v1",
    )
    incident = await module.get_incident(incident_id)

    assert incident["id"] == incident_id
    assert incident["status"] == "new"
    assert incident["platform"] == "feishu"
    assert incident["chat_id"] == "oc_ops"
    assert incident["root_message_id"] == "om_root"
    assert incident["thread_id"] == "omt_thread"
    assert incident["status_card_message_id"] == "om_card"
    assert incident["dedup_key"] == "PodCrashLooping|default|prod-a"
    assert incident["dedup_key_version"] == "v1"
    assert incident["reopen_count"] == 0
    assert incident["closed_at"] is None

    store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_incident_store.py::test_create_incident_stores_dedup_and_feishu_fields -v`

Expected: FAIL with `create_incident() got an unexpected keyword argument 'platform'` or missing `get_incident`.

- [ ] **Step 3: Add schema fields and migration helper**

In `toolsets/incident_store.py`, update `incidents` schema and replace `_ensure_operator_column()` with a generic migration helper:

```python
_INCIDENT_EXTRA_COLUMNS = {
    "operator": "TEXT",
    "closed_at": "REAL",
    "platform": "TEXT",
    "chat_id": "TEXT",
    "root_message_id": "TEXT",
    "thread_id": "TEXT",
    "status_card_message_id": "TEXT",
    "dedup_key": "TEXT",
    "dedup_key_version": "TEXT",
    "reopen_count": "INTEGER NOT NULL DEFAULT 0",
}
```

Ensure `__init__()` calls `_ensure_incident_columns()` and creates indexes:

```python
CREATE INDEX IF NOT EXISTS idx_incidents_dedup ON incidents(dedup_key, dedup_key_version, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_thread ON incidents(platform, chat_id, thread_id);
```

Implement:

```python
def _ensure_incident_columns(self) -> None:
    for column, definition in _INCIDENT_EXTRA_COLUMNS.items():
        try:
            self._conn.execute(f"ALTER TABLE incidents ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            pass
```

- [ ] **Step 4: Extend create/read APIs**

Change `create_incident()` signature to accept optional fields:

```python
async def create_incident(
    self,
    alert_name: str,
    namespace: str,
    cluster: str,
    summary: str,
    *,
    platform: str | None = None,
    chat_id: str | None = None,
    root_message_id: str | None = None,
    thread_id: str | None = None,
    status_card_message_id: str | None = None,
    dedup_key: str | None = None,
    dedup_key_version: str | None = None,
) -> str:
```

Insert status as `new`, not `active`. Add:

```python
async def get_incident(self, incident_id: str) -> dict[str, Any]:
    def _read() -> dict[str, Any]:
        row = self._fetchone("SELECT * FROM incidents WHERE id = ?", (incident_id,))
        if row is None:
            raise ValueError(f"事件不存在: {incident_id}")
        return row
    return await asyncio.to_thread(_read)
```

Update module-level wrapper `create_incident()` with the same keyword-only optional parameters and add a module-level `get_incident()` wrapper.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_incident_store.py::test_create_incident_stores_dedup_and_feishu_fields -v`

Expected: PASS.

- [ ] **Step 6: Run existing incident tests**

Run: `pytest tests/test_incident_store.py -v`

Expected: Existing lifecycle tests may fail because they expect `active`; update assertions to expect `new` for newly-created incidents and active list to exclude only `resolved`/`closed`.

- [ ] **Step 7: Commit**

```bash
git add toolsets/incident_store.py tests/test_incident_store.py
git commit -m "feat: extend incident store for Feishu metadata"
```

---

### Task 2: Add Incident State Machine

**Files:**
- Modify: `toolsets/incident_store.py`
- Modify: `tests/test_incident_store.py`

- [ ] **Step 1: Write failing tests for valid and invalid transitions**

Append:

```python
@pytest.mark.asyncio
async def test_incident_status_transition_validation(tmp_path: Path, **_: object) -> None:
    """incident 主状态只能按设计状态机迁移。"""
    module, store = _load_module(tmp_path)
    incident_id = await module.create_incident("HighMemory", "prod", "cluster-a", "内存升高")

    await module.update_status(incident_id, "triaging")
    await module.update_status(incident_id, "investigating")
    await module.update_status(incident_id, "pending_approval")

    with pytest.raises(ValueError, match="非法状态迁移"):
        await module.update_status(incident_id, "resolved")

    await module.update_status(incident_id, "executing")
    await module.update_status(incident_id, "verifying")
    await module.update_status(incident_id, "resolved", resolved_at=123.0)
    await module.update_status(incident_id, "closed", closed_at=456.0)

    incident = await module.get_incident(incident_id)
    assert incident["status"] == "closed"
    assert incident["resolved_at"] == 123.0
    assert incident["closed_at"] == 456.0

    store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_incident_store.py::test_incident_status_transition_validation -v`

Expected: FAIL because `closed_at` parameter and transition validation do not exist.

- [ ] **Step 3: Add status constants and transition map**

In `toolsets/incident_store.py`, add:

```python
TERMINAL_STATUSES = {"resolved", "closed"}
ACTIVE_STATUSES = {"new", "triaging", "investigating", "pending_approval", "executing", "verifying", "abnormal"}
_ALLOWED_TRANSITIONS = {
    "new": {"triaging", "resolved", "abnormal"},
    "triaging": {"investigating", "resolved", "abnormal"},
    "investigating": {"pending_approval", "executing", "resolved", "abnormal"},
    "pending_approval": {"investigating", "executing", "abnormal"},
    "executing": {"verifying", "abnormal"},
    "verifying": {"resolved", "investigating", "abnormal"},
    "resolved": {"triaging", "closed"},
    "closed": set(),
    "abnormal": {"triaging", "investigating", "closed"},
}
```

- [ ] **Step 4: Validate update_status**

Update `update_status()` to load current status inside the transaction, reject unknown statuses, reject transitions not in `_ALLOWED_TRANSITIONS[current]`, and write `closed_at` when provided.

Use signature:

```python
async def update_status(
    self,
    incident_id: str,
    status: str,
    resolved_at: float | None = None,
    closed_at: float | None = None,
) -> None:
```

- [ ] **Step 5: Update active query**

Change `list_active()` query to:

```sql
WHERE status NOT IN ('resolved', 'closed')
```

- [ ] **Step 6: Run focused tests**

Run: `pytest tests/test_incident_store.py::test_incident_status_transition_validation tests/test_incident_store.py::test_incident_store_lifecycle -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add toolsets/incident_store.py tests/test_incident_store.py
git commit -m "feat: enforce incident state transitions"
```

---

### Task 3: Add Dedup and Reopen Helpers

**Files:**
- Modify: `toolsets/incident_store.py`
- Modify: `tests/test_incident_store.py`
- Modify: `config.yaml`

- [ ] **Step 1: Write failing dedup/reopen tests**

Append:

```python
@pytest.mark.asyncio
async def test_find_reusable_incident_by_dedup_key(tmp_path: Path, **_: object) -> None:
    """相同 dedup key 的未关闭 incident 应被复用。"""
    module, store = _load_module(tmp_path)
    incident_id = await module.create_incident(
        "PodCrashLooping",
        "default",
        "prod-a",
        "pod 重启",
        dedup_key="PodCrashLooping|default|prod-a",
        dedup_key_version="v1",
    )

    found = await module.find_reusable_incident("PodCrashLooping|default|prod-a", "v1")

    assert found["id"] == incident_id
    assert found["status"] == "new"
    store.close()


@pytest.mark.asyncio
async def test_reopen_resolved_incident_increments_count(tmp_path: Path, **_: object) -> None:
    """resolved incident 在窗口内 reopen 时应递增 reopen_count 并写 timeline。"""
    module, store = _load_module(tmp_path)
    incident_id = await module.create_incident(
        "PodCrashLooping",
        "default",
        "prod-a",
        "pod 重启",
        dedup_key="PodCrashLooping|default|prod-a",
        dedup_key_version="v1",
    )
    await module.update_status(incident_id, "triaging")
    await module.update_status(incident_id, "resolved", resolved_at=100.0)

    reopened = await module.reopen_incident(incident_id, "Alertmanager firing again")
    timeline = await module.get_timeline(incident_id)

    assert reopened["status"] == "triaging"
    assert reopened["reopen_count"] == 1
    assert timeline[-1]["event_type"] == "reopened"
    assert timeline[-1]["output_summary"] == "Alertmanager firing again"
    store.close()
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_incident_store.py::test_find_reusable_incident_by_dedup_key tests/test_incident_store.py::test_reopen_resolved_incident_increments_count -v`

Expected: FAIL because helpers and `reopened` event type do not exist.

- [ ] **Step 3: Add event type and APIs**

Add `"reopened"` to `_VALID_EVENT_TYPES`.

Implement:

```python
async def find_reusable_incident(self, dedup_key: str, dedup_key_version: str) -> dict[str, Any] | None:
    def _read() -> dict[str, Any] | None:
        return self._fetchone(
            """
            SELECT * FROM incidents
            WHERE dedup_key = ? AND dedup_key_version = ? AND status != 'closed'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (dedup_key, dedup_key_version),
        )
    return await asyncio.to_thread(_read)
```

Implement `reopen_incident()` as one transaction that sets `status='triaging'`, clears `closed_at`, increments `reopen_count`, then inserts `reopened` timeline.

- [ ] **Step 4: Add config defaults**

In `config.yaml`, add:

```yaml
sre:
  dedup_key_version: "v1"
  raw_output_ttl_hours: 24
  system_mode_store: "sqlite"

notification:
  reopen_window_seconds: 3600
  storm_threshold_per_minute: 60
```

Preserve existing `sre` and `notification` keys by merging values into existing sections, not duplicating sections.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_incident_store.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add toolsets/incident_store.py tests/test_incident_store.py config.yaml
git commit -m "feat: add incident dedup and reopen helpers"
```

---

### Task 4: Link Approvals to Incidents and Messages

**Files:**
- Modify: `toolsets/approval_async.py`
- Modify: `tests/test_approval_async.py`

- [ ] **Step 1: Write failing approval association test**

Append:

```python
@pytest.mark.asyncio
async def test_approval_records_incident_and_message_ids(tmp_path: Path, **_kwargs) -> None:
    """审批记录应关联 incident 与飞书审批消息。"""
    module = _load_module(tmp_path)

    approval_id = await module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/web -n prod",
        {"resource": "deployment/web"},
        "prod",
        "alice",
        "dangerous",
        incident_id="incident-1",
        approval_message_id="om_approval",
    )
    checked = await module.check_approval(approval_id)

    assert checked["incident_id"] == "incident-1"
    assert checked["approval_message_id"] == "om_approval"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_approval_async.py::test_approval_records_incident_and_message_ids -v`

Expected: FAIL because `request_approval()` does not accept `incident_id` or `approval_message_id`.

- [ ] **Step 3: Extend schema and migration**

Add columns to schema:

```sql
incident_id TEXT,
approval_message_id TEXT,
```

Extend `_init_db()` with:

```python
for column, definition in {
    "denial_reason": "TEXT",
    "incident_id": "TEXT",
    "approval_message_id": "TEXT",
}.items():
    try:
        self._conn.execute(f"ALTER TABLE approvals ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError:
        pass
```

- [ ] **Step 4: Extend request and response APIs**

Add keyword-only args to `request_approval()`:

```python
incident_id: str | None = None,
approval_message_id: str | None = None,
```

Insert them into `approvals`, and include both fields in `check_approval()` output.

Update module-level `request_approval()` and tool handler to pass optional `incident_id` / `approval_message_id` from args.

- [ ] **Step 5: Run approval tests**

Run: `pytest tests/test_approval_async.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add toolsets/approval_async.py tests/test_approval_async.py
git commit -m "feat: associate approvals with incidents"
```

---

### Task 5: Add Message Delivery Store

**Files:**
- Create: `toolsets/message_delivery.py`
- Create: `tests/test_message_delivery.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_message_delivery.py`:

```python
"""测试消息投递补偿状态。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module(tmp_path: Path):
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "message_delivery.py"
    spec = importlib.util.spec_from_file_location("test_message_delivery_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._DB.close()
    module._DB = module.MessageDeliveryDB(tmp_path / "message_deliveries.db")
    return module


@pytest.mark.asyncio
async def test_message_delivery_lifecycle(tmp_path: Path, **_: object) -> None:
    module = _load_module(tmp_path)

    delivery_id = await module.upsert_delivery(
        incident_id="incident-1",
        target_type="thread_summary",
        platform="feishu",
        chat_id="oc_ops",
        thread_id="omt_thread",
        payload_hash="hash-1",
    )
    pending = await module.list_pending()
    assert pending[0]["id"] == delivery_id
    assert pending[0]["delivery_status"] == "pending"

    await module.mark_failed(delivery_id, "timeout")
    failed = await module.get_delivery(delivery_id)
    assert failed["delivery_status"] == "failed"
    assert failed["delivery_attempts"] == 1
    assert failed["last_delivery_error"] == "timeout"

    await module.mark_sent(delivery_id, "om_msg")
    sent = await module.get_delivery(delivery_id)
    assert sent["delivery_status"] == "sent"
    assert sent["target_message_id"] == "om_msg"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_message_delivery.py -v`

Expected: FAIL because `toolsets/message_delivery.py` does not exist.

- [ ] **Step 3: Implement `MessageDeliveryDB`**

Create `toolsets/message_delivery.py` using the same SQLite WAL and `_execute_write()` retry pattern as `incident_store.py`. Schema:

```sql
CREATE TABLE IF NOT EXISTS message_deliveries (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    approval_id TEXT,
    target_type TEXT NOT NULL,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT,
    target_message_id TEXT,
    delivery_status TEXT NOT NULL,
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    last_delivery_error TEXT,
    last_delivery_at REAL,
    payload_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(incident_id, target_type, payload_hash)
);
CREATE INDEX IF NOT EXISTS idx_message_deliveries_status ON message_deliveries(delivery_status, updated_at);
```

Expose async functions:

```python
async def upsert_delivery(...): ...
async def get_delivery(delivery_id: str) -> dict[str, Any]: ...
async def mark_failed(delivery_id: str, error: str) -> None: ...
async def mark_sent(delivery_id: str, target_message_id: str) -> None: ...
async def list_pending(limit: int = 100) -> list[dict[str, Any]]: ...
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_message_delivery.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add toolsets/message_delivery.py tests/test_message_delivery.py
git commit -m "feat: add message delivery compensation store"
```

---

### Task 6: Make Alert Webhook Incident-Aware

**Files:**
- Modify: `hooks/alert_webhook.py`
- Modify: `tests/test_alert_webhook.py`

- [ ] **Step 1: Write failing webhook incident test**

Update `test_webhook_formats_prompt_and_skips_resolved` to expect incident IDs:

```python
assert data["incidents"][0]["incident_id"]
assert data["incidents"][0]["event_type"] == "alert_fired"
assert data["prompts"][0].startswith("[Incident ")
```

Add a monkeypatchable fake incident module in the test:

```python
class FakeIncidentStore:
    def __init__(self) -> None:
        self.created = []
        self.events = []

    async def create_incident(self, alert_name, namespace, cluster, summary, **kwargs):
        self.created.append((alert_name, namespace, cluster, summary, kwargs))
        return "incident-1"

    async def add_event(self, incident_id, event_type, tool_name, input_summary, output_summary, metadata=None):
        self.events.append((incident_id, event_type, tool_name, input_summary, output_summary, metadata))
        return 1

    async def find_reusable_incident(self, dedup_key, dedup_key_version):
        return None
```

Patch it with:

```python
fake_store = FakeIncidentStore()
monkeypatch.setattr(module, "incident_store", fake_store)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_alert_webhook.py::test_webhook_formats_prompt_and_skips_resolved -v`

Expected: FAIL because webhook does not load or call `incident_store`.

- [ ] **Step 3: Load incident store and compute dedup key**

In `hooks/alert_webhook.py`, add a loader similar to `_load_alert_dedup_module()` for `toolsets/incident_store.py`.

Add:

```python
def _build_dedup_key(alert: Dict[str, Any]) -> str:
    return "|".join([alert["alertname"], alert["namespace"], alert["cluster"]])
```

Add config helper:

```python
def _dedup_key_version(config: Dict[str, Any]) -> str:
    sre = config.get("sre") if isinstance(config.get("sre"), dict) else {}
    return str(sre.get("dedup_key_version", "v1"))
```

- [ ] **Step 4: Create or reuse incidents in webhook**

Inside firing processing:

```python
dedup_key = _build_dedup_key(alert)
dedup_key_version = _dedup_key_version(config)
existing = await incident_store.find_reusable_incident(dedup_key, dedup_key_version)
if existing is None:
    incident_id = await incident_store.create_incident(..., platform="feishu", dedup_key=dedup_key, dedup_key_version=dedup_key_version)
else:
    incident_id = existing["id"]
await incident_store.add_event(incident_id, "alert_fired", "alert_webhook", alert["alertname"], alert["description"], alert)
```

Return:

```python
"incidents": [{"incident_id": incident_id, "event_type": "alert_fired", "dedup_key": dedup_key}]
```

- [ ] **Step 5: Update prompt format**

Change `_build_triage_prompt(alert)` to `_build_triage_prompt(alert, incident_id)` and return:

```python
f"[Incident {incident_id}] [Alertmanager] {alert['severity']} 告警: ..."
```

- [ ] **Step 6: Run webhook tests**

Run: `pytest tests/test_alert_webhook.py -v`

Expected: PASS after updating expected prompt strings.

- [ ] **Step 7: Commit**

```bash
git add hooks/alert_webhook.py tests/test_alert_webhook.py
git commit -m "feat: create incidents from alert webhook"
```

---

### Task 7: Add System Mode Store

**Files:**
- Create: `toolsets/system_mode.py`
- Create: `tests/test_system_mode.py`
- Modify: `hooks/health_check.py`

- [ ] **Step 1: Write tests**

Create `tests/test_system_mode.py`:

```python
"""测试系统运行模式存储。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module(tmp_path: Path):
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "system_mode.py"
    spec = importlib.util.spec_from_file_location("test_system_mode_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._DB.close()
    module._DB = module.SystemModeDB(tmp_path / "system_mode.db")
    return module


@pytest.mark.asyncio
async def test_system_mode_defaults_and_updates(tmp_path: Path, **_: object) -> None:
    module = _load_module(tmp_path)

    default = await module.get_system_mode()
    await module.set_system_mode("read_only", "database unavailable")
    updated = await module.get_system_mode()

    assert default["mode"] == "normal"
    assert updated["mode"] == "read_only"
    assert updated["reason"] == "database unavailable"


@pytest.mark.asyncio
async def test_invalid_system_mode_is_rejected(tmp_path: Path, **_: object) -> None:
    module = _load_module(tmp_path)

    with pytest.raises(ValueError, match="不支持的 system_mode"):
        await module.set_system_mode("maintenance", "bad")
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_system_mode.py -v`

Expected: FAIL because module does not exist.

- [ ] **Step 3: Implement module**

Create `toolsets/system_mode.py` with valid modes `{normal, degraded, read_only}` and schema:

```sql
CREATE TABLE IF NOT EXISTS system_mode (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL,
    reason TEXT,
    updated_at REAL NOT NULL
);
```

Expose:

```python
async def get_system_mode() -> dict[str, Any]
async def set_system_mode(mode: str, reason: str | None = None) -> dict[str, Any]
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_system_mode.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add toolsets/system_mode.py tests/test_system_mode.py
git commit -m "feat: add system mode store"
```

---

### Task 8: Document Implemented Mapping

**Files:**
- Modify: `docs/feishu-sre-agent-detailed-design.md`
- Modify: `docs/user-guide.md`

- [ ] **Step 1: Update design implementation mapping**

In `docs/feishu-sre-agent-detailed-design.md`, under `26.0 当前项目实现差距与落地修订`, add a short “MVP 已实现映射” list after implementation:

```markdown
MVP 已实现映射：

- `toolsets/incident_store.py`：incident 主状态、timeline、dedup、reopen、飞书绑定字段。
- `toolsets/approval_async.py`：审批状态、incident 关联、飞书审批消息关联。
- `toolsets/message_delivery.py`：主群卡片、Thread 摘要、审批通知的投递补偿状态。
- `hooks/alert_webhook.py`：Alertmanager firing 告警到 incident/timeline 的入口编排。
- `toolsets/system_mode.py`：平台级 normal/degraded/read_only 运行模式。
```

- [ ] **Step 2: Update user guide run commands**

In `docs/user-guide.md`, add:

```markdown
### Alertmanager Webhook MVP

启动 webhook 后，`POST /webhooks/alertmanager` 会为 firing 告警创建或复用 incident，并写入 `alert_fired` timeline。resolved 告警当前默认跳过；reopen 由 firing 告警命中同一 `dedup_key` 时触发。

关键配置：

- `sre.dedup_key_version`
- `notification.reopen_window_seconds`
- `notification.storm_threshold_per_minute`
```

- [ ] **Step 3: Run docs grep checks**

Run:

```bash
rg -n "message_delivery|system_mode|dedup_key_version|reopen_window_seconds" docs config.yaml
```

Expected: Each term appears in the expected docs/config files.

- [ ] **Step 4: Commit**

```bash
git add docs/feishu-sre-agent-detailed-design.md docs/user-guide.md
git commit -m "docs: map Feishu MVP implementation"
```

---

## Integration Verification

- [ ] **Step 1: Run focused storage tests**

Run:

```bash
pytest tests/test_incident_store.py tests/test_approval_async.py tests/test_message_delivery.py tests/test_system_mode.py -v
```

Expected: PASS.

- [ ] **Step 2: Run webhook tests**

Run:

```bash
pytest tests/test_alert_webhook.py -v
```

Expected: PASS.

- [ ] **Step 3: Run full project tests**

Run:

```bash
pytest tests -v
```

Expected: PASS or only unrelated pre-existing failures. Do not fix unrelated failures in this branch.

- [ ] **Step 4: Push branch**

Run:

```bash
git status --short
git push
```

Expected: Working tree clean and branch pushed.

---

## Self-Review

**Spec coverage:** This plan covers the design document’s MVP gaps: incident fields, status machine, dedup/reopen, approval linkage, message delivery compensation, alert ingestion, system mode, and docs mapping.

**Placeholder scan:** No task contains TBD/TODO/fill-in-later placeholders. Each implementation task includes concrete file paths, test code, commands, and expected outputs.

**Type consistency:** API names are consistent across tasks: `get_incident`, `find_reusable_incident`, `reopen_incident`, `upsert_delivery`, `mark_failed`, `mark_sent`, `get_system_mode`, and `set_system_mode`.
