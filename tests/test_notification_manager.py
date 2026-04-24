"""测试通知防疲劳管理器。"""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "notification_manager.py"
    spec = importlib.util.spec_from_file_location("test_notification_manager_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _build_manager(module, config: dict):
    """构造可控配置的管理器实例。"""
    manager = module.NotificationManager()
    manager._config = config
    return manager


@pytest.mark.asyncio
async def test_severity_below_threshold_goes_digest(**_: object) -> None:
    module = _load_module()
    manager = _build_manager(module, {"severity_filter": "warning", "max_per_hour": 10})
    result = await manager.should_notify({"alertname": "A", "severity": "info"})
    assert result == {"notify": False, "reason": "severity_below_threshold", "queued_for_digest": True}


@pytest.mark.asyncio
async def test_severity_reaches_threshold_allowed(**_: object) -> None:
    module = _load_module()
    manager = _build_manager(module, {"severity_filter": "warning", "max_per_hour": 10})
    result = await manager.should_notify({"alertname": "A", "severity": "warning"})
    assert result == {"notify": True, "reason": "allowed"}


@pytest.mark.asyncio
async def test_critical_bypasses_quiet_hours(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()
    manager = _build_manager(module, {"severity_filter": "info", "max_per_hour": 10, "quiet_hours": {"start": "23:00", "end": "07:00", "except": "critical"}})
    monkeypatch.setattr(manager, "_now", lambda: datetime(2026, 4, 22, 23, 30, 0))
    result = await manager.should_notify({"alertname": "A", "severity": "critical"})
    assert result == {"notify": True, "reason": "allowed"}


@pytest.mark.asyncio
async def test_warning_blocked_during_quiet_hours(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()
    manager = _build_manager(module, {"severity_filter": "info", "max_per_hour": 10, "quiet_hours": {"start": "23:00", "end": "07:00", "except": "critical"}})
    monkeypatch.setattr(manager, "_now", lambda: datetime(2026, 4, 22, 23, 30, 0))
    result = await manager.should_notify({"alertname": "A", "severity": "warning"})
    assert result == {"notify": False, "reason": "quiet_hours", "queued_for_digest": True}


@pytest.mark.asyncio
async def test_hourly_limit_reached(**_: object) -> None:
    module = _load_module()
    manager = _build_manager(module, {"severity_filter": "info", "max_per_hour": 1})
    first = await manager.should_notify({"alertname": "A", "severity": "warning"})
    second = await manager.should_notify({"alertname": "B", "severity": "warning"})
    assert first == {"notify": True, "reason": "allowed"}
    assert second == {"notify": False, "reason": "hourly_limit_reached", "queued_for_digest": True}


@pytest.mark.asyncio
async def test_get_digest_returns_and_clears_queue(**_: object) -> None:
    module = _load_module()
    manager = _build_manager(module, {"severity_filter": "warning", "max_per_hour": 10})
    await manager.should_notify({"alertname": "A", "severity": "info"})
    digest = await manager.get_digest()
    digest_again = await manager.get_digest()
    assert len(digest) == 1
    assert digest_again == []


@pytest.mark.asyncio
async def test_hour_switch_resets_counter(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    module = _load_module()
    manager = _build_manager(module, {"severity_filter": "info", "max_per_hour": 1})
    manager._hour_key = "2026042209"
    manager._hourly_counter = 1
    monkeypatch.setattr(manager, "_current_hour_key", lambda: "2026042210")
    result = await manager.should_notify({"alertname": "A", "severity": "warning"})
    assert result == {"notify": True, "reason": "allowed"}
    assert manager._hourly_counter == 1

