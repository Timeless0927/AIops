"""测试 SRE 数据结构化提取。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_module():
    """按文件路径加载提取模块。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "sre_extractor.py"
    spec = importlib.util.spec_from_file_location("test_sre_extractor_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _make_lines(count: int, marker: str) -> str:
    """生成多行测试输入。"""
    return "\n".join(f"line-{idx} {marker}" for idx in range(count))


@pytest.mark.asyncio
async def test_short_input_skips_extraction() -> None:
    """低于阈值的输入应直接返回原文。"""
    module = _load_module()
    output = _make_lines(100, "INFO")

    result = await module.extract_if_needed(output, "log")

    assert result["extracted"] is False
    assert result["data"] == output
    assert result["line_count"] == 100


@pytest.mark.asyncio
async def test_long_error_log_triggers_regex_extraction() -> None:
    """超过阈值且包含错误日志时应通过正则提取返回结果。"""
    module = _load_module()
    output = _make_lines(300, "ERROR connection timeout")

    result = await module.extract_if_needed(output, "log")

    assert result["extracted"] is True
    assert result["line_count"] == 300
    assert any(item["class"] == "error" for item in result["data"])


@pytest.mark.asyncio
async def test_long_k8s_output_with_crashloop_triggers_regex_extraction() -> None:
    """超过阈值且包含 CrashLoopBackOff 的 K8s 输出应通过正则提取返回结果。"""
    module = _load_module()
    normal_lines = "\n".join(f"pod-{i}   1/1   Running   0   1h" for i in range(250))
    crash_line = "nginx-abc   0/1   CrashLoopBackOff   15   2h"
    output = normal_lines + "\n" + crash_line

    result = await module.extract_if_needed(output, "k8s")

    assert result["extracted"] is True
    assert any(item["class"] == "unhealthy_pod" for item in result["data"])

