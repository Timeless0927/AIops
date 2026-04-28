# Pod/Workload Root Cause Analysis MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a Pod/Workload-focused incident analysis MVP that extracts target objects from incoming alerts, collects targeted evidence, and posts a fixed-format diagnosis summary back into the bound Feishu thread.

**Architecture:** Keep the existing `Alertmanager -> incident -> incident_analysis -> Feishu main chat/thread` flow intact. Extend `hooks/alert_webhook.py` so it extracts pod/workload targets from alert labels, prefers targeted `kubectl describe/logs/get` evidence before namespace-wide fallbacks, and renders a fixed summary via a new shared formatter module. Reuse the existing incident/thread binding in `hooks/feishu_conversation.py` to reply into the same thread, and reuse the same formatter in `hooks/voice_context.py` so follow-up questions see the same structure the human operator sees.

**Tech Stack:** Python 3.11, aiohttp, SQLite WAL, pytest, existing Hermes/Feishu integration, existing `incident_analysis` persistence.

---

## File Structure

- Modify: `hooks/alert_webhook.py` — parse pod/workload target fields from Alertmanager payloads, collect targeted evidence, update analysis with Pod/Workload-specific clues, and publish the first structured summary to the incident thread.
- Create: `hooks/incident_analysis_summary.py` — single-purpose summary renderer that converts `incident + alert + analysis + evidence` into the fixed MVP output template.
- Modify: `hooks/feishu_conversation.py` — add a thread-reply helper that can post a summary reply under an existing incident root message.
- Modify: `hooks/voice_context.py` — consume the shared summary renderer so thread follow-up context matches the thread summary format.
- Modify: `tests/test_alert_webhook.py` — add extraction coverage, targeted evidence coverage, and summary publication coverage.
- Create: `tests/test_incident_analysis_summary.py` — unit tests for the fixed template renderer.
- Modify: `tests/test_feishu_conversation.py` — unit tests for thread reply behavior.
- Modify: `tests/test_voice_context.py` — verify follow-up context uses the shared summary format.

## What Already Exists

- `toolsets/incident_store.py` already persists `incident_analysis` and `incident_evidence`; do not create a parallel store.
- `hooks/alert_webhook.py` already seeds generic evidence and writes `incident_analysis`; extend that flow instead of creating a second analysis pipeline.
- `hooks/feishu_conversation.py` already publishes the main alert status message and returns `chat_id/root_message_id/thread_id`; reuse those bindings for summary replies.
- `hooks/voice_context.py` already loads the bound incident and latest analysis; reuse that context instead of introducing a new conversation state model.

## Not In Scope

- Automatic remediation or approval execution. This plan only improves diagnosis quality and thread output.
- Case profile recall in the thread UI. Existing case persistence stays untouched.
- Generic chat-based cluster control. Only alert-driven Pod/Workload incidents are in scope.
- New databases, queues, or background services. The MVP stays inside the existing webhook + SQLite + Feishu path.

### Task 1: Parse Pod/Workload targets from incoming alerts

**Files:**
- Modify: `tests/test_alert_webhook.py`
- Modify: `hooks/alert_webhook.py`

- [ ] **Step 1: Write the failing target extraction test**

Add to `tests/test_alert_webhook.py`:

```python
def test_extract_alert_includes_target_fields() -> None:
    module = _load_module()

    alert = module._extract_alert(
        {
            "status": "firing",
            "labels": {
                "alertname": "PodCrashLooping",
                "severity": "critical",
                "namespace": "default",
                "cluster": "prod-a",
                "pod": "api-123",
                "container": "api",
                "deployment": "api",
            },
            "annotations": {"description": "pod 重启次数持续增加"},
        }
    )

    assert alert == {
        "alertname": "PodCrashLooping",
        "severity": "critical",
        "namespace": "default",
        "cluster": "prod-a",
        "description": "pod 重启次数持续增加",
        "status": "firing",
        "pod_name": "api-123",
        "container_name": "api",
        "workload_kind": "Deployment",
        "workload_name": "api",
    }
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `rtk pytest tests/test_alert_webhook.py::test_extract_alert_includes_target_fields -q`
Expected: `FAIL` because `_extract_alert()` does not return `pod_name`, `container_name`, `workload_kind`, or `workload_name` yet.

- [ ] **Step 3: Implement minimal target extraction**

In `hooks/alert_webhook.py`, add these helpers and extend `_extract_alert()`:

```python
def _pick_first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_target_fields(labels: Dict[str, Any], annotations: Dict[str, Any]) -> Dict[str, str | None]:
    pod_name = _pick_first_text(labels.get("pod"), labels.get("pod_name"), annotations.get("pod"))
    container_name = _pick_first_text(labels.get("container"), labels.get("container_name"), annotations.get("container"))
    workload_pairs = (
        ("Deployment", _pick_first_text(labels.get("deployment"), labels.get("deployment_name"))),
        ("StatefulSet", _pick_first_text(labels.get("statefulset"), labels.get("statefulset_name"))),
        ("DaemonSet", _pick_first_text(labels.get("daemonset"), labels.get("daemonset_name"))),
        ("Job", _pick_first_text(labels.get("job"), labels.get("job_name"))),
        ("CronJob", _pick_first_text(labels.get("cronjob"), labels.get("cronjob_name"))),
    )
    for workload_kind, workload_name in workload_pairs:
        if workload_name:
            return {
                "pod_name": pod_name,
                "container_name": container_name,
                "workload_kind": workload_kind,
                "workload_name": workload_name,
            }
    return {
        "pod_name": pod_name,
        "container_name": container_name,
        "workload_kind": None,
        "workload_name": _pick_first_text(
            annotations.get("workload_name"),
            labels.get("app.kubernetes.io/name"),
            labels.get("app"),
        ),
    }


def _extract_alert(alert: Dict[str, Any]) -> Dict[str, Any]:
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    annotations = alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
    return {
        "alertname": str(labels.get("alertname", "")).strip(),
        "severity": str(labels.get("severity", "info")).strip().lower() or "info",
        "namespace": str(labels.get("namespace", "default")).strip() or "default",
        "cluster": str(labels.get("cluster", "default")).strip() or "default",
        "description": str(annotations.get("description") or annotations.get("summary") or "").strip(),
        "status": str(alert.get("status", "")).strip().lower(),
        **_extract_target_fields(labels, annotations),
    }
```

- [ ] **Step 4: Run the focused test to verify it passes**

Run: `rtk pytest tests/test_alert_webhook.py::test_extract_alert_includes_target_fields -q`
Expected: `PASS`.

- [ ] **Step 5: Run adjacent webhook parsing regressions**

Run: `rtk pytest tests/test_alert_webhook.py::test_webhook_formats_prompt_and_skips_resolved tests/test_alert_webhook.py::test_webhook_sends_status_to_main_chat_and_binds_incident -q`
Expected: `2 passed`.

- [ ] **Step 6: Commit**

```bash
rtk git add tests/test_alert_webhook.py hooks/alert_webhook.py
rtk git commit -m "功能: 解析告警目标对象"
```

### Task 2: Prefer targeted Pod/Workload evidence over namespace-wide sampling

**Files:**
- Modify: `tests/test_alert_webhook.py`
- Modify: `hooks/alert_webhook.py`

- [ ] **Step 1: Write the failing targeted-evidence test**

Add to `tests/test_alert_webhook.py`:

```python
@pytest.mark.asyncio
async def test_webhook_collects_targeted_pod_evidence_before_namespace_fallback(monkeypatch, **_kwargs) -> None:
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {}
    await module.setup_alert_webhook(app)

    payload = _payload("firing")
    payload["alerts"][0]["labels"].update({
        "pod": "api-123",
        "container": "api",
        "deployment": "api",
    })

    async def _should_process(alert: dict) -> bool:
        return True

    class _NoopFeishuConversation:
        @staticmethod
        async def publish_incident_status(incident_id, alert, config):
            del incident_id, alert, config
            return {"chat_id": None, "root_message_id": None, "thread_id": None, "status_card_message_id": None}

        @staticmethod
        async def publish_incident_analysis_summary(incident, summary_text, config):
            del incident, summary_text, config
            return {"message_id": None, "root_message_id": None, "thread_id": None}

    calls: list[str] = []

    class _FakeK8sReadTool:
        @staticmethod
        async def k8s_read(command, context=None):
            del context
            calls.append(command)
            if command == "kubectl describe pod api-123 -n default":
                return {"ok": True, "stdout": "Reason: CrashLoopBackOff\nLast State: Terminated\nReason: Error", "stderr": ""}
            if command == "kubectl logs api-123 -n default --container api --tail=50 --since=15m":
                return {"ok": True, "stdout": "ERROR database connection timeout", "stderr": ""}
            if command == "kubectl get pod api-123 -n default -o wide":
                return {"ok": True, "stdout": "NAME READY STATUS RESTARTS AGE IP NODE\napi-123 0/1 CrashLoopBackOff 7 10m 10.0.0.1 node-a", "stderr": ""}
            if command == "kubectl get deploy api -n default":
                return {"ok": True, "stdout": "NAME READY UP-TO-DATE AVAILABLE AGE\napi 0/2 2 0 10m", "stderr": ""}
            if command == "kubectl get events -n default --sort-by=.lastTimestamp":
                return {"ok": False, "stdout": "", "stderr": ""}
            if command == "kubectl get nodes":
                return {"ok": False, "stdout": "", "stderr": ""}
            raise AssertionError(f"unexpected command: {command}")

    class _FakePrometheusTool:
        @staticmethod
        async def prometheus_query(query, start=None, end=None):
            del query, start, end
            return {"allowed": True, "results": []}

    fake_store = FakeIncidentStore()
    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _NoopFeishuConversation)
    monkeypatch.setattr(module, "k8s_read_tool", _FakeK8sReadTool, raising=False)
    monkeypatch.setattr(module, "prometheus_query_tool", _FakePrometheusTool, raising=False)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=payload)
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    assert calls[:4] == [
        "kubectl describe pod api-123 -n default",
        "kubectl logs api-123 -n default --container api --tail=50 --since=15m",
        "kubectl get pod api-123 -n default -o wide",
        "kubectl get deploy api -n default",
    ]
    analysis = fake_store.analyses[-1]
    assert "缺少 pod 日志摘要" not in analysis["missing_evidence"]
    assert any(item["source_type"] == "target_pod_logs" for item in analysis["supporting_evidence"])
    assert any("应用日志显示运行时异常" in item["summary"] for item in analysis["suspected_root_causes"])
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `rtk pytest tests/test_alert_webhook.py::test_webhook_collects_targeted_pod_evidence_before_namespace_fallback -q`
Expected: `FAIL` because `_seed_initial_observability_context()` only does namespace-wide sampling today.

- [ ] **Step 3: Implement targeted evidence helpers and wire them into analysis seeding**

In `hooks/alert_webhook.py`, add these helpers above `_seed_initial_observability_context()`:

```python
def _targeted_log_command(alert: Dict[str, Any]) -> str | None:
    pod_name = alert.get("pod_name")
    if not pod_name:
        return None
    command = f"kubectl logs {pod_name} -n {alert['namespace']} --tail=50 --since=15m"
    container_name = alert.get("container_name")
    if container_name:
        command += f" --container {container_name}"
    return command


def _first_matching_lines(text: str, keywords: tuple[str, ...], limit: int = 3) -> list[str]:
    selected: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and any(keyword in stripped for keyword in keywords):
            selected.append(stripped)
        if len(selected) >= limit:
            break
    if selected:
        return selected
    return [line.strip() for line in text.splitlines() if line.strip()][:limit]


def _summarize_targeted_output(source_type: str, stdout: str) -> str:
    if source_type == "target_pod_describe":
        lines = _first_matching_lines(
            stdout,
            ("Back-off", "CrashLoopBackOff", "OOMKilled", "Readiness probe failed", "Liveness probe failed", "Reason:", "Exit Code:", "Error"),
        )
    elif source_type == "target_pod_logs":
        lines = [line.strip() for line in stdout.splitlines() if line.strip()][:2]
    else:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()][1:3]
    return " | ".join(lines)


async def _collect_targeted_k8s_evidence(
    incident_id: str,
    alert: Dict[str, Any],
    *,
    now: float,
    add_evidence,
    k8s_read,
    supporting_evidence: list[dict[str, Any]],
    suspected_root_causes: list[dict[str, Any]],
    next_best_actions: list[str],
    missing_evidence: list[str],
) -> list[str]:
    commands: list[tuple[str, str]] = []
    pod_name = alert.get("pod_name")
    workload_kind = alert.get("workload_kind")
    workload_name = alert.get("workload_name")
    namespace = alert["namespace"]

    if pod_name:
        commands.append(("target_pod_describe", f"kubectl describe pod {pod_name} -n {namespace}"))
        log_command = _targeted_log_command(alert)
        if log_command:
            commands.append(("target_pod_logs", log_command))
        commands.append(("target_pod_status", f"kubectl get pod {pod_name} -n {namespace} -o wide"))

    if workload_kind == "Deployment" and workload_name:
        commands.append(("target_workload_status", f"kubectl get deploy {workload_name} -n {namespace}"))

    for source_type, command in commands:
        result = await k8s_read(command)
        if not result.get("ok"):
            continue
        summary = _summarize_targeted_output(source_type, str(result.get("stdout") or ""))
        if not summary:
            continue
        await add_evidence(
            incident_id,
            source_type=source_type,
            source_ref=command,
            summary=summary,
            payload={"command": command},
            window_start_ts=now - 900,
            window_end_ts=now,
            collector_version="phase2.v2",
            confidence=0.8,
        )
        supporting_evidence.append({"source_type": source_type, "summary": summary})

        if source_type == "target_pod_logs":
            missing_evidence = [item for item in missing_evidence if item != "缺少 pod 日志摘要"]

        if "OOMKilled" in summary or "memory" in summary.lower():
            suspected_root_causes.append({"summary": "资源不足可能导致容器退出", "confidence": 0.75})
            next_best_actions.append("检查目标容器的内存 limit 与最近内存峰值")
        elif "Readiness probe failed" in summary or "Liveness probe failed" in summary:
            suspected_root_causes.append({"summary": "探针失败说明应用未通过健康检查", "confidence": 0.75})
            next_best_actions.append("检查探针依赖、启动耗时与最近错误日志")
        elif "ERROR" in summary or "Exception" in summary or "timeout" in summary.lower():
            suspected_root_causes.append({"summary": "应用日志显示运行时异常", "confidence": 0.8})
            next_best_actions.append("检查目标 Pod 最近 50 行错误日志")

    return missing_evidence
```

Then update `_seed_initial_observability_context()` so the initial `missing_evidence` list includes pod logs and the targeted collector runs before the current namespace-wide fallbacks:

```python
    missing_evidence = [
        "缺少 Kubernetes events",
        "缺少关键指标片段",
        "缺少最近变更线索",
        "缺少 pod 日志摘要",
    ]

    if k8s_read is not None:
        missing_evidence = await _collect_targeted_k8s_evidence(
            incident_id,
            alert,
            now=now,
            add_evidence=add_evidence,
            k8s_read=k8s_read,
            supporting_evidence=supporting_evidence,
            suspected_root_causes=suspected_root_causes,
            next_best_actions=next_best_actions,
            missing_evidence=missing_evidence,
        )
```

- [ ] **Step 4: Run the focused test to verify it passes**

Run: `rtk pytest tests/test_alert_webhook.py::test_webhook_collects_targeted_pod_evidence_before_namespace_fallback -q`
Expected: `PASS`.

- [ ] **Step 5: Run adjacent webhook evidence regressions**

Run: `rtk pytest tests/test_alert_webhook.py::test_webhook_collects_logs_window_and_updates_analysis tests/test_alert_webhook.py::test_webhook_collects_workload_topology_evidence tests/test_alert_webhook.py::test_webhook_collects_metrics_window_and_updates_analysis -q`
Expected: `3 passed`.

- [ ] **Step 6: Commit**

```bash
rtk git add tests/test_alert_webhook.py hooks/alert_webhook.py
rtk git commit -m "功能: 优先采集目标对象证据"
```

### Task 3: Render the fixed MVP summary and reply inside the bound Feishu thread

**Files:**
- Create: `tests/test_incident_analysis_summary.py`
- Create: `hooks/incident_analysis_summary.py`
- Modify: `tests/test_feishu_conversation.py`
- Modify: `hooks/feishu_conversation.py`

- [ ] **Step 1: Write the failing summary-renderer and thread-reply tests**

Create `tests/test_incident_analysis_summary.py` with:

```python
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    module_path = Path(__file__).resolve().parents[1] / "hooks" / "incident_analysis_summary.py"
    spec = importlib.util.spec_from_file_location("test_incident_analysis_summary_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_render_thread_summary_formats_required_sections() -> None:
    module = _load_module()

    summary = module.render_thread_summary(
        incident={"id": "incident-1", "alert_name": "PodCrashLooping", "namespace": "default", "cluster": "prod-a"},
        alert={
            "severity": "critical",
            "namespace": "default",
            "cluster": "prod-a",
            "workload_kind": "Deployment",
            "workload_name": "api",
            "pod_name": "api-123",
        },
        analysis={
            "likely_scope": "workload",
            "suspected_root_causes": [
                {"summary": "近期变更可能引发工作负载异常", "confidence": 0.8},
                {"summary": "应用日志显示运行时异常", "confidence": 0.7},
            ],
            "supporting_evidence": [
                {"source_type": "target_pod_logs", "summary": "ERROR database connection timeout"},
                {"source_type": "target_pod_describe", "summary": "Readiness probe failed"},
            ],
            "next_best_actions": [
                "核对最近 30 分钟 Deployment 发布与配置变更",
                "检查目标 Pod 最近 50 行错误日志",
            ],
            "confidence": 0.82,
        },
        evidence_rows=[
            {"source_type": "target_workload_status", "payload": {"affected_pod_count": 1, "desired_pod_count": 2}},
        ],
    )

    assert "【当前判断】" in summary
    assert "更像：配置发布问题" in summary
    assert "置信度：高" in summary
    assert "工作负载：Deployment/api" in summary
    assert "影响 Pod：1/2" in summary
    assert "是否像集群级问题：否" in summary
    assert "是否像用户可感知：高" in summary
    assert "【关键证据】" in summary
    assert "【根因候选】" in summary
    assert "【建议下一步】" in summary
```

Add to `tests/test_feishu_conversation.py`:

```python
@pytest.mark.asyncio
async def test_publish_incident_analysis_summary_replies_to_root_message(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    captured: dict[str, object] = {}

    async def _fake_reply(message_id: str, payload: dict[str, object], config: dict[str, object]):
        captured["message_id"] = message_id
        captured["payload"] = payload
        captured["config"] = config
        return {"data": {"message_id": "om_summary", "root_id": "om_root", "thread_id": "omt_thread"}}

    monkeypatch.setattr(module, "_reply_feishu_message", _fake_reply)

    result = await module.publish_incident_analysis_summary(
        {"id": "incident-1", "chat_id": "oc_ops", "root_message_id": "om_root", "thread_id": "omt_thread"},
        "【当前判断】\n更像：应用错误",
        {"platforms": {"feishu": {"main_chat_id": "oc_ops"}}},
    )

    assert captured["message_id"] == "om_root"
    assert captured["payload"] == {
        "content": '{"text": "【当前判断】\\n更像：应用错误"}',
        "msg_type": "text",
        "reply_in_thread": True,
        "uuid": "incident-summary-incident-1",
    }
    assert result == {"message_id": "om_summary", "root_message_id": "om_root", "thread_id": "omt_thread"}
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `rtk pytest tests/test_incident_analysis_summary.py tests/test_feishu_conversation.py::test_publish_incident_analysis_summary_replies_to_root_message -q`
Expected: `FAIL` because the summary module and the reply helper do not exist yet.

- [ ] **Step 3: Implement the shared summary renderer and thread reply helper**

Create `hooks/incident_analysis_summary.py` with:

```python
from __future__ import annotations

from typing import Any


def _confidence_label(confidence: float | None) -> str:
    if confidence is None:
        return "低"
    if confidence >= 0.75:
        return "高"
    if confidence >= 0.45:
        return "中"
    return "低"


def _primary_judgement(root_causes: list[dict[str, Any]]) -> str:
    joined = " ".join(str(item.get("summary", "")) for item in root_causes[:2])
    if "节点" in joined or "Node" in joined:
        return "节点异常"
    if "变更" in joined or "发布" in joined or "配置" in joined:
        return "配置发布问题"
    if "资源" in joined or "OOM" in joined or "内存" in joined or "CPU" in joined:
        return "资源不足"
    return "应用错误"


def _user_impact_label(severity: str) -> str:
    if severity == "critical":
        return "高"
    if severity == "warning":
        return "中"
    return "低"


def _affected_pod_label(alert: dict[str, Any], evidence_rows: list[dict[str, Any]]) -> str:
    for row in evidence_rows:
        if row.get("source_type") == "target_workload_status":
            payload = row.get("payload") or {}
            affected = int(payload.get("affected_pod_count") or 1)
            desired = int(payload.get("desired_pod_count") or affected)
            return f"{affected}/{desired}"
    if alert.get("pod_name"):
        return "1/1（已定位异常 Pod）"
    return "unknown"


def render_thread_summary(
    incident: dict[str, Any],
    alert: dict[str, Any],
    analysis: dict[str, Any],
    evidence_rows: list[dict[str, Any]],
) -> str:
    root_causes = analysis.get("suspected_root_causes") or []
    evidence = analysis.get("supporting_evidence") or []
    next_actions = analysis.get("next_best_actions") or []
    workload_kind = alert.get("workload_kind") or "Pod"
    workload_name = alert.get("workload_name") or alert.get("pod_name") or incident.get("alert_name") or "unknown"
    judgement = _primary_judgement(root_causes)

    lines = [
        "【当前判断】",
        f"更像：{judgement}",
        f"置信度：{_confidence_label(analysis.get('confidence'))}",
        "",
        "【影响范围】",
        f"命名空间：{alert.get('namespace') or incident.get('namespace') or 'default'}",
        f"工作负载：{workload_kind}/{workload_name}",
        f"影响 Pod：{_affected_pod_label(alert, evidence_rows)}",
        f"是否像集群级问题：{'是' if judgement == '节点异常' else '否'}",
        f"是否像用户可感知：{_user_impact_label(str(alert.get('severity') or 'info'))}",
        "",
        "【关键证据】",
    ]
    for index, item in enumerate(evidence[:4], start=1):
        lines.append(f"{index}. {item.get('summary', '无')}")
    if not evidence:
        lines.append("1. 暂无结构化证据，先补充 describe、日志和指标")

    lines.extend(["", "【根因候选】"])
    for index, item in enumerate(root_causes[:3], start=1):
        lines.append(f"{index}. {item.get('summary', '待补充')}")
    if not root_causes:
        lines.append("1. 待更多证据收敛根因")

    lines.extend(["", "【建议下一步】"])
    lines.append(f"优先动作：{next_actions[0] if next_actions else '先补充目标 Pod 的 describe 与错误日志'}")
    lines.append(f"备选动作：{next_actions[1] if len(next_actions) > 1 else '确认最近 30 分钟内是否存在发布或配置变更'}")
    lines.append("是否需要审批执行：否")
    return "\n".join(lines)


def render_context_summary(incident: dict[str, Any], analysis: dict[str, Any]) -> str:
    return render_thread_summary(
        incident=incident,
        alert={
            "severity": "warning",
            "namespace": incident.get("namespace"),
            "workload_kind": "Workload",
            "workload_name": incident.get("alert_name"),
            "pod_name": None,
        },
        analysis=analysis,
        evidence_rows=[],
    )
```

In `hooks/feishu_conversation.py`, add:

```python
async def _reply_feishu_message(message_id: str, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    token = await _tenant_access_token(config)
    if not token:
        return {}

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            return await response.json()


async def publish_incident_analysis_summary(
    incident: dict[str, Any],
    summary_text: str,
    config: dict[str, Any],
) -> dict[str, str | None]:
    reply_to = incident.get("root_message_id") or incident.get("status_card_message_id")
    if not reply_to:
        return {"message_id": None, "root_message_id": None, "thread_id": incident.get("thread_id")}

    payload = {
        "content": json.dumps({"text": summary_text}, ensure_ascii=False),
        "msg_type": "text",
        "reply_in_thread": True,
        "uuid": f"incident-summary-{incident.get('id', 'unknown')}",
    }
    response = await _reply_feishu_message(str(reply_to), payload, config)
    ids = _extract_message_ids(response)
    return {
        "message_id": ids["message_id"],
        "root_message_id": ids["root_id"] or incident.get("root_message_id"),
        "thread_id": ids["thread_id"] or incident.get("thread_id"),
    }
```

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `rtk pytest tests/test_incident_analysis_summary.py tests/test_feishu_conversation.py::test_publish_incident_analysis_summary_replies_to_root_message -q`
Expected: `2 passed`.

- [ ] **Step 5: Run adjacent Feishu reply regressions**

Run: `rtk pytest tests/test_feishu_conversation.py -q`
Expected: `3 passed`.

- [ ] **Step 6: Commit**

```bash
rtk git add tests/test_incident_analysis_summary.py hooks/incident_analysis_summary.py tests/test_feishu_conversation.py hooks/feishu_conversation.py
rtk git commit -m "功能: 生成并回写事件分析摘要"
```

### Task 4: Publish the summary from the webhook and reuse the same format in thread follow-up context

**Files:**
- Modify: `tests/test_alert_webhook.py`
- Modify: `hooks/alert_webhook.py`
- Modify: `tests/test_voice_context.py`
- Modify: `hooks/voice_context.py`

- [ ] **Step 1: Write the failing webhook-publication and voice-context tests**

Add to `tests/test_alert_webhook.py`:

```python
@pytest.mark.asyncio
async def test_webhook_publishes_analysis_summary_to_bound_thread(monkeypatch, **_kwargs) -> None:
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {"platforms": {"feishu": {"main_chat_id": "oc_ops"}}}
    await module.setup_alert_webhook(app)

    async def _should_process(alert: dict) -> bool:
        return True

    summary_calls: list[tuple[dict, str, dict]] = []

    class _FakeFeishuConversation:
        @staticmethod
        async def publish_incident_status(incident_id, alert, config):
            del incident_id, alert, config
            return {
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "omt_thread",
                "status_card_message_id": "om_card",
            }

        @staticmethod
        async def publish_incident_analysis_summary(incident, summary_text, config):
            summary_calls.append((incident, summary_text, config))
            return {"message_id": "om_summary", "root_message_id": "om_root", "thread_id": "omt_thread"}

    fake_store = FakeIncidentStore()
    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _FakeFeishuConversation)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    assert len(summary_calls) == 1
    incident, summary_text, _config = summary_calls[0]
    assert incident["thread_id"] == "omt_thread"
    assert "【当前判断】" in summary_text
    assert "【建议下一步】" in summary_text
```

Add to `tests/test_voice_context.py`:

```python
@pytest.mark.asyncio
async def test_thread_message_uses_shared_summary_template(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
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
                "likely_scope": "workload",
                "suspected_root_causes": [{"summary": "应用日志显示运行时异常", "confidence": 0.7}],
                "supporting_evidence": [{"source_type": "target_pod_logs", "summary": "ERROR database connection timeout"}],
                "next_best_actions": ["检查目标 Pod 最近 50 行错误日志"],
                "confidence": 0.7,
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
    assert "【当前判断】" in result["enriched_text"]
    assert "【影响范围】" in result["enriched_text"]
    assert "【建议下一步】" in result["enriched_text"]
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `rtk pytest tests/test_alert_webhook.py::test_webhook_publishes_analysis_summary_to_bound_thread tests/test_voice_context.py::test_thread_message_uses_shared_summary_template -q`
Expected: `FAIL` because the webhook does not publish the rendered summary yet and `voice_context.py` still uses its old inline formatter.

- [ ] **Step 3: Wire summary publishing into the webhook and reuse the same renderer in follow-up context**

In `hooks/alert_webhook.py`, load the renderer module once near the top:

```python
def _load_summary_module():
    module_name = "hooks.incident_analysis_summary"
    if module_name in sys.modules:
        return sys.modules[module_name]
    project_root = _project_root()
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    return __import__(module_name, fromlist=["render_thread_summary"])


summary_module = _load_summary_module()
```

Then, inside `handle_alertmanager_payload()`, right after `update_feishu_binding(...)`, publish the first thread summary:

```python
            if feishu_binding.get("chat_id"):
                await incident_store.update_feishu_binding(incident_id, **feishu_binding)
                get_analysis = getattr(incident_store, "get_analysis", None)
                list_evidence = getattr(incident_store, "list_evidence", None)
                publish_summary = getattr(feishu_conversation, "publish_incident_analysis_summary", None)
                if get_analysis is not None and list_evidence is not None and publish_summary is not None:
                    analysis = await get_analysis(incident_id) or {}
                    evidence_rows = await list_evidence(incident_id)
                    summary_text = summary_module.render_thread_summary(
                        incident={
                            "id": incident_id,
                            "alert_name": alert["alertname"],
                            "namespace": alert["namespace"],
                            "cluster": alert["cluster"],
                            **feishu_binding,
                        },
                        alert=alert,
                        analysis=analysis,
                        evidence_rows=evidence_rows,
                    )
                    await publish_summary(
                        {
                            "id": incident_id,
                            "chat_id": feishu_binding.get("chat_id"),
                            "root_message_id": feishu_binding.get("root_message_id"),
                            "thread_id": feishu_binding.get("thread_id"),
                            "status_card_message_id": feishu_binding.get("status_card_message_id"),
                        },
                        summary_text,
                        config,
                    )
```

In `hooks/voice_context.py`, replace the old `_build_analysis_summary()` body with the shared renderer:

```python
def _load_summary_module():
    module_name = "hooks.incident_analysis_summary"
    if module_name in sys.modules:
        return sys.modules[module_name]
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    return __import__(module_name, fromlist=["render_context_summary"])


def _build_analysis_summary(incident: dict[str, Any], analysis: dict[str, Any] | None) -> str:
    if not analysis:
        return ""
    summary_module = _load_summary_module()
    return summary_module.render_context_summary(incident, analysis)
```

Update `_build_bound_incident_context()` to pass `incident` into `_build_analysis_summary()`:

```python
def _build_bound_incident_context(
    incident: dict[str, Any],
    timeline: list[dict[str, Any]],
    analysis: dict[str, Any] | None,
) -> str:
    base = _build_bound_incident_prefix(incident, timeline)
    analysis_summary = _build_analysis_summary(incident, analysis)
    if not analysis_summary:
        return base
    return f"{base}\n{analysis_summary}"
```

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `rtk pytest tests/test_alert_webhook.py::test_webhook_publishes_analysis_summary_to_bound_thread tests/test_voice_context.py::test_thread_message_uses_shared_summary_template -q`
Expected: `2 passed`.

- [ ] **Step 5: Run the full MVP regression set**

Run: `rtk pytest tests/test_alert_webhook.py tests/test_incident_analysis_summary.py tests/test_feishu_conversation.py tests/test_voice_context.py -q`
Expected: `all passed`.

- [ ] **Step 6: Commit**

```bash
rtk git add tests/test_alert_webhook.py hooks/alert_webhook.py tests/test_voice_context.py hooks/voice_context.py
rtk git add tests/test_incident_analysis_summary.py hooks/incident_analysis_summary.py
rtk git add tests/test_feishu_conversation.py hooks/feishu_conversation.py
rtk git commit -m "功能: 在线程中输出固定分析摘要"
```

## Self-Review

- Spec coverage: the plan implements the full MVP from the design doc: target extraction, targeted Pod/Workload evidence, fixed-format diagnosis output, thread publication, and follow-up context alignment. It intentionally excludes remediation, case recall UI, and generic cluster chat control.
- Placeholder scan: no `TODO`, `TBD`, “similar to Task N”, or “add appropriate handling” placeholders remain. Every task has exact files, code, commands, and expected outcomes.
- Type consistency: `pod_name`, `container_name`, `workload_kind`, `workload_name`, `target_pod_logs`, `target_workload_status`, `render_thread_summary()`, and `publish_incident_analysis_summary()` are used consistently across the plan.
