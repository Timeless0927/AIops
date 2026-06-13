"""测试 Alertmanager Webhook 处理模块。"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
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


def test_load_config_prefers_hermes_config_over_hermes_home(
    tmp_path: Path,
    monkeypatch,
    **_kwargs,
) -> None:
    """运行时配置应优先读取 HERMES_CONFIG，再回退 HERMES_HOME/config.yaml。"""
    module = _load_module()
    explicit_config = tmp_path / "explicit.yaml"
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    explicit_config.write_text(
        "platforms:\n  feishu:\n    main_chat_id: oc_explicit\n",
        encoding="utf-8",
    )
    (hermes_home / "config.yaml").write_text(
        "platforms:\n  feishu:\n    main_chat_id: oc_home\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_CONFIG", str(explicit_config))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert module._load_config_sync()["platforms"]["feishu"]["main_chat_id"] == "oc_explicit"


def test_load_config_falls_back_to_hermes_home_config(
    tmp_path: Path,
    monkeypatch,
    **_kwargs,
) -> None:
    """未设置 HERMES_CONFIG 时，应读取 HERMES_HOME/config.yaml。"""
    module = _load_module()
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "platforms:\n  feishu:\n    main_chat_id: oc_home\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert module._load_config_sync()["platforms"]["feishu"]["main_chat_id"] == "oc_home"


def test_load_config_without_env_does_not_read_repo_root_config(
    tmp_path: Path,
    monkeypatch,
    **_kwargs,
) -> None:
    """未设置运行时 env 时，即使 CWD 有 config.yaml 也不得读取。"""
    module = _load_module()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "config.yaml").write_text(
        "platforms:\n  feishu:\n    main_chat_id: oc_repo_root\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    old_cwd = Path.cwd()
    os.chdir(cwd)
    try:
        assert module._load_config_sync() == {}
    finally:
        os.chdir(old_cwd)


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
        "service": None,
        "app_id": None,
        "labels": {
            "alertname": "PodCrashLooping",
            "severity": "critical",
            "namespace": "default",
            "cluster": "prod-a",
            "pod": "api-123",
            "container": "api",
            "deployment": "api",
        },
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
            {
                "platform": "feishu",
                "dedup_key": "PodCrashLooping|default|prod-a",
                "dedup_key_version": "v1",
                "service_id": "prod-a/default/unknown-service",
                "owner_team": "sre",
                "ownership_source": "default_team",
                "ownership_status": "unowned",
                "ownership_confidence": 0.2,
                "notification_channel": None,
                "rbac_scope": "team:sre",
                "approval_scope": "team:sre",
            },
        )
    ]
    assert fake_store.events[0][0] == "incident-1"
    assert fake_store.events[0][1] == "alert_fired"
    assert data_resolved["processed"] == 0
    assert data_resolved["skipped"] == 1


@pytest.mark.asyncio
async def test_webhook_resolves_service_ownership_from_cmdb_config(monkeypatch, **_kwargs) -> None:
    """incident 创建前应解析服务归属并透传 owner/team/routing scope 字段。"""
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {
        "cmdb": {
            "default_team": "sre",
            "service_ownership": [
                {
                    "service_key": "prod-a/default/api",
                    "service_id": "svc-api",
                    "service_name": "api",
                    "owner_team": "api-dev",
                    "notification_channel": "oc_api",
                    "rbac_scope": "team:api-dev",
                    "approval_scope": "api-prod",
                }
            ],
        }
    }
    await module.setup_alert_webhook(app)

    async def _should_process(alert: dict) -> bool:
        return True

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    fake_store = FakeIncidentStore()
    monkeypatch.setattr(module, "incident_store", fake_store)

    payload = _payload("firing")
    payload["alerts"][0]["labels"]["service"] = "api"

    data = await module.handle_alertmanager_payload(payload, {}, app["alert_webhook_config"])

    assert data["processed"] == 1
    assert fake_store.created[0][4] == {
        "platform": "feishu",
        "dedup_key": "PodCrashLooping|default|prod-a",
        "dedup_key_version": "v1",
        "service_id": "svc-api",
        "owner_team": "api-dev",
        "ownership_source": "bk_cmdb",
        "ownership_status": "owned",
        "ownership_confidence": 0.95,
        "notification_channel": "oc_api",
        "rbac_scope": "team:api-dev",
        "approval_scope": "api-prod",
    }
    assert fake_store.events[0][5]["ownership"]["owner_team"] == "api-dev"


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
async def test_webhook_requests_approval_for_next_best_action(monkeypatch, **_kwargs) -> None:
    """firing webhook 应从分析建议生成一次非阻塞审批。"""
    module = _load_module()
    fake_store = FakeIncidentStore()
    approval_calls: list[dict] = []

    async def _should_process(_alert: dict) -> bool:
        return True

    async def _collect_targeted_k8s_evidence(_alert: dict, analysis: dict, _config: dict) -> None:
        analysis["supporting_evidence"].append({"kind": "pod_status", "summary": "deployment/nginx pod 重启"})
        analysis["suspected_root_causes"].append("应用进程反复退出")
        analysis["next_best_actions"].append("重启 deployment/nginx")
        analysis["missing_evidence"].remove("缺少 pod 日志摘要")

    class _FakeApprovalAsync:
        @staticmethod
        async def find_pending_approval(incident_id: str, action_signature: str):
            assert incident_id == "incident-1"
            assert action_signature == "restart_deployment:prod-a:default:deployment/nginx"
            return None

        @staticmethod
        async def request_approval_with_card(
            operation_type,
            command,
            context,
            namespace,
            requester,
            risk_level,
            *,
            incident_id=None,
            config=None,
        ):
            assert config == {}
            approval_calls.append(
                {
                    "operation_type": operation_type,
                    "command": command,
                    "context": context,
                    "namespace": namespace,
                    "requester": requester,
                    "risk_level": risk_level,
                    "incident_id": incident_id,
                    "approval_message_id": "om_approval",
                    "delivery_status": "sent",
                }
            )
            return {
                "ok": True,
                "approval_id": "approval-1",
                "approval_message_id": "om_approval",
                "delivery_status": "sent",
            }

        @staticmethod
        async def check_approval(approval_id: str):
            return {"approval_id": approval_id, "status": "pending", "approval_message_id": "om_approval"}

    class _FakeFeishuConversation:
        @staticmethod
        async def publish_incident_status(incident_id, alert, config):
            del incident_id, alert, config
            return {
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "omt_thread",
                "status_card_message_id": "om_status",
            }

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "approval_async", _FakeApprovalAsync)
    monkeypatch.setattr(module, "feishu_conversation", _FakeFeishuConversation)
    monkeypatch.setattr(module, "_collect_targeted_k8s_evidence", _collect_targeted_k8s_evidence, raising=False)

    result = await module.handle_alertmanager_payload({"alerts": [_payload("firing")["alerts"][0]]}, config={})

    assert result["processed"] == 1
    assert approval_calls == [
        {
            "operation_type": "k8s_write",
            "command": "重启 deployment/nginx",
            "context": {
                "action_signature": "restart_deployment:prod-a:default:deployment/nginx",
                "executable": True,
                "remediation_action": {
                    "action_schema_version": "remediation.action.v1",
                    "action_signature": "restart_deployment:prod-a:default:deployment/nginx",
                    "action_type": "restart_deployment",
                    "cluster": "prod-a",
                    "namespace": "default",
                    "resource_kind": "deployment",
                    "resource_name": "nginx",
                    "parameters": {"strategy": "rollout_restart"},
                    "source": {
                        "incident_id": "incident-1",
                        "alertname": "PodCrashLooping",
                        "analysis_action": "重启 deployment/nginx",
                    },
                    "risk": {"risk_level": "low", "operation_type": "k8s_write"},
                },
                "alertname": "PodCrashLooping",
                "namespace": "default",
                "cluster": "prod-a",
                "source": "alert_webhook",
            },
            "namespace": "default",
            "requester": "alert_webhook",
            "risk_level": "low",
            "incident_id": "incident-1",
            "approval_message_id": "om_approval",
            "delivery_status": "sent",
        }
    ]
    assert any(event[1] == "approval_requested" for event in fake_store.events)


@pytest.mark.asyncio
async def test_webhook_publishes_status_before_approval_request_with_empty_config(monkeypatch, **_kwargs) -> None:
    """主流程应先写 incident 飞书绑定，再触发审批请求。"""
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {}
    await module.setup_alert_webhook(app)
    fake_store = FakeIncidentStore()
    calls: list[str] = []
    approval_alerts: list[dict] = []

    async def _should_process(_alert: dict) -> bool:
        return True

    async def _collect_targeted_k8s_evidence(_alert: dict, _analysis: dict, _config: dict) -> None:
        return None

    async def _maybe_request_phase3_approval(_incident_id, alert, _analysis, _config):
        approval_alerts.append(alert)
        calls.append("approval")
        return {"approval_id": "ap-1", "status": "pending"}

    async def _update_feishu_binding(incident_id, **kwargs):
        calls.append("binding")
        fake_store.bindings.append((incident_id, kwargs))

    class _FakeFeishuConversation:
        @staticmethod
        async def publish_incident_status(incident_id, alert, config):
            del alert, config
            calls.append("status")
            assert incident_id == "incident-1"
            return {
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "om_root",
                "status_card_message_id": "om_card",
            }

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _FakeFeishuConversation)
    monkeypatch.setattr(module, "_maybe_request_phase3_approval", _maybe_request_phase3_approval, raising=False)
    monkeypatch.setattr(module, "_collect_targeted_k8s_evidence", _collect_targeted_k8s_evidence, raising=False)
    monkeypatch.setattr(fake_store, "update_feishu_binding", _update_feishu_binding)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    assert calls == ["status", "binding", "approval"]
    assert approval_alerts[0]["feishu_binding"] == {
        "chat_id": "oc_ops",
        "root_message_id": "om_root",
        "thread_id": "om_root",
        "status_card_message_id": "om_card",
    }


@pytest.mark.asyncio
async def test_webhook_unknown_action_creates_non_executable_approval(monkeypatch, **_kwargs) -> None:
    module = _load_module()
    fake_store = FakeIncidentStore()
    approval_calls: list[dict] = []

    class _FakeApprovalAsync:
        @staticmethod
        async def find_pending_approval(_incident_id: str, _action_signature: str):
            return None

        @staticmethod
        async def request_approval(
            operation_type,
            command,
            context,
            namespace,
            requester,
            risk_level,
            *,
            incident_id=None,
            approval_message_id=None,
        ):
            approval_calls.append(
                {
                    "operation_type": operation_type,
                    "command": command,
                    "context": context,
                    "namespace": namespace,
                    "requester": requester,
                    "risk_level": risk_level,
                    "incident_id": incident_id,
                    "approval_message_id": approval_message_id,
                }
            )
            return "approval-unknown"

        @staticmethod
        async def check_approval(approval_id: str):
            return {"approval_id": approval_id, "status": "pending"}

    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "approval_async", _FakeApprovalAsync)

    await module._maybe_request_phase3_approval(
        "incident-1",
        {"alertname": "PodCrashLooping", "namespace": "default", "cluster": "prod-a"},
        {"next_best_actions": ["检查最近 15 分钟的应用启动失败日志"]},
    )

    assert approval_calls[0]["operation_type"] == "manual_remediation"
    assert approval_calls[0]["risk_level"] == "standard"
    assert approval_calls[0]["context"]["executable"] is False
    assert approval_calls[0]["context"]["non_executable_reason"] == "unsupported_action"
    assert "remediation_action" not in approval_calls[0]["context"]


@pytest.mark.asyncio
async def test_webhook_scale_action_creates_executable_approval(monkeypatch, **_kwargs) -> None:
    module = _load_module()
    fake_store = FakeIncidentStore()
    approval_calls: list[dict] = []

    class _FakeApprovalAsync:
        @staticmethod
        async def find_pending_approval(_incident_id: str, action_signature: str):
            assert action_signature == "scale_deployment:prod-a:default:deployment/nginx:replicas=3"
            return None

        @staticmethod
        async def request_approval(
            operation_type,
            command,
            context,
            namespace,
            requester,
            risk_level,
            *,
            incident_id=None,
            approval_message_id=None,
        ):
            approval_calls.append(
                {
                    "operation_type": operation_type,
                    "command": command,
                    "context": context,
                    "namespace": namespace,
                    "requester": requester,
                    "risk_level": risk_level,
                    "incident_id": incident_id,
                    "approval_message_id": approval_message_id,
                }
            )
            return "approval-scale"

        @staticmethod
        async def check_approval(approval_id: str):
            return {"approval_id": approval_id, "status": "pending"}

    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "approval_async", _FakeApprovalAsync)

    await module._maybe_request_phase3_approval(
        "incident-1",
        {"alertname": "KubeDeploymentReplicasMismatch", "namespace": "default", "cluster": "prod-a"},
        {"next_best_actions": ["扩容 deployment/nginx 到 3 副本"]},
    )

    context = approval_calls[0]["context"]
    assert approval_calls[0]["operation_type"] == "k8s_write"
    assert approval_calls[0]["risk_level"] == "low"
    assert context["executable"] is True
    assert context["remediation_action"]["action_type"] == "scale_deployment"
    assert context["remediation_action"]["parameters"] == {"replicas": 3}


@pytest.mark.asyncio
async def test_webhook_publishes_status_before_approval_request(monkeypatch, **_kwargs) -> None:
    """主流程应先写 incident 飞书绑定，再触发审批请求。"""
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {}
    await module.setup_alert_webhook(app)
    fake_store = FakeIncidentStore()
    calls: list[str] = []
    approval_alerts: list[dict] = []

    async def _should_process(_alert: dict) -> bool:
        return True

    async def _collect_targeted_k8s_evidence(_alert: dict, _analysis: dict, _config: dict) -> None:
        return None

    async def _maybe_request_phase3_approval(_incident_id, alert, _analysis, _config):
        approval_alerts.append(alert)
        calls.append("approval")
        return {"approval_id": "ap-1", "status": "pending"}

    async def _update_feishu_binding(incident_id, **kwargs):
        calls.append("binding")
        fake_store.bindings.append((incident_id, kwargs))

    class _FakeFeishuConversation:
        @staticmethod
        async def publish_incident_status(incident_id, alert, config):
            del alert, config
            calls.append("status")
            assert incident_id == "incident-1"
            return {
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "om_root",
                "status_card_message_id": "om_card",
            }

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)
    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "feishu_conversation", _FakeFeishuConversation)
    monkeypatch.setattr(module, "_maybe_request_phase3_approval", _maybe_request_phase3_approval, raising=False)
    monkeypatch.setattr(module, "_collect_targeted_k8s_evidence", _collect_targeted_k8s_evidence, raising=False)
    monkeypatch.setattr(fake_store, "update_feishu_binding", _update_feishu_binding)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()
    finally:
        await client.close()

    assert data["processed"] == 1
    assert calls == ["status", "binding", "approval"]
    assert approval_alerts[0]["feishu_binding"] == {
        "chat_id": "oc_ops",
        "root_message_id": "om_root",
        "thread_id": "om_root",
        "status_card_message_id": "om_card",
    }


@pytest.mark.asyncio
async def test_webhook_invalid_replicas_creates_non_executable_approval(monkeypatch, **_kwargs) -> None:
    module = _load_module()
    fake_store = FakeIncidentStore()
    approval_calls: list[dict] = []

    class _FakeApprovalAsync:
        @staticmethod
        async def find_pending_approval(_incident_id: str, _action_signature: str):
            return None

        @staticmethod
        async def request_approval(
            operation_type,
            command,
            context,
            namespace,
            requester,
            risk_level,
            *,
            incident_id=None,
            approval_message_id=None,
        ):
            approval_calls.append(
                {
                    "operation_type": operation_type,
                    "command": command,
                    "context": context,
                    "namespace": namespace,
                    "requester": requester,
                    "risk_level": risk_level,
                    "incident_id": incident_id,
                    "approval_message_id": approval_message_id,
                }
            )
            return "approval-invalid"

        @staticmethod
        async def check_approval(approval_id: str):
            return {"approval_id": approval_id, "status": "pending"}

    monkeypatch.setattr(module, "incident_store", fake_store)
    monkeypatch.setattr(module, "approval_async", _FakeApprovalAsync)

    await module._maybe_request_phase3_approval(
        "incident-1",
        {"alertname": "KubeDeploymentReplicasMismatch", "namespace": "default", "cluster": "prod-a"},
        {"next_best_actions": ["扩容 deployment/nginx 到 21 副本"]},
    )

    assert approval_calls[0]["operation_type"] == "manual_remediation"
    assert approval_calls[0]["context"]["executable"] is False
    assert approval_calls[0]["context"]["non_executable_reason"] == "invalid_replicas"
    assert "remediation_action" not in approval_calls[0]["context"]


@pytest.mark.asyncio
async def test_maybe_request_phase3_approval_repairs_existing_pending_without_card(monkeypatch, **_kwargs) -> None:
    """已有 pending 但没有 approval_message_id 时，应先补发卡片再返回最新状态。"""
    module = _load_module()
    fake_store = FakeIncidentStore()
    calls: list[str] = []

    class _FakeApprovalAsync:
        @staticmethod
        async def find_pending_approval(_incident_id: str, _action_signature: str):
            return {
                "approval_id": "ap-1",
                "status": "pending",
                "approval_message_id": None,
            }

        @staticmethod
        async def publish_or_queue_approval_card(approval_id: str, config=None):
            del config
            calls.append(approval_id)
            return {
                "ok": True,
                "approval_id": approval_id,
                "approval_message_id": "om_approval",
                "delivery_status": "sent",
            }

        @staticmethod
        async def check_approval(approval_id: str):
            return {"approval_id": approval_id, "status": "pending", "approval_message_id": "om_approval"}

    monkeypatch.setattr(module, "approval_async", _FakeApprovalAsync)
    monkeypatch.setattr(module, "incident_store", fake_store)

    result = await module._maybe_request_phase3_approval(
        "incident-1",
        {"alertname": "KubeDeploymentReplicasMismatch", "namespace": "default", "cluster": "prod-a"},
        {"next_best_actions": ["扩容 deployment/nginx 到 3 副本"]},
    )

    assert calls == ["ap-1"]
    assert result["approval_message_id"] == "om_approval"
    assert result["delivery_status"] == "sent"


@pytest.mark.asyncio
async def test_existing_external_pending_without_message_id_does_not_send_legacy_approval_card_by_default(
    monkeypatch,
    **_kwargs,
) -> None:
    """已有原生 external_pending 审批时，默认不得补发可批准的旧交互卡片。"""
    module = _load_module()
    fake_store = FakeIncidentStore()
    legacy_card_calls: list[str] = []

    class _FakeApprovalAsync:
        @staticmethod
        async def find_pending_approval(_incident_id: str, _action_signature: str):
            return {
                "approval_id": "ap-native-1",
                "status": "external_pending",
                "approval_message_id": None,
                "external_provider": "feishu",
                "external_instance_code": "INST-001",
                "external_url": "https://approval.feishu.cn/approval/INST-001",
            }

        @staticmethod
        async def publish_or_queue_approval_card(approval_id: str, config=None):
            del config
            legacy_card_calls.append(approval_id)
            return {
                "ok": True,
                "approval_id": approval_id,
                "approval_message_id": "om_legacy_card",
                "delivery_status": "sent",
            }

        @staticmethod
        async def check_approval(approval_id: str):
            return {
                "approval_id": approval_id,
                "status": "external_pending",
                "approval_message_id": None,
                "external_provider": "feishu",
                "external_instance_code": "INST-001",
                "external_url": "https://approval.feishu.cn/approval/INST-001",
            }

    monkeypatch.setattr(module, "approval_async", _FakeApprovalAsync)
    monkeypatch.setattr(module, "incident_store", fake_store)

    result = await module._maybe_request_phase3_approval(
        "incident-1",
        {"alertname": "KubeDeploymentReplicasMismatch", "namespace": "default", "cluster": "prod-a"},
        {"next_best_actions": ["扩容 deployment/nginx 到 3 副本"]},
        config={"platforms": {"feishu": {"approval": {"enabled": True}}}},
    )

    assert legacy_card_calls == []
    assert result["status"] == "external_pending"
    assert result["approval_message_id"] is None
    assert result["external_instance_code"] == "INST-001"


@pytest.mark.asyncio
async def test_webhook_creates_feishu_native_approval_and_thread_notice(monkeypatch, **_kwargs) -> None:
    """需要审批的动作应创建飞书原生审批，并在 thread 回写链接、风险摘要和操作摘要。"""
    module = _load_module()
    fake_store = FakeIncidentStore()
    approval_calls: list[dict] = []
    message_updates: list[tuple[str, str]] = []
    native_calls: list[dict] = []
    notices: list[dict] = []
    deliveries: list[dict] = []
    sent_deliveries: list[tuple[str, str]] = []

    class _FakeApprovalAsync:
        @staticmethod
        async def find_pending_approval(_incident_id: str, _action_signature: str):
            return None

        @staticmethod
        async def request_external_approval(
            operation_type,
            command,
            context,
            namespace,
            requester,
            risk_level,
            *,
            incident_id=None,
            config=None,
        ):
            approval_calls.append(
                {
                    "operation_type": operation_type,
                    "command": command,
                    "context": context,
                    "namespace": namespace,
                    "requester": requester,
                    "risk_level": risk_level,
                    "incident_id": incident_id,
                    "config": config,
                }
            )
            return {"ok": True, "approval_id": "ap-native-1", "status": "external_pending"}

        @staticmethod
        async def request_approval_with_card(*_args, **_kwargs):
            raise AssertionError("native approval branch must not call request_approval_with_card")

        @staticmethod
        async def record_external_approval_created(approval_id, **fields):
            return {"ok": True, "approval_id": approval_id, "status": "external_pending", **fields}

        @staticmethod
        async def check_approval(approval_id: str):
            return {
                "approval_id": approval_id,
                "status": "external_pending",
                "approval_message_id": None,
                "risk_level": "low",
                "command": "重启 deployment/nginx",
                "external_url": "https://approval.feishu.cn/approval/INST-001",
                "external_instance_code": "INST-001",
            }

        @staticmethod
        async def update_approval_message_id(approval_id: str, message_id: str):
            message_updates.append((approval_id, message_id))
            return {"ok": True, "approval_id": approval_id, "approval_message_id": message_id}

    class _FakeNativeApproval:
        @staticmethod
        async def create_approval_instance(**kwargs):
            native_calls.append(kwargs)
            return {
                "ok": True,
                "external_provider": "feishu",
                "external_uuid": kwargs["approval_id"],
                "external_instance_code": "INST-001",
                "external_status": "PENDING",
                "external_url": "https://approval.feishu.cn/approval/INST-001",
            }

    class _FakeConversation:
        @staticmethod
        async def publish_native_approval_notice(incident, approval, config):
            notices.append({"incident": incident, "approval": approval, "config": config})
            return {"message_id": "om_notice", "thread_id": incident["thread_id"]}

    class _FakeMessageDelivery:
        @staticmethod
        async def find_sent_delivery_for_approval(*, approval_id, target_type):
            assert approval_id == "ap-native-1"
            assert target_type == "approval_notice"
            return None

        @staticmethod
        async def upsert_delivery(**kwargs):
            deliveries.append(kwargs)
            return "delivery-notice-1"

        @staticmethod
        async def mark_sent(delivery_id: str, target_message_id: str):
            sent_deliveries.append((delivery_id, target_message_id))

        @staticmethod
        async def mark_failed(_delivery_id: str, _error: str):
            raise AssertionError("native approval notice should not fail")

    monkeypatch.setattr(module, "approval_async", _FakeApprovalAsync)
    monkeypatch.setattr(module, "feishu_native_approval", _FakeNativeApproval, raising=False)
    monkeypatch.setattr(module, "feishu_conversation", _FakeConversation, raising=False)
    monkeypatch.setattr(module, "message_delivery", _FakeMessageDelivery, raising=False)
    monkeypatch.setattr(module, "incident_store", fake_store)

    result = await module._maybe_request_phase3_approval(
        "incident-1",
        {
            "alertname": "KubeDeploymentReplicasMismatch",
            "namespace": "default",
            "cluster": "prod-a",
            "feishu_binding": {
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "omt_thread",
            },
        },
        {"next_best_actions": ["重启 deployment/nginx"]},
        config={"platforms": {"feishu": {"approval": {"enabled": True, "approval_code": "approval-code"}}}},
    )

    assert approval_calls[0]["operation_type"] == "k8s_write"
    assert native_calls[0]["approval_id"] == "ap-native-1"
    assert native_calls[0]["command"] == "重启 deployment/nginx"
    assert result["status"] == "external_pending"
    assert result["external_instance_code"] == "INST-001"
    assert result["external_url"] == "https://approval.feishu.cn/approval/INST-001"
    assert notices
    notice_text = json.dumps(notices[0]["approval"], ensure_ascii=False)
    assert "https://approval.feishu.cn/approval/INST-001" in notice_text
    assert "low" in notice_text
    assert "重启 deployment/nginx" in notice_text
    assert result["approval_message_id"] == "om_notice"
    assert result["delivery_status"] == "sent"
    assert deliveries[0]["target_type"] == "approval_notice"
    assert deliveries[0]["approval_id"] == "ap-native-1"
    assert deliveries[0]["incident_id"] == "incident-1"
    assert deliveries[0]["chat_id"] == "oc_ops"
    assert deliveries[0]["thread_id"] == "omt_thread"
    assert sent_deliveries == [("delivery-notice-1", "om_notice")]
    assert message_updates == [("ap-native-1", "om_notice")]


@pytest.mark.asyncio
async def test_webhook_native_notice_without_message_id_records_failed_delivery(monkeypatch, **_kwargs) -> None:
    """原生审批通知未返回 message_id 时，应保留 failed delivery 且不回写 approval_message_id。"""
    module = _load_module()
    fake_store = FakeIncidentStore()
    failed_deliveries: list[tuple[str, str]] = []
    message_updates: list[tuple[str, str]] = []

    class _FakeApprovalAsync:
        @staticmethod
        async def find_pending_approval(_incident_id: str, _action_signature: str):
            return None

        @staticmethod
        async def request_external_approval(*_args, **_kwargs):
            return {"ok": True, "approval_id": "ap-native-1", "status": "external_pending"}

        @staticmethod
        async def record_external_approval_created(approval_id, **fields):
            return {"ok": True, "approval_id": approval_id, "status": "external_pending", **fields}

        @staticmethod
        async def check_approval(approval_id: str):
            return {
                "approval_id": approval_id,
                "status": "external_pending",
                "approval_message_id": None,
                "risk_level": "low",
                "command": "重启 deployment/nginx",
                "external_url": "https://approval.feishu.cn/approval/INST-001",
                "external_instance_code": "INST-001",
            }

        @staticmethod
        async def update_approval_message_id(approval_id: str, message_id: str):
            message_updates.append((approval_id, message_id))

    class _FakeNativeApproval:
        @staticmethod
        async def create_approval_instance(**kwargs):
            return {
                "ok": True,
                "external_uuid": kwargs["approval_id"],
                "external_instance_code": "INST-001",
                "external_status": "PENDING",
                "external_url": "https://approval.feishu.cn/approval/INST-001",
            }

    class _FakeConversation:
        @staticmethod
        async def publish_native_approval_notice(_incident, _approval, _config):
            return {"message_id": None, "thread_id": "omt_thread"}

    class _FakeMessageDelivery:
        @staticmethod
        async def find_sent_delivery_for_approval(*, approval_id, target_type):
            assert approval_id == "ap-native-1"
            assert target_type == "approval_notice"
            return None

        @staticmethod
        async def upsert_delivery(**_kwargs):
            return "delivery-notice-1"

        @staticmethod
        async def mark_sent(_delivery_id: str, _target_message_id: str):
            raise AssertionError("delivery without message_id must not be marked sent")

        @staticmethod
        async def mark_failed(delivery_id: str, error: str):
            failed_deliveries.append((delivery_id, error))

    monkeypatch.setattr(module, "approval_async", _FakeApprovalAsync)
    monkeypatch.setattr(module, "feishu_native_approval", _FakeNativeApproval, raising=False)
    monkeypatch.setattr(module, "feishu_conversation", _FakeConversation, raising=False)
    monkeypatch.setattr(module, "message_delivery", _FakeMessageDelivery, raising=False)
    monkeypatch.setattr(module, "incident_store", fake_store)

    result = await module._maybe_request_phase3_approval(
        "incident-1",
        {
            "alertname": "KubeDeploymentReplicasMismatch",
            "namespace": "default",
            "cluster": "prod-a",
            "feishu_binding": {
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "omt_thread",
            },
        },
        {"next_best_actions": ["重启 deployment/nginx"]},
        config={"platforms": {"feishu": {"approval": {"enabled": True, "approval_code": "approval-code"}}}},
    )

    assert result["status"] == "external_pending"
    assert result["approval_message_id"] is None
    assert result["delivery_status"] == "pending_retry"
    assert failed_deliveries == [("delivery-notice-1", "飞书原生审批通知未返回 message_id")]
    assert message_updates == []


@pytest.mark.asyncio
async def test_webhook_native_notice_thread_reply_sets_sent_delivery(monkeypatch, **_kwargs) -> None:
    """原生审批 thread fallback 通知拿到 message_id 后，必须回写并标记 delivery sent。"""
    module = _load_module()
    fake_store = FakeIncidentStore()
    deliveries: list[dict] = []
    sent_deliveries: list[tuple[str, str]] = []
    message_updates: list[tuple[str, str]] = []
    notice_calls: list[dict] = []

    class _FakeApprovalAsync:
        @staticmethod
        async def find_pending_approval(_incident_id: str, _action_signature: str):
            return None

        @staticmethod
        async def request_external_approval(*_args, **_kwargs):
            return {"ok": True, "approval_id": "ap-native-1", "status": "external_pending"}

        @staticmethod
        async def record_external_approval_created(approval_id, **fields):
            return {"ok": True, "approval_id": approval_id, "status": "external_pending", **fields}

        @staticmethod
        async def check_approval(approval_id: str):
            return {
                "approval_id": approval_id,
                "status": "external_pending",
                "approval_message_id": None,
                "risk_level": "low",
                "command": "重启 deployment/nginx",
                "external_url": "https://approval.feishu.cn/approval/INST-001",
                "external_instance_code": "INST-001",
            }

        @staticmethod
        async def update_approval_message_id(approval_id: str, message_id: str):
            message_updates.append((approval_id, message_id))
            return {"ok": True, "approval_id": approval_id, "approval_message_id": message_id}

    class _FakeNativeApproval:
        @staticmethod
        async def create_approval_instance(**kwargs):
            return {
                "ok": True,
                "external_uuid": kwargs["approval_id"],
                "external_instance_code": "INST-001",
                "external_status": "PENDING",
                "external_url": "https://approval.feishu.cn/approval/INST-001",
            }

    class _FakeConversation:
        @staticmethod
        async def publish_native_approval_notice(incident, approval, config):
            notice_calls.append({"incident": incident, "approval": approval, "config": config})
            return {"message_id": "om_notice", "root_message_id": "om_root", "thread_id": "omt_thread"}

    class _FakeMessageDelivery:
        @staticmethod
        async def find_sent_delivery_for_approval(*, approval_id, target_type):
            assert approval_id == "ap-native-1"
            assert target_type == "approval_notice"
            return None

        @staticmethod
        async def upsert_delivery(**kwargs):
            deliveries.append(kwargs)
            return "delivery-notice-1"

        @staticmethod
        async def mark_sent(delivery_id: str, target_message_id: str):
            sent_deliveries.append((delivery_id, target_message_id))

        @staticmethod
        async def mark_failed(_delivery_id: str, _error: str):
            raise AssertionError("thread notice with message_id must be marked sent")

    monkeypatch.setattr(module, "approval_async", _FakeApprovalAsync)
    monkeypatch.setattr(module, "feishu_native_approval", _FakeNativeApproval, raising=False)
    monkeypatch.setattr(module, "feishu_conversation", _FakeConversation, raising=False)
    monkeypatch.setattr(module, "message_delivery", _FakeMessageDelivery, raising=False)
    monkeypatch.setattr(module, "incident_store", fake_store)

    result = await module._maybe_request_phase3_approval(
        "incident-1",
        {
            "alertname": "KubeDeploymentReplicasMismatch",
            "namespace": "default",
            "cluster": "prod-a",
            "feishu_binding": {
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "omt_thread",
            },
        },
        {"next_best_actions": ["重启 deployment/nginx"]},
        config={"platforms": {"feishu": {"approval": {"enabled": True, "approval_code": "approval-code"}}}},
    )

    assert notice_calls[0]["incident"] == {
        "incident_id": "incident-1",
        "chat_id": "oc_ops",
        "root_message_id": "om_root",
        "thread_id": "omt_thread",
    }
    assert deliveries[0]["target_type"] == "approval_notice"
    assert deliveries[0]["thread_id"] == "omt_thread"
    assert sent_deliveries == [("delivery-notice-1", "om_notice")]
    assert message_updates == [("ap-native-1", "om_notice")]
    assert result["approval_message_id"] == "om_notice"
    assert result["delivery_status"] == "sent"


@pytest.mark.asyncio
async def test_webhook_native_notice_without_feishu_binding_records_failed_delivery(monkeypatch, **_kwargs) -> None:
    """缺少 chat/thread 绑定时，原生审批通知也必须写入 failed delivery 以便审计和补偿。"""
    module = _load_module()
    fake_store = FakeIncidentStore()
    deliveries: list[dict] = []
    failed_deliveries: list[tuple[str, str]] = []
    message_updates: list[tuple[str, str]] = []

    class _FakeApprovalAsync:
        @staticmethod
        async def find_pending_approval(_incident_id: str, _action_signature: str):
            return None

        @staticmethod
        async def request_external_approval(*_args, **_kwargs):
            return {"ok": True, "approval_id": "ap-native-1", "status": "external_pending"}

        @staticmethod
        async def record_external_approval_created(approval_id, **fields):
            return {"ok": True, "approval_id": approval_id, "status": "external_pending", **fields}

        @staticmethod
        async def check_approval(approval_id: str):
            return {
                "approval_id": approval_id,
                "status": "external_pending",
                "approval_message_id": None,
                "risk_level": "low",
                "command": "重启 deployment/nginx",
                "external_url": "https://approval.feishu.cn/approval/INST-001",
                "external_instance_code": "INST-001",
            }

        @staticmethod
        async def update_approval_message_id(approval_id: str, message_id: str):
            message_updates.append((approval_id, message_id))

    class _FakeNativeApproval:
        @staticmethod
        async def create_approval_instance(**kwargs):
            return {
                "ok": True,
                "external_uuid": kwargs["approval_id"],
                "external_instance_code": "INST-001",
                "external_status": "PENDING",
                "external_url": "https://approval.feishu.cn/approval/INST-001",
            }

    class _FakeConversation:
        @staticmethod
        async def publish_native_approval_notice(_incident, _approval, _config):
            raise AssertionError("missing binding must not attempt a Feishu publish")

    class _FakeMessageDelivery:
        @staticmethod
        async def find_sent_delivery_for_approval(*, approval_id, target_type):
            assert approval_id == "ap-native-1"
            assert target_type == "approval_notice"
            return None

        @staticmethod
        async def upsert_delivery(**kwargs):
            deliveries.append(kwargs)
            return "delivery-notice-1"

        @staticmethod
        async def mark_sent(_delivery_id: str, _target_message_id: str):
            raise AssertionError("missing binding delivery must not be marked sent")

        @staticmethod
        async def mark_failed(delivery_id: str, error: str):
            failed_deliveries.append((delivery_id, error))

    monkeypatch.setattr(module, "approval_async", _FakeApprovalAsync)
    monkeypatch.setattr(module, "feishu_native_approval", _FakeNativeApproval, raising=False)
    monkeypatch.setattr(module, "feishu_conversation", _FakeConversation, raising=False)
    monkeypatch.setattr(module, "message_delivery", _FakeMessageDelivery, raising=False)
    monkeypatch.setattr(module, "incident_store", fake_store)

    result = await module._maybe_request_phase3_approval(
        "incident-1",
        {
            "alertname": "KubeDeploymentReplicasMismatch",
            "namespace": "default",
            "cluster": "prod-a",
        },
        {"next_best_actions": ["重启 deployment/nginx"]},
        config={"platforms": {"feishu": {"approval": {"enabled": True, "approval_code": "approval-code"}}}},
    )

    assert result["status"] == "external_pending"
    assert result["approval_message_id"] is None
    assert result["delivery_status"] == "pending_retry"
    assert result["delivery_id"] == "delivery-notice-1"
    assert deliveries[0]["target_type"] == "approval_notice"
    assert deliveries[0]["approval_id"] == "ap-native-1"
    assert deliveries[0]["incident_id"] == "incident-1"
    assert deliveries[0]["chat_id"] == ""
    assert deliveries[0]["thread_id"] is None
    assert failed_deliveries == [("delivery-notice-1", "incident 飞书 thread 绑定未就绪")]
    assert message_updates == []


@pytest.mark.asyncio
async def test_existing_native_notice_sent_delivery_backfills_without_duplicate_publish(monkeypatch, **_kwargs) -> None:
    """重复触发已有原生审批时，应复用 sent approval_notice 并补回写，不重复发布 thread 通知。"""
    module = _load_module()
    fake_store = FakeIncidentStore()
    publish_calls: list[tuple[dict, dict, dict]] = []
    message_updates: list[tuple[str, str]] = []
    legacy_card_calls: list[str] = []

    class _FakeApprovalAsync:
        @staticmethod
        async def find_pending_approval(_incident_id: str, _action_signature: str):
            return {
                "approval_id": "ap-native-1",
                "status": "external_pending",
                "approval_message_id": None,
                "external_provider": "feishu",
                "external_instance_code": "INST-001",
                "external_url": "https://approval.feishu.cn/approval/INST-001",
            }

        @staticmethod
        async def check_approval(approval_id: str):
            return {
                "approval_id": approval_id,
                "status": "external_pending",
                "approval_message_id": None,
                "external_provider": "feishu",
                "external_instance_code": "INST-001",
                "external_url": "https://approval.feishu.cn/approval/INST-001",
                "risk_level": "low",
                "command": "扩容 deployment/nginx 到 3 副本",
            }

        @staticmethod
        async def update_approval_message_id(approval_id: str, message_id: str):
            message_updates.append((approval_id, message_id))
            return {"ok": True, "approval_id": approval_id, "approval_message_id": message_id}

        @staticmethod
        async def publish_or_queue_approval_card(approval_id: str, config=None):
            del config
            legacy_card_calls.append(approval_id)
            return {
                "ok": True,
                "approval_id": approval_id,
                "approval_message_id": "om_legacy_card",
                "delivery_status": "sent",
            }

    class _FakeConversation:
        @staticmethod
        async def publish_native_approval_notice(incident, approval, config):
            publish_calls.append((incident, approval, config))
            return {"message_id": "om_duplicate_notice", "thread_id": "omt_thread"}

    class _FakeMessageDelivery:
        @staticmethod
        async def find_sent_delivery_for_approval(*, approval_id, target_type):
            assert approval_id == "ap-native-1"
            assert target_type == "approval_notice"
            return {
                "id": "delivery-existing",
                "target_message_id": "om_notice",
                "thread_id": "omt_thread",
            }

        @staticmethod
        async def upsert_delivery(**_kwargs):
            raise AssertionError("existing sent delivery should be reused")

        @staticmethod
        async def mark_sent(_delivery_id: str, _target_message_id: str):
            raise AssertionError("existing sent delivery should not be marked again")

        @staticmethod
        async def mark_failed(_delivery_id: str, _error: str):
            raise AssertionError("existing sent delivery should not fail")

    monkeypatch.setattr(module, "approval_async", _FakeApprovalAsync)
    monkeypatch.setattr(module, "feishu_conversation", _FakeConversation, raising=False)
    monkeypatch.setattr(module, "message_delivery", _FakeMessageDelivery, raising=False)
    monkeypatch.setattr(module, "incident_store", fake_store)

    result = await module._maybe_request_phase3_approval(
        "incident-1",
        {
            "alertname": "KubeDeploymentReplicasMismatch",
            "namespace": "default",
            "cluster": "prod-a",
            "feishu_binding": {
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "omt_thread",
            },
        },
        {"next_best_actions": ["扩容 deployment/nginx 到 3 副本"]},
        config={"platforms": {"feishu": {"approval": {"enabled": True}}}},
    )

    assert result["approval_message_id"] == "om_notice"
    assert result["delivery_status"] == "sent"
    assert result["delivery_id"] == "delivery-existing"
    assert message_updates == [("ap-native-1", "om_notice")]
    assert publish_calls == []
    assert legacy_card_calls == []


@pytest.mark.asyncio
async def test_webhook_native_approval_create_failure_does_not_execute(
    tmp_path: Path,
    monkeypatch,
    **_kwargs,
) -> None:
    """飞书原生审批创建失败时应记录失败状态，且不能直接执行修复动作。"""
    module = _load_module()
    store = IncidentStore(tmp_path / "incidents.db")
    incident_id = await store.create_incident(
        "KubeDeploymentReplicasMismatch",
        "default",
        "prod-a",
        "replica mismatch",
    )
    create_failures: list[dict] = []
    execution_calls: list[str] = []

    class _FakeApprovalAsync:
        @staticmethod
        async def find_pending_approval(_incident_id: str, _action_signature: str):
            return None

        @staticmethod
        async def request_external_approval(*args, **kwargs):
            return {"ok": True, "approval_id": "ap-native-1", "status": "external_pending"}

        @staticmethod
        async def record_external_approval_create_failed(approval_id, **fields):
            create_failures.append({"approval_id": approval_id, **fields})
            return {"ok": True, "approval_id": approval_id, "status": "approval_create_failed"}

        @staticmethod
        async def check_approval(approval_id: str):
            return {"approval_id": approval_id, "status": "approval_create_failed"}

        @staticmethod
        async def execute_approved(approval_id: str):
            execution_calls.append(approval_id)
            return {"ok": False, "message": "should not execute"}

    class _FakeNativeApproval:
        @staticmethod
        async def create_approval_instance(**_kwargs):
            return {"ok": False, "error_type": "http_timeout", "message": "timeout creating approval"}

    monkeypatch.setattr(module, "approval_async", _FakeApprovalAsync)
    monkeypatch.setattr(module, "feishu_native_approval", _FakeNativeApproval, raising=False)
    monkeypatch.setattr(module, "incident_store", store)

    result = await module._maybe_request_phase3_approval(
        incident_id,
        {
            "alertname": "KubeDeploymentReplicasMismatch",
            "namespace": "default",
            "cluster": "prod-a",
            "feishu_binding": {
                "chat_id": "oc_ops",
                "root_message_id": "om_root",
                "thread_id": "omt_thread",
            },
        },
        {"next_best_actions": ["重启 deployment/nginx"]},
        config={"platforms": {"feishu": {"approval": {"enabled": True, "approval_code": "approval-code"}}}},
    )

    timeline = await store.get_timeline(incident_id)

    assert result["status"] == "approval_create_failed"
    assert create_failures == [
        {
            "approval_id": "ap-native-1",
            "provider": "feishu",
            "error_type": "http_timeout",
            "message": "timeout creating approval",
        }
    ]
    assert timeline[-1]["event_type"] == "approval_create_failed"
    assert timeline[-1]["output_summary"] == "ap-native-1"
    assert execution_calls == []
    store.close()


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
    event_types = [item["event_type"] for item in timeline]
    assert event_types[0] == "alert_fired"
    assert event_types[-1] == "resolved"

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
