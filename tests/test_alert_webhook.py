"""测试 Alertmanager Webhook 处理模块。"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import sys

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

    async def upsert_case_profile(self, incident_id, **kwargs):
        self.case_profiles.append({"incident_id": incident_id, **kwargs})

    async def get_analysis(self, incident_id):
        for row in reversed(self.analyses):
            if row["incident_id"] == incident_id:
                return row
        return None

    async def list_evidence(self, incident_id):
        return [row for row in self.evidence if row["incident_id"] == incident_id]


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


def test_extract_alert_ignores_generic_job_label_for_workload() -> None:
    module = _load_module()

    alert = module._extract_alert(
        {
            "status": "firing",
            "labels": {
                "alertname": "PodCrashLooping",
                "severity": "critical",
                "namespace": "default",
                "cluster": "prod-a",
                "job": "kubernetes-pods",
            },
            "annotations": {"description": "pod 重启次数持续增加"},
        }
    )

    assert alert["workload_kind"] is None
    assert alert["workload_name"] is None


def test_extract_alert_accepts_cronjob_label_for_workload() -> None:
    module = _load_module()

    alert = module._extract_alert(
        {
            "status": "firing",
            "labels": {
                "alertname": "PodCrashLooping",
                "severity": "critical",
                "namespace": "default",
                "cluster": "prod-a",
                "cronjob": "nightly-backup",
            },
            "annotations": {"description": "pod 重启次数持续增加"},
        }
    )

    assert alert["workload_kind"] == "CronJob"
    assert alert["workload_name"] == "nightly-backup"


def test_extract_alert_prefers_cronjob_over_spawned_job() -> None:
    module = _load_module()

    alert = module._extract_alert(
        {
            "status": "firing",
            "labels": {
                "alertname": "PodCrashLooping",
                "severity": "critical",
                "namespace": "default",
                "cluster": "prod-a",
                "job_name": "nightly-backup-28654800",
                "cronjob": "nightly-backup",
            },
            "annotations": {"description": "pod 重启次数持续增加"},
        }
    )

    assert alert["workload_kind"] == "CronJob"
    assert alert["workload_name"] == "nightly-backup"


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
async def test_webhook_publishes_analysis_summary_to_bound_thread(monkeypatch, **_kwargs) -> None:
    """飞书 thread 绑定完成后，应在线程内发布首条分析摘要。"""
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {"platforms": {"feishu": {"main_chat_id": "oc_ops"}}}
    await module.setup_alert_webhook(app)

    fake_store = FakeIncidentStore()

    async def _should_process(_alert: dict) -> bool:
        return True

    async def _collect_targeted_k8s_evidence(_alert: dict, analysis: dict, _config: dict) -> None:
        analysis["supporting_evidence"].append(
            {
                "kind": "pod_logs",
                "source": "kubectl logs api-123 -n default --tail=50 --since=15m",
                "summary": "CrashLoopBackOff repeated 3 times",
            }
        )
        analysis["suspected_root_causes"].append("容器反复 CrashLoopBackOff")
        analysis["next_best_actions"].append("检查最近 15 分钟的应用启动失败日志")
        analysis["missing_evidence"].remove("缺少 pod 日志摘要")

    class _FakeFeishuConversation:
        published: list[tuple[dict, str, dict]] = []

        @staticmethod
        async def publish_incident_status(incident_id, alert, config):
            assert incident_id == "incident-1"
            assert alert["analysis"]["suspected_root_causes"] == ["容器反复 CrashLoopBackOff"]
            return {
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "omt_thread",
                "status_card_message_id": "om_card",
            }

        @staticmethod
        async def publish_incident_analysis_summary(incident, summary_text, config):
            _FakeFeishuConversation.published.append((incident, summary_text, config))
            return {
                "message_id": "om_summary",
                "root_message_id": "om_root",
                "thread_id": "omt_thread",
            }

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _FakeFeishuConversation)
    monkeypatch.setattr(module, "_collect_targeted_k8s_evidence", _collect_targeted_k8s_evidence, raising=False)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    assert len(_FakeFeishuConversation.published) == 1
    incident_arg, summary_text, config_arg = _FakeFeishuConversation.published[0]
    assert incident_arg == {
        "id": "incident-1",
        "chat_id": "oc_ops",
        "root_message_id": "om_root",
        "thread_id": "omt_thread",
        "status_card_message_id": "om_card",
    }
    assert config_arg == app["alert_webhook_config"]
    assert summary_text == (
        "【当前判断】\n"
        "default/prod-a 的 PodCrashLooping 已有初步结论，仍需在线程内持续跟进。\n\n"
        "【关键证据】\n"
        "- CrashLoopBackOff repeated 3 times\n\n"
        "【根因候选】\n"
        "- 容器反复 CrashLoopBackOff\n\n"
        "【建议下一步】\n"
        "- 检查最近 15 分钟的应用启动失败日志"
    )


@pytest.mark.asyncio
async def test_webhook_persists_targeted_analysis_context(monkeypatch, **_kwargs) -> None:
    """firing webhook 应将现有 targeted analysis 持久化，而不是只留在 timeline metadata。"""
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {"platforms": {"feishu": {"main_chat_id": "oc_ops"}}}
    await module.setup_alert_webhook(app)

    fake_store = FakeIncidentStore()

    async def _should_process(_alert: dict) -> bool:
        return True

    async def _collect_targeted_k8s_evidence(_alert: dict, analysis: dict, _config: dict) -> None:
        analysis["supporting_evidence"].append(
            {
                "kind": "pod_logs",
                "source": "kubectl logs api-123 -n default --tail=50 --since=15m",
                "summary": "CrashLoopBackOff repeated 3 times",
            }
        )
        analysis["suspected_root_causes"].append("容器反复 CrashLoopBackOff")
        analysis["next_best_actions"].append("检查最近 15 分钟的应用启动失败日志")
        analysis["missing_evidence"].remove("缺少 pod 日志摘要")

    class _FakeFeishuConversation:
        @staticmethod
        async def publish_incident_status(incident_id, alert, config):
            del alert, config
            assert incident_id == "incident-1"
            return {
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "omt_thread",
                "status_card_message_id": "om_card",
            }

        @staticmethod
        async def publish_incident_analysis_summary(incident, summary_text, config):
            del incident, summary_text, config
            return {"message_id": "om_summary", "root_message_id": "om_root", "thread_id": "omt_thread"}

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _FakeFeishuConversation)
    monkeypatch.setattr(module, "_collect_targeted_k8s_evidence", _collect_targeted_k8s_evidence, raising=False)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    assert [row["source_type"] for row in fake_store.evidence] == ["alert_window", "pod_logs"]
    assert fake_store.analyses[0]["likely_scope"] == "workload"
    assert fake_store.analyses[0]["suspected_root_causes"][0]["summary"] == "容器反复 CrashLoopBackOff"


@pytest.mark.asyncio
async def test_webhook_rebinds_incident_thread_from_summary_reply(monkeypatch, **_kwargs) -> None:
    """摘要 reply 返回真实 thread_id 后，应回写 incident 绑定，供后续 thread 追问命中。"""
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {"platforms": {"feishu": {"main_chat_id": "oc_ops"}}}
    await module.setup_alert_webhook(app)

    fake_store = FakeIncidentStore()

    async def _should_process(_alert: dict) -> bool:
        return True

    class _FakeFeishuConversation:
        @staticmethod
        async def publish_incident_status(incident_id, alert, config):
            del alert, config
            assert incident_id == "incident-1"
            return {
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "om_root",
                "status_card_message_id": "om_card",
            }

        @staticmethod
        async def publish_incident_analysis_summary(incident, summary_text, config):
            del summary_text, config
            assert incident == {
                "id": "incident-1",
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "om_root",
                "status_card_message_id": "om_card",
            }
            return {
                "message_id": "om_summary",
                "root_message_id": "om_root",
                "thread_id": "omt_thread",
            }

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
    assert fake_store.bindings == [
        (
            "incident-1",
            {
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "om_root",
                "status_card_message_id": "om_card",
            },
        ),
        (
            "incident-1",
            {
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "omt_thread",
                "status_card_message_id": "om_card",
            },
        ),
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
        "created_at": 100.0,
    }
    fake_store.evidence.extend(
        [
            {
                "incident_id": "incident-1",
                "source_type": "metrics_window",
                "summary": "restart_max=7",
                "payload": {"restart_max": "7"},
            },
            {
                "incident_id": "incident-1",
                "source_type": "audit_change",
                "summary": "最近 1 条变更线索",
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


@pytest.mark.asyncio
async def test_webhook_collects_targeted_pod_evidence_before_namespace_fallback(monkeypatch, **_kwargs) -> None:
    """命中 pod/container/deployment 目标时，应先做定向采样，再进入 namespace fallback。"""
    module = _load_module()

    alert = {
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
    call_order: list[str] = []
    fake_store = FakeIncidentStore()

    async def _should_process(_alert: dict) -> bool:
        return True

    async def _collect_targeted_k8s_evidence(target_alert: dict, analysis: dict, _config: dict) -> None:
        assert target_alert["pod_name"] == "api-123"
        call_order.append("targeted")
        analysis["supporting_evidence"].append(
            {
                "kind": "pod_logs",
                "source": "kubectl logs api-123 -n default --container api --tail=50 --since=15m",
                "summary": "CrashLoopBackOff repeated 3 times",
            }
        )
        analysis["suspected_root_causes"].append("容器反复 CrashLoopBackOff")
        analysis["next_best_actions"].append("检查最近 15 分钟的应用启动失败日志")
        analysis["missing_evidence"].remove("缺少 pod 日志摘要")

    async def _collect_namespace_fallback_evidence(_target_alert: dict, analysis: dict, _config: dict) -> None:
        assert call_order == ["targeted"]
        call_order.append("fallback")
        analysis["supporting_evidence"].append(
            {
                "kind": "namespace_fallback",
                "summary": "namespace fallback evidence",
            }
        )

    class _FakeFeishuConversation:
        @staticmethod
        async def publish_incident_status(incident_id, enriched_alert, config):
            del incident_id, config
            event_analysis = fake_store.events[0][5]["analysis"]
            assert enriched_alert["analysis"] == event_analysis
            return {
                "chat_id": None,
                "root_message_id": None,
                "thread_id": None,
                "status_card_message_id": None,
            }

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _FakeFeishuConversation)
    monkeypatch.setattr(module, "_collect_targeted_k8s_evidence", _collect_targeted_k8s_evidence, raising=False)
    monkeypatch.setattr(
        module,
        "_collect_namespace_fallback_evidence",
        _collect_namespace_fallback_evidence,
        raising=False,
    )

    result = await module.handle_alertmanager_payload({"alerts": [alert]}, config={})

    assert result["processed"] == 1
    assert call_order == ["targeted", "fallback"]
    assert fake_store.events[0][5]["analysis"] == {
        "suspected_root_causes": ["容器反复 CrashLoopBackOff"],
        "supporting_evidence": [
            {
                "kind": "pod_logs",
                "source": "kubectl logs api-123 -n default --container api --tail=50 --since=15m",
                "summary": "CrashLoopBackOff repeated 3 times",
            },
            {
                "kind": "namespace_fallback",
                "summary": "namespace fallback evidence",
            },
        ],
        "missing_evidence": [],
        "next_best_actions": ["检查最近 15 分钟的应用启动失败日志"],
    }


@pytest.mark.asyncio
async def test_webhook_targeted_collection_startup_failure_does_not_crash(monkeypatch, **_kwargs) -> None:
    """kubectl 进程启动失败时，webhook 应降级继续处理。"""
    module = _load_module()
    fake_store = FakeIncidentStore()

    async def _should_process(_alert: dict) -> bool:
        return True

    async def _raise_startup_error(*_args, **_kwargs):
        raise FileNotFoundError("kubectl not found")

    class _FakeFeishuConversation:
        @staticmethod
        async def publish_incident_status(incident_id, enriched_alert, config):
            del incident_id, config
            assert enriched_alert["analysis"]["supporting_evidence"] == []
            assert enriched_alert["analysis"]["missing_evidence"] == ["缺少 pod 日志摘要"]
            return {
                "chat_id": None,
                "root_message_id": None,
                "thread_id": None,
                "status_card_message_id": None,
            }

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _FakeFeishuConversation)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", _raise_startup_error)

    result = await module.handle_alertmanager_payload(
        {
            "alerts": [
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
            ]
        },
        config={},
    )

    assert result["processed"] == 1
    assert fake_store.events[0][5]["analysis"] == {
        "suspected_root_causes": [],
        "supporting_evidence": [],
        "missing_evidence": ["缺少 pod 日志摘要"],
        "next_best_actions": [],
    }


@pytest.mark.asyncio
async def test_webhook_failed_targeted_kubectl_results_do_not_count_as_evidence(monkeypatch, **_kwargs) -> None:
    """targeted kubectl 失败时，不应伪造 supporting evidence 或清空缺失日志项。"""
    module = _load_module()
    fake_store = FakeIncidentStore()

    async def _should_process(_alert: dict) -> bool:
        return True

    async def _failed_kubectl(_command: str) -> dict:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "Error from server (NotFound): pods \"api-123\" not found",
        }

    class _FakeFeishuConversation:
        @staticmethod
        async def publish_incident_status(incident_id, enriched_alert, config):
            del incident_id, config
            assert enriched_alert["analysis"]["supporting_evidence"] == []
            assert enriched_alert["analysis"]["missing_evidence"] == ["缺少 pod 日志摘要"]
            return {
                "chat_id": None,
                "root_message_id": None,
                "thread_id": None,
                "status_card_message_id": None,
            }

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _FakeFeishuConversation)
    monkeypatch.setattr(module, "_run_kubectl_command", _failed_kubectl)

    result = await module.handle_alertmanager_payload(
        {
            "alerts": [
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
            ]
        },
        config={},
    )

    assert result["processed"] == 1
    assert fake_store.events[0][5]["analysis"] == {
        "suspected_root_causes": [],
        "supporting_evidence": [],
        "missing_evidence": ["缺少 pod 日志摘要"],
        "next_best_actions": [],
    }


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
    """resolved webhook 应在真实 SQLite store 中写入 case profile，并支持 similar_incident_ids。"""
    module = _load_module()
    store = IncidentStore(tmp_path / "case-profiles.db")
    old_store = getattr(module.incident_store, "_STORE", None)
    if old_store is not None:
        old_store.close()
    module.incident_store._STORE = store
    app = web.Application()
    app["alert_webhook_config"] = {}
    await module.setup_alert_webhook(app)

    async def _should_process(alert: dict) -> bool:
        return True

    async def _collect_targeted_k8s_evidence(_alert: dict, analysis: dict, _config: dict) -> None:
        analysis["supporting_evidence"].append(
            {
                "kind": "pod_logs",
                "source": "kubectl logs api-123 -n default --tail=50 --since=15m",
                "summary": "CrashLoopBackOff repeated 3 times",
            }
        )
        analysis["suspected_root_causes"].append("容器反复 CrashLoopBackOff")
        analysis["next_best_actions"].append("检查最近 15 分钟的应用启动失败日志")
        analysis["missing_evidence"].remove("缺少 pod 日志摘要")

    class _NoopFeishuConversation:
        @staticmethod
        async def publish_incident_status(incident_id, alert, config):
            del incident_id, alert, config
            return {"chat_id": None, "root_message_id": None, "thread_id": None, "status_card_message_id": None}

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "feishu_conversation", _NoopFeishuConversation)
    monkeypatch.setattr(module, "_collect_targeted_k8s_evidence", _collect_targeted_k8s_evidence, raising=False)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        firing_response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        firing_data = await firing_response.json()
        incident_id = firing_data["incidents"][0]["incident_id"]

        older_incident = await module.incident_store.create_incident(
            "PodCrashLooping",
            "default",
            "prod-a",
            "older case",
        )

        await module.incident_store.upsert_case_profile(
            older_incident,
            incident_signature="PodCrashLooping|default|workload|resolved",
            final_scope="workload",
            final_root_cause="older root cause",
            similar_incident_ids=[incident_id],
        )
    finally:
        await client.close()

    profile = await module.incident_store.get_case_profile(older_incident)

    assert profile is not None
    assert profile["similar_incident_ids"] == [incident_id]

    store.close()


@pytest.mark.asyncio
async def test_webhook_recalls_similar_case_from_local_store(tmp_path: Path, monkeypatch, **_kwargs) -> None:
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {}
    await module.setup_alert_webhook(app)

    store = IncidentStore(tmp_path / "data" / "incidents.db")
    old_store = getattr(module.incident_store, "_STORE", None)
    if old_store is not None:
        old_store.close()
    module.incident_store._STORE = store

    old_incident = await store.create_incident("PodCrashLooping", "default", "prod-a", "older")
    await store.upsert_case_profile(
        old_incident,
        incident_signature="PodCrashLooping|default|workload|resolved",
        final_scope="workload",
        final_root_cause="应用日志显示运行时异常",
        effective_actions=["检查相关 Pod 最近错误日志与超时信息"],
    )

    async def _should_process(_alert: dict) -> bool:
        return True

    async def _collect_targeted_k8s_evidence(_alert: dict, analysis: dict, _config: dict) -> None:
        analysis["supporting_evidence"].append(
            {
                "kind": "pod_logs",
                "source": "kubectl logs api-123 -n default --tail=50 --since=15m",
                "summary": "CrashLoopBackOff repeated 3 times",
            }
        )
        analysis["suspected_root_causes"].append("容器反复 CrashLoopBackOff")
        analysis["next_best_actions"].append("检查最近 15 分钟的应用启动失败日志")
        analysis["missing_evidence"].remove("缺少 pod 日志摘要")

    class _NoopFeishuConversation:
        @staticmethod
        async def publish_incident_status(incident_id, alert, config):
            del incident_id, alert, config
            return {"chat_id": None, "root_message_id": None, "thread_id": None, "status_card_message_id": None}

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "_collect_targeted_k8s_evidence", _collect_targeted_k8s_evidence, raising=False)
    monkeypatch.setattr(module, "feishu_conversation", _NoopFeishuConversation)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()
        incident_id = data["incidents"][0]["incident_id"]
        analysis = await module.incident_store.get_analysis(incident_id)
        evidence_rows = await module.incident_store.list_evidence(incident_id)
    finally:
        await client.close()
        store.close()

    assert any(row["source_type"] == "case_recall" for row in evidence_rows)
    assert any("历史相似 case" in item["summary"] for item in analysis["supporting_evidence"])
