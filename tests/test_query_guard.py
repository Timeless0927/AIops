"""测试查询安全护栏。"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest


def _load_module():
    """按文件路径加载查询护栏模块。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "query_guard.py"
    spec = importlib.util.spec_from_file_location("test_query_guard_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_query_guard_import_does_not_require_pyyaml() -> None:
    """Connector images should import query_guard even without optional PyYAML."""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "query_guard.py"
    spec = importlib.util.spec_from_file_location("test_query_guard_without_yaml", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None

    real_import = __import__

    def _import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: ANN001
        if name == "yaml":
            raise ModuleNotFoundError("No module named 'yaml'")
        return real_import(name, globals, locals, fromlist, level)

    sys.modules.pop("test_query_guard_without_yaml", None)
    with patch("builtins.__import__", side_effect=_import):
        spec.loader.exec_module(module)

    assert hasattr(module, "validate_prometheus_query")


def test_root_requirements_declares_pyyaml() -> None:
    requirements = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")

    assert "PyYAML" in requirements


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
