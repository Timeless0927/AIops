"""测试 Kubernetes 命令安全护栏。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块，避免依赖包安装状态。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "k8s_guard.py"
    spec = importlib.util.spec_from_file_location("test_k8s_guard_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_classify_command_levels() -> None:
    """验证常见 kubectl 命令会被分到正确级别。"""
    module = _load_module()

    assert (await module.classify_command("kubectl get pods"))["level"] == "read"
    assert (await module.classify_command("kubectl scale deployment/app --replicas=3"))["level"] == "write"
    assert (await module.classify_command("kubectl delete pod xxx"))["level"] == "write_dangerous"
    assert (await module.classify_command("kubectl exec -it pod -- bash"))["level"] == "exec"
    assert (await module.classify_command("kubectl proxy"))["level"] == "forbidden"


@pytest.mark.asyncio
async def test_guard_check_read_tool_allows_read_command() -> None:
    """只读工具应放行只读命令。"""
    module = _load_module()

    result = await module.guard_check("kubectl get pods", "read")

    assert result["allowed"] is True
    assert result["classification"]["level"] == "read"


@pytest.mark.asyncio
async def test_guard_check_read_tool_rejects_delete_command() -> None:
    """只读工具遇到删除命令时应拒绝并提示改用写工具。"""
    module = _load_module()

    result = await module.guard_check("kubectl delete pod xxx", "read")

    assert result["allowed"] is False
    assert "请改用 k8s_write" in result["message"]


@pytest.mark.asyncio
async def test_unknown_subcommand_defaults_to_write() -> None:
    """未知子命令默认按写操作处理。"""
    module = _load_module()

    result = await module.classify_command("kubectl mystery pods")

    assert result["level"] == "write"

