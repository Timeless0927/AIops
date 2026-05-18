"""Gateway 启动后的会话恢复 Hook。"""

from __future__ import annotations

import importlib.util
import sys
import time
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
feishu_native_approval = _load_tool_module("feishu_native_approval", "aiops_feishu_native_approval")


def _approval_config(config: Dict[str, Any] | None) -> Dict[str, Any]:
    root = config if isinstance(config, dict) else {}
    platforms = root.get("platforms") if isinstance(root.get("platforms"), dict) else {}
    feishu = platforms.get("feishu") if isinstance(platforms.get("feishu"), dict) else {}
    approval = feishu.get("approval") if isinstance(feishu.get("approval"), dict) else {}
    return approval


def _polling_config(config: Dict[str, Any] | None) -> Dict[str, Any]:
    approval = _approval_config(config)
    nested = approval.get("polling") if isinstance(approval.get("polling"), dict) else {}
    return {**nested, **approval}


def _polling_enabled(config: Dict[str, Any] | None) -> bool:
    polling = _polling_config(config)
    if "polling_enabled" in polling:
        return bool(polling.get("polling_enabled"))
    if "enabled" in polling:
        return bool(polling.get("enabled"))
    return False


def _polling_int(config: Dict[str, Any] | None, *keys: str, default: int) -> int:
    polling = _polling_config(config)
    for key in keys:
        if key in polling:
            try:
                return int(polling.get(key))
            except (TypeError, ValueError):
                return default
    return default


async def poll_external_pending_approvals(*, config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """补偿同步 webhook 遗失的 external_pending 审批。"""
    if not _polling_enabled(config):
        return {"ok": True, "enabled": False, "scanned": 0, "synced": 0, "approved": 0, "denied": 0, "canceled": 0, "failed": 0}

    batch_size = _polling_int(config, "polling_batch_size", "batch_size", default=50)
    max_backoff = _polling_int(config, "polling_max_backoff_seconds", "max_backoff_seconds", default=300)
    stale_seconds = _polling_int(config, "polling_stale_seconds", "stale_seconds", default=0)
    interval_seconds = _polling_int(config, "polling_interval_seconds", "interval_seconds", default=60)
    current_time = time.time()
    try:
        rows = await approval_async.list_external_pending_approvals(
            limit=batch_size,
            now=current_time,
            stale_seconds=stale_seconds,
        )
    except TypeError as exc:
        if "stale_seconds" not in str(exc):
            raise
        rows = await approval_async.list_external_pending_approvals(limit=batch_size, now=current_time)
    result = {
        "ok": True,
        "enabled": True,
        "scanned": len(rows),
        "synced": 0,
        "approved": 0,
        "denied": 0,
        "canceled": 0,
        "failed": 0,
    }
    supported = {"APPROVED", "REJECTED", "CANCELED", "CANCELLED"}

    for row in rows:
        instance_code = str(row.get("external_instance_code") or "").strip()
        if not instance_code:
            result["failed"] += 1
            continue
        query = await feishu_native_approval.query_approval_instance(instance_code=instance_code, config=config or {})
        if not query.get("ok"):
            result["failed"] += 1
            attempts = int(row.get("external_poll_attempts") or 0) + 1
            backoff = min(max_backoff, max(1, 2 ** min(attempts, 8)))
            record_failure = getattr(approval_async, "record_external_poll_failure", None)
            if callable(record_failure):
                await record_failure(
                    str(row.get("approval_id") or row.get("external_uuid") or ""),
                    error_type=str(query.get("error_type") or "poll_error"),
                    message=str(query.get("message") or "query approval instance failed"),
                    backoff_seconds=backoff,
                )
            continue

        external_status = str(query.get("external_status") or "").strip().upper()
        if external_status == "CANCELLED":
            external_status = "CANCELED"
        if external_status == "PENDING":
            record_pending = getattr(approval_async, "record_external_poll_pending", None)
            if callable(record_pending):
                await record_pending(
                    str(row.get("approval_id") or row.get("external_uuid") or ""),
                    external_status=external_status,
                    interval_seconds=interval_seconds,
                    now=current_time,
                )
            continue
        if external_status not in supported:
            continue
        synced = await approval_async.resolve_external_approval(
            external_uuid=str(row.get("external_uuid") or row.get("approval_id") or "").strip(),
            external_instance_code=instance_code,
            external_status=external_status,
            source="feishu_polling",
            raw_event=query,
        )
        if not synced.get("ok"):
            result["failed"] += 1
            continue
        result["synced"] += 1
        status = str(synced.get("status") or "")
        if status == "approved":
            result["approved"] += 1
        elif status == "denied":
            result["denied"] += 1
        elif status == "canceled":
            result["canceled"] += 1
    return result


async def handle(event_type: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """在 gateway 启动时恢复未完成的会话状态。"""
    config = context.get("config") if isinstance(context, dict) else None

    if event_type != "gateway:startup":
        return {
            "pending_approval": [],
            "interrupted": [],
            "abnormal": [],
            "expired_approvals": 0,
            "expired_locks": 0,
            "external_approval_polling": {"ok": True, "scanned": 0, "synced": 0},
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

    recover_pending_cards = getattr(approval_async, "recover_pending_approval_cards", None)
    if callable(recover_pending_cards):
        approval_card_recovery = await recover_pending_cards()
    else:
        approval_card_recovery = {
            "ok": True,
            "scanned": 0,
            "sent": 0,
            "pending_retry": 0,
            "failed": 0,
            "approvals": [],
            "results": [],
        }
    expired_approvals = await approval_async.expire_stale()
    for approval in expired_approvals.get("approvals", []):
        if not isinstance(approval, dict):
            continue
        incident_id = approval.get("incident_id")
        approval_id = approval.get("approval_id")
        if incident_id and approval_id:
            await incident_store.add_event(str(incident_id), "approval_expired", "recovery", str(approval_id), "")
    expired_locks = await operation_lock.cleanup_expired()
    external_approval_polling = await poll_external_pending_approvals(config=config)

    return {
        "pending_approval": pending_list,
        "interrupted": interrupted_list,
        "abnormal": abnormal_list,
        "approval_card_recovery": approval_card_recovery,
        "recovered_approval_cards": int(approval_card_recovery.get("sent", 0)),
        "pending_approval_cards": int(approval_card_recovery.get("pending_retry", 0)),
        "expired_approvals": int(expired_approvals.get("expired", 0)),
        "expired_locks": int(expired_locks.get("deleted", 0)),
        "external_approval_polling": external_approval_polling,
    }
