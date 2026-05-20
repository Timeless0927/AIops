"""测试事件时间线持久化模块。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


def _load_module(tmp_path: Path):
    """按文件路径加载模块，并重定向数据库路径。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "incident_store.py"
    spec = importlib.util.spec_from_file_location("test_incident_store_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)

    store = module.IncidentStore(tmp_path / "data" / "incidents.db")
    old_store = module._STORE
    old_store.close()
    module._STORE = store
    return module, store


@pytest.mark.asyncio
async def test_incident_store_crud_and_list_active(tmp_path: Path, **_: object) -> None:
    """验证事件创建、追加、查询和状态更新。"""
    module, store = _load_module(tmp_path)

    incident_id = await module.create_incident("PodCrash", "default", "prod", "pod 连续重启")
    event_id = await module.add_event(
        incident_id,
        "alert_fired",
        "k8s_read",
        "读取 pod 状态",
        "发现 CrashLoopBackOff",
        {"severity": "critical"},
    )
    timeline = await module.get_timeline(incident_id)
    active = await module.list_active()

    assert incident_id
    assert event_id > 0
    assert len(timeline) == 1
    assert timeline[0]["metadata"]["severity"] == "critical"
    assert [item["id"] for item in active] == [incident_id]

    await module.update_status(incident_id, "resolved", resolved_at=123.0)
    active_after = await module.list_active()
    assert active_after == []

    store.close()


@pytest.mark.asyncio
async def test_incident_store_concurrent_add_event(tmp_path: Path, **_: object) -> None:
    """验证两条线程同时写入事件记录不会互相覆盖。"""
    module, store = _load_module(tmp_path)
    incident_id = await module.create_incident("HighMemory", "prod", "cluster-a", "内存升高")

    async def _write(index: int) -> int:
        return await module.add_event(
            incident_id,
            "investigate_start",
            f"tool-{index}",
            f"input-{index}",
            f"output-{index}",
            {"index": index},
        )

    results = await asyncio.gather(_write(1), _write(2))
    timeline = await module.get_timeline(incident_id)

    assert len(results) == 2
    assert len(timeline) == 2
    assert {item["metadata"]["index"] for item in timeline} == {1, 2}

    store.close()


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


def test_incident_store_uses_wal_mode(tmp_path: Path) -> None:
    """验证数据库启用了 WAL 模式。"""
    module, store = _load_module(tmp_path)
    conn = sqlite3.connect(str(store.db_path))
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()

    assert str(journal_mode).lower() == "wal"
    store.close()


@pytest.mark.asyncio
async def test_incident_evidence_and_analysis_round_trip(tmp_path: Path, **_: object) -> None:
    """incident 应能持久化结构化 evidence 与 analysis。"""
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
        symptoms=["PodCrashLooping firing in default/prod-a"],
        likely_scope="workload",
        suspected_root_causes=[{"summary": "应用容器异常退出", "confidence": 0.4}],
        supporting_evidence=[{"summary": "告警持续 firing", "source_type": "alert_window"}],
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
    assert analysis["suspected_root_causes"][0]["summary"] == "应用容器异常退出"

    store.close()


@pytest.mark.asyncio
async def test_incident_case_profile_round_trip(tmp_path: Path, **_: object) -> None:
    module, store = _load_module(tmp_path)
    incident_id = await module.create_incident("PodCrashLooping", "default", "prod-a", "pod 重启")

    await module.upsert_case_profile(
        incident_id,
        incident_signature="PodCrashLooping|default|workload|resolved",
        symptom_fingerprint="restart+unready+backoff",
        final_scope="workload",
        final_root_cause="应用日志显示运行时异常",
        effective_actions=["检查相关 Pod 最近错误日志与超时信息"],
        invalid_actions=["仅观察告警不处理"],
        metric_delta_summary={"restart_max": "7"},
        change_clue_summary="最近 1 条变更线索",
        resolution_seconds=600.0,
        similar_incident_ids=["inc-older-1"],
    )

    profile = await module.get_case_profile(incident_id)

    assert profile is not None
    assert profile["incident_signature"] == "PodCrashLooping|default|workload|resolved"
    assert profile["final_root_cause"] == "应用日志显示运行时异常"
    assert profile["similar_incident_ids"] == ["inc-older-1"]

    store.close()


@pytest.mark.asyncio
async def test_find_similar_case_profiles_by_signature(tmp_path: Path, **_: object) -> None:
    module, store = _load_module(tmp_path)
    inc_a = await module.create_incident("PodCrashLooping", "default", "prod-a", "older")
    inc_b = await module.create_incident("PodCrashLooping", "default", "prod-a", "newer")

    await module.upsert_case_profile(
        inc_a,
        incident_signature="PodCrashLooping|default|workload|resolved",
        final_scope="workload",
        final_root_cause="资源压力可能导致工作负载异常",
        effective_actions=["检查 Pod CPU/内存指标与资源配置"],
        updated_at=100.0,
    )
    await module.upsert_case_profile(
        inc_b,
        incident_signature="PodCrashLooping|default|workload|resolved",
        final_scope="workload",
        final_root_cause="应用日志显示运行时异常",
        effective_actions=["检查相关 Pod 最近错误日志与超时信息"],
        updated_at=200.0,
    )

    similar = await module.find_similar_case_profiles(
        "PodCrashLooping|default|workload|resolved",
        exclude_incident_id=inc_b,
        limit=3,
    )

    assert len(similar) == 1
    assert similar[0]["incident_id"] == inc_a
    assert similar[0]["final_root_cause"] == "资源压力可能导致工作负载异常"

    store.close()


@pytest.mark.asyncio
async def test_invalid_event_type_is_rejected(tmp_path: Path, **_: object) -> None:
    """非法事件类型应直接拒绝。"""
    module, store = _load_module(tmp_path)
    incident_id = await module.create_incident("BadEvent", "default", "prod", "测试非法事件")

    with pytest.raises(ValueError, match="不支持的 event_type"):
        await module.add_event(incident_id, "bad_type", "tool", "in", "out")

    store.close()


@pytest.mark.asyncio
async def test_mark_rollback_required_updates_status_and_timeline(tmp_path: Path, **_: object) -> None:
    """健康检查失败应能把 incident 标记为 rollback_required 并写入时间线。"""
    module, store = _load_module(tmp_path)
    incident_id = await module.create_incident("DeploymentUnavailable", "default", "prod-a", "nginx unavailable")

    await module.update_status(incident_id, "triaging")
    await module.update_status(incident_id, "investigating")
    await module.update_status(incident_id, "pending_approval")
    await module.update_status(incident_id, "executing")
    await module.update_status(incident_id, "verifying")
    event_id = await module.mark_rollback_required(
        incident_id,
        reason_code="deployment_unavailable",
        summary="1/3 replicas available",
    )
    incident = await module.get_incident(incident_id)
    timeline = await module.get_timeline(incident_id)

    assert event_id > 0
    assert incident["status"] == "rollback_required"
    assert timeline[-1]["event_type"] == "rollback_required"
    assert timeline[-1]["metadata"]["reason_code"] == "deployment_unavailable"
    assert timeline[-1]["metadata"]["previous_status"] == "verifying"

    store.close()


@pytest.mark.asyncio
async def test_mark_rollback_required_allows_pending_approval_execution_failure(
    tmp_path: Path, **_: object
) -> None:
    """执行链路未及时推进状态时，健康失败仍应 fail closed 到 rollback_required。"""
    module, store = _load_module(tmp_path)
    incident_id = await module.create_incident("DeploymentUnavailable", "default", "prod-a", "nginx unavailable")

    await module.update_status(incident_id, "triaging")
    await module.update_status(incident_id, "investigating")
    await module.update_status(incident_id, "pending_approval")
    event_id = await module.mark_rollback_required(
        incident_id,
        reason_code="execution_health_failed",
        summary="执行后健康检查失败",
    )
    incident = await module.get_incident(incident_id)
    timeline = await module.get_timeline(incident_id)

    assert event_id > 0
    assert incident["status"] == "rollback_required"
    assert timeline[-1]["event_type"] == "rollback_required"
    assert timeline[-1]["metadata"]["previous_status"] == "pending_approval"

    store.close()


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


@pytest.mark.asyncio
async def test_update_feishu_binding_and_find_by_thread(tmp_path: Path, **_: object) -> None:
    """应能回写飞书会话绑定并通过同一 thread 反查 incident。"""
    module, store = _load_module(tmp_path)

    incident_id = await module.create_incident(
        "PodCrashLooping",
        "default",
        "prod-a",
        "pod 重启次数持续增加",
        platform="feishu",
    )

    await module.update_feishu_binding(
        incident_id,
        chat_id="oc_ops",
        root_message_id="om_root",
        thread_id="omt_thread",
        status_card_message_id="om_card",
    )

    incident = await module.get_incident(incident_id)
    located = await module.find_by_feishu_context(
        chat_id="oc_ops",
        thread_id="omt_thread",
        message_id="om_reply",
    )

    assert incident["chat_id"] == "oc_ops"
    assert incident["root_message_id"] == "om_root"
    assert incident["thread_id"] == "omt_thread"
    assert incident["status_card_message_id"] == "om_card"
    assert located is not None
    assert located["id"] == incident_id

    store.close()


@pytest.mark.asyncio
async def test_tool_incident_timeline_returns_readable_summary(tmp_path: Path, **_: object) -> None:
    """timeline 工具应返回可读摘要，避免把历史排查误读成当前结论。"""
    module, store = _load_module(tmp_path)
    incident_id = await module.create_incident(
        "FeishuGatewayBindingTest",
        "default",
        "prod-a",
        "测试飞书主群绑定",
        platform="feishu",
        chat_id="oc_ops",
        thread_id="om_thread",
    )
    await module.add_event(
        incident_id,
        "alert_fired",
        "alert_webhook",
        "FeishuGatewayBindingTest",
        "测试飞书主群绑定",
        {},
    )
    await module.add_event(
        incident_id,
        "investigate_end",
        "pytest+terminal+read_file",
        "继续排查",
        "历史排查记录：曾发现配置优先级问题，后续已验证主群绑定成功",
        {},
    )

    result_json = await module._tool_incident_timeline({"incident_id": incident_id})
    result = json.loads(result_json)

    assert result["incident"]["id"] == incident_id
    assert result["incident"]["status"] == "new"
    assert result["reply_guidance"] == "请优先说明这是历史时间线，并结合当前 incident 状态作答。"
    assert "当前状态: new" in result["readable_summary"]
    assert "飞书会话: chat_id=oc_ops, thread_id=om_thread" in result["readable_summary"]
    assert "历史记录" in result["readable_summary"]
    assert "1970" not in result["readable_summary"]
    assert len(result["events"]) == 2

    store.close()


@pytest.mark.asyncio
async def test_progress_event_types_are_accepted(tmp_path: Path, **_: object) -> None:
    """agent 记录阶段进展时应接受 progress 类事件。"""
    module, store = _load_module(tmp_path)
    incident_id = await module.create_incident("PodCrash", "default", "prod", "pod crash")

    event_id = await module.add_event(
        incident_id,
        "investigate_progress",
        "k8s_read",
        "查看 pod 状态",
        "发现 CrashLoopBackOff，继续查日志",
        {},
    )

    timeline = await module.get_timeline(incident_id)

    assert event_id > 0
    assert timeline[0]["event_type"] == "investigate_progress"

    store.close()


@pytest.mark.asyncio
async def test_approval_event_types_are_accepted(tmp_path: Path, **_: object) -> None:
    """Phase 3 审批事件应能进入 incident timeline。"""
    module, store = _load_module(tmp_path)
    incident_id = await module.create_incident("PodCrash", "default", "prod", "pod crash")

    for event_type in (
        "approval_requested",
        "approval_approved",
        "approval_denied",
        "approval_expired",
        "approval_create_failed",
        "approval_skipped",
        "approval_unauthorized",
    ):
        event_id = await module.add_event(incident_id, event_type, "approval", "ap-1", "ok", {})
        assert event_id > 0

    timeline = await module.get_timeline(incident_id)

    assert [event["event_type"] for event in timeline] == [
        "approval_requested",
        "approval_approved",
        "approval_denied",
        "approval_expired",
        "approval_create_failed",
        "approval_skipped",
        "approval_unauthorized",
    ]

    store.close()


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

    assert found is not None
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
