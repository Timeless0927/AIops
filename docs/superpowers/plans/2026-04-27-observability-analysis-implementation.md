# Observability Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Phase 2 incident-level observability analysis foundations for a single Kubernetes cluster by storing structured evidence, maintaining an incident analysis view, and upgrading incident-aware replies to consume that structured context.

**Architecture:** Keep the existing `Alertmanager -> incident -> Feishu thread` flow intact and extend it with two new persistence layers inside `toolsets/incident_store.py`: `incident_evidence` for time-window evidence records and `incident_analysis` for the latest structured analysis view. Seed the first evidence and analysis skeleton from `hooks/alert_webhook.py`, then make `hooks/voice_context.py` prefer that analysis summary while preserving the current timeline fallback.

**Tech Stack:** Python 3.11, SQLite WAL, aiohttp, pytest, existing Hermes tool registry.

---

## File Structure

- Modify: `toolsets/incident_store.py` — add evidence and analysis schema, persistence APIs, serializers, and optional read tools.
- Modify: `hooks/alert_webhook.py` — seed alert-window evidence and initial analysis skeleton when a firing alert creates or updates an incident.
- Modify: `hooks/voice_context.py` — inject structured incident analysis into bound Feishu thread context, with timeline fallback preserved.
- Modify: `toolsets/sre_metrics.py` — later phase: read structured incident evidence/case data for repeat-frequency and trend counters.
- Modify: `tests/test_incident_store.py` — add evidence/analysis storage round-trip coverage.
- Modify: `tests/test_alert_webhook.py` — verify webhook seeds initial evidence and analysis without changing Phase 1 incident behavior.
- Modify: `tests/test_voice_context.py` — verify bound incident context prefers structured analysis and degrades gracefully.
- Modify: `tests/test_sre_metrics.py` — later phase: verify repeat-incident and trend-baseline counters.

### Task 1: Add incident evidence and analysis persistence

**Files:**
- Modify: `tests/test_incident_store.py`
- Modify: `toolsets/incident_store.py`

- [ ] **Step 1: Write the failing evidence/analysis round-trip test**

Add to `tests/test_incident_store.py`:

```python
@pytest.mark.asyncio
async def test_incident_evidence_and_analysis_round_trip(tmp_path: Path, **_: object) -> None:
    module, store = _load_module(tmp_path)
    incident_id = await module.create_incident("PodCrashLooping", "default", "prod-a", "pod 重启")

    evidence_id = await module.add_evidence(
        incident_id,
        source_type="alert_window",
        source_ref="alertmanager/default/PodCrashLooping",
        summary="记录告警触发时间窗",
        payload={"severity": "critical", "status": "firing"},
        window_start_ts=100.0,
        window_end_ts=160.0,
        collector_version="phase2.v1",
        confidence=0.9,
    )
    await module.upsert_analysis(
        incident_id,
        symptoms=["PodCrashLooping firing in default"],
        likely_scope="workload",
        suspected_root_causes=[{"summary": "应用容器异常退出", "confidence": 0.4}],
        supporting_evidence=[{"source_type": "alert_window", "summary": "告警持续 firing"}],
        missing_evidence=["缺少 pod 日志摘要"],
        next_best_actions=["检查最近 15 分钟 Pod 日志"],
        confidence=0.35,
    )

    evidence_rows = await module.list_evidence(incident_id)
    analysis = await module.get_analysis(incident_id)

    assert evidence_id > 0
    assert evidence_rows[0]["source_type"] == "alert_window"
    assert evidence_rows[0]["payload"]["severity"] == "critical"
    assert analysis is not None
    assert analysis["likely_scope"] == "workload"
    assert analysis["missing_evidence"] == ["缺少 pod 日志摘要"]

    store.close()
```

- [ ] **Step 2: Run the focused test to verify RED**

Run: `rtk pytest tests/test_incident_store.py::test_incident_evidence_and_analysis_round_trip -q`
Expected: FAIL with `AttributeError` because `add_evidence`, `list_evidence`, `upsert_analysis`, and `get_analysis` do not exist yet.

- [ ] **Step 3: Implement minimal schema and storage APIs**

In `toolsets/incident_store.py`, extend `_SCHEMA_SQL` with:

```sql
CREATE TABLE IF NOT EXISTS incident_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    window_start_ts REAL,
    window_end_ts REAL,
    collected_at REAL NOT NULL,
    collector_version TEXT,
    confidence REAL,
    FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS incident_analysis (
    incident_id TEXT PRIMARY KEY,
    symptoms_json TEXT NOT NULL,
    likely_scope TEXT,
    suspected_root_causes_json TEXT NOT NULL,
    supporting_evidence_json TEXT NOT NULL,
    missing_evidence_json TEXT NOT NULL,
    next_best_actions_json TEXT NOT NULL,
    confidence REAL,
    last_analyzed_at REAL NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE CASCADE
);
```

Add async wrappers with this shape:

```python
async def add_evidence(
    incident_id: str,
    source_type: str,
    source_ref: str | None,
    summary: str,
    *,
    payload: dict[str, Any] | None = None,
    window_start_ts: float | None = None,
    window_end_ts: float | None = None,
    collected_at: float | None = None,
    collector_version: str | None = None,
    confidence: float | None = None,
) -> int:
    ...


async def list_evidence(incident_id: str) -> list[dict[str, Any]]:
    ...


async def upsert_analysis(
    incident_id: str,
    *,
    symptoms: list[str],
    likely_scope: str | None = None,
    suspected_root_causes: list[dict[str, Any]] | None = None,
    supporting_evidence: list[dict[str, Any]] | None = None,
    missing_evidence: list[str] | None = None,
    next_best_actions: list[str] | None = None,
    confidence: float | None = None,
    last_analyzed_at: float | None = None,
) -> None:
    ...


async def get_analysis(incident_id: str) -> dict[str, Any] | None:
    ...
```

- [ ] **Step 4: Run focused incident store tests**

Run: `rtk pytest tests/test_incident_store.py::test_incident_evidence_and_analysis_round_trip -q`
Expected: PASS.

- [ ] **Step 5: Run adjacent incident store regression tests**

Run: `rtk pytest tests/test_incident_store.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
rtk git add tests/test_incident_store.py toolsets/incident_store.py
rtk git commit -m "feat: add incident evidence persistence"
```

### Task 2: Seed initial observability context from Alertmanager

**Files:**
- Modify: `tests/test_alert_webhook.py`
- Modify: `hooks/alert_webhook.py`

- [ ] **Step 1: Write the failing webhook seeding test**

Add to `tests/test_alert_webhook.py` and extend `FakeIncidentStore` with `evidence` / `analyses` recorders:

```python
@pytest.mark.asyncio
async def test_webhook_seeds_initial_observability_context(monkeypatch, **_kwargs) -> None:
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {}
    await module.setup_alert_webhook(app)

    async def _should_process(alert: dict) -> bool:
        return True

    class _NoopFeishuConversation:
        @staticmethod
        async def publish_incident_status(incident_id, alert, config):
            del incident_id, alert, config
            return {"chat_id": None, "root_message_id": None, "thread_id": None, "status_card_message_id": None}

    fake_store = FakeIncidentStore()
    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _NoopFeishuConversation)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    assert fake_store.evidence[0]["source_type"] == "alert_window"
    assert fake_store.analyses[0]["likely_scope"] == "workload"
    assert fake_store.analyses[0]["missing_evidence"]
```

- [ ] **Step 2: Run the focused test to verify RED**

Run: `rtk pytest tests/test_alert_webhook.py::test_webhook_seeds_initial_observability_context -q`
Expected: FAIL because webhook does not seed evidence or analysis yet.

- [ ] **Step 3: Implement minimal webhook seeding**

In `hooks/alert_webhook.py`, add a helper with this shape:

```python
async def _seed_initial_observability_context(
    incident_id: str,
    alert: Dict[str, Any],
) -> None:
    now = time.time()
    await incident_store.add_evidence(
        incident_id,
        source_type="alert_window",
        source_ref=f"alertmanager/{alert['namespace']}/{alert['alertname']}",
        summary=f"{alert['severity']} alert entered firing state",
        payload=alert,
        window_start_ts=now - 300,
        window_end_ts=now + 300,
        collector_version="phase2.v1",
        confidence=0.9,
    )
    await incident_store.upsert_analysis(
        incident_id,
        symptoms=[f"{alert['alertname']} firing in {alert['namespace']}/{alert['cluster']}"],
        likely_scope="workload",
        suspected_root_causes=[{"summary": "等待更多证据收敛根因", "confidence": 0.2}],
        supporting_evidence=[{"source_type": "alert_window", "summary": alert['description']}],
        missing_evidence=["缺少 Kubernetes events", "缺少关键指标片段", "缺少最近变更线索"],
        next_best_actions=["采集 incident 时间窗内的指标、事件与变更线索"],
        confidence=0.2,
    )
```

Call it immediately after `incident_store.add_event(... alert_fired ...)`. Guard it with `getattr` so older test doubles still degrade cleanly.

- [ ] **Step 4: Run focused webhook tests**

Run: `rtk pytest tests/test_alert_webhook.py::test_webhook_seeds_initial_observability_context -q`
Expected: PASS.

- [ ] **Step 5: Run adjacent webhook regression tests**

Run: `rtk pytest tests/test_alert_webhook.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
rtk git add tests/test_alert_webhook.py hooks/alert_webhook.py
rtk git commit -m "feat: seed incident observability context"
```

### Task 3: Upgrade incident-aware voice/thread context to prefer structured analysis

**Files:**
- Modify: `tests/test_voice_context.py`
- Modify: `hooks/voice_context.py`

- [ ] **Step 1: Write the failing structured-context test**

Add to `tests/test_voice_context.py`:

```python
@pytest.mark.asyncio
async def test_thread_message_prefers_structured_analysis(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()

    class _IncidentStore:
        @staticmethod
        async def find_by_feishu_context(chat_id=None, thread_id=None, message_id=None):
            del chat_id, thread_id, message_id
            return {"id": "inc-1", "alert_name": "PodCrash", "namespace": "default", "status": "triaging"}

        @staticmethod
        async def get_timeline(incident_id):
            assert incident_id == "inc-1"
            return [{"event_type": "alert_fired", "output_summary": "pod 重启次数持续增加"}]

        @staticmethod
        async def get_analysis(incident_id):
            assert incident_id == "inc-1"
            return {
                "symptoms": ["PodCrash firing in default/prod-a"],
                "likely_scope": "workload",
                "suspected_root_causes": [{"summary": "应用容器异常退出", "confidence": 0.4}],
                "missing_evidence": ["缺少 pod 日志摘要"],
                "next_best_actions": ["检查最近 15 分钟 Pod 日志"],
                "confidence": 0.35,
            }

    monkeypatch.setattr(module, "_load_incident_store_module", lambda: _IncidentStore)

    result = await module.handle(
        "session:message",
        {
            "platform": "feishu",
            "chat_id": "oc_ops",
            "thread_id": "omt_thread",
            "message_id": "om_reply",
            "text": "继续排查",
        },
    )

    assert result["modified"] is True
    assert "结构化分析" in result["enriched_text"]
    assert "范围=workload" in result["enriched_text"]
    assert "缺少 pod 日志摘要" in result["enriched_text"]
```

- [ ] **Step 2: Run the focused test to verify RED**

Run: `rtk pytest tests/test_voice_context.py::test_thread_message_prefers_structured_analysis -q`
Expected: FAIL because the hook only injects timeline summary today.

- [ ] **Step 3: Implement structured analysis rendering with graceful fallback**

In `hooks/voice_context.py`, add a formatter like:

```python
def _build_analysis_summary(analysis: dict[str, Any] | None) -> str:
    if not analysis:
        return ""
    symptoms = ", ".join(str(item) for item in analysis.get("symptoms") or []) or "unknown"
    causes = "; ".join(str(item.get("summary", "")) for item in analysis.get("suspected_root_causes") or []) or "待补充"
    missing = "; ".join(str(item) for item in analysis.get("missing_evidence") or []) or "无"
    actions = "; ".join(str(item) for item in analysis.get("next_best_actions") or []) or "无"
    scope = analysis.get("likely_scope") or "unknown"
    return f"[结构化分析: 症状={symptoms}; 范围={scope}; 候选根因={causes}; 缺失证据={missing}; 下一步={actions}]"
```

Fetch `analysis` via `getattr(incident_store, "get_analysis", None)` so older mocks keep working. Append the analysis line before timeline history when available.

- [ ] **Step 4: Run focused voice-context tests**

Run: `rtk pytest tests/test_voice_context.py::test_thread_message_prefers_structured_analysis -q`
Expected: PASS.

- [ ] **Step 5: Run adjacent voice-context regression tests**

Run: `rtk pytest tests/test_voice_context.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
rtk git add tests/test_voice_context.py hooks/voice_context.py
rtk git commit -m "feat: use structured incident analysis in voice context"
```

### Task 4: Prepare repeat-incident and trend baseline metrics

**Files:**
- Modify: `tests/test_sre_metrics.py`
- Modify: `toolsets/sre_metrics.py`
- Modify: `toolsets/incident_store.py`

- [ ] **Step 1: Write the failing metrics test**

Add to `tests/test_sre_metrics.py`:

```python
@pytest.mark.asyncio
async def test_compute_metrics_includes_repeat_incident_baseline(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()
    now = time.time()

    async def _list_active() -> list[dict]:
        return [{"id": "inc-1", "created_at": now - 60, "reopen_count": 2}]

    async def _get_timeline(incident_id: str) -> list[dict]:
        del incident_id
        return []

    async def _query_audit(**kwargs) -> list[dict]:
        del kwargs
        return []

    monkeypatch.setattr(module.incident_store, "list_active", _list_active)
    monkeypatch.setattr(module.incident_store, "get_timeline", _get_timeline)
    monkeypatch.setattr(module.audit_log, "query_audit", _query_audit)
    monkeypatch.setattr(module.asyncio, "to_thread", _wrap_sync)
    monkeypatch.setattr(module.approval_async, "_DB", type("DB", (), {"_lock": DummyLock(), "_conn": DummyConn((0, 0))})())

    result = await module.compute_metrics(days=7)

    assert result["repeat_incident_count"] == 1
    assert result["repeat_incident_rate"] == 1.0
```

- [ ] **Step 2: Run the focused test to verify RED**

Run: `rtk pytest tests/test_sre_metrics.py::test_compute_metrics_includes_repeat_incident_baseline -q`
Expected: FAIL because these fields do not exist yet.

- [ ] **Step 3: Implement minimal repeat/trend counters**

In `toolsets/sre_metrics.py`, extend `compute_metrics()` to calculate:

```python
repeat_incidents = [incident for incident in recent_incidents if int(incident.get("reopen_count", 0) or 0) > 0]
repeat_incident_count = len(repeat_incidents)
repeat_incident_rate = (repeat_incident_count / len(recent_incidents)) if recent_incidents else None
```

Return them in the result payload without changing existing fields.

- [ ] **Step 4: Run focused metrics tests**

Run: `rtk pytest tests/test_sre_metrics.py::test_compute_metrics_includes_repeat_incident_baseline -q`
Expected: PASS.

- [ ] **Step 5: Run adjacent metrics regression tests**

Run: `rtk pytest tests/test_sre_metrics.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
rtk git add tests/test_sre_metrics.py toolsets/sre_metrics.py
rtk git commit -m "feat: add repeat incident baseline metrics"
```

## Self-Review

- Spec coverage: this plan covers the Phase 2 requirements that are already concrete in the roadmap: incident time-window evidence, structured incident evidence, multi-source analysis scaffolding, and trend-baseline preparation. It intentionally does not include predictive models, multi-cluster abstractions, or broad workflow refactors.
- Placeholder scan: no `TODO` or `TBD` markers remain. Each task includes target files, test commands, expected failure/pass signals, and the minimum implementation shape.
- Type consistency: `incident_evidence` and `incident_analysis` naming is used consistently across storage, webhook seeding, and voice-context consumption.

