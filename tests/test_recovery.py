"""测试会话中断恢复 Hook。"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "hooks" / "recovery.py"
    spec = importlib.util.spec_from_file_location("test_recovery_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_approval_module(tmp_path: Path):
    """按文件路径加载审批模块，并隔离 SQLite 数据库。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "approval_async.py"
    module_name = "test_recovery_approval_async_module"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module._DB.close()
    module._DB = module.ApprovalDB(tmp_path / "approvals.db")
    return module


@pytest.mark.asyncio
async def test_recovery_classifies_pending_and_interrupted(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """应识别待审批和调查中事件。"""
    module = _load_module()

    incidents = [
        {"id": "inc-1", "status": "pending_approval"},
        {"id": "inc-2", "status": "investigating"},
    ]

    async def _list_active() -> list[dict]:
        return incidents

    async def _expire_stale(timeout_minutes: int = 30) -> dict:
        assert timeout_minutes == 30
        return {"ok": True, "expired": 2}

    async def _cleanup_expired() -> dict:
        return {"ok": True, "deleted": 1}

    monkeypatch.setattr(module.incident_store, "list_active", _list_active)
    monkeypatch.setattr(module.approval_async, "expire_stale", _expire_stale)
    monkeypatch.setattr(module.operation_lock, "cleanup_expired", _cleanup_expired)

    result = await module.handle("gateway:startup", {})

    assert [item["id"] for item in result["pending_approval"]] == ["inc-1"]
    assert [item["id"] for item in result["interrupted"]] == ["inc-2"]
    assert result["expired_approvals"] == 2
    assert result["expired_locks"] == 1


@pytest.mark.asyncio
async def test_recovery_marks_executing_without_lock_as_abnormal(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """执行中但无锁的事件应标记为 abnormal。"""
    module = _load_module()
    updated: list[tuple[str, str]] = []

    async def _list_active() -> list[dict]:
        return [{"id": "inc-3", "status": "executing"}]

    async def _is_locked(resource_key: str) -> bool:
        assert resource_key == "inc-3"
        return False

    async def _update_status(incident_id: str, status: str, resolved_at: float | None = None) -> None:
        del resolved_at
        updated.append((incident_id, status))

    async def _expire_stale(timeout_minutes: int = 30) -> dict:
        del timeout_minutes
        return {"ok": True, "expired": 0}

    async def _cleanup_expired() -> dict:
        return {"ok": True, "deleted": 0}

    monkeypatch.setattr(module.incident_store, "list_active", _list_active)
    monkeypatch.setattr(module.incident_store, "update_status", _update_status)
    monkeypatch.setattr(module.operation_lock, "is_locked", _is_locked)
    monkeypatch.setattr(module.approval_async, "expire_stale", _expire_stale)
    monkeypatch.setattr(module.operation_lock, "cleanup_expired", _cleanup_expired)

    result = await module.handle("gateway:startup", {})

    assert updated == [("inc-3", "abnormal")]
    assert [item["id"] for item in result["abnormal"]] == ["inc-3"]
    assert result["abnormal"][0]["status"] == "abnormal"


@pytest.mark.asyncio
async def test_recovery_cleanup_is_called(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """恢复流程应调用审批过期和锁清理逻辑。"""
    module = _load_module()
    calls = {"expire": 0, "cleanup": 0}

    async def _list_active() -> list[dict]:
        return []

    async def _expire_stale(timeout_minutes: int = 30) -> dict:
        assert timeout_minutes == 30
        calls["expire"] += 1
        return {"ok": True, "expired": 3}

    async def _cleanup_expired() -> dict:
        calls["cleanup"] += 1
        return {"ok": True, "deleted": 4}

    monkeypatch.setattr(module.incident_store, "list_active", _list_active)
    monkeypatch.setattr(module.approval_async, "expire_stale", _expire_stale)
    monkeypatch.setattr(module.operation_lock, "cleanup_expired", _cleanup_expired)

    result = await module.handle("gateway:startup", {})

    assert calls == {"expire": 1, "cleanup": 1}
    assert result["expired_approvals"] == 3
    assert result["expired_locks"] == 4


@pytest.mark.asyncio
async def test_recovery_retries_pending_approval_cards_before_expiring(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """恢复流程应先补发无 message_id 的 pending 审批，再做过期处理。"""
    module = _load_module()
    calls = {"recover": 0, "expire": 0, "cleanup": 0}

    async def _list_active() -> list[dict]:
        return []

    async def _recover_pending_approval_cards(timeout_seconds: int = 60) -> dict:
        assert timeout_seconds == 60
        calls["recover"] += 1
        return {
            "ok": True,
            "scanned": 2,
            "sent": 1,
            "pending_retry": 1,
            "failed": 0,
            "approvals": [{"approval_id": "ap-1"}],
            "results": [{"approval_id": "ap-1", "delivery_status": "sent"}],
        }

    async def _expire_stale(timeout_minutes: int = 30) -> dict:
        assert timeout_minutes == 30
        calls["expire"] += 1
        return {"ok": True, "expired": 0, "approvals": []}

    async def _cleanup_expired() -> dict:
        calls["cleanup"] += 1
        return {"ok": True, "deleted": 0}

    monkeypatch.setattr(module.incident_store, "list_active", _list_active)
    monkeypatch.setattr(module.approval_async, "recover_pending_approval_cards", _recover_pending_approval_cards)
    monkeypatch.setattr(module.approval_async, "expire_stale", _expire_stale)
    monkeypatch.setattr(module.operation_lock, "cleanup_expired", _cleanup_expired)

    result = await module.handle("gateway:startup", {})

    assert calls == {"recover": 1, "expire": 1, "cleanup": 1}
    assert result["recovered_approval_cards"] == 1
    assert result["pending_approval_cards"] == 1
    assert result["approval_card_recovery"]["scanned"] == 2


@pytest.mark.asyncio
async def test_recovery_records_expired_approval_timeline(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """恢复流程应把超时审批写回 incident timeline。"""
    module = _load_module()
    events: list[tuple] = []

    async def _list_active() -> list[dict]:
        return []

    async def _expire_stale(timeout_minutes: int = 30) -> dict:
        assert timeout_minutes == 30
        return {"ok": True, "expired": 1, "approvals": [{"approval_id": "ap-1", "incident_id": "inc-1"}]}

    async def _cleanup_expired() -> dict:
        return {"ok": True, "deleted": 0}

    async def _add_event(incident_id, event_type, tool_name, input_summary, output_summary, metadata=None):
        events.append((incident_id, event_type, tool_name, input_summary, output_summary, metadata))
        return 1

    monkeypatch.setattr(module.incident_store, "list_active", _list_active)
    monkeypatch.setattr(module.incident_store, "add_event", _add_event)
    monkeypatch.setattr(module.approval_async, "expire_stale", _expire_stale)
    monkeypatch.setattr(module.operation_lock, "cleanup_expired", _cleanup_expired)

    result = await module.handle("gateway:startup", {})

    assert result["expired_approvals"] == 1
    assert events == [("inc-1", "approval_expired", "recovery", "ap-1", "", None)]


@pytest.mark.asyncio
async def test_external_pending_polling_worker_syncs_approved_and_rejected(
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    """webhook 丢失时 polling worker 应补偿同步 approved/rejected。"""
    module = _load_module()
    pending_rows = [
        {
            "approval_id": "ap-approved",
            "external_uuid": "ap-approved",
            "external_instance_code": "INST-APPROVED",
            "external_status": "PENDING",
        },
        {
            "approval_id": "ap-rejected",
            "external_uuid": "ap-rejected",
            "external_instance_code": "INST-REJECTED",
            "external_status": "PENDING",
        },
    ]
    queried: list[str] = []
    synced: list[dict] = []

    async def _list_external_pending_approvals(*, limit: int, now=None):
        del now
        assert limit == 2
        return pending_rows

    async def _resolve_external_approval(**kwargs):
        synced.append(kwargs)
        status = "approved" if kwargs["external_status"] == "APPROVED" else "denied"
        return {"ok": True, "approval_id": kwargs["external_uuid"], "status": status}

    async def _query_approval_instance(*, instance_code: str, config: dict):
        assert config["platforms"]["feishu"]["approval"]["polling_batch_size"] == 2
        queried.append(instance_code)
        external_status = "APPROVED" if instance_code == "INST-APPROVED" else "REJECTED"
        return {"ok": True, "external_status": external_status, "external_instance_code": instance_code}

    monkeypatch.setattr(
        module,
        "approval_async",
        types.SimpleNamespace(
            list_external_pending_approvals=_list_external_pending_approvals,
            resolve_external_approval=_resolve_external_approval,
        ),
    )
    monkeypatch.setattr(
        module,
        "feishu_native_approval",
        types.SimpleNamespace(query_approval_instance=_query_approval_instance),
        raising=False,
    )

    worker = getattr(module, "poll_external_pending_approvals", None)
    assert callable(worker), "poll_external_pending_approvals(...) is required"
    result = await worker(
        config={"platforms": {"feishu": {"approval": {"polling_enabled": True, "polling_batch_size": 2}}}}
    )

    assert queried == ["INST-APPROVED", "INST-REJECTED"]
    assert [item["external_status"] for item in synced] == ["APPROVED", "REJECTED"]
    assert result["synced"] == 2
    assert result["approved"] == 1
    assert result["denied"] == 1


@pytest.mark.asyncio
async def test_external_pending_polling_treats_string_false_as_disabled(
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    """配置模板渲染出的字符串 false 不应误启动 polling。"""
    module = _load_module()
    called = False

    async def _list_external_pending_approvals(*, limit: int, now=None, stale_seconds: int = 0):
        del limit, now, stale_seconds
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(
        module,
        "approval_async",
        types.SimpleNamespace(list_external_pending_approvals=_list_external_pending_approvals),
    )

    result = await module.poll_external_pending_approvals(
        config={"platforms": {"feishu": {"approval": {"polling_enabled": "false"}}}}
    )

    assert result["enabled"] is False
    assert result["scanned"] == 0
    assert called is False


@pytest.mark.asyncio
async def test_external_pending_polling_skips_rows_until_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **_: object,
) -> None:
    """polling 应只查询超过 stale 限制的 external_pending 审批。"""
    module = _load_module()
    approval_module = _load_approval_module(tmp_path)

    stale_id = await approval_module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/stale",
        {"action_signature": "restart_deployment:prod-a:default:deployment/stale"},
        "default",
        "alert_webhook",
        "low",
        incident_id="inc-stale",
    )
    fresh_id = await approval_module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/fresh",
        {"action_signature": "restart_deployment:prod-a:default:deployment/fresh"},
        "default",
        "alert_webhook",
        "low",
        incident_id="inc-fresh",
    )
    await approval_module.record_external_approval_created(
        stale_id,
        provider="feishu",
        external_uuid=stale_id,
        external_instance_code="INST-STALE",
        external_status="PENDING",
    )
    await approval_module.record_external_approval_created(
        fresh_id,
        provider="feishu",
        external_uuid=fresh_id,
        external_instance_code="INST-FRESH",
        external_status="PENDING",
    )
    approval_module._DB._conn.execute(
        "UPDATE approvals SET external_updated_at = CASE id WHEN ? THEN ? WHEN ? THEN ? END WHERE id IN (?, ?)",
        (stale_id, 600.0, fresh_id, 970.0, stale_id, fresh_id),
    )

    queried: list[str] = []

    async def _query_approval_instance(*, instance_code: str, config: dict):
        queried.append(instance_code)
        return {"ok": True, "external_status": "PENDING", "external_instance_code": instance_code}

    monkeypatch.setattr(module, "approval_async", approval_module)
    monkeypatch.setattr(
        module,
        "feishu_native_approval",
        types.SimpleNamespace(query_approval_instance=_query_approval_instance),
        raising=False,
    )
    monkeypatch.setattr(module.time, "time", lambda: 1000.0)

    result = await module.poll_external_pending_approvals(
        config={
            "platforms": {
                "feishu": {
                    "approval": {
                        "polling_enabled": True,
                        "polling_batch_size": 10,
                        "polling_stale_seconds": 300,
                        "polling_interval_seconds": 60,
                    }
                }
            }
        }
    )

    assert queried == ["INST-STALE"]
    assert result["scanned"] == 1


@pytest.mark.asyncio
async def test_external_pending_polling_throttles_still_pending_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **_: object,
) -> None:
    """飞书仍返回 PENDING 时，应按 polling_interval_seconds 写入下次轮询节流。"""
    module = _load_module()
    approval_module = _load_approval_module(tmp_path)
    approval_id = await approval_module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/nginx",
        {"action_signature": "restart_deployment:prod-a:default:deployment/nginx"},
        "default",
        "alert_webhook",
        "low",
        incident_id="inc-1",
    )
    await approval_module.record_external_approval_created(
        approval_id,
        provider="feishu",
        external_uuid=approval_id,
        external_instance_code="INST-PENDING",
        external_status="PENDING",
    )

    queried: list[str] = []
    now = {"value": 1000.0}

    async def _query_approval_instance(*, instance_code: str, config: dict):
        queried.append(instance_code)
        return {"ok": True, "external_status": "PENDING", "external_instance_code": instance_code}

    monkeypatch.setattr(module, "approval_async", approval_module)
    monkeypatch.setattr(
        module,
        "feishu_native_approval",
        types.SimpleNamespace(query_approval_instance=_query_approval_instance),
        raising=False,
    )
    monkeypatch.setattr(module.time, "time", lambda: now["value"])
    config = {
        "platforms": {
            "feishu": {
                "approval": {
                    "polling_enabled": True,
                    "polling_batch_size": 5,
                    "polling_interval_seconds": 60,
                }
            }
        }
    }

    first = await module.poll_external_pending_approvals(config=config)
    now["value"] = 1059.0
    second = await module.poll_external_pending_approvals(config=config)
    now["value"] = 1061.0
    third = await module.poll_external_pending_approvals(config=config)

    assert queried == ["INST-PENDING", "INST-PENDING"]
    assert first["scanned"] == 1
    assert second["scanned"] == 0
    assert third["scanned"] == 1


@pytest.mark.asyncio
async def test_external_pending_polling_throttles_in_progress_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **_: object,
) -> None:
    """飞书查询返回 RUNNING/STARTED 等处理中状态时也应保留 external_pending 并节流。"""
    module = _load_module()
    approval_module = _load_approval_module(tmp_path)
    approval_id = await approval_module.request_approval(
        "k8s_write",
        "kubectl rollout restart deployment/nginx",
        {"action_signature": "restart_deployment:prod-a:default:deployment/nginx"},
        "default",
        "alert_webhook",
        "low",
        incident_id="inc-1",
    )
    await approval_module.record_external_approval_created(
        approval_id,
        provider="feishu",
        external_uuid=approval_id,
        external_instance_code="INST-RUNNING",
        external_status="PENDING",
    )

    async def _query_approval_instance(*, instance_code: str, config: dict):
        assert instance_code == "INST-RUNNING"
        return {"ok": True, "external_status": "RUNNING", "external_instance_code": instance_code}

    monkeypatch.setattr(module, "approval_async", approval_module)
    monkeypatch.setattr(
        module,
        "feishu_native_approval",
        types.SimpleNamespace(query_approval_instance=_query_approval_instance),
        raising=False,
    )
    monkeypatch.setattr(module.time, "time", lambda: 1000.0)

    result = await module.poll_external_pending_approvals(
        config={
            "platforms": {
                "feishu": {
                    "approval": {
                        "polling_enabled": "true",
                        "polling_batch_size": 5,
                        "polling_interval_seconds": 45,
                    }
                }
            }
        }
    )
    checked = await approval_module.check_approval(approval_id)

    assert result["scanned"] == 1
    assert result["synced"] == 0
    assert checked["status"] == "external_pending"
    assert checked["external_status"] == "RUNNING"
    assert checked["external_poll_attempts"] == 1
    assert checked["external_next_poll_at"] == 1045.0


@pytest.mark.asyncio
async def test_recovery_runs_external_pending_polling_on_startup(
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    """startup recovery 应触发 external_pending polling 补偿。"""
    module = _load_module()
    calls: list[dict] = []

    async def _list_active() -> list[dict]:
        return []

    async def _expire_stale(timeout_minutes: int = 30) -> dict:
        del timeout_minutes
        return {"ok": True, "expired": 0, "approvals": []}

    async def _cleanup_expired() -> dict:
        return {"ok": True, "deleted": 0}

    async def _poll_external_pending_approvals(*, config=None):
        calls.append(config or {})
        return {"ok": True, "scanned": 1, "synced": 1, "approved": 1, "denied": 0, "failed": 0}

    monkeypatch.setattr(module.incident_store, "list_active", _list_active)
    monkeypatch.setattr(module.approval_async, "expire_stale", _expire_stale)
    monkeypatch.setattr(module.operation_lock, "cleanup_expired", _cleanup_expired)
    monkeypatch.setattr(
        module,
        "poll_external_pending_approvals",
        _poll_external_pending_approvals,
        raising=False,
    )

    result = await module.handle("gateway:startup", {})

    assert calls == [{}]
    assert result["external_approval_polling"]["synced"] == 1
