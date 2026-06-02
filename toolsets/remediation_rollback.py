"""Deterministic rollback helpers for selected remediation actions."""

from __future__ import annotations

import json
import uuid
from typing import Any

try:
    from . import audit_log, incident_store, operation_lock
    from .k8s_write import execute_approved
    from .remediation_execution import (
        DEFAULT_MAX_REPLICAS,
        LOCK_TTL_SECONDS,
        build_kubectl_command,
        dry_run_action,
        resource_lock_key,
        validate_remediation_action,
    )
    from .remediation_kube_context import resolve_kube_context
except ImportError:  # pragma: no cover - script-style import compatibility
    import audit_log  # type: ignore
    import incident_store  # type: ignore
    import operation_lock  # type: ignore
    from k8s_write import execute_approved  # type: ignore
    from remediation_execution import (  # type: ignore
        DEFAULT_MAX_REPLICAS,
        LOCK_TTL_SECONDS,
        build_kubectl_command,
        dry_run_action,
        resource_lock_key,
        validate_remediation_action,
    )
    from remediation_kube_context import resolve_kube_context  # type: ignore


def build_rollback_action(
    action: dict[str, Any],
    *,
    max_replicas: int = DEFAULT_MAX_REPLICAS,
) -> dict[str, Any]:
    """Build a trusted rollback command for a rollback-eligible action."""

    validation = validate_remediation_action(action, max_replicas=max_replicas)
    if not validation["ok"]:
        return _refused(
            action,
            str(validation.get("reason_code") or "invalid_action"),
            str(validation.get("summary") or "remediation action 校验失败"),
            validation=validation,
        )

    validated = validation["action"]
    action_type = str(validated.get("action_type") or "")
    if action_type != "scale_deployment":
        return _refused(validated, "unsupported_rollback_action", "仅支持 scale_deployment 确定性 rollback")

    replicas = _previous_replicas(action)
    if replicas is None:
        return _refused(validated, "rollback_before_replicas_missing", "缺少 before.replicas，无法确定性 rollback")
    if not isinstance(replicas, int) or isinstance(replicas, bool) or replicas < 0:
        return _refused(validated, "rollback_before_replicas_invalid", "before.replicas 必须是非负整数")

    rollback_action = _rollback_scale_action(validated, replicas)
    rollback_validation = validate_remediation_action(rollback_action, max_replicas=max_replicas)
    if not rollback_validation["ok"]:
        return _refused(
            validated,
            f"rollback_{rollback_validation.get('reason_code', 'invalid_action')}",
            str(rollback_validation.get("summary") or "rollback action 校验失败"),
            rollback_action=rollback_action,
            validation=rollback_validation,
        )

    rollback_validated = rollback_validation["action"]
    command = build_kubectl_command(rollback_validated, dry_run=False)
    dry_run_command = build_kubectl_command(rollback_validated, dry_run=True)
    rollback_action = {
        **rollback_validated,
        "source_action_signature": validated.get("action_signature"),
    }
    try:
        kube_context = resolve_kube_context(rollback_action)
    except ValueError as exc:
        return _refused(
            validated,
            "invalid_kube_context",
            str(exc),
            rollback_action=rollback_action,
        )
    return {
        "ok": True,
        "action_type": action_type,
        "action_signature": validated["action_signature"],
        "rollback_action": rollback_action,
        "command": command,
        "command_preview": command,
        "dry_run_command_preview": dry_run_command,
        "cluster": rollback_validated["cluster"],
        "context": kube_context,
        "kube_context": kube_context,
        "resource_key": resource_lock_key(rollback_validated),
        "previous_replicas": replicas,
        "summary": (
            f"rollback scale deployment/{rollback_validated['resource_name']} "
            f"to {replicas} replicas"
        ),
    }


async def execute_rollback(
    action: dict[str, Any],
    *,
    incident_id: str | None = None,
    approver_id: str | None = None,
    approval_id: str | None = None,
    session_id: str | None = None,
    lock_ttl_seconds: int = LOCK_TTL_SECONDS,
    max_replicas: int = DEFAULT_MAX_REPLICAS,
) -> dict[str, Any]:
    """Execute a deterministic rollback command generated from structured action data."""

    rollback = build_rollback_action(action, max_replicas=max_replicas)
    if not rollback["ok"]:
        if incident_id:
            await _record_rollback_event(
                incident_id,
                event_type="rollback_failed",
                action=action,
                rollback=rollback,
                approver_id=approver_id,
                approval_id=approval_id,
            )
        return rollback

    rollback_action = rollback["rollback_action"]
    dry_run_result = await dry_run_action(rollback_action, max_replicas=max_replicas)
    if not dry_run_result.get("ok"):
        audit_id = await _record_rollback_audit(
            rollback=rollback,
            status="dry_run_failed",
            dry_run_result=dry_run_result,
            execution_result=None,
            approver_id=approver_id,
            approval_id=approval_id,
            incident_id=incident_id,
        )
        result = {
            **rollback,
            "ok": False,
            "status": "dry_run_failed",
            "reason_code": str(dry_run_result.get("reason_code") or "dry_run_failed"),
            "dry_run": dry_run_result,
            "audit_id": audit_id,
            "summary": "rollback dry-run 失败，已拒绝执行",
        }
        if incident_id:
            result["timeline_event_id"] = await _record_rollback_event(
                incident_id,
                event_type="rollback_failed",
                action=action,
                rollback=result,
                approver_id=approver_id,
                approval_id=approval_id,
            )
        return result

    lock_key = str(rollback["resource_key"])
    lock_session_id = session_id or f"remediation-rollback:{approval_id or uuid.uuid4()}"
    acquired = await operation_lock.acquire_lock(lock_key, lock_session_id, lock_ttl_seconds)
    if not acquired:
        audit_id = await _record_rollback_audit(
            rollback=rollback,
            status="lock_busy",
            dry_run_result=dry_run_result,
            execution_result=None,
            approver_id=approver_id,
            approval_id=approval_id,
            incident_id=incident_id,
        )
        result = {
            **rollback,
            "ok": False,
            "status": "lock_busy",
            "reason_code": "operation_locked",
            "dry_run": dry_run_result,
            "audit_id": audit_id,
            "summary": "rollback 资源锁被占用，已拒绝执行",
        }
        if incident_id:
            result["timeline_event_id"] = await _record_rollback_event(
                incident_id,
                event_type="rollback_failed",
                action=action,
                rollback=result,
                approver_id=approver_id,
                approval_id=approval_id,
            )
        return result

    started_audit_id: int | None = None
    started_timeline_event_id: int | None = None
    try:
        started_audit_id = await _record_rollback_audit(
            rollback=rollback,
            status="started",
            dry_run_result=dry_run_result,
            execution_result=None,
            approver_id=approver_id,
            approval_id=approval_id,
            incident_id=incident_id,
        )
        if incident_id:
            started_timeline_event_id = await _record_rollback_event(
                incident_id,
                event_type="rollback_started",
                action=action,
                rollback={
                    **rollback,
                    "dry_run": dry_run_result,
                    "audit_id": started_audit_id,
                    "summary": "rollback dry-run 通过，已获得资源锁",
                },
                approver_id=approver_id,
                approval_id=approval_id,
            )

        try:
            execution = await execute_approved(str(rollback["command"]), rollback.get("kube_context"))
        except Exception as exc:  # pragma: no cover - defensive adapter boundary
            execution = {
                "ok": False,
                "exit_code": None,
                "stdout": "",
                "stderr": str(exc),
            }
    finally:
        await operation_lock.release_lock(lock_key, lock_session_id)

    ok = bool(execution.get("ok"))
    status = "succeeded" if ok else "execution_failed"
    audit_id = await _record_rollback_audit(
        rollback=rollback,
        status=status,
        dry_run_result=dry_run_result,
        execution_result=execution,
        approver_id=approver_id,
        approval_id=approval_id,
        incident_id=incident_id,
    )
    result = {
        "ok": ok,
        "status": status,
        "reason_code": None if ok else "rollback_execution_failed",
        "action_type": rollback["action_type"],
        "action_signature": rollback["action_signature"],
        "rollback_action": rollback["rollback_action"],
        "command_preview": rollback["command_preview"],
        "dry_run_command_preview": rollback["dry_run_command_preview"],
        "context": rollback.get("context"),
        "resource_key": lock_key,
        "dry_run": dry_run_result,
        "execution": execution,
        "started_audit_id": started_audit_id,
        "audit_id": audit_id,
        "started_timeline_event_id": started_timeline_event_id,
        "summary": "rollback 执行成功" if ok else "rollback 执行失败",
    }
    if incident_id:
        await _record_rollback_event(
            incident_id,
            event_type="rollback_executed" if ok else "rollback_failed",
            action=action,
            rollback=result,
            approver_id=approver_id,
            approval_id=approval_id,
        )
    return result


async def rollback_execution(
    execution: dict[str, Any] | str,
    *,
    approver_id: str | None = None,
) -> dict[str, Any]:
    """Compatibility API for future execution-store integration."""

    if not isinstance(execution, dict):
        return {
            "ok": False,
            "reason_code": "execution_store_unavailable",
            "summary": "当前版本没有 execution_id 查询存储，请传入 execution dict",
        }
    action = execution.get("action") or execution.get("remediation_action")
    if not isinstance(action, dict):
        return {
            "ok": False,
            "reason_code": "execution_action_missing",
            "summary": "execution 缺少 remediation action",
        }
    incident_id = execution.get("incident_id")
    approval_id = execution.get("approval_id")
    return await execute_rollback(
        action,
        incident_id=str(incident_id) if incident_id else None,
        approver_id=approver_id,
        approval_id=str(approval_id) if approval_id else None,
    )


async def _record_rollback_event(
    incident_id: str,
    *,
    event_type: str,
    action: dict[str, Any],
    rollback: dict[str, Any],
    approver_id: str | None,
    approval_id: str | None,
) -> int:
    return await incident_store.add_event(
        incident_id,
        event_type,
        "remediation_rollback",
        str(action.get("action_signature") or action.get("action_type") or "unknown_action"),
        str(rollback.get("summary") or rollback.get("reason_code") or "rollback result"),
        {
            "action_type": action.get("action_type"),
            "rollback": _compact_rollback(rollback),
            "approver_id": approver_id,
            "approval_id": approval_id,
        },
    )


def _previous_replicas(action: dict[str, Any]) -> int | None:
    before = action.get("before")
    if isinstance(before, dict) and "replicas" in before:
        return before["replicas"]
    rollback = action.get("rollback")
    if isinstance(rollback, dict):
        rollback_before = rollback.get("before")
        if isinstance(rollback_before, dict) and "replicas" in rollback_before:
            return rollback_before["replicas"]
    return None


def _rollback_scale_action(action: dict[str, Any], replicas: int) -> dict[str, Any]:
    source = action.get("source") if isinstance(action.get("source"), dict) else {}
    rollback_action = {
        "action_schema_version": action.get("action_schema_version"),
        "action_type": "scale_deployment",
        "cluster": action["cluster"],
        "namespace": action["namespace"],
        "resource_kind": "deployment",
        "resource_name": action["resource_name"],
        "parameters": {"replicas": replicas},
        "risk": action.get("risk"),
        "source": {
            **source,
            "rollback_of_action_signature": action.get("action_signature"),
        },
    }
    for key in ("kube_context", "context"):
        if action.get(key):
            rollback_action[key] = action[key]
    rollback_action["action_signature"] = _scale_signature(rollback_action)
    return rollback_action


def _compact_rollback(rollback: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": rollback.get("ok"),
        "reason_code": rollback.get("reason_code"),
        "status": rollback.get("status"),
        "summary": rollback.get("summary"),
        "command_preview": rollback.get("command_preview"),
        "dry_run_command_preview": rollback.get("dry_run_command_preview"),
        "cluster": rollback.get("cluster"),
        "context": rollback.get("context"),
        "kube_context": rollback.get("kube_context"),
        "resource_key": rollback.get("resource_key"),
        "rollback_action": rollback.get("rollback_action"),
        "audit_id": rollback.get("audit_id"),
    }


async def _record_rollback_audit(
    *,
    rollback: dict[str, Any],
    status: str,
    dry_run_result: dict[str, Any] | None,
    execution_result: dict[str, Any] | None,
    approver_id: str | None,
    approval_id: str | None,
    incident_id: str | None,
) -> int:
    rollback_action = rollback.get("rollback_action") or {}
    return await audit_log.record_audit(
        who=approver_id or "remediation_rollback",
        what=f"remediation_rollback:{rollback.get('action_type')}:{status}",
        cluster=rollback_action.get("cluster"),
        namespace=rollback_action.get("namespace"),
        trigger="rollback_required",
        tool_level="k8s_write",
        tool_name="remediation_rollback",
        result=_json_dumps(
            {
                "status": status,
                "approval_id": approval_id,
                "rollback": _compact_rollback(rollback),
                "execution": execution_result,
            }
        ),
        dry_run=_json_dumps(dry_run_result) if dry_run_result is not None else None,
        approval_by=approver_id,
        rollback=True,
        incident_id=incident_id,
    )


def _refused(
    action: dict[str, Any] | None,
    reason_code: str,
    summary: str,
    **extra: Any,
) -> dict[str, Any]:
    result = {
        "ok": False,
        "action_type": action.get("action_type") if isinstance(action, dict) else None,
        "reason_code": reason_code,
        "summary": summary,
        "rollback_action": extra.pop("rollback_action", None),
    }
    result.update(extra)
    return result


def _scale_signature(action: dict[str, Any]) -> str:
    return (
        f"scale_deployment:{action['cluster']}:{action['namespace']}:"
        f"deployment/{action['resource_name']}:replicas={action['parameters']['replicas']}"
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
