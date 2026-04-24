"""测试事件时间线持久化模块。"""

from __future__ import annotations

import asyncio
import importlib.util
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
async def test_invalid_event_type_is_rejected(tmp_path: Path, **_: object) -> None:
    """非法事件类型应直接拒绝。"""
    module, store = _load_module(tmp_path)
    incident_id = await module.create_incident("BadEvent", "default", "prod", "测试非法事件")

    with pytest.raises(ValueError, match="不支持的 event_type"):
        await module.add_event(incident_id, "bad_type", "tool", "in", "out")

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
