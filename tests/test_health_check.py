"""测试自监控健康检查 Hook。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "hooks" / "health_check.py"
    spec = importlib.util.spec_from_file_location("test_health_check_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_gateway_startup_returns_health_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()
    incident_db = tmp_path / "incidents.db"
    audit_db = tmp_path / "audit.db"
    incident_db.write_text("x", encoding="utf-8")
    audit_db.write_text("y", encoding="utf-8")
    monkeypatch.setattr(module.incident_store._STORE, "db_path", incident_db)
    monkeypatch.setattr(module.audit_log._DB, "db_path", audit_db)
    result = await module.handle("gateway:startup", {})
    assert result["healthy"] is True
    assert result["checks"]["incident_store_db"] is True
    assert result["checks"]["audit_log_db"] is True


@pytest.mark.asyncio
async def test_non_startup_event_returns_handled_false(**_: object) -> None:
    module = _load_module()
    result = await module.handle("agent:step", {})
    assert result == {"handled": False}


@pytest.mark.asyncio
async def test_missing_db_files_mark_unhealthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()
    incident_db = tmp_path / "missing_incidents.db"
    audit_db = tmp_path / "missing_audit.db"
    monkeypatch.setattr(module.incident_store._STORE, "db_path", incident_db)
    monkeypatch.setattr(module.audit_log._DB, "db_path", audit_db)
    result = await module.check_health()
    assert result["healthy"] is False
    assert result["checks"]["incident_store_db"] is False
    assert result["checks"]["audit_log_db"] is False
