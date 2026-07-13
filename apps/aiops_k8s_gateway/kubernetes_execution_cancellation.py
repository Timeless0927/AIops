"""Durable cancellation of a Kubernetes Plan before or during its active step."""

from __future__ import annotations

from typing import Any

from .gateway_db import GatewayDatabase, insert_admin_audit
from .kubernetes_execution_codec import canonical_digest as _digest, canonical_json as _json


class KubernetesExecutionCancellationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def cancel_execution(
    database: GatewayDatabase,
    *,
    approvals: Any,
    phases: Any,
    change_request_id: str,
    phase_id: str,
    actor_id: str,
    reason: str,
    idempotency_key: str,
    request_id: str,
    now: float,
) -> tuple[str, bool]:
    request_hash = _digest({
        "change_request_id": change_request_id, "phase_id": phase_id,
        "actor_id": actor_id, "reason": reason,
    })
    with database.connect() as conn:
        replay = conn.execute(
            "SELECT * FROM kubernetes_execution_cancellations "
            "WHERE actor_id = ? AND idempotency_key = ?",
            (actor_id, idempotency_key),
        ).fetchone()
        if replay is not None:
            if replay["request_hash"] != request_hash:
                raise KubernetesExecutionCancellationError(
                    "idempotency_conflict", "Idempotency key was used for another cancellation",
                )
            return str(replay["execution_id"]), True
    approval = approvals.authorize_cancel(phase_id, actor_id=actor_id, request_id=request_id)
    if approval.get("change_request_id") != change_request_id:
        raise KubernetesExecutionCancellationError(
            "phase_stale", "Phase belongs to another Change Request",
        )
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        replay = conn.execute(
            "SELECT * FROM kubernetes_execution_cancellations "
            "WHERE actor_id = ? AND idempotency_key = ?",
            (actor_id, idempotency_key),
        ).fetchone()
        if replay is not None:
            if replay["request_hash"] != request_hash:
                raise KubernetesExecutionCancellationError(
                    "idempotency_conflict", "Idempotency key was used for another cancellation",
                )
            conn.commit()
            return str(replay["execution_id"]), True
        row = conn.execute(
            "SELECT * FROM kubernetes_change_executions WHERE phase_id = ?", (phase_id,),
        ).fetchone()
        if row is None or row["change_request_id"] != change_request_id:
            raise KubernetesExecutionCancellationError("not_found", "Phase execution not found")
        if row["status"] in {
            "succeeded", "failed", "stale", "post_check_failed", "unknown_outcome",
            "cancelled", "rolled_back", "rollback_failed", "rolling_back",
        }:
            raise KubernetesExecutionCancellationError(
                "execution_not_cancellable", "Execution is already terminal or rolling back",
            )
        conn.execute(
            """
            INSERT INTO kubernetes_execution_cancellations (
                execution_id, actor_id, reason, request_id, idempotency_key,
                request_hash, requested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (row["id"], actor_id, reason, request_id, idempotency_key, request_hash, now),
        )
        active_started = conn.execute(
            "SELECT 1 FROM kubernetes_change_execution_steps "
            "WHERE execution_id = ? AND status = 'started'", (row["id"],),
        ).fetchone() is not None
        status = "cancel_requested" if active_started else "cancelled"
        if active_started:
            conn.execute(
                "UPDATE kubernetes_execution_grants SET revoked_at = COALESCE(revoked_at, ?) "
                "WHERE execution_id = ? AND consumed_at IS NULL", (now, row["id"]),
            )
            conn.execute(
                "UPDATE kubernetes_change_execution_steps SET status = 'cancelled', completed_at = ? "
                "WHERE execution_id = ? AND status IN ('pending', 'queued')",
                (now, row["id"]),
            )
        else:
            conn.execute(
                "UPDATE kubernetes_execution_grants SET revoked_at = COALESCE(revoked_at, ?) "
                "WHERE execution_id = ?", (now, row["id"]),
            )
            rejection = _json({
                "status": "rejected", "stdout": "", "stderr": "", "exit_code": None,
                "truncated": False, "error_code": "execution_cancelled",
                "error_message": "execution cancelled before Connector start",
            })
            conn.execute(
                "UPDATE connector_commands SET status = 'rejected', result_json = ?, "
                "result_received_at = ?, updated_at = ? WHERE id IN ("
                "SELECT command_id FROM kubernetes_change_execution_steps "
                "WHERE execution_id = ?) AND status = 'leased'",
                (rejection, now, now, row["id"]),
            )
            conn.execute(
                "UPDATE kubernetes_change_execution_steps SET status = 'cancelled', completed_at = ? "
                "WHERE execution_id = ? AND status IN ('pending', 'queued', 'dispatched')",
                (now, row["id"]),
            )
        conn.execute(
            "UPDATE kubernetes_change_executions SET status = ?, cancel_requested_at = ?, "
            "cancelled_at = ?, completed_at = ? WHERE id = ?",
            (
                status, now, now if status == "cancelled" else None,
                now if status == "cancelled" else None, row["id"],
            ),
        )
        phases.record_cancel_in(
            conn, change_request_id=change_request_id, phase_id=phase_id,
            execution_id=str(row["id"]), actor_id=actor_id, reason=reason,
            status=status, request_id=request_id, now=now,
        )
        insert_admin_audit(
            conn, actor_id=actor_id, target_type="kubernetes_change_executions",
            target_id=str(row["id"]), action="kubernetes_change_execution_cancel",
            reason=reason, before={"status": row["status"]}, after={"status": status},
            result="success", request_id=request_id,
        )
        conn.commit()
        return str(row["id"]), False
