"""自动审计 Hook。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def _load_audit_log_module():
    """按文件路径加载 audit_log 模块。"""
    module_name = "toolsets.audit_log"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = Path(__file__).resolve().parent.parent / "toolsets" / "audit_log.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


audit_log = _load_audit_log_module()


SRE_TOOL_NAMES = {
    "k8s_read",
    "k8s_write",
    "k8s_exec",
    "prometheus_query",
    "query_metrics",
    "loki_query",
    "query_logs",
}


def _extract_tool_names(context: dict[str, Any]) -> list[str]:
    """从 hook context 中提取工具名列表。"""
    raw = context.get("tool_names") or context.get("tools") or []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _extract_who(context: dict[str, Any]) -> str:
    """提取操作者标识。"""
    user_id = context.get("user_id")
    if isinstance(user_id, str) and user_id.strip():
        return user_id.strip()

    return "unknown"


def _tool_level(tool_name: str) -> str:
    """根据工具名推断工具级别。"""
    if tool_name == "k8s_write":
        return "write"
    if tool_name == "k8s_exec":
        return "exec"
    return "read"


async def handle(event_type: str, context: dict[str, Any]) -> dict[str, Any]:
    """处理 agent:step 事件并自动记录 SRE 工具调用。"""
    if event_type != "agent:step":
        return {"recorded": 0}

    tool_names = [name for name in _extract_tool_names(context) if name in SRE_TOOL_NAMES]
    if not tool_names:
        return {"recorded": 0}

    who = _extract_who(context)
    cluster = context.get("cluster")
    namespace = context.get("namespace")
    trigger = str(context.get("trigger") or "manual")
    result = str(context.get("result") or "success")
    incident_id = context.get("incident_id")

    count = 0
    for tool_name in tool_names:
        await audit_log.record_audit(
            who=who,
            what=f"调用工具 {tool_name}",
            cluster=cluster,
            namespace=namespace,
            trigger=trigger,
            tool_level=_tool_level(tool_name),
            tool_name=tool_name,
            result=result,
            incident_id=incident_id,
        )
        count += 1

    return {"recorded": count}
