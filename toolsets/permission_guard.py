"""SRE 权限守卫模块。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict


def _ensure_registry_import() -> None:
    """确保可以导入 Hermes 的工具注册器。"""
    hermes_root = Path(__file__).resolve().parents[1] / "hermes-agent"
    if str(hermes_root) not in sys.path:
        sys.path.insert(0, str(hermes_root))


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parents[1]


def _load_identity_module():
    """按文件路径加载本地 identity 模块。"""
    module_name = "aiops_hooks_identity"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = _project_root() / "hooks" / "identity.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_ensure_registry_import()

try:
    from tools.registry import registry  # noqa: E402
except ImportError:  # pragma: no cover - 本地测试未安装 hermes-agent 时使用
    class _NoopRegistry:
        def register(self, **_: Any) -> None:
            return None

    registry = _NoopRegistry()


identity = _load_identity_module()


def check_tool_access(operator_profile: Dict[str, Any], tool_name: str, namespace: str) -> Dict[str, Any]:
    """检查工具访问权限。"""
    return identity.check_permission(operator_profile, tool_name, namespace)


def check_approval_requirement(tool_name: str, namespace: str, command: str | None = None) -> Dict[str, Any]:
    """检查是否命中审批规则。"""
    return identity.match_approval_rule(tool_name, namespace, command)


SRE_CHECK_PERMISSION_SCHEMA = {
    "name": "sre_check_permission",
    "description": "检查操作者对指定工具和命名空间的访问权限，并返回审批要求。",
    "parameters": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string", "description": "工具名称"},
            "namespace": {"type": "string", "description": "命名空间"},
            "operator_name": {"type": "string", "description": "可选的操作者名称"},
            "command": {"type": "string", "description": "可选的命令字符串"},
        },
        "required": ["tool_name", "namespace"],
    },
}


async def _tool_sre_check_permission(args: Dict[str, Any], **_: Any) -> str:
    """工具入口：权限与审批联合检查。"""
    tool_name = str(args.get("tool_name", "")).strip()
    namespace = str(args.get("namespace", "")).strip()
    operator_name = str(args.get("operator_name", "")).strip()
    command = args.get("command")

    operators = await identity._load_operators()  # type: ignore[attr-defined]
    operator_profile: Dict[str, Any] | None = None
    if operator_name:
        for operator in operators:
            if str(operator.get("name", "")).strip() == operator_name:
                operator_profile = {
                    "name": operator.get("name", ""),
                    "role": operator.get("role", ""),
                    "namespaces": operator.get("namespaces", ["*"]),
                    "allowed_tools": operator.get("allowed_tools", []),
                    "can_approve": bool(operator.get("can_approve", False)),
                }
                break

    if operator_name and operator_profile is None:
        return json.dumps({
            "allowed": False,
            "message": f"未找到操作者 {operator_name}",
            "approval_required": False,
            "approval_from": None,
            "auto_approve": False,
        }, ensure_ascii=False)

    if operator_profile is None:
        operator_profile = {
            "name": operator_name or "anonymous",
            "role": "unknown",
            "namespaces": ["*"],
            "allowed_tools": [tool_name],
            "can_approve": False,
        }

    access = check_tool_access(operator_profile, tool_name, namespace)
    approval = check_approval_requirement(tool_name, namespace, command if isinstance(command, str) else None)

    return json.dumps({
        "allowed": access["allowed"],
        "message": access["message"],
        "approval_required": approval["required"],
        "approval_from": approval["approval_from"],
        "auto_approve": approval["auto_approve"],
    }, ensure_ascii=False)


registry.register(
    name="sre_check_permission",
    toolset="sre",
    schema=SRE_CHECK_PERMISSION_SCHEMA,
    handler=_tool_sre_check_permission,
    is_async=True,
)
