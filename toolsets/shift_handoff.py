"""运维交接工具。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

try:
    from tools.registry import registry
except ImportError:  # pragma: no cover - 测试环境兼容
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-agent"))
    from tools.registry import registry


def _load_tool_module(module_filename: str, module_name: str):
    """按文件路径加载 toolsets 模块。"""
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = Path(__file__).resolve().parent / module_filename
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


incident_store = _load_tool_module("incident_store.py", "toolsets.incident_store")
audit_log = _load_tool_module("audit_log.py", "toolsets.audit_log")


SRE_SHIFT_HANDOFF_SCHEMA = {
    "name": "sre_shift_handoff",
    "description": "将当前活跃事件交接给新的值班人员。",
    "parameters": {
        "type": "object",
        "properties": {
            "new_operator_name": {
                "type": "string",
                "description": "新的值班人员名称",
            },
        },
        "required": ["new_operator_name"],
    },
}


async def sre_shift_handoff(new_operator_name: str) -> dict[str, Any]:
    """执行运维交接并返回交接摘要。"""
    incidents = await incident_store.list_active()
    summaries: list[dict[str, Any]] = []

    for incident in incidents:
        incident_id = str(incident.get("id", ""))
        timeline = await incident_store.get_timeline(incident_id)
        recent_events = [
            {
                "event_type": item.get("event_type"),
                "tool_name": item.get("tool_name"),
                "output_summary": item.get("output_summary"),
            }
            for item in timeline[-3:]
        ]
        summaries.append(
            {
                "incident_id": incident_id,
                "alert_name": incident.get("alert_name"),
                "namespace": incident.get("namespace"),
                "status": incident.get("status"),
                "recent_events": recent_events,
            }
        )
        await incident_store.update_operator(incident_id, new_operator_name)
        await audit_log.record_audit(
            who=new_operator_name,
            what="运维交接",
            cluster=incident.get("cluster"),
            namespace=incident.get("namespace"),
            trigger="manual",
            tool_level="write",
            tool_name="sre_shift_handoff",
            result="success",
            incident_id=incident_id,
        )

    return {"ok": True, "handoff_to": new_operator_name, "incidents": summaries}


async def _tool_sre_shift_handoff(args: dict[str, Any], **_: Any) -> str:
    """工具入口：执行交接。"""
    return json.dumps(await sre_shift_handoff(args.get("new_operator_name", "")), ensure_ascii=False)


registry.register(name="sre_shift_handoff", toolset="sre", schema=SRE_SHIFT_HANDOFF_SCHEMA, handler=_tool_sre_shift_handoff, is_async=True)
