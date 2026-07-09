"""测试权限守卫模块。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "permission_guard.py"
    spec = importlib.util.spec_from_file_location("test_permission_guard_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _admin_profile() -> dict:
    """构造管理员身份。"""
    return {
        "name": "管理员",
        "role": "admin",
        "namespaces": ["*"],
        "allowed_tools": ["k8s_read", "k8s_write", "k8s_exec"],
        "can_approve": True,
    }


def _operator_profile() -> dict:
    """构造运维员身份。"""
    return {
        "name": "运维员",
        "role": "operator",
        "namespaces": ["default", "staging"],
        "allowed_tools": ["k8s_read", "k8s_write"],
        "can_approve": False,
    }


@pytest.mark.asyncio
async def test_admin_has_full_access() -> None:
    """管理员应通过全权限检查。"""
    module = _load_module()

    result = module.check_tool_access(_admin_profile(), "k8s_exec", "production")

    assert result["allowed"] is True


@pytest.mark.asyncio
async def test_operator_rejects_restricted_tool() -> None:
    """运维员访问未授权工具应被拒绝。"""
    module = _load_module()

    result = module.check_tool_access(_operator_profile(), "k8s_exec", "staging")

    assert result["allowed"] is False
    assert "无权使用工具" in result["message"]


@pytest.mark.asyncio
async def test_namespace_wildcard_allows_all() -> None:
    """命名空间通配符应允许任意命名空间。"""
    module = _load_module()

    result = module.check_tool_access(_admin_profile(), "k8s_read", "random-ns")

    assert result["allowed"] is True


@pytest.mark.asyncio
async def test_approval_requirement_defaults_to_gateway_policy() -> None:
    """工具层不再读取 legacy config，审批由 Gateway 主线负责。"""
    module = _load_module()

    result = module.check_approval_requirement("k8s_write", "default", "kubectl delete pod app-1")

    assert result == {"required": False, "approval_from": None, "auto_approve": False}
