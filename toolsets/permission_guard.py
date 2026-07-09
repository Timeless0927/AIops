"""Small permission guard for tool-level operator profiles."""

from __future__ import annotations

import json
from typing import Any

from toolsets.registry import registry


def check_tool_access(operator_profile: dict[str, Any], tool_name: str, namespace: str) -> dict[str, Any]:
    allowed_tools = {str(item) for item in operator_profile.get("allowed_tools") or []}
    namespaces = {str(item) for item in operator_profile.get("namespaces") or []}
    if tool_name not in allowed_tools and "*" not in allowed_tools:
        return {"allowed": False, "message": f"无权使用工具 {tool_name}"}
    if namespace not in namespaces and "*" not in namespaces:
        return {"allowed": False, "message": f"无权访问命名空间 {namespace}"}
    return {"allowed": True, "message": "allowed"}


def check_approval_requirement(tool_name: str, namespace: str, command: str | None = None) -> dict[str, Any]:
    del tool_name, namespace, command
    return {"required": False, "approval_from": None, "auto_approve": False}


SRE_CHECK_PERMISSION_SCHEMA = {
    "name": "sre_check_permission",
    "description": "检查操作者对指定工具和命名空间的访问权限。",
    "parameters": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "namespace": {"type": "string"},
            "operator_profile": {"type": "object"},
        },
        "required": ["tool_name", "namespace"],
    },
}


async def _tool_sre_check_permission(args: dict[str, Any], **_: Any) -> str:
    tool_name = str(args.get("tool_name", "")).strip()
    namespace = str(args.get("namespace", "")).strip()
    operator_profile = args.get("operator_profile")
    if not isinstance(operator_profile, dict):
        operator_profile = {"allowed_tools": [tool_name], "namespaces": ["*"]}
    access = check_tool_access(operator_profile, tool_name, namespace)
    approval = check_approval_requirement(tool_name, namespace)
    return json.dumps(
        {
            "allowed": access["allowed"],
            "message": access["message"],
            "approval_required": approval["required"],
            "approval_from": approval["approval_from"],
            "auto_approve": approval["auto_approve"],
        },
        ensure_ascii=False,
    )


registry.register(
    name="sre_check_permission",
    toolset="sre",
    schema=SRE_CHECK_PERMISSION_SCHEMA,
    handler=_tool_sre_check_permission,
    is_async=True,
)
