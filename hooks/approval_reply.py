"""飞书文本审批回复处理。"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parent.parent


def _load_project_module(relative_path: str, alias: str):
    """按项目相对路径加载模块，避免包导入冲突。"""
    if alias in sys.modules:
        return sys.modules[alias]

    module_path = _project_root() / relative_path
    spec = importlib.util.spec_from_file_location(alias, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


approval_async = _load_project_module("toolsets/approval_async.py", "aiops_approval_async")
incident_store = _load_project_module("toolsets/incident_store.py", "aiops_incident_store")
approval_authorization = _load_project_module("hooks/approval_authorization.py", "aiops_approval_authorization")


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

    return await handle_approval_decision(
        approval_id=str(parsed["approval_id"]),
        decision=str(parsed["decision"]),
        reason=parsed.get("reason"),
        approver_id=approver,
        source="approval_reply",
    )


async def handle_approval_decision(
    *,
    approval_id: str,
    decision: str,
    reason: str | None = None,
    approver_id: str,
    source: str = "approval_reply",
) -> dict[str, Any]:
    """处理已归一化的审批决策，供文本和 Feishu card callback 复用。"""
    normalized_approval_id = approval_id.strip()
    normalized_decision = decision.strip().lower()
    approver = approver_id.strip()
    event_source = source.strip() or "approval_reply"
    normalized_reason = reason.strip() if isinstance(reason, str) and reason.strip() else None

    if not normalized_approval_id:
        return {"handled": True, "ok": False, "approval_id": "", "message": "缺少 approval_id"}
    if normalized_decision not in {"approved", "denied"}:
        return {
            "handled": True,
            "ok": False,
            "approval_id": normalized_approval_id,
            "message": "decision 仅支持 approved 或 denied",
        }

    approval = await approval_async.check_approval(normalized_approval_id)
    if approval.get("found") is False:
        return {
            "handled": True,
            "ok": False,
            "approval_id": normalized_approval_id,
            "message": approval.get("message") or "审批记录不存在",
        }

    authorization = await approval_authorization.authorize_approval_reply(
        approval=approval,
        approver_id=approver,
        decision=normalized_decision,
    )
    if not authorization.get("ok"):
        await _record_unauthorized_attempt(
            approval,
            normalized_approval_id,
            approver,
            normalized_decision,
            authorization,
            event_source,
        )
        return {
            "handled": True,
            "ok": False,
            "approval_id": normalized_approval_id,
            "message": authorization.get("message"),
        }

    result = await approval_async.resolve_approval(
        normalized_approval_id,
        normalized_decision,
        approver,
        normalized_reason,
    )
    if not result.get("ok"):
        return {
            "handled": True,
            "ok": False,
            "approval_id": normalized_approval_id,
            "message": result.get("message"),
        }

    incident_id = approval.get("incident_id")
    if incident_id:
        event_type = "approval_approved" if normalized_decision == "approved" else "approval_denied"
        await incident_store.add_event(
            str(incident_id),
            event_type,
            event_source,
            normalized_approval_id,
            approver,
        )

    return {
        "handled": True,
        "ok": True,
        "approval_id": normalized_approval_id,
        "status": result.get("status"),
    }


async def _record_unauthorized_attempt(
    approval: dict[str, Any],
    approval_id: str,
    approver: str,
    decision: str,
    authorization: dict[str, Any],
    source: str = "approval_reply",
) -> None:
    """记录未授权审批尝试。"""
    incident_id = approval.get("incident_id")
    if not incident_id:
        logger.warning(
            "approval unauthorized without incident_id: approval_id=%s approver=%s reason_code=%s",
            approval_id,
            approver,
            authorization.get("reason_code"),
        )
        return

    metadata = {
        "approval_id": approval_id,
        "approver_id": approver,
        "decision": decision,
        "reason_code": authorization.get("reason_code"),
        "operation_type": approval.get("operation_type"),
        "namespace": approval.get("namespace"),
    }
    await incident_store.add_event(
        str(incident_id),
        "approval_unauthorized",
        source,
        approval_id,
        approver,
        metadata,
    )
