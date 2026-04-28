"""测试 Alertmanager Webhook 处理模块。"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import sys
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from toolsets.incident_store import IncidentStore


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "hooks" / "alert_webhook.py"
    spec = importlib.util.spec_from_file_location("test_alert_webhook_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _payload(status: str = "firing") -> dict:
    """构造标准 Alertmanager payload。"""
    return {
        "alerts": [
            {
                "status": status,
                "labels": {
                    "alertname": "PodCrashLooping",
                    "severity": "critical",
                    "namespace": "default",
                    "cluster": "prod-a",
                },
                "annotations": {
                    "description": "pod 重启次数持续增加",
                },
            }
        ]
    }


class FakeIncidentStore:
    def __init__(self) -> None:
        self.created = []
        self.events = []
        self.bindings = []
        self.status_updates = []
        self.evidence = []
        self.analyses = []
        self.case_profiles = []
        self.reusable = None

    async def create_incident(self, alert_name, namespace, cluster, summary, **kwargs):
        self.created.append((alert_name, namespace, cluster, summary, kwargs))
        return "incident-1"

    async def add_event(self, incident_id, event_type, tool_name, input_summary, output_summary, metadata=None):
        self.events.append((incident_id, event_type, tool_name, input_summary, output_summary, metadata))
        return 1

    async def find_reusable_incident(self, dedup_key, dedup_key_version):
        return self.reusable

    async def update_feishu_binding(self, incident_id, **kwargs):
        self.bindings.append((incident_id, kwargs))

    async def update_status(self, incident_id, status, resolved_at=None, closed_at=None):
        self.status_updates.append((incident_id, status, resolved_at, closed_at))

    async def add_evidence(self, incident_id, source_type, source_ref, summary, **kwargs):
        self.evidence.append(
            {
                "incident_id": incident_id,
                "source_type": source_type,
                "source_ref": source_ref,
                "summary": summary,
                **kwargs,
            }
        )
        return 1

    async def upsert_analysis(self, incident_id, **kwargs):
        self.analyses.append({"incident_id": incident_id, **kwargs})

    async def get_analysis(self, incident_id):
        for item in reversed(self.analyses):
            if item["incident_id"] == incident_id:
                return item
        return None

    async def list_evidence(self, incident_id):
        return [item for item in self.evidence if item["incident_id"] == incident_id]

    async def upsert_case_profile(self, incident_id, **kwargs):
        self.case_profiles.append({"incident_id": incident_id, **kwargs})


@pytest.mark.asyncio
async def test_webhook_formats_prompt_and_skips_resolved(monkeypatch, **_kwargs) -> None:
    """firing 告警应生成 triage 提示词，resolved 告警应跳过。"""
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {}
    await module.setup_alert_webhook(app)

    async def _should_process(alert: dict) -> bool:
        return True

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    fake_store = FakeIncidentStore()
    monkeypatch.setattr(module, "incident_store", fake_store)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()

        response_resolved = await client.post("/webhooks/alertmanager", json=_payload("resolved"))
        data_resolved = await response_resolved.json()
    finally:
        await client.close()

    assert data["ok"] is True
    assert data["processed"] == 1
    assert data["incidents"][0]["incident_id"]
    assert data["incidents"][0]["event_type"] == "alert_fired"
    assert data["prompts"][0].startswith("[Incident ")
    assert data["prompts"][0] == (
        "[Incident incident-1] [Alertmanager] critical 告警: PodCrashLooping in default/prod-a. "
        "pod 重启次数持续增加. 请执行 triage 流程。"
    )
    assert fake_store.created == [
        (
            "PodCrashLooping",
            "default",
            "prod-a",
            "pod 重启次数持续增加",
            {"platform": "feishu", "dedup_key": "PodCrashLooping|default|prod-a", "dedup_key_version": "v1"},
        )
    ]
    assert fake_store.events[0][0] == "incident-1"
    assert fake_store.events[0][1] == "alert_fired"
    assert data_resolved["processed"] == 0
    assert data_resolved["skipped"] == 1


@pytest.mark.asyncio
async def test_webhook_sends_status_to_main_chat_and_binds_incident(monkeypatch, **_kwargs) -> None:
    """告警创建 incident 后应发到主群并回写飞书消息上下文。"""
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {"platforms": {"feishu": {"main_chat_id": "oc_ops"}}}
    await module.setup_alert_webhook(app)

    async def _should_process(alert: dict) -> bool:
        return True

    class _FakeFeishuConversation:
        @staticmethod
        async def publish_incident_status(incident_id, alert, config):
            assert incident_id == "incident-1"
            assert alert["alertname"] == "PodCrashLooping"
            assert config["platforms"]["feishu"]["main_chat_id"] == "oc_ops"
            return {
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "omt_thread",
                "status_card_message_id": "om_card",
            }

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    fake_store = FakeIncidentStore()
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
    assert data["incidents"][0]["feishu_binding"] == {
        "chat_id": "oc_ops",
        "root_message_id": "om_root",
        "thread_id": "omt_thread",
        "status_card_message_id": "om_card",
    }
    assert fake_store.bindings == [
        (
            "incident-1",
            {
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "omt_thread",
                "status_card_message_id": "om_card",
            },
        )
    ]


@pytest.mark.asyncio
async def test_webhook_resolved_updates_existing_incident(monkeypatch, **_kwargs) -> None:
    """resolved 告警应命中同 dedup incident，写 resolved 时间线并更新状态。"""
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {}
    await module.setup_alert_webhook(app)

    fake_store = FakeIncidentStore()
    fake_store.reusable = {"id": "incident-1", "status": "investigating"}
    monkeypatch.setattr(module, "incident_store", fake_store)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("resolved"))
        data = await response.json()
    finally:
        await client.close()

    assert data["ok"] is True
    assert data["processed"] == 1
    assert data["skipped"] == 0
    assert data["incidents"] == [
        {
            "incident_id": "incident-1",
            "event_type": "resolved",
            "dedup_key": "PodCrashLooping|default|prod-a",
        }
    ]
    assert fake_store.events[0][0] == "incident-1"
    assert fake_store.events[0][1] == "resolved"
    assert fake_store.status_updates[0][0] == "incident-1"
    assert fake_store.status_updates[0][1] == "resolved"
    assert fake_store.status_updates[0][2] is not None


@pytest.mark.asyncio
async def test_webhook_resolved_persists_case_profile(monkeypatch, **_kwargs) -> None:
    """resolved 告警应自动沉淀 incident case profile。"""
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {}
    await module.setup_alert_webhook(app)

    fake_store = FakeIncidentStore()
    fake_store.reusable = {
        "id": "incident-1",
        "status": "investigating",
        "alert_name": "PodCrashLooping",
        "namespace": "default",
        "cluster": "prod-a",
        "created_at": time.time() - 600,
        "reopen_count": 1,
    }
    fake_store.evidence.extend(
        [
            {
                "incident_id": "incident-1",
                "source_type": "metrics_window",
                "summary": "cpu_max=0.92, restart_max=7",
                "payload": {"cpu_max": "0.92", "restart_max": "7"},
            },
            {
                "incident_id": "incident-1",
                "source_type": "audit_change",
                "summary": "最近 1 条变更线索，最新动作: kubectl rollout restart deploy/api",
                "payload": {"count": 1},
            },
        ]
    )
    fake_store.analyses.append(
        {
            "incident_id": "incident-1",
            "likely_scope": "workload",
            "suspected_root_causes": [{"summary": "资源压力可能导致工作负载异常", "confidence": 0.7}],
            "next_best_actions": ["检查 Pod CPU/内存指标与资源配置"],
            "symptoms": ["PodCrashLooping firing in default/prod-a"],
            "confidence": 0.85,
        }
    )
    monkeypatch.setattr(module, "incident_store", fake_store)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("resolved"))
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    assert len(fake_store.case_profiles) == 1
    profile = fake_store.case_profiles[0]
    assert profile["incident_signature"] == "PodCrashLooping|default|workload|resolved"
    assert profile["final_root_cause"] == "资源压力可能导致工作负载异常"
    assert profile["metric_delta_summary"]["restart_max"] == "7"
    assert profile["change_clue_summary"].startswith("最近 1 条变更线索")


@pytest.mark.asyncio
async def test_webhook_seeds_initial_observability_context(monkeypatch, **_kwargs) -> None:
    """firing webhook 应播种首批 evidence 与结构化 analysis。"""
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


@pytest.mark.asyncio
async def test_webhook_collects_recent_audit_change_clue(monkeypatch, **_kwargs) -> None:
    """firing webhook 应补充最近审计变更线索 evidence。"""
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

    async def _query_audit(**kwargs):
        assert kwargs["namespace"] == "default"
        assert kwargs["cluster"] == "prod-a"
        assert kwargs["limit"] == 20
        return [
            {
                "id": 1,
                "who": "feishu:ou_1",
                "what": "kubectl rollout restart deploy/api",
                "when_ts": time.time() - 30,
                "tool_name": "k8s_write",
                "result": "success",
            }
        ]

    fake_store = FakeIncidentStore()
    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _NoopFeishuConversation)
    monkeypatch.setattr(module.audit_log, "query_audit", _query_audit)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    audit_rows = [row for row in fake_store.evidence if row["source_type"] == "audit_change"]
    assert len(audit_rows) == 1
    assert audit_rows[0]["payload"]["count"] == 1
    assert "rollout restart" in audit_rows[0]["summary"]


@pytest.mark.asyncio
async def test_webhook_collects_recent_k8s_events_clue(monkeypatch, **_kwargs) -> None:
    """firing webhook 应补充 namespace 内最近 Kubernetes events evidence。"""
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

    class _FakeK8sReadTool:
        @staticmethod
        async def k8s_read(command, context=None):
            assert command == "kubectl get events -n default --sort-by=.lastTimestamp"
            assert context is None
            return {
                "ok": True,
                "stdout": (
                    "LAST SEEN   TYPE      REASON    OBJECT            MESSAGE\n"
                    "2m          Warning   BackOff   pod/api-123       Back-off restarting failed container\n"
                    "1m          Warning   Unhealthy pod/api-123       Readiness probe failed"
                ),
                "stderr": "",
                "result": {"extracted": False, "data": "ignored", "line_count": 3},
            }

    async def _query_audit(**kwargs):
        del kwargs
        return []

    fake_store = FakeIncidentStore()
    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _NoopFeishuConversation)
    monkeypatch.setattr(module.audit_log, "query_audit", _query_audit)
    monkeypatch.setattr(module, "k8s_read_tool", _FakeK8sReadTool, raising=False)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    event_rows = [row for row in fake_store.evidence if row["source_type"] == "k8s_events"]
    assert len(event_rows) == 1
    assert "BackOff" in event_rows[0]["summary"]
    assert event_rows[0]["payload"]["line_count"] == 2
    assert "缺少 Kubernetes events" not in fake_store.analyses[0]["missing_evidence"]
    assert any(item["source_type"] == "k8s_events" for item in fake_store.analyses[0]["supporting_evidence"])


@pytest.mark.asyncio
async def test_webhook_analysis_uses_audit_and_k8s_event_clues(monkeypatch, **_kwargs) -> None:
    """当存在变更与异常 events 时，analysis 应给出更具体的根因假设与动作。"""
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

    class _FakeK8sReadTool:
        @staticmethod
        async def k8s_read(command, context=None):
            del context
            assert command == "kubectl get events -n default --sort-by=.lastTimestamp"
            return {
                "ok": True,
                "stdout": (
                    "LAST SEEN   TYPE      REASON      OBJECT            MESSAGE\n"
                    "2m          Warning   BackOff     pod/api-123       Back-off restarting failed container\n"
                    "1m          Warning   Unhealthy   pod/api-123       Readiness probe failed"
                ),
                "stderr": "",
                "result": {"extracted": False, "data": "ignored", "line_count": 3},
            }

    async def _query_audit(**kwargs):
        del kwargs
        return [
            {
                "id": 1,
                "who": "feishu:ou_1",
                "what": "kubectl rollout restart deploy/api",
                "when_ts": time.time() - 60,
                "tool_name": "k8s_write",
                "result": "success",
            }
        ]

    fake_store = FakeIncidentStore()
    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _NoopFeishuConversation)
    monkeypatch.setattr(module.audit_log, "query_audit", _query_audit)
    monkeypatch.setattr(module, "k8s_read_tool", _FakeK8sReadTool, raising=False)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    analysis = fake_store.analyses[0]
    assert any("近期变更可能引发工作负载异常" in item["summary"] for item in analysis["suspected_root_causes"])
    assert any("Kubernetes events 显示工作负载异常" in item["summary"] for item in analysis["suspected_root_causes"])
    assert any("核对最近 30 分钟内的发布或变更" == item for item in analysis["next_best_actions"])
    assert any("检查异常 Pod 的事件与探针失败细节" == item for item in analysis["next_best_actions"])


@pytest.mark.asyncio
async def test_webhook_collects_metrics_window_and_updates_analysis(monkeypatch, **_kwargs) -> None:
    """firing webhook 应补充 metrics_window evidence，并用资源异常更新 analysis。"""
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

    class _FakePrometheusTool:
        @staticmethod
        async def prometheus_query(query, start=None, end=None):
            assert start is not None
            assert end is not None
            if "container_cpu_usage_seconds_total" in query:
                return {
                    "allowed": True,
                    "query": query,
                    "results": [{"metric": {"pod": "api-123"}, "values": [[1, "0.92"]]}],
                }
            if "container_memory_working_set_bytes" in query:
                return {
                    "allowed": True,
                    "query": query,
                    "results": [{"metric": {"pod": "api-123"}, "values": [[1, "8.4e+08"]]}],
                }
            if "kube_pod_container_status_restarts_total" in query:
                return {
                    "allowed": True,
                    "query": query,
                    "results": [{"metric": {"pod": "api-123"}, "values": [[1, "0"]]}],
                }
            if "kube_pod_status_ready" in query:
                return {
                    "allowed": True,
                    "query": query,
                    "results": [{"metric": {"pod": "api-123", "condition": "false"}, "values": [[1, "0"]]}],
                }
            raise AssertionError(f"unexpected query: {query}")

    class _FakeK8sReadTool:
        @staticmethod
        async def k8s_read(command, context=None):
            del command, context
            return {"ok": False, "stdout": "", "stderr": ""}

    async def _query_audit(**kwargs):
        del kwargs
        return []

    fake_store = FakeIncidentStore()
    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _NoopFeishuConversation)
    monkeypatch.setattr(module.audit_log, "query_audit", _query_audit)
    monkeypatch.setattr(module, "k8s_read_tool", _FakeK8sReadTool, raising=False)
    monkeypatch.setattr(module, "prometheus_query_tool", _FakePrometheusTool, raising=False)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    metric_rows = [row for row in fake_store.evidence if row["source_type"] == "metrics_window"]
    assert len(metric_rows) == 1
    assert metric_rows[0]["payload"]["cpu_max"] == "0.92"
    assert metric_rows[0]["payload"]["memory_max"] == "8.4e+08"
    analysis = fake_store.analyses[0]
    assert any("资源压力可能导致工作负载异常" in item["summary"] for item in analysis["suspected_root_causes"])
    assert any("检查 Pod CPU/内存指标与资源配置" == item for item in analysis["next_best_actions"])


@pytest.mark.asyncio
async def test_webhook_metrics_window_includes_restart_and_readiness_clues(monkeypatch, **_kwargs) -> None:
    """metrics_window 应补充 restart 与 ready/unready 健康线索。"""
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

    class _FakePrometheusTool:
        @staticmethod
        async def prometheus_query(query, start=None, end=None):
            del start, end
            if "container_cpu_usage_seconds_total" in query:
                return {"allowed": True, "query": query, "results": [{"metric": {"pod": "api-123"}, "values": [[1, "0.15"]]}]}
            if "container_memory_working_set_bytes" in query:
                return {"allowed": True, "query": query, "results": [{"metric": {"pod": "api-123"}, "values": [[1, "2.1e+08"]]}]}
            if "kube_pod_container_status_restarts_total" in query:
                return {"allowed": True, "query": query, "results": [{"metric": {"pod": "api-123"}, "values": [[1, "7"]]}]}
            if "kube_pod_status_ready" in query:
                return {"allowed": True, "query": query, "results": [{"metric": {"pod": "api-123", "condition": "false"}, "values": [[1, "1"]]}]}
            raise AssertionError(f"unexpected query: {query}")

    class _FakeK8sReadTool:
        @staticmethod
        async def k8s_read(command, context=None):
            del command, context
            return {"ok": False, "stdout": "", "stderr": ""}

    async def _query_audit(**kwargs):
        del kwargs
        return []

    fake_store = FakeIncidentStore()
    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _NoopFeishuConversation)
    monkeypatch.setattr(module.audit_log, "query_audit", _query_audit)
    monkeypatch.setattr(module, "k8s_read_tool", _FakeK8sReadTool, raising=False)
    monkeypatch.setattr(module, "prometheus_query_tool", _FakePrometheusTool, raising=False)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    metric_rows = [row for row in fake_store.evidence if row["source_type"] == "metrics_window"]
    assert len(metric_rows) == 1
    assert metric_rows[0]["payload"]["restart_max"] == "7"
    assert metric_rows[0]["payload"]["unready_count"] == "1"
    analysis = fake_store.analyses[0]
    assert any("工作负载健康状态异常" in item["summary"] for item in analysis["suspected_root_causes"])
    assert any("检查 Pod 重启次数与 Ready 状态" == item for item in analysis["next_best_actions"])


@pytest.mark.asyncio
async def test_webhook_analysis_confidence_increases_with_more_evidence(monkeypatch, **_kwargs) -> None:
    """多源证据命中越多，analysis confidence 应越高。"""
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

    class _FakeK8sReadTool:
        @staticmethod
        async def k8s_read(command, context=None):
            del command, context
            return {
                "ok": True,
                "stdout": (
                    "LAST SEEN   TYPE      REASON      OBJECT            MESSAGE\n"
                    "2m          Warning   BackOff     pod/api-123       Back-off restarting failed container\n"
                    "1m          Warning   Unhealthy   pod/api-123       Readiness probe failed"
                ),
                "stderr": "",
                "result": {"extracted": False, "data": "ignored", "line_count": 3},
            }

    class _FakePrometheusTool:
        @staticmethod
        async def prometheus_query(query, start=None, end=None):
            del start, end
            if "container_cpu_usage_seconds_total" in query:
                return {"allowed": True, "query": query, "results": [{"metric": {"pod": "api-123"}, "values": [[1, "0.92"]]}]}
            if "container_memory_working_set_bytes" in query:
                return {"allowed": True, "query": query, "results": [{"metric": {"pod": "api-123"}, "values": [[1, "8.4e+08"]]}]}
            if "kube_pod_container_status_restarts_total" in query:
                return {"allowed": True, "query": query, "results": [{"metric": {"pod": "api-123"}, "values": [[1, "7"]]}]}
            if "kube_pod_status_ready" in query:
                return {"allowed": True, "query": query, "results": [{"metric": {"pod": "api-123", "condition": "false"}, "values": [[1, "1"]]}]}
            raise AssertionError(f"unexpected query: {query}")

    async def _query_audit(**kwargs):
        del kwargs
        return [
            {
                "id": 1,
                "who": "feishu:ou_1",
                "what": "kubectl rollout restart deploy/api",
                "when_ts": time.time() - 60,
                "tool_name": "k8s_write",
                "result": "success",
            }
        ]

    fake_store = FakeIncidentStore()
    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _NoopFeishuConversation)
    monkeypatch.setattr(module.audit_log, "query_audit", _query_audit)
    monkeypatch.setattr(module, "k8s_read_tool", _FakeK8sReadTool, raising=False)
    monkeypatch.setattr(module, "prometheus_query_tool", _FakePrometheusTool, raising=False)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    assert fake_store.analyses[0]["confidence"] >= 0.8


@pytest.mark.asyncio
async def test_webhook_collects_logs_window_and_updates_analysis(monkeypatch, **_kwargs) -> None:
    """firing webhook 应补充 logs_window evidence，并将日志异常纳入 analysis。"""
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

    class _FakeK8sReadTool:
        @staticmethod
        async def k8s_read(command, context=None):
            del command, context
            return {"ok": False, "stdout": "", "stderr": ""}

    class _FakePrometheusTool:
        @staticmethod
        async def prometheus_query(query, start=None, end=None):
            del query, start, end
            return {"allowed": True, "results": []}

    class _FakeLokiTool:
        @staticmethod
        async def loki_query(query, start=None, end=None, limit=None):
            assert '{namespace="default"}' in query
            assert start is not None
            assert end is not None
            assert limit == 20
            return {
                "allowed": True,
                "query": query,
                "results": [
                    {
                        "stream": {"pod": "api-123"},
                        "values": [
                            ["1", "ERROR database connection timeout"],
                            ["2", "WARN retrying upstream request"],
                        ],
                    }
                ],
            }

    async def _query_audit(**kwargs):
        del kwargs
        return []

    fake_store = FakeIncidentStore()
    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _NoopFeishuConversation)
    monkeypatch.setattr(module.audit_log, "query_audit", _query_audit)
    monkeypatch.setattr(module, "k8s_read_tool", _FakeK8sReadTool, raising=False)
    monkeypatch.setattr(module, "prometheus_query_tool", _FakePrometheusTool, raising=False)
    monkeypatch.setattr(module, "loki_query_tool", _FakeLokiTool, raising=False)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    log_rows = [row for row in fake_store.evidence if row["source_type"] == "logs_window"]
    assert len(log_rows) == 1
    assert log_rows[0]["payload"]["line_count"] == 2
    assert "ERROR database connection timeout" in log_rows[0]["summary"]
    analysis = fake_store.analyses[0]
    assert any("应用日志显示运行时异常" in item["summary"] for item in analysis["suspected_root_causes"])
    assert any("检查相关 Pod 最近错误日志与超时信息" == item for item in analysis["next_best_actions"])
    assert "缺少 pod 日志摘要" not in analysis["missing_evidence"]


@pytest.mark.asyncio
async def test_webhook_collects_workload_topology_evidence(monkeypatch, **_kwargs) -> None:
    """firing webhook 应补充 Pod / Deployment 状态摘要 evidence。"""
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

    class _FakeK8sReadTool:
        @staticmethod
        async def k8s_read(command, context=None):
            del context
            if command == "kubectl get events -n default --sort-by=.lastTimestamp":
                return {"ok": False, "stdout": "", "stderr": ""}
            if command == "kubectl get pods -n default":
                return {
                    "ok": True,
                    "stdout": "NAME READY STATUS RESTARTS AGE\napi-123 0/1 CrashLoopBackOff 7 10m\nworker-1 1/1 Running 0 2h",
                    "stderr": "",
                }
            if command == "kubectl get deploy -n default":
                return {
                    "ok": True,
                    "stdout": "NAME READY UP-TO-DATE AVAILABLE AGE\napi 0/2 2 0 10m",
                    "stderr": "",
                }
            if command == "kubectl get nodes":
                return {"ok": False, "stdout": "", "stderr": ""}
            raise AssertionError(f"unexpected command: {command}")

    class _FakePrometheusTool:
        @staticmethod
        async def prometheus_query(query, start=None, end=None):
            del query, start, end
            return {"allowed": True, "results": []}

    class _FakeLokiTool:
        @staticmethod
        async def loki_query(query, start=None, end=None, limit=None):
            del query, start, end, limit
            return {"allowed": True, "results": []}

    async def _query_audit(**kwargs):
        del kwargs
        return []

    fake_store = FakeIncidentStore()
    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _NoopFeishuConversation)
    monkeypatch.setattr(module.audit_log, "query_audit", _query_audit)
    monkeypatch.setattr(module, "k8s_read_tool", _FakeK8sReadTool, raising=False)
    monkeypatch.setattr(module, "prometheus_query_tool", _FakePrometheusTool, raising=False)
    monkeypatch.setattr(module, "loki_query_tool", _FakeLokiTool, raising=False)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    topology_rows = [row for row in fake_store.evidence if row["source_type"] == "workload_topology"]
    assert len(topology_rows) == 1
    assert "CrashLoopBackOff" in topology_rows[0]["summary"]
    assert topology_rows[0]["payload"]["pod_line_count"] == 2
    analysis = fake_store.analyses[0]
    assert any("工作负载拓扑状态显示副本或 Pod 异常" in item["summary"] for item in analysis["suspected_root_causes"])
    assert any("检查 Deployment 可用副本与异常 Pod 状态" == item for item in analysis["next_best_actions"])


@pytest.mark.asyncio
async def test_webhook_collects_node_status_evidence(monkeypatch, **_kwargs) -> None:
    """firing webhook 应补充 node 状态摘要 evidence。"""
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

    class _FakeK8sReadTool:
        @staticmethod
        async def k8s_read(command, context=None):
            del context
            if command == "kubectl get events -n default --sort-by=.lastTimestamp":
                return {"ok": False, "stdout": "", "stderr": ""}
            if command == "kubectl get pods -n default":
                return {"ok": False, "stdout": "", "stderr": ""}
            if command == "kubectl get deploy -n default":
                return {"ok": False, "stdout": "", "stderr": ""}
            if command == "kubectl get nodes":
                return {
                    "ok": True,
                    "stdout": "NAME STATUS ROLES AGE VERSION\nnode-a Ready control-plane 10d v1.30\nnode-b NotReady worker 5d v1.30",
                    "stderr": "",
                }
            raise AssertionError(f"unexpected command: {command}")

    class _FakePrometheusTool:
        @staticmethod
        async def prometheus_query(query, start=None, end=None):
            del query, start, end
            return {"allowed": True, "results": []}

    class _FakeLokiTool:
        @staticmethod
        async def loki_query(query, start=None, end=None, limit=None):
            del query, start, end, limit
            return {"allowed": True, "results": []}

    async def _query_audit(**kwargs):
        del kwargs
        return []

    fake_store = FakeIncidentStore()
    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _NoopFeishuConversation)
    monkeypatch.setattr(module.audit_log, "query_audit", _query_audit)
    monkeypatch.setattr(module, "k8s_read_tool", _FakeK8sReadTool, raising=False)
    monkeypatch.setattr(module, "prometheus_query_tool", _FakePrometheusTool, raising=False)
    monkeypatch.setattr(module, "loki_query_tool", _FakeLokiTool, raising=False)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    node_rows = [row for row in fake_store.evidence if row["source_type"] == "node_status"]
    assert len(node_rows) == 1
    assert "NotReady" in node_rows[0]["summary"]
    assert node_rows[0]["payload"]["node_line_count"] == 2
    analysis = fake_store.analyses[0]
    assert any("节点状态异常可能扩大影响范围" in item["summary"] for item in analysis["suspected_root_causes"])
    assert any("检查异常 Node 状态与受影响工作负载分布" == item for item in analysis["next_best_actions"])


@pytest.mark.asyncio
async def test_webhook_integrates_dedup(monkeypatch, **_kwargs) -> None:
    """当去重器拒绝时 webhook 不应生成提示词。"""
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {}
    await module.setup_alert_webhook(app)

    calls = {"count": 0}

    async def _should_process(alert: dict) -> bool:
        calls["count"] += 1
        return calls["count"] == 1

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        first = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        second = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        first_data = await first.json()
        second_data = await second.json()
    finally:
        await client.close()

    assert first_data["processed"] == 1
    assert second_data["processed"] == 0
    assert second_data["skipped"] == 1


@pytest.mark.asyncio
async def test_webhook_hmac_validation(monkeypatch, **_kwargs) -> None:
    """配置了密钥时应校验 HMAC。"""
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {"webhooks": {"alertmanager": {"secret": "top-secret"}}}
    await module.setup_alert_webhook(app)

    async def _should_process(alert: dict) -> bool:
        return True

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)

    body = json.dumps(_payload("firing"), ensure_ascii=False).encode("utf-8")
    good_sig = hmac.new(b"top-secret", body, hashlib.sha256).hexdigest()

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        bad_response = await client.post(
            "/webhooks/alertmanager",
            data=body,
            headers={"Content-Type": "application/json", "X-Signature": "bad-signature"},
        )
        good_response = await client.post(
            "/webhooks/alertmanager",
            data=body,
            headers={"Content-Type": "application/json", "X-Signature": f"sha256={good_sig}"},
        )
        bad_data = await bad_response.json()
        good_data = await good_response.json()
    finally:
        await client.close()

    assert bad_response.status == 401
    assert bad_data["ok"] is False
    assert good_response.status == 200
    assert good_data["processed"] == 1


@pytest.mark.asyncio
async def test_webhook_resolved_persists_status_with_real_store(tmp_path: Path, monkeypatch, **_kwargs) -> None:
    """resolved webhook 应在真实 SQLite store 中更新 status 并写 timeline。"""
    module = _load_module()
    store = IncidentStore(tmp_path / "incidents.db")
    old_store = getattr(module.incident_store, "_STORE", None)
    if old_store is not None:
        old_store.close()
    module.incident_store._STORE = store
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

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "feishu_conversation", _NoopFeishuConversation)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        firing_response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        firing_data = await firing_response.json()
        incident_id = firing_data["incidents"][0]["incident_id"]

        resolved_response = await client.post("/webhooks/alertmanager", json=_payload("resolved"))
        resolved_data = await resolved_response.json()
    finally:
        await client.close()

    incident = await module.incident_store.get_incident(incident_id)
    timeline = await module.incident_store.get_timeline(incident_id)

    assert resolved_data["processed"] == 1
    assert resolved_data["incidents"][0]["event_type"] == "resolved"
    assert incident["status"] == "resolved"
    assert incident["resolved_at"] is not None
    assert [item["event_type"] for item in timeline] == ["alert_fired", "resolved"]

    store.close()


@pytest.mark.asyncio
async def test_webhook_resolved_persists_case_profile_with_real_store(tmp_path: Path, monkeypatch, **_kwargs) -> None:
    """resolved webhook 应在真实 SQLite store 中写入 case profile。"""
    module = _load_module()
    store = IncidentStore(tmp_path / "incidents.db")
    old_store = getattr(module.incident_store, "_STORE", None)
    if old_store is not None:
        old_store.close()
    module.incident_store._STORE = store
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

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "feishu_conversation", _NoopFeishuConversation)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        firing_response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        firing_data = await firing_response.json()
        incident_id = firing_data["incidents"][0]["incident_id"]

        resolved_response = await client.post("/webhooks/alertmanager", json=_payload("resolved"))
        resolved_data = await resolved_response.json()
    finally:
        await client.close()

    profile = await module.incident_store.get_case_profile(incident_id)

    assert resolved_data["processed"] == 1
    assert profile is not None
    assert profile["incident_signature"].startswith("PodCrashLooping|default|")
    assert profile["final_scope"] == "workload"
    assert isinstance(profile["effective_actions"], list)

    store.close()


@pytest.mark.asyncio
async def test_webhook_resolved_backfills_similar_case_ids_with_real_store(tmp_path: Path, monkeypatch, **_kwargs) -> None:
    """resolved webhook 应将历史相似 case 回填到 similar_incident_ids。"""
    module = _load_module()
    store = IncidentStore(tmp_path / "incidents.db")
    old_store = getattr(module.incident_store, "_STORE", None)
    if old_store is not None:
        old_store.close()
    module.incident_store._STORE = store

    older_incident = await module.incident_store.create_incident("PodCrashLooping", "default", "prod-a", "older")
    await module.incident_store.upsert_case_profile(
        older_incident,
        incident_signature="PodCrashLooping|default|workload|resolved",
        final_scope="workload",
        final_root_cause="资源压力可能导致工作负载异常",
        effective_actions=["检查 Pod CPU/内存指标与资源配置"],
    )

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

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "feishu_conversation", _NoopFeishuConversation)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        firing_response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        firing_data = await firing_response.json()
        incident_id = firing_data["incidents"][0]["incident_id"]

        resolved_response = await client.post("/webhooks/alertmanager", json=_payload("resolved"))
        resolved_data = await resolved_response.json()
    finally:
        await client.close()

    profile = await module.incident_store.get_case_profile(incident_id)

    assert resolved_data["processed"] == 1
    assert profile is not None
    assert older_incident in profile["similar_incident_ids"]

    store.close()
