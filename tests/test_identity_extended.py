"""测试扩展后的身份与审批模型。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hooks import identity


def _operators() -> list[dict]:
    """构造测试专用 operator 列表。"""
    return [
        {
            "name": "管理员",
            "platform": "feishu",
            "platform_user_id": "ou_admin",
            "role": "admin",
            "namespaces": ["*"],
            "allowed_tools": ["k8s_read", "k8s_write", "k8s_exec"],
            "can_approve": True,
        },
        {
            "name": "运维员",
            "platform": "feishu",
            "platform_user_id": "ou_operator",
            "role": "operator",
            "namespaces": ["default", "staging"],
            "allowed_tools": ["k8s_read", "k8s_write"],
            "can_approve": False,
        },
    ]


@pytest.mark.asyncio
async def test_match_operator_returns_namespaces_and_allowed_tools() -> None:
    """匹配到的 operator 应包含命名空间和工具权限。"""
    matched = identity._match_operator(_operators(), "feishu", "ou_operator")

    assert matched is not None
    assert matched["namespaces"] == ["default", "staging"]
    assert matched["allowed_tools"] == ["k8s_read", "k8s_write"]


@pytest.mark.asyncio
async def test_check_permission_scenarios() -> None:
    """权限检查应覆盖工具拒绝、命名空间拒绝与放行。"""
    operator = identity._match_operator(_operators(), "feishu", "ou_operator")
    assert operator is not None

    tool_denied = identity.check_permission(operator, "k8s_exec", "staging")
    namespace_denied = identity.check_permission(operator, "k8s_write", "production")
    allowed = identity.check_permission(operator, "k8s_write", "staging")

    assert tool_denied["allowed"] is False
    assert namespace_denied["allowed"] is False
    assert allowed["allowed"] is True


@pytest.mark.asyncio
async def test_load_approval_rules_reads_config() -> None:
    """应能从配置加载审批规则。"""
    rules = identity.load_approval_rules()

    assert any(rule.get("tool") == "k8s_exec" for rule in rules)
    assert any(rule.get("namespace") == "staging" and rule.get("auto_approve") for rule in rules)


@pytest.mark.asyncio
async def test_match_approval_rule_logic() -> None:
    """审批规则匹配应覆盖工具、命名空间和命令关键字。"""
    exec_rule = identity.match_approval_rule("k8s_exec", "default")
    staging_rule = identity.match_approval_rule("k8s_write", "staging", "kubectl apply -f deploy.yaml")
    delete_rule = identity.match_approval_rule("k8s_write", "default", "kubectl delete pod test")
    miss_rule = identity.match_approval_rule("k8s_read", "default", "kubectl get pods")

    assert exec_rule == {"required": True, "approval_from": "admin", "auto_approve": False}
    assert staging_rule == {"required": False, "approval_from": None, "auto_approve": True}
    assert delete_rule == {"required": True, "approval_from": "admin", "auto_approve": False}
    assert miss_rule == {"required": False, "approval_from": None, "auto_approve": False}
