"""测试查询安全护栏。"""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载查询护栏模块。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "query_guard.py"
    spec = importlib.util.spec_from_file_location("test_query_guard_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_prometheus_query_gets_default_one_hour_window() -> None:
    """未传入时间窗时应自动补最近 1 小时。"""
    module = _load_module()

    result = await module.validate_prometheus_query("up", None, None)

    assert result["allowed"] is True
    start = datetime.fromisoformat(result["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(result["end"].replace("Z", "+00:00"))
    delta_seconds = int((end - start).total_seconds())
    assert 3590 <= delta_seconds <= 3610


@pytest.mark.asyncio
async def test_loki_full_match_query_is_rejected() -> None:
    """全量匹配的 Loki 查询应被拒绝。"""
    module = _load_module()

    result = await module.validate_loki_query('{job=~".+"}', None, None, None)

    assert result["allowed"] is False
    assert "全量匹配" in result["message"]


@pytest.mark.asyncio
async def test_loki_query_gets_default_limit() -> None:
    """普通 Loki 查询应放行并补默认 limit。"""
    module = _load_module()

    result = await module.validate_loki_query('{app="api"}', None, None, None)

    assert result["allowed"] is True
    assert result["limit"] == 200

