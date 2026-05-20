"""会话身份绑定 Hook。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


def _config_path() -> Path:
    """返回第一个运行时配置文件路径。"""
    candidates = _candidate_config_paths()
    for config_path in candidates:
        if config_path.exists():
            return config_path
    return candidates[0] if candidates else Path(__file__).resolve().with_name(".missing-hermes-config.yaml")


def _candidate_config_paths() -> List[Path]:
    """按优先级返回运行时配置候选。"""
    candidates: List[Path] = []

    override = os.getenv("HERMES_CONFIG")
    if override:
        candidates.append(Path(override).expanduser())

    hermes_home = os.getenv("HERMES_HOME")
    if hermes_home:
        candidates.append(Path(hermes_home).expanduser() / "config.yaml")
    return candidates


def _extract_platform(event: Dict[str, Any]) -> str:
    """从事件中提取平台名称，默认返回空字符串。"""
    platform = event.get("platform") or event.get("source") or ""
    if isinstance(platform, str):
        return platform.strip().lower()
    return ""


def _extract_platform_user_id(event: Dict[str, Any]) -> Optional[str]:
    """从事件中提取平台用户 ID。"""
    platform = _extract_platform(event)
    sender = event.get("sender")
    if not isinstance(sender, dict):
        sender = {}

    # 飞书优先使用 sender.open_id。
    if platform == "feishu":
        open_id = sender.get("open_id")
        if isinstance(open_id, str) and open_id.strip():
            return open_id.strip()

    # 钉钉优先使用 senderStaffId，也兼容放在 sender 内的场景。
    if platform == "dingtalk":
        sender_staff_id = event.get("senderStaffId") or sender.get("senderStaffId")
        if isinstance(sender_staff_id, str) and sender_staff_id.strip():
            return sender_staff_id.strip()

    # 为了兼容测试和上层透传，允许直接传 platform_user_id。
    direct_user_id = event.get("platform_user_id")
    if isinstance(direct_user_id, str) and direct_user_id.strip():
        return direct_user_id.strip()

    return None


def _load_operators_sync() -> List[Dict[str, Any]]:
    """同步读取配置中的 operators 列表。"""
    config = _load_config_sync()

    permissions = config.get("sre_permissions")
    if not isinstance(permissions, dict):
        return []

    operators = permissions.get("operators")
    if not isinstance(operators, list):
        return []

    return [item for item in operators if isinstance(item, dict)]


async def _load_operators() -> List[Dict[str, Any]]:
    """异步读取配置，避免阻塞事件循环。"""
    return await asyncio.to_thread(_load_operators_sync)


async def load_operators() -> List[Dict[str, Any]]:
    """异步读取配置中的 operators 列表。"""
    return await _load_operators()


def _load_config_sync() -> Dict[str, Any]:
    """同步读取完整配置。"""
    for config_path in _candidate_config_paths():
        if not config_path.exists():
            continue

        with config_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}

        if isinstance(config, dict):
            return config
    return {}


def _normalize_string_list(value: Any, default: List[str]) -> List[str]:
    """将配置项标准化为字符串列表。"""
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return list(default)

    normalized = [str(item).strip() for item in value if str(item).strip()]
    return normalized or list(default)


def _match_operator(
    operators: List[Dict[str, Any]],
    platform: str,
    platform_user_id: str,
) -> Optional[Dict[str, Any]]:
    """按平台和平台用户 ID 匹配操作者。"""
    for operator in operators:
        operator_platform = str(operator.get("platform", "")).strip().lower()
        operator_user_id = str(operator.get("platform_user_id", "")).strip()
        if operator_platform == platform and operator_user_id == platform_user_id:
            return {
                "name": operator.get("name", ""),
                "role": operator.get("role", ""),
                "namespaces": _normalize_string_list(operator.get("namespaces"), ["*"]),
                "allowed_tools": _normalize_string_list(operator.get("allowed_tools"), []),
                "can_approve": bool(operator.get("can_approve", False)),
                "platform": operator_platform,
                "platform_user_id": operator_user_id,
            }
    return None


def check_permission(operator_profile: Dict[str, Any], tool_name: str, namespace: str) -> Dict[str, Any]:
    """检查操作者是否有权限访问指定工具和命名空间。"""
    allowed_tools = _normalize_string_list(operator_profile.get("allowed_tools"), [])
    namespaces = _normalize_string_list(operator_profile.get("namespaces"), ["*"])

    if tool_name not in allowed_tools:
        return {
            "allowed": False,
            "message": f"当前身份无权使用工具 {tool_name}",
        }

    if "*" not in namespaces and namespace not in namespaces:
        return {
            "allowed": False,
            "message": f"当前身份无权访问命名空间 {namespace}",
        }

    return {
        "allowed": True,
        "message": "允许访问",
    }


def load_approval_rules() -> List[Dict[str, Any]]:
    """读取配置中的审批规则列表。"""
    config = _load_config_sync()
    permissions = config.get("sre_permissions")
    if not isinstance(permissions, dict):
        return []

    approval_rules = permissions.get("approval_rules")
    if not isinstance(approval_rules, list):
        return []

    return [rule for rule in approval_rules if isinstance(rule, dict)]


def load_approval_policy() -> Dict[str, Any]:
    """读取审批策略，缺省项由调用方按安全默认值补齐。"""
    config = _load_config_sync()
    permissions = config.get("sre_permissions")
    if not isinstance(permissions, dict):
        return {}

    policy = permissions.get("approval_policy")
    return policy if isinstance(policy, dict) else {}


def match_approval_rule(tool_name: str, namespace: str, command: str | None = None) -> Dict[str, Any]:
    """按工具、命名空间和命令匹配审批规则。"""
    normalized_command = (command or "").strip().lower()

    for rule in load_approval_rules():
        if str(rule.get("tool", "")).strip() != tool_name:
            continue

        rule_namespace = str(rule.get("namespace", "")).strip()
        if rule_namespace and rule_namespace != namespace:
            continue

        command_match = str(rule.get("command_match", "")).strip().lower()
        if command_match and command_match not in normalized_command:
            continue

        approval_from = rule.get("require_approval_from")
        auto_approve = bool(rule.get("auto_approve", False))
        return {
            "required": bool(approval_from),
            "approval_from": str(approval_from) if approval_from else None,
            "auto_approve": auto_approve,
        }

    return {
        "required": False,
        "approval_from": None,
        "auto_approve": False,
    }


async def on_session_start(event: Dict[str, Any]) -> Dict[str, Any]:
    """在会话开始时绑定操作者身份。"""
    platform = _extract_platform(event)
    platform_user_id = _extract_platform_user_id(event)

    if not platform or not platform_user_id:
        return {
            "allowed": False,
            "message": "你没有权限使用此 agent，请联系管理员",
            "reason": "missing_platform_identity",
        }

    operators = await _load_operators()
    operator = _match_operator(operators, platform, platform_user_id)
    if operator is None:
        return {
            "allowed": False,
            "message": "你没有权限使用此 agent，请联系管理员",
            "reason": "operator_not_found",
            "platform": platform,
            "platform_user_id": platform_user_id,
        }

    # 返回可注入 session context 的操作者资料。
    return {
        "allowed": True,
        "operator_profile": {
            "name": operator["name"],
            "role": operator["role"],
            "namespaces": operator["namespaces"],
            "allowed_tools": operator["allowed_tools"],
            "can_approve": operator["can_approve"],
        },
        "session_context": {
            "operator": operator,
        },
        "platform": platform,
        "platform_user_id": platform_user_id,
    }
