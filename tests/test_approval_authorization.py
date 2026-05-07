"""测试审批回复授权。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "hooks" / "approval_authorization.py"
    module_name = "test_approval_authorization_module"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _approval(**overrides: object) -> dict[str, object]:
    """构造完整 pending approval。"""
    approval: dict[str, object] = {
        "approval_id": "ap-1",
        "status": "pending",
        "operation_type": "k8s_write",
        "namespace": "default",
        "risk_level": "medium",
        "requester": "alert_webhook",
        "context": {"source": "test"},
        "incident_id": "inc-1",
    }
    approval.update(overrides)
    return approval


def _operators() -> list[dict[str, object]]:
    """构造测试 operator。"""
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
            "name": "值班员",
            "platform": "feishu",
            "platform_user_id": "ou_default",
            "role": "operator",
            "namespaces": ["default"],
            "allowed_tools": ["k8s_read", "k8s_write"],
            "can_approve": False,
        },
        {
            "name": "审批员",
            "platform": "feishu",
            "platform_user_id": "ou_approver",
            "role": "operator",
            "namespaces": ["default"],
            "allowed_tools": ["k8s_read", "k8s_write"],
            "can_approve": True,
        },
    ]


def _patch_identity(monkeypatch: pytest.MonkeyPatch, module, *, policy: dict[str, object] | None = None) -> None:
    async def _load_operators():
        return _operators()

    monkeypatch.setattr(module.identity, "load_operators", _load_operators)
    monkeypatch.setattr(module.identity, "load_approval_policy", lambda: policy or {})
    monkeypatch.setattr(
        module.identity,
        "match_approval_rule",
        lambda tool_name, namespace, command=None: {
            "required": False,
            "approval_from": None,
            "auto_approve": False,
        },
    )


@pytest.mark.asyncio
async def test_admin_can_authorize_pending_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    """admin 可以审批 pending approval。"""
    module = _load_module()
    _patch_identity(monkeypatch, module)

    result = await module.authorize_approval_reply(
        approval=_approval(),
        approver_id="ou_admin",
        decision="approved",
    )

    assert result["ok"] is True
    assert result["operator"]["role"] == "admin"


@pytest.mark.asyncio
async def test_admin_can_authorize_pending_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    """admin 可以拒绝 pending approval。"""
    module = _load_module()
    _patch_identity(monkeypatch, module)

    result = await module.authorize_approval_reply(
        approval=_approval(),
        approver_id="ou_admin",
        decision="denied",
    )

    assert result["ok"] is True
    assert result["operator"]["role"] == "admin"


@pytest.mark.asyncio
async def test_unknown_feishu_user_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置 Feishu 用户必须拒绝。"""
    module = _load_module()
    _patch_identity(monkeypatch, module)

    result = await module.authorize_approval_reply(
        approval=_approval(),
        approver_id="ou_missing",
        decision="approved",
    )

    assert result == {
        "ok": False,
        "message": "审批人未授权",
        "reason_code": "unknown_approver",
    }


@pytest.mark.asyncio
async def test_namespace_scoped_approver_cannot_approve_other_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """namespace 不匹配必须拒绝。"""
    module = _load_module()
    _patch_identity(monkeypatch, module)

    result = await module.authorize_approval_reply(
        approval=_approval(namespace="production"),
        approver_id="ou_approver",
        decision="approved",
    )

    assert result["ok"] is False
    assert result["reason_code"] == "namespace_not_allowed"
    assert result["message"] == "审批人无权审批该命名空间"


@pytest.mark.asyncio
async def test_high_risk_requires_admin_or_can_approve(monkeypatch: pytest.MonkeyPatch) -> None:
    """高风险操作必须由 admin 或 can_approve 审批。"""
    module = _load_module()
    _patch_identity(monkeypatch, module)

    result = await module.authorize_approval_reply(
        approval=_approval(risk_level="high"),
        approver_id="ou_default",
        decision="approved",
    )

    assert result["ok"] is False
    assert result["reason_code"] == "approver_not_allowed"


@pytest.mark.asyncio
async def test_high_risk_self_approval_is_denied_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """高风险自批默认拒绝。"""
    module = _load_module()
    _patch_identity(monkeypatch, module)

    result = await module.authorize_approval_reply(
        approval=_approval(risk_level="high", requester="ou_approver"),
        approver_id="ou_approver",
        decision="approved",
    )

    assert result["ok"] is False
    assert result["reason_code"] == "self_approval_denied"


@pytest.mark.asyncio
async def test_non_pending_approval_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """已处理 approval 不能重复审批。"""
    module = _load_module()
    _patch_identity(monkeypatch, module)

    result = await module.authorize_approval_reply(
        approval=_approval(status="approved"),
        approver_id="ou_admin",
        decision="denied",
    )

    assert result["ok"] is False
    assert result["reason_code"] == "approval_not_pending"


@pytest.mark.asyncio
async def test_missing_context_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少关键 approval 字段时 fail closed。"""
    module = _load_module()
    _patch_identity(monkeypatch, module)
    approval = _approval()
    del approval["risk_level"]

    result = await module.authorize_approval_reply(
        approval=approval,
        approver_id="ou_admin",
        decision="approved",
    )

    assert result["ok"] is False
    assert result["reason_code"] == "approval_context_incomplete"
