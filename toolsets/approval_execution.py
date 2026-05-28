"""Approved approval execution coordinator.

This module owns execution idempotency and status transitions. Execution
details are delegated to an adapter so dry-run, locks, audit, health, and
notification can land independently without parsing approval command text.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from typing import Any, Dict, Protocol

try:
    from toolsets import approval_async, remediation_execution
except ImportError:  # pragma: no cover - direct file loading fallback
    import approval_async  # type: ignore
    import remediation_execution  # type: ignore


ACTION_SCHEMA_VERSION = "remediation.action.v1"
ALLOWED_ACTION_TYPES = {"scale_deployment", "restart_deployment"}
TERMINAL_STATUSES = {
    "succeeded",
    "failed",
    "dry_run_failed",
    "rollback_required",
    "rolled_back",
    "cancelled",
}
MAX_REPLICAS = 20
_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


class ApprovalExecutionAdapter(Protocol):
    """Stable adapter interface for later safe execution stages."""

    async def dry_run_action(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> Dict[str, Any]:
        ...

    async def acquire_lock(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> Dict[str, Any]:
        ...

    async def execute_action(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> Dict[str, Any]:
        ...

    async def record_audit(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        ...

    async def check_health(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        ...

    async def append_timeline(
        self,
        event_type: str,
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> None:
        ...

    async def notify(
        self,
        event_type: str,
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> None:
        ...

    async def release_lock(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> None:
        ...


class NoopExecutionAdapter:
    """Fail-closed placeholder until dry-run/safe execution modules are connected."""

    async def dry_run_action(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "ok": False,
            "reason_code": "no_execution_adapter",
            "message": "execution_adapter_missing",
            "stage": "dry_run",
        }

    async def acquire_lock(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {"ok": False, "reason_code": "no_execution_adapter", "message": "execution_adapter_missing"}

    async def execute_action(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {"ok": False, "reason_code": "no_execution_adapter", "message": "execution_adapter_missing"}

    async def record_audit(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {"ok": False, "reason_code": "no_execution_adapter", "message": "execution_adapter_missing"}

    async def check_health(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {"ok": False, "reason_code": "no_execution_adapter", "message": "execution_adapter_missing"}

    async def append_timeline(
        self,
        event_type: str,
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> None:
        return None

    async def notify(
        self,
        event_type: str,
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> None:
        return None

    async def release_lock(
        self,
        action: Dict[str, Any],
        approval: Dict[str, Any],
        execution: Dict[str, Any],
    ) -> None:
        return None


DEFAULT_ADAPTER = NoopExecutionAdapter()


async def process_pending_executions(
    limit: int = 10,
    adapter: ApprovalExecutionAdapter | None = None,
    approved_after: float | None = None,
) -> Dict[str, Any]:
    """Process approved approvals that do not yet have execution records."""
    approval_ids = await asyncio.to_thread(
        _STORE.list_approved_without_execution,
        limit,
        approved_after=approved_after,
    )
    results = [await process_approval_execution(approval_id, adapter=adapter) for approval_id in approval_ids]
    return {"ok": True, "processed": len(results), "results": results}


async def process_approval_execution(
    approval_id: str,
    adapter: ApprovalExecutionAdapter | None = None,
) -> Dict[str, Any]:
    """Process one approved executable approval idempotently."""
    approval = await approval_async.check_approval(approval_id)
    if not approval.get("found"):
        return _rejected(approval_id, "approval_not_found", approval.get("message"))

    existing = await asyncio.to_thread(_STORE.get_execution, approval_id)
    if existing is not None:
        validation = _validate_approval_context(approval)
        mismatch = _existing_signature_mismatch(existing, approval, validation=validation)
        if mismatch:
            return _rejected(approval_id, "action_signature_mismatch", mismatch, execution=existing)
        if existing["status"] in TERMINAL_STATUSES:
            selected_adapter = adapter if not _is_missing_execution_adapter(adapter) else None
            if selected_adapter is not None:
                await _safe_notify_terminal_if_needed(selected_adapter, approval, existing)
            return _existing_terminal_result(approval_id, existing)
        if approval.get("status") != "approved":
            return _rejected(
                approval_id,
                "approval_not_approved",
                f"current approval status is {approval.get('status')}",
                execution=existing,
            )
        if existing["status"] != "queued":
            return _rejected(approval_id, "already_processing", "execution is already in progress", execution=existing)
        if not validation["ok"]:
            failed = await asyncio.to_thread(
                _STORE.update_execution,
                approval_id,
                validation["status"],
                error_message=validation["message"],
            )
            return _rejected(
                approval_id,
                validation["reason_code"],
                validation["message"],
                execution=failed,
            )
        action = validation["action"]
        execution = existing
    else:
        if approval.get("status") != "approved":
            return _rejected(
                approval_id,
                "approval_not_approved",
                f"current approval status is {approval.get('status')}",
            )

        validation = _validate_approval_context(approval)
        if not validation["ok"]:
            failed = await asyncio.to_thread(
                _STORE.create_execution,
                approval,
                validation.get("action") or {},
                validation["status"],
                validation["message"],
            )
            return _rejected(
                approval_id,
                validation["reason_code"],
                validation["message"],
                execution=failed,
            )

        action = validation["action"]
        execution = await asyncio.to_thread(_STORE.create_execution, approval, action, "queued", None)

    claimed, claimed_execution = await asyncio.to_thread(_STORE.claim_queued_execution, approval_id)
    if claimed_execution is None:
        return _rejected(approval_id, "execution_claim_failed", "execution record disappeared before claim")
    execution = claimed_execution
    if not claimed:
        return _existing_nonclaim_result(approval_id, execution)

    if _is_missing_execution_adapter(adapter):
        return await _finish_missing_execution_adapter(approval)

    selected_adapter = adapter
    return await _run_execution_pipeline(selected_adapter, approval, action, execution)


async def check_execution(approval_id: str) -> Dict[str, Any] | None:
    """Return persisted execution record for an approval."""
    return await asyncio.to_thread(_STORE.get_execution, approval_id)


class _ExecutionStore:
    """Small persistence layer over approval_async's SQLite connection."""

    def get_execution(self, approval_id: str) -> Dict[str, Any] | None:
        self._ensure_schema()
        with approval_async._DB._lock:
            row = approval_async._DB._conn.execute(
                "SELECT * FROM approval_executions WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return _execution_from_row(row) if row is not None else None

    def list_approved_without_execution(self, limit: int, *, approved_after: float | None = None) -> list[str]:
        self._ensure_schema()
        safe_limit = max(1, min(int(limit or 1), 100))
        params: list[Any] = []
        cutoff_sql = ""
        if approved_after is not None:
            cutoff_sql = "AND COALESCE(a.decided_at, a.created_at) >= ?"
            params.append(float(approved_after))
        params.append(safe_limit)
        with approval_async._DB._lock:
            rows = approval_async._DB._conn.execute(
                f"""
                SELECT a.id
                FROM approvals a
                LEFT JOIN approval_executions e ON e.approval_id = a.id
                WHERE a.status = 'approved'
                  AND (e.approval_id IS NULL OR e.status = 'queued')
                  {cutoff_sql}
                ORDER BY COALESCE(a.decided_at, a.created_at) ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def create_execution(
        self,
        approval: Dict[str, Any],
        action: Dict[str, Any],
        status: str,
        error_message: str | None,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        now = time.time()
        fields = _execution_fields(approval, action)
        completed_at = now if status in TERMINAL_STATUSES else None

        def _write(conn: sqlite3.Connection) -> Dict[str, Any]:
            conn.execute(
                """
                INSERT OR IGNORE INTO approval_executions (
                    id, approval_id, incident_id, action_signature,
                    action_schema_version, action_type, cluster, namespace,
                    resource_kind, resource_name, status, dry_run_result_json,
                    lock_key, audit_id, health_result_json, rollback_result_json,
                    error_message, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"exec-{approval['approval_id']}",
                    approval["approval_id"],
                    approval.get("incident_id"),
                    fields["action_signature"],
                    fields["action_schema_version"],
                    fields["action_type"],
                    fields["cluster"],
                    fields["namespace"],
                    fields["resource_kind"],
                    fields["resource_name"],
                    status,
                    None,
                    None,
                    None,
                    None,
                    None,
                    error_message,
                    now,
                    now,
                    completed_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM approval_executions WHERE approval_id = ?",
                (approval["approval_id"],),
            ).fetchone()
            return _execution_from_row(row)

        return approval_async._DB._execute_write(_write)

    def claim_queued_execution(self, approval_id: str) -> tuple[bool, Dict[str, Any] | None]:
        """Atomically claim a queued execution for one coordinator owner."""
        self._ensure_schema()
        now = time.time()

        def _write(conn: sqlite3.Connection) -> tuple[bool, Dict[str, Any] | None]:
            current = conn.execute(
                "SELECT * FROM approval_executions WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if current is None:
                return False, None
            if current["status"] != "queued":
                return False, _execution_from_row(current)

            cursor = conn.execute(
                """
                UPDATE approval_executions
                SET status = ?, updated_at = ?
                WHERE approval_id = ? AND status = 'queued'
                """,
                ("running", now, approval_id),
            )
            row = conn.execute(
                "SELECT * FROM approval_executions WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            return cursor.rowcount == 1, _execution_from_row(row)

        return approval_async._DB._execute_write(_write)

    def update_execution(
        self,
        approval_id: str,
        status: str,
        *,
        dry_run_result: Dict[str, Any] | None = None,
        lock_key: str | None = None,
        audit_id: int | None = None,
        health_result: Dict[str, Any] | None = None,
        rollback_result: Dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> Dict[str, Any]:
        self._ensure_schema()
        now = time.time()
        completed_at = now if status in TERMINAL_STATUSES else None

        def _write(conn: sqlite3.Connection) -> Dict[str, Any]:
            current = conn.execute(
                "SELECT * FROM approval_executions WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if current is None:
                raise ValueError(f"execution not found for approval: {approval_id}")

            values = {
                "status": status,
                "updated_at": now,
                "completed_at": completed_at,
                "dry_run_result_json": _json_or_existing(dry_run_result, current["dry_run_result_json"]),
                "lock_key": lock_key if lock_key is not None else current["lock_key"],
                "audit_id": audit_id if audit_id is not None else current["audit_id"],
                "health_result_json": _json_or_existing(health_result, current["health_result_json"]),
                "rollback_result_json": _json_or_existing(rollback_result, current["rollback_result_json"]),
                "error_message": error_message if error_message is not None else current["error_message"],
            }
            conn.execute(
                """
                UPDATE approval_executions
                SET status = ?, updated_at = ?, completed_at = ?,
                    dry_run_result_json = ?, lock_key = ?, audit_id = ?,
                    health_result_json = ?, rollback_result_json = ?,
                    error_message = ?
                WHERE approval_id = ?
                """,
                (
                    values["status"],
                    values["updated_at"],
                    values["completed_at"],
                    values["dry_run_result_json"],
                    values["lock_key"],
                    values["audit_id"],
                    values["health_result_json"],
                    values["rollback_result_json"],
                    values["error_message"],
                    approval_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM approval_executions WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            return _execution_from_row(row)

        return approval_async._DB._execute_write(_write)

    def _ensure_schema(self) -> None:
        with approval_async._DB._lock:
            approval_async._DB._conn.executescript(approval_async.SCHEMA_SQL)


_STORE = _ExecutionStore()


async def _run_execution_pipeline(
    adapter: ApprovalExecutionAdapter,
    approval: Dict[str, Any],
    action: Dict[str, Any],
    execution: Dict[str, Any],
) -> Dict[str, Any]:
    approval_id = approval["approval_id"]
    locked = False

    try:
        await _safe_append_timeline(adapter, "approval_execution_queued", approval, execution)

        execution = await asyncio.to_thread(_STORE.update_execution, approval_id, "dry_run_running")
        dry_run = await adapter.dry_run_action(action, approval, execution)
        if not dry_run.get("ok"):
            return await _finish_failed(
                adapter,
                approval,
                "dry_run_failed",
                "dry_run_failed",
                execution,
                dry_run_result=dry_run,
                error_message=_adapter_error(dry_run, "dry-run failed"),
            )

        execution = await asyncio.to_thread(
            _STORE.update_execution,
            approval_id,
            "lock_waiting",
            dry_run_result=dry_run,
        )
        lock_result = await adapter.acquire_lock(action, approval, execution)
        if not lock_result.get("ok"):
            return await _finish_failed(
                adapter,
                approval,
                "failed",
                "lock_not_acquired",
                execution,
                error_message=_adapter_error(lock_result, "lock not acquired"),
            )
        locked = True
        lock_key = str(lock_result.get("lock_key") or _lock_key(action))

        execution = await asyncio.to_thread(
            _STORE.update_execution,
            approval_id,
            "executing",
            lock_key=lock_key,
        )
        execute_result = await adapter.execute_action(action, approval, execution)
        if not execute_result.get("ok"):
            return await _finish_failed(
                adapter,
                approval,
                "failed",
                "execution_failed",
                execution,
                error_message=_adapter_error(execute_result, "execution failed"),
            )

        audit_result = await adapter.record_audit(action, approval, execution, execute_result)
        audit_id = audit_result.get("audit_id") if isinstance(audit_result, dict) else None
        execution = await asyncio.to_thread(
            _STORE.update_execution,
            approval_id,
            "health_checking",
            audit_id=audit_id if isinstance(audit_id, int) else None,
        )
        health_result = await adapter.check_health(action, approval, execution, execute_result)
        if not health_result.get("ok"):
            execution = await asyncio.to_thread(
                _STORE.update_execution,
                approval_id,
                "rollback_required",
                health_result=health_result,
                error_message=_adapter_error(health_result, "health check failed"),
            )
            await _safe_append_timeline(adapter, "approval_execution_rollback_required", approval, execution)
            await _safe_notify(adapter, "approval_execution_rollback_required", approval, execution)
            return {
                "ok": False,
                "approval_id": approval_id,
                "status": "rollback_required",
                "reason_code": "health_check_failed",
                "execution": execution,
            }

        mark_result = await approval_async.execute_approved(approval_id)
        if not mark_result.get("ok"):
            return await _finish_failed(
                adapter,
                approval,
                "failed",
                "approval_mark_executed_failed",
                execution,
                health_result=health_result,
                error_message=str(mark_result.get("message") or "approval mark executed failed"),
            )

        execution = await asyncio.to_thread(
            _STORE.update_execution,
            approval_id,
            "succeeded",
            health_result=health_result,
        )
        await _safe_append_timeline(adapter, "approval_execution_succeeded", approval, execution)
        await _safe_notify(adapter, "approval_execution_succeeded", approval, execution)
        return {"ok": True, "approval_id": approval_id, "status": "succeeded", "execution": execution}
    except Exception as exc:
        execution = await asyncio.to_thread(
            _STORE.update_execution,
            approval_id,
            "failed",
            error_message=str(exc),
        )
        await _safe_append_timeline(adapter, "approval_execution_failed", approval, execution)
        await _safe_notify(adapter, "approval_execution_failed", approval, execution)
        return {
            "ok": False,
            "approval_id": approval_id,
            "status": "failed",
            "reason_code": "coordinator_exception",
            "message": str(exc),
            "execution": execution,
        }
    finally:
        if locked:
            await _safe_release_lock(adapter, action, approval, execution)


async def _finish_failed(
    adapter: ApprovalExecutionAdapter,
    approval: Dict[str, Any],
    status: str,
    reason_code: str,
    execution: Dict[str, Any],
    *,
    dry_run_result: Dict[str, Any] | None = None,
    health_result: Dict[str, Any] | None = None,
    error_message: str,
) -> Dict[str, Any]:
    updated = await asyncio.to_thread(
        _STORE.update_execution,
        approval["approval_id"],
        status,
        dry_run_result=dry_run_result,
        health_result=health_result,
        error_message=error_message,
    )
    await _safe_append_timeline(adapter, f"approval_execution_{status}", approval, updated)
    await _safe_notify(adapter, f"approval_execution_{status}", approval, updated)
    return {
        "ok": False,
        "approval_id": approval["approval_id"],
        "status": status,
        "reason_code": reason_code,
        "message": error_message,
        "execution": updated,
    }


async def _finish_missing_execution_adapter(
    approval: Dict[str, Any],
) -> Dict[str, Any]:
    updated = await asyncio.to_thread(
        _STORE.update_execution,
        approval["approval_id"],
        "failed",
        error_message="execution_adapter_missing",
    )
    return {
        "ok": False,
        "approval_id": approval["approval_id"],
        "status": "failed",
        "reason_code": "no_execution_adapter",
        "message": "execution_adapter_missing",
        "execution": updated,
    }


def _is_missing_execution_adapter(adapter: ApprovalExecutionAdapter | None) -> bool:
    return adapter is None or isinstance(adapter, NoopExecutionAdapter)


def _existing_terminal_result(approval_id: str, execution: Dict[str, Any]) -> Dict[str, Any]:
    reason_code = "already_executed" if execution["status"] == "succeeded" else "already_finished"
    return {
        "ok": execution["status"] == "succeeded",
        "approval_id": approval_id,
        "status": execution["status"],
        "reason_code": reason_code,
        "execution": execution,
    }


def _existing_nonclaim_result(approval_id: str, execution: Dict[str, Any]) -> Dict[str, Any]:
    if execution["status"] in TERMINAL_STATUSES:
        return _existing_terminal_result(approval_id, execution)
    return _rejected(
        approval_id,
        "already_processing",
        "execution is already in progress",
        execution=execution,
    )


def _validate_approval_context(approval: Dict[str, Any]) -> Dict[str, Any]:
    missing = _missing_approval_fields(approval)
    if missing:
        return _invalid("failed", "approval_context_incomplete", f"missing fields: {', '.join(missing)}")

    context = approval.get("context")
    if not isinstance(context, dict):
        return _invalid("failed", "approval_context_incomplete", "approval context must be an object")

    if not context.get("action_signature"):
        return _invalid("failed", "approval_context_incomplete", "missing context action_signature")

    if "executable" not in context:
        return _invalid("failed", "approval_context_incomplete", "missing context executable flag")

    if context.get("executable") is not True:
        return _invalid(
            "cancelled",
            "ignored_non_executable",
            str(context.get("non_executable_reason") or "approval context is not executable"),
        )

    action = context.get("remediation_action")
    if not isinstance(action, dict):
        return _invalid("failed", "approval_context_incomplete", "missing remediation_action")

    action_missing = _missing_action_fields(action)
    if action_missing:
        return _invalid(
            "failed",
            "approval_context_incomplete",
            f"missing action fields: {', '.join(action_missing)}",
            action=action,
        )

    if action.get("action_signature") != context.get("action_signature"):
        return _invalid("failed", "action_signature_mismatch", "action signature mismatch", action=action)

    if action.get("namespace") != approval.get("namespace"):
        return _invalid("failed", "approval_context_incomplete", "approval namespace/action namespace mismatch", action=action)

    if action.get("action_schema_version") != ACTION_SCHEMA_VERSION:
        return _invalid("failed", "unsupported_action_schema", "unsupported action schema version", action=action)

    if action.get("action_type") not in ALLOWED_ACTION_TYPES:
        return _invalid("failed", "unsupported_action_type", "action type is not allowlisted", action=action)

    if approval.get("operation_type") != "k8s_write":
        return _invalid("failed", "unsupported_operation_type", "only k8s_write is supported", action=action)

    risk = action.get("risk") if isinstance(action.get("risk"), dict) else {}
    if risk.get("risk_level") != "low" or approval.get("risk_level") != "low":
        return _invalid("failed", "unsupported_risk_level", "only low-risk actions are supported", action=action)

    if action.get("resource_kind") != "deployment":
        return _invalid("failed", "unsupported_resource_kind", "only deployment resources are supported", action=action)

    if not _valid_dns_label(str(action.get("namespace") or "")):
        return _invalid("failed", "invalid_namespace", "namespace must be a DNS label", action=action)

    if not _valid_dns_label(str(action.get("resource_name") or "")):
        return _invalid("failed", "invalid_resource_name", "resource_name must be a DNS label", action=action)

    parameter_error = _validate_parameters(action)
    if parameter_error:
        return _invalid("failed", parameter_error, parameter_error, action=action)

    remediation_validation = remediation_execution.validate_remediation_action(
        action,
        max_replicas=MAX_REPLICAS,
    )
    if not remediation_validation.get("ok"):
        reason_code = str(remediation_validation.get("reason_code") or "invalid_remediation_action")
        if reason_code == "signature_mismatch":
            reason_code = "action_signature_mismatch"
        message = str(
            remediation_validation.get("summary")
            or remediation_validation.get("message")
            or reason_code
        )
        return _invalid("failed", reason_code, message, action=action)

    return {"ok": True, "action": remediation_validation["action"]}


def _missing_approval_fields(approval: Dict[str, Any]) -> list[str]:
    missing = []
    for field in (
        "approval_id",
        "status",
        "operation_type",
        "namespace",
        "risk_level",
        "requester",
        "approver",
        "context",
        "incident_id",
    ):
        if approval.get(field) in (None, ""):
            missing.append(field)
    return missing


def _missing_action_fields(action: Dict[str, Any]) -> list[str]:
    missing = []
    for field in (
        "action_schema_version",
        "action_signature",
        "action_type",
        "cluster",
        "namespace",
        "resource_kind",
        "resource_name",
        "parameters",
    ):
        value = action.get(field)
        if value in (None, ""):
            missing.append(field)
    return missing


def _validate_parameters(action: Dict[str, Any]) -> str | None:
    parameters = action.get("parameters")
    if not isinstance(parameters, dict):
        return "invalid_parameters"

    if action.get("action_type") == "scale_deployment":
        replicas = parameters.get("replicas")
        if isinstance(replicas, bool) or not isinstance(replicas, int):
            return "invalid_replicas"
        if replicas < 0 or replicas > MAX_REPLICAS:
            return "invalid_replicas"
        return None

    if action.get("action_type") == "restart_deployment":
        if parameters.get("strategy") != "rollout_restart":
            return "invalid_restart_strategy"
        return None

    return "unsupported_action_type"


def _execution_fields(approval: Dict[str, Any], action: Dict[str, Any]) -> Dict[str, Any]:
    context = approval.get("context") if isinstance(approval.get("context"), dict) else {}
    return {
        "action_signature": str(
            action.get("action_signature")
            or context.get("action_signature")
            or f"invalid:{approval['approval_id']}"
        ),
        "action_schema_version": str(action.get("action_schema_version") or "unknown"),
        "action_type": str(action.get("action_type") or "invalid"),
        "cluster": action.get("cluster") or context.get("cluster"),
        "namespace": str(action.get("namespace") or approval.get("namespace") or context.get("namespace") or "unknown"),
        "resource_kind": str(action.get("resource_kind") or "unknown"),
        "resource_name": str(action.get("resource_name") or "unknown"),
    }


def _execution_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "approval_id": row["approval_id"],
        "incident_id": row["incident_id"],
        "action_signature": row["action_signature"],
        "action_schema_version": row["action_schema_version"],
        "action_type": row["action_type"],
        "cluster": row["cluster"],
        "namespace": row["namespace"],
        "resource_kind": row["resource_kind"],
        "resource_name": row["resource_name"],
        "status": row["status"],
        "dry_run_result": _json_loads(row["dry_run_result_json"]),
        "lock_key": row["lock_key"],
        "audit_id": row["audit_id"],
        "health_result": _json_loads(row["health_result_json"]),
        "rollback_result": _json_loads(row["rollback_result_json"]),
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }


def _invalid(
    status: str,
    reason_code: str,
    message: str,
    *,
    action: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return {
        "ok": False,
        "status": status,
        "reason_code": reason_code,
        "message": message,
        "action": action,
    }


def _rejected(
    approval_id: str,
    reason_code: str,
    message: str | None,
    *,
    execution: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    result = {
        "ok": False,
        "approval_id": approval_id,
        "status": execution["status"] if execution else "rejected",
        "reason_code": reason_code,
        "message": message or reason_code,
    }
    if execution is not None:
        result["execution"] = execution
    return result


def _existing_signature_mismatch(
    existing: Dict[str, Any],
    approval: Dict[str, Any],
    *,
    validation: Dict[str, Any] | None = None,
) -> str | None:
    context = approval.get("context") if isinstance(approval.get("context"), dict) else {}
    validation = validation or _validate_approval_context(approval)
    if not validation.get("ok") and validation.get("reason_code") == "action_signature_mismatch":
        return str(validation.get("message") or "action signature mismatch")
    action = validation.get("action") if isinstance(validation.get("action"), dict) else {}
    signature = action.get("action_signature") or context.get("action_signature")
    if signature and existing.get("action_signature") != signature:
        return "persisted execution action_signature differs from current approval context"
    return None


def _adapter_error(result: Dict[str, Any], default: str) -> str:
    return str(result.get("error") or result.get("message") or result.get("reason_code") or default)


def _json_or_existing(value: Dict[str, Any] | None, existing: str | None) -> str | None:
    if value is None:
        return existing
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _valid_dns_label(value: str) -> bool:
    return bool(value) and len(value) <= 63 and bool(_DNS_LABEL_RE.match(value))


def _lock_key(action: Dict[str, Any]) -> str:
    return ":".join(
        str(part or "")
        for part in (
            action.get("cluster"),
            action.get("namespace"),
            action.get("resource_kind"),
            action.get("resource_name"),
        )
    )


async def _safe_append_timeline(
    adapter: ApprovalExecutionAdapter,
    event_type: str,
    approval: Dict[str, Any],
    execution: Dict[str, Any],
) -> None:
    try:
        await adapter.append_timeline(event_type, approval, execution)
    except Exception:
        return None


async def _safe_notify(
    adapter: ApprovalExecutionAdapter,
    event_type: str,
    approval: Dict[str, Any],
    execution: Dict[str, Any],
) -> None:
    try:
        await adapter.notify(event_type, approval, execution)
    except Exception:
        return None


async def _safe_notify_terminal_if_needed(
    adapter: ApprovalExecutionAdapter,
    approval: Dict[str, Any],
    execution: Dict[str, Any],
) -> None:
    status = str(execution.get("status") or "")
    if status not in {"succeeded", "failed", "dry_run_failed"}:
        return None
    await _safe_notify(adapter, f"approval_execution_{status}", approval, execution)


async def _safe_release_lock(
    adapter: ApprovalExecutionAdapter,
    action: Dict[str, Any],
    approval: Dict[str, Any],
    execution: Dict[str, Any],
) -> None:
    try:
        await adapter.release_lock(action, approval, execution)
    except Exception:
        return None
