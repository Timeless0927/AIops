"""审批回复授权。"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parent.parent


def _load_hook_module(module_basename: str, alias: str):
    """按文件路径加载 hook 模块，兼容直接文件加载测试。"""
    if alias in sys.modules:
        return sys.modules[alias]

    module_path = _project_root() / "hooks" / f"{module_basename}.py"
    spec = importlib.util.spec_from_file_location(alias, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


identity = _load_hook_module("identity", "aiops_identity_for_approval_authorization")

REQUIRED_APPROVAL_FIELDS = (
    "status",
    "operation_type",
    "namespace",
    "risk_level",
    "requester",
    "context",
    "incident_id",
)
HIGH_RISK_LEVELS = {"high", "dangerous", "critical"}
DANGEROUS_OPERATION_TYPES = {"k8s_exec", "dangerous", "namespace", "node", "pv", "crd"}
DEFAULT_APPROVAL_POLICY = {
    "allow_self_approval_low_risk": False,
    "require_admin_for_exec": True,
    "require_admin_for_dangerous": True,
}


def _deny(message: str, reason_code: str) -> dict[str, Any]:
    """构造拒绝结果。"""
    return {"ok": False, "message": message, "reason_code": reason_code}


def _normalize_string(value: Any) -> str:
    """标准化字符串字段。"""
    return value.strip() if isinstance(value, str) else ""


def _normalize_string_list(value: Any, default: list[str]) -> list[str]:
    """标准化字符串列表。"""
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return list(default)
    normalized = [str(item).strip() for item in value if str(item).strip()]
    return normalized or list(default)


def _operator_matches(operator: dict[str, Any], approver_id: str) -> bool:
    """匹配 Feishu open_id/platform_user_id。"""
    platform = _normalize_string(operator.get("platform")).lower()
    platform_user_id = _normalize_string(operator.get("platform_user_id"))
    return platform == "feishu" and platform_user_id == approver_id


async def _find_operator(approver_id: str) -> dict[str, Any] | None:
    """按 Feishu open_id 查找 operator。"""
    for operator in await identity.load_operators():
        if isinstance(operator, dict) and _operator_matches(operator, approver_id):
            return {
                "name": operator.get("name", ""),
                "role": operator.get("role", ""),
                "namespaces": _normalize_string_list(operator.get("namespaces"), ["*"]),
                "allowed_tools": _normalize_string_list(operator.get("allowed_tools"), []),
                "can_approve": bool(operator.get("can_approve", False)),
                "platform": "feishu",
                "platform_user_id": approver_id,
            }
    return None


def _approval_id(approval: dict[str, Any]) -> str:
    """返回 approval id。"""
    return _normalize_string(approval.get("approval_id") or approval.get("id"))


def _has_complete_context(approval: dict[str, Any]) -> bool:
    """检查授权所需字段是否完整。"""
    if not _approval_id(approval):
        return False
    for field in REQUIRED_APPROVAL_FIELDS:
        if field not in approval or approval.get(field) is None:
            return False
        if field != "context" and not _normalize_string(approval.get(field)):
            return False
    return isinstance(approval.get("context"), dict)


def _approval_policy() -> dict[str, Any]:
    """读取审批策略并补齐安全默认值。"""
    policy = dict(DEFAULT_APPROVAL_POLICY)
    configured = identity.load_approval_policy()
    if isinstance(configured, dict):
        policy.update(configured)
    return policy


def _is_admin(operator: dict[str, Any]) -> bool:
    """判断 operator 是否 admin。"""
    return _normalize_string(operator.get("role")).lower() == "admin"


def _is_high_risk(risk_level: str) -> bool:
    """判断风险等级是否高风险。"""
    return risk_level.lower() in HIGH_RISK_LEVELS


def _is_dangerous_operation(operation_type: str) -> bool:
    """判断操作类型是否需要强审批人。"""
    normalized = operation_type.lower()
    if normalized in DANGEROUS_OPERATION_TYPES:
        return True
    return any(token in normalized for token in ("namespace", "node", "pv", "crd"))


def _rule_allows_operator(operator: dict[str, Any], operation_type: str, namespace: str, command: str) -> bool:
    """检查 approval rule 是否允许该 role/name 审批。"""
    rule = identity.match_approval_rule(operation_type, namespace, command)
    approval_from = _normalize_string(rule.get("approval_from")) if isinstance(rule, dict) else ""
    if not approval_from:
        return False

    allowed = {item.strip() for item in approval_from.split(",") if item.strip()}
    role = _normalize_string(operator.get("role"))
    name = _normalize_string(operator.get("name"))
    return role in allowed or name in allowed


def _is_self_approval(operator: dict[str, Any], requester: str, approver_id: str) -> bool:
    """判断是否自批。"""
    if requester == approver_id:
        return True
    return requester in {
        _normalize_string(operator.get("name")),
        _normalize_string(operator.get("platform_user_id")),
    }


async def authorize_approval_reply(
    *,
    approval: dict[str, Any],
    approver_id: str,
    decision: str,
) -> dict[str, Any]:
    """校验 Feishu 审批回复是否有权修改 approval。"""
    del decision
    approver = _normalize_string(approver_id)
    if not approver:
        return _deny("无法识别审批人身份", "missing_approver_id")

    if not _has_complete_context(approval):
        return _deny("审批上下文不完整", "approval_context_incomplete")

    if _normalize_string(approval.get("status")).lower() != "pending":
        return _deny("审批已处理或已过期", "approval_not_pending")

    operator = await _find_operator(approver)
    if operator is None:
        return _deny("审批人未授权", "unknown_approver")

    namespace = _normalize_string(approval.get("namespace"))
    namespaces = _normalize_string_list(operator.get("namespaces"), ["*"])
    if "*" not in namespaces and namespace not in namespaces:
        return _deny("审批人无权审批该命名空间", "namespace_not_allowed")

    operation_type = _normalize_string(approval.get("operation_type"))
    risk_level = _normalize_string(approval.get("risk_level"))
    command = _normalize_string(approval.get("command"))
    can_approve = bool(operator.get("can_approve"))
    admin = _is_admin(operator)
    rule_allowed = _rule_allows_operator(operator, operation_type, namespace, command)

    if not (can_approve or rule_allowed):
        return _deny("审批人无权审批该操作", "approver_not_allowed")

    policy = _approval_policy()
    dangerous = _is_dangerous_operation(operation_type)
    if operation_type.lower() == "k8s_exec" and policy.get("require_admin_for_exec", True) and not (admin or can_approve):
        return _deny("审批人无权审批该操作", "approver_not_allowed")
    if dangerous and policy.get("require_admin_for_dangerous", True) and not (admin or can_approve):
        return _deny("审批人无权审批该操作", "approver_not_allowed")
    if _is_high_risk(risk_level) and not (admin or can_approve):
        return _deny("审批人无权审批该操作", "approver_not_allowed")

    requester = _normalize_string(approval.get("requester"))
    if _is_self_approval(operator, requester, approver):
        allow_low_risk = bool(policy.get("allow_self_approval_low_risk", False))
        if _is_high_risk(risk_level) or not allow_low_risk:
            return _deny("不能审批自己发起的高风险操作", "self_approval_denied")

    return {"ok": True, "operator": operator}
