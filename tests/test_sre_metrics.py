"""测试 SRE 效果度量模块。"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "sre_metrics.py"
    spec = importlib.util.spec_from_file_location("test_sre_metrics_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_compute_metrics_returns_none_on_empty_data(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """无数据时核心比率字段应返回 None。"""
    module = _load_module()

    async def _list_active() -> list[dict]:
        return []

    async def _query_audit(**kwargs) -> list[dict]:
        del kwargs
        return []

    def _read_approvals() -> tuple[int, int]:
        return (0, 0)

    monkeypatch.setattr(module.incident_store, "list_active", _list_active)
    monkeypatch.setattr(module.audit_log, "query_audit", _query_audit)
    monkeypatch.setattr(module.asyncio, "to_thread", lambda fn, *args, **kwargs: _wrap_sync(fn, *args, **kwargs) if fn == _read_approvals else _wrap_sync(fn, *args, **kwargs))
    monkeypatch.setattr(module.approval_async, "_DB", type("DB", (), {"_lock": DummyLock(), "_conn": DummyConn((0, 0))})())

    result = await module.compute_metrics(days=7)

    assert result["mttd_seconds"] is None
    assert result["adoption_rate"] is None
    assert result["rollback_rate"] is None


class DummyLock:
    """测试用空锁。"""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class DummyRow(dict):
    """测试用行对象。"""

    def __getitem__(self, item):
        return super().get(item)


class DummyCursor:
    """测试用游标对象。"""

    def __init__(self, approved: int, denied: int) -> None:
        self._row = DummyRow({"approved_count": approved, "denied_count": denied})

    def fetchone(self):
        return self._row


class DummyConn:
    """测试用连接对象。"""

    def __init__(self, counts: tuple[int, int]) -> None:
        self._counts = counts

    def execute(self, sql: str, params: tuple):
        del sql, params
        return DummyCursor(*self._counts)


async def _wrap_sync(fn, *args, **kwargs):
    """将同步函数包装成异步返回。"""
    return fn(*args, **kwargs)


@pytest.mark.asyncio
async def test_mttd_and_adoption_rate_are_computed(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """应正确计算 MTTD 和采纳率。"""
    module = _load_module()
    now = time.time()

    async def _list_active() -> list[dict]:
        return [{"id": "inc-1", "created_at": now - 60}]

    async def _get_timeline(incident_id: str) -> list[dict]:
        assert incident_id == "inc-1"
        return [
            {"event_type": "alert_fired", "timestamp": 100.0},
            {"event_type": "triage_start", "timestamp": 130.0},
        ]

    async def _query_audit(**kwargs) -> list[dict]:
        del kwargs
        return [{"rollback": 0}, {"rollback": 1}]

    monkeypatch.setattr(module.incident_store, "list_active", _list_active)
    monkeypatch.setattr(module.incident_store, "get_timeline", _get_timeline)
    monkeypatch.setattr(module.audit_log, "query_audit", _query_audit)
    monkeypatch.setattr(module.asyncio, "to_thread", _wrap_sync)
    monkeypatch.setattr(module.approval_async, "_DB", type("DB", (), {"_lock": DummyLock(), "_conn": DummyConn((3, 1))})())

    result = await module.compute_metrics(days=7)

    assert result["mttd_seconds"] == 30.0
    assert result["adoption_rate"] == 0.75
    assert result["rollback_rate"] == 0.5


@pytest.mark.asyncio
async def test_repeat_incident_baseline_is_reported(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """应输出重复 incident 的基线计数与比率。"""
    module = _load_module()
    now = time.time()

    async def _list_active() -> list[dict]:
        return [
            {"id": "inc-1", "created_at": now - 60, "reopen_count": 1},
            {"id": "inc-2", "created_at": now - 120, "reopen_count": 0},
            {"id": "inc-3", "created_at": now - 180, "reopen_count": 2},
        ]

    async def _get_timeline(incident_id: str) -> list[dict]:
        return [
            {"event_type": "alert_fired", "timestamp": 100.0},
            {"event_type": "triage_start", "timestamp": 130.0},
        ]

    async def _query_audit(**kwargs) -> list[dict]:
        del kwargs
        return []

    monkeypatch.setattr(module.incident_store, "list_active", _list_active)
    monkeypatch.setattr(module.incident_store, "get_timeline", _get_timeline)
    monkeypatch.setattr(module.audit_log, "query_audit", _query_audit)
    monkeypatch.setattr(module.asyncio, "to_thread", _wrap_sync)
    monkeypatch.setattr(module.approval_async, "_DB", type("DB", (), {"_lock": DummyLock(), "_conn": DummyConn((0, 0))})())

    result = await module.compute_metrics(days=7)

    assert result["repeat_incident_count"] == 2
    assert result["repeat_incident_rate"] == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_generate_weekly_summary_contains_key_fields(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """周报文本应包含关键字段。"""
    module = _load_module()

    async def _compute_metrics(days: int = 7) -> dict:
        assert days == 7
        return {
            "period_days": 7,
            "mttd_seconds": 45.0,
            "adoption_rate": 0.8,
            "rollback_rate": 0.1,
            "total_incidents": 5,
            "total_approvals": 10,
        }

    monkeypatch.setattr(module, "compute_metrics", _compute_metrics)

    summary = await module.generate_weekly_summary()

    assert "📊 SRE Agent 周报（最近 7 天）" in summary
    assert "处理事件：5 起" in summary
    assert "平均诊断时间（MTTD）：45.0 秒" in summary
    assert "方案采纳率：80.0%" in summary
