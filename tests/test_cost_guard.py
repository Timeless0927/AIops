"""测试成本监控守卫。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module(tmp_path: Path):
    """按文件路径加载模块，并重定向数据库。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "cost_guard.py"
    spec = importlib.util.spec_from_file_location("test_cost_guard_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    module._DB.close()
    module._DB = module.CostGuardDB(tmp_path / "cost_tracking.db")
    module._DB._config = {
        "daily_budget": 1.0,
        "alert_threshold": 0.8,
        "per_incident_budget": 0.5,
        "exceeded_action": "degrade",
    }
    return module


@pytest.mark.asyncio
async def test_record_cost_writes_successfully(tmp_path: Path, **_: object) -> None:
    """应能成功写入成本记录。"""
    module = _load_module(tmp_path)

    record_id = await module.record_cost("claude", 100, 50, 0.1, "inc-1", "sess-1")

    assert record_id > 0


@pytest.mark.asyncio
async def test_get_daily_total_accumulates_cost(tmp_path: Path, **_: object) -> None:
    """当天成本应正确累计。"""
    module = _load_module(tmp_path)
    await module.record_cost("claude", 100, 50, 0.1)
    await module.record_cost("claude", 200, 80, 0.2)

    result = await module.get_daily_total()

    assert result["total_cost"] == pytest.approx(0.3)
    assert result["record_count"] == 2


@pytest.mark.asyncio
async def test_get_incident_total_accumulates_by_incident(tmp_path: Path, **_: object) -> None:
    """应按 incident 统计成本。"""
    module = _load_module(tmp_path)
    await module.record_cost("claude", 100, 50, 0.1, incident_id="inc-1")
    await module.record_cost("claude", 100, 50, 0.2, incident_id="inc-1")
    await module.record_cost("claude", 100, 50, 0.3, incident_id="inc-2")

    result = await module.get_incident_total("inc-1")

    assert result["incident_id"] == "inc-1"
    assert result["total_cost"] == pytest.approx(0.3)
    assert result["record_count"] == 2


@pytest.mark.asyncio
async def test_check_budget_within_limit(tmp_path: Path, **_: object) -> None:
    """未超预算时应返回 within_budget=True。"""
    module = _load_module(tmp_path)
    await module.record_cost("claude", 100, 50, 0.1, incident_id="inc-1")

    result = await module.check_budget("inc-1")

    assert result["within_budget"] is True
    assert result["action"] is None


@pytest.mark.asyncio
async def test_check_budget_exceeded_returns_degrade(tmp_path: Path, **_: object) -> None:
    """超预算时应返回 degrade 动作。"""
    module = _load_module(tmp_path)
    await module.record_cost("claude", 100, 50, 0.8, incident_id="inc-1")

    result = await module.check_budget("inc-1")

    assert result["within_budget"] is False
    assert result["action"] == "degrade"


@pytest.mark.asyncio
async def test_get_daily_total_empty_returns_zero(tmp_path: Path, **_: object) -> None:
    """无数据时当天成本应为 0。"""
    module = _load_module(tmp_path)

    result = await module.get_daily_total()

    assert result == {"total_cost": 0.0, "record_count": 0}
