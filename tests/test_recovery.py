"""测试会话中断恢复 Hook。"""

from __future__ import annotations

import importlib.util
import sys
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
