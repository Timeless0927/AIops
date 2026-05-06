"""飞书文本审批回复处理。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parent.parent


def _load_tool_module(module_basename: str, alias: str):
    """按文件路径加载 toolsets 模块，避免包导入冲突。"""
    if alias in sys.modules:
        return sys.modules[alias]

    module_path = _project_root() / "toolsets" / f"{module_basename}.py"
    spec = importlib.util.spec_from_file_location(alias, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


approval_async = _load_tool_module("approval_async", "aiops_approval_async")
incident_store = _load_tool_module("incident_store", "aiops_incident_store")


def parse_approval_reply(text: str) -> dict[str, str | None] | None:
    """解析 `批准 <approval_id>` / `拒绝 <approval_id> <reason>` 文本。"""
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 2:
        return None

    verb = parts[0]
    if verb not in {"批准", "拒绝"}:
        return None

    return {
        "decision": "approved" if verb == "批准" else "denied",
        "approval_id": parts[1],
        "reason": parts[2].strip() if len(parts) > 2 and parts[2].strip() else None,
    }


async def handle_approval_reply(text: str, approver: str) -> dict[str, Any]:
    """处理审批回复并回写 incident timeline。"""
    parsed = parse_approval_reply(text)
    if parsed is None:
        return {"handled": False}

    approval_id = str(parsed["approval_id"])
    decision = str(parsed["decision"])
    result = await approval_async.resolve_approval(approval_id, decision, approver, parsed.get("reason"))
    if not result.get("ok"):
        return {
            "handled": True,
            "ok": False,
            "approval_id": approval_id,
            "message": result.get("message"),
        }

    approval = await approval_async.check_approval(approval_id)
    incident_id = approval.get("incident_id")
    if incident_id:
        event_type = "approval_approved" if decision == "approved" else "approval_denied"
        await incident_store.add_event(str(incident_id), event_type, "approval_reply", approval_id, approver)

    return {"handled": True, "ok": True, "approval_id": approval_id, "status": result.get("status")}
