"""Gateway 启动后的会话恢复 Hook。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict


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


incident_store = _load_tool_module("incident_store", "aiops_incident_store")
approval_async = _load_tool_module("approval_async", "aiops_approval_async")
operation_lock = _load_tool_module("operation_lock", "aiops_operation_lock")


async def handle(event_type: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """在 gateway 启动时恢复未完成的会话状态。"""
    del context

    if event_type != "gateway:startup":
        return {
            "pending_approval": [],
            "interrupted": [],
            "abnormal": [],
            "expired_approvals": 0,
            "expired_locks": 0,
        }

    incidents = await incident_store.list_active()
    pending_list: list[dict[str, Any]] = []
    interrupted_list: list[dict[str, Any]] = []
    abnormal_list: list[dict[str, Any]] = []

    for incident in incidents:
        status = str(incident.get("status", "")).strip().lower()
        if "pending_approval" in status:
            pending_list.append(incident)
            continue

        if "investigating" in status:
            interrupted_list.append(incident)
            continue

        if "executing" in status:
            resource_key = str(incident.get("id", "")).strip()
            locked = await operation_lock.is_locked(resource_key)
            if not locked:
                await incident_store.update_status(resource_key, "abnormal")
                updated_incident = dict(incident)
                updated_incident["status"] = "abnormal"
                abnormal_list.append(updated_incident)

    expired_approvals = await approval_async.expire_stale()
    expired_locks = await operation_lock.cleanup_expired()

    return {
        "pending_approval": pending_list,
        "interrupted": interrupted_list,
        "abnormal": abnormal_list,
        "expired_approvals": int(expired_approvals.get("expired", 0)),
        "expired_locks": int(expired_locks.get("deleted", 0)),
    }
