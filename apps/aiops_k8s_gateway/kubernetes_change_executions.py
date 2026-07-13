"""Gateway owner for executing one frozen generic Kubernetes Change."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from typing import Any

from aiops.domain.identity import IdentityError

from . import kubernetes_change_execution_schema as _schema
from .change_plan_phases import ChangePlanPhases
from .connector_enrollments import ConnectorEnrollments
from .gateway_db import GatewayDatabase, insert_admin_audit
from .kubernetes_phase_approvals import KubernetesPhaseApprovalError


_schema.register_plan_execution_migrations()


class KubernetesChangeExecutionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class KubernetesChangeExecutions:
    """Issues single-use grants and projects one frozen Change execution."""

    def __init__(
        self,
        database: GatewayDatabase,
        *,
        approvals: Any,
        enrollments: ConnectorEnrollments,
        phases: ChangePlanPhases | None = None,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._database = database
        self._approvals = approvals
        self._enrollments = enrollments
        self._phases = phases or ChangePlanPhases()
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")

    def start(
        self,
        change_request_id: str,
        *,
        phase_id: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
        request_id: str,
        execution_timeout_seconds: object = 300,
    ) -> dict[str, object]:
        timeout = _execution_timeout(execution_timeout_seconds)
        change_request_id = _text(change_request_id, "change_request_id")
        phase_id = _text(phase_id, "phase_id")
        actor_id = _text(actor_id, "actor_id")
        reason = _text(reason, "reason")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        request_hash = _digest({
            "change_request_id": change_request_id,
            "phase_id": phase_id,
            "actor_id": actor_id,
            "reason": reason,
            "execution_timeout_seconds": timeout,
        })
        now = self._clock()
        with self._database.connect() as conn:
            replay = conn.execute(
                "SELECT * FROM kubernetes_change_executions WHERE actor_id = ? AND idempotency_key = ?",
                (actor_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if str(replay["request_hash"]) != request_hash:
                    raise KubernetesChangeExecutionError(
                        "idempotency_conflict", "Idempotency key was used for another execution",
                    )
                return self._record_in(conn, replay, idempotent=True)

        try:
            approval = self._approvals.authorize_start(
                phase_id, request_id=request_id, stage="grant",
            )
        except KubernetesPhaseApprovalError as exc:
            raise KubernetesChangeExecutionError(exc.code, exc.message) from exc
        if approval.get("change_request_id") != change_request_id:
            raise KubernetesChangeExecutionError("phase_stale", "Phase belongs to another Change Request")
        if approval.get("approver_id") != actor_id:
            raise KubernetesChangeExecutionError("approval_actor_mismatch", "Only the approver may start this Phase")
        change, change_hash = _single_frozen_change(approval)
        cluster_id = _text(approval.get("cluster_id"), "cluster_id")
        namespace = _change_namespace(change)
        execution_id = self._id_factory("kubernetes-execution")
        grant_id = self._id_factory("kubernetes-grant")
        command_id = self._id_factory("command")
        step_id = f"{execution_id}:forward:1"
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = conn.execute(
                "SELECT * FROM kubernetes_change_executions WHERE actor_id = ? AND idempotency_key = ?",
                (actor_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if str(replay["request_hash"]) != request_hash:
                    raise KubernetesChangeExecutionError(
                        "idempotency_conflict", "Idempotency key was used for another execution",
                    )
                conn.commit()
                return self._record_in(conn, replay, idempotent=True)
            if conn.execute(
                "SELECT 1 FROM kubernetes_change_executions WHERE phase_id = ?", (phase_id,),
            ).fetchone() is not None:
                raise KubernetesChangeExecutionError("execution_exists", "Phase already has an execution")
            try:
                connector_id = self._enrollments.execution_connector_in(conn, cluster_id)
            except IdentityError as exc:
                raise KubernetesChangeExecutionError(exc.code, exc.message) from exc
            conn.execute(
                """
                INSERT INTO kubernetes_change_executions (
                    id, change_request_id, phase_id, revision_id, approval_id,
                    connector_id, cluster_id, actor_id, reason, request_id,
                    idempotency_key, request_hash, execution_timeout_seconds,
                    rollback_policy, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                (
                    execution_id, change_request_id, phase_id, approval["revision_id"], approval["id"],
                    connector_id, cluster_id, actor_id, reason, request_id,
                    idempotency_key, request_hash, timeout,
                    approval.get("rollback_policy", "stop_only"), now,
                ),
            )
            conn.execute(
                """
                INSERT INTO kubernetes_change_execution_steps (
                    id, execution_id, ordinal, direction, command_id, change_hash,
                    change_json, status, created_at
                ) VALUES (?, ?, 1, 'forward', ?, ?, ?, 'queued', ?)
                """,
                (step_id, execution_id, command_id, change_hash, _json(change), now),
            )
            conn.execute(
                """
                INSERT INTO kubernetes_execution_grants (
                    id, execution_id, step_id, phase_id, approval_id, command_id,
                    change_hash, issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    grant_id, execution_id, step_id, phase_id, approval["id"],
                    command_id, change_hash, now, now + 60,
                ),
            )
            self._phases.record_execution_queued_in(
                conn, change_request_id=change_request_id, phase_id=phase_id,
                execution_id=execution_id, grant_id=grant_id, actor_id=actor_id,
                request_id=request_id, now=now,
            )
            insert_admin_audit(
                conn, actor_id=actor_id, target_type="kubernetes_change_executions",
                target_id=execution_id, action="kubernetes_change_execution_grant",
                reason=reason, before=None,
                after={"phase_id": phase_id, "grant_id": grant_id, "namespace": namespace},
                result="success", request_id=request_id,
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM kubernetes_change_executions WHERE id = ?", (execution_id,),
            ).fetchone()
            return self._record_in(conn, row, idempotent=False)

    def dispatch_next(
        self, connector_id: str, cluster_id: str, *, request_id: str,
    ) -> dict[str, object] | None:
        connector_id = _text(connector_id, "connector_id")
        cluster_id = _text(cluster_id, "cluster_id")
        now = self._clock()
        self._reconcile_transport_failures(now=now, request_id=request_id)
        with self._database.connect() as conn:
            candidate = conn.execute(
                """
                SELECT execution.*, step.id AS step_id, step.command_id, step.change_hash,
                       step.change_json, grant.id AS grant_id, grant.issued_at, grant.expires_at
                FROM kubernetes_change_executions execution
                JOIN kubernetes_change_execution_steps step ON step.execution_id = execution.id
                JOIN kubernetes_execution_grants grant ON grant.execution_id = execution.id
                WHERE execution.connector_id = ? AND execution.cluster_id = ?
                  AND execution.status = 'queued' AND step.status = 'queued'
                  AND grant.step_id = step.id AND grant.consumed_at IS NULL
                ORDER BY execution.created_at, execution.id LIMIT 1
                """,
                (connector_id, cluster_id),
            ).fetchone()
        if candidate is None:
            return None
        if float(candidate["expires_at"]) <= now:
            self._fail_before_dispatch(candidate, "execution_grant_expired", request_id, now)
            return None
        try:
            approval = self._approvals.authorize_start(
                str(candidate["phase_id"]), request_id=request_id, stage="dispatch",
            )
        except KubernetesPhaseApprovalError as exc:
            self._fail_before_dispatch(candidate, exc.code, request_id, now)
            return None
        _, change_hash = _single_frozen_change(approval)
        if change_hash != candidate["change_hash"] or approval.get("cluster_id") != cluster_id:
            self._fail_before_dispatch(candidate, "phase_stale", request_id, now)
            return None
        change = json.loads(str(candidate["change_json"]))
        namespace = _change_namespace(change)
        lease_id = self._id_factory("lease")
        lease_expires_at = min(now + 30, float(candidate["expires_at"]))
        parameters = {
            "grant": {
                "id": candidate["grant_id"], "phase_id": candidate["phase_id"],
                "approval_id": candidate["approval_id"], "change_hash": change_hash,
                "issued_at": float(candidate["issued_at"]),
                "expires_at": float(candidate["expires_at"]),
                "execution_timeout_seconds": int(candidate["execution_timeout_seconds"]),
            },
            "change": change,
        }
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            consumed = conn.execute(
                """UPDATE kubernetes_execution_grants SET consumed_at = ?
                   WHERE id = ? AND consumed_at IS NULL AND expires_at > ?""",
                (now, candidate["grant_id"], now),
            )
            if consumed.rowcount != 1:
                conn.rollback()
                return None
            conn.execute(
                """
                INSERT INTO connector_commands (
                    id, connector_id, cluster_id, namespace, action, parameters_json,
                    kubernetes_execution_grant_id, execution_grant_expires_at,
                    action_hash, status, lease_id, lease_expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'execute_kubernetes_change', ?, ?, ?, ?,
                          'leased', ?, ?, ?, ?)
                """,
                (
                    candidate["command_id"], connector_id, cluster_id, namespace,
                    _json(parameters), candidate["grant_id"], candidate["expires_at"],
                    change_hash, lease_id, lease_expires_at, now, now,
                ),
            )
            conn.execute(
                "INSERT INTO command_leases (lease_id, command_id, connector_id, granted_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (lease_id, candidate["command_id"], connector_id, now, lease_expires_at),
            )
            conn.execute(
                "UPDATE kubernetes_change_execution_steps SET status = 'dispatched' "
                "WHERE id = ? AND status = 'queued'", (candidate["step_id"],),
            )
            conn.execute(
                "UPDATE kubernetes_change_executions SET status = 'dispatched' "
                "WHERE id = ? AND status = 'queued'", (candidate["id"],),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM connector_commands WHERE id = ?", (candidate["command_id"],),
            ).fetchone()
        return _command_record(row)

    def for_phase(self, phase_id: str) -> dict[str, object] | None:
        with self._database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM kubernetes_change_executions WHERE phase_id = ?", (phase_id,),
            ).fetchone()
            return self._record_in(conn, row, idempotent=False) if row is not None else None

    def record_started_in(self, conn: sqlite3.Connection, command_id: str, now: float) -> None:
        row = conn.execute(
            """SELECT execution.*, step.id AS step_id, step.status AS step_status
               FROM kubernetes_change_executions execution
               JOIN kubernetes_change_execution_steps step ON step.execution_id = execution.id
               WHERE step.command_id = ?""",
            (command_id,),
        ).fetchone()
        if row is None or row["step_status"] == "started":
            return
        updated = conn.execute(
            "UPDATE kubernetes_change_execution_steps SET status = 'started', started_at = ? "
            "WHERE command_id = ? AND status = 'dispatched'", (now, command_id),
        )
        if updated.rowcount != 1:
            raise KubernetesChangeExecutionError("execution_stale", "Execution is no longer startable")
        conn.execute(
            "UPDATE kubernetes_change_executions SET status = 'started', "
            "started_at = COALESCE(started_at, ?) WHERE id = ?",
            (now, row["id"]),
        )
        conn.execute(
            "UPDATE connector_commands SET execution_expires_at = ? WHERE id = ?",
            (now + int(row["execution_timeout_seconds"]), command_id),
        )
        self._phases.record_execution_started_in(
            conn, change_request_id=str(row["change_request_id"]), phase_id=str(row["phase_id"]),
            execution_id=str(row["id"]), command_id=command_id, now=now,
        )

    def record_result_in(
        self, conn: sqlite3.Connection, command_id: str,
        result: dict[str, object], now: float,
    ) -> None:
        row = conn.execute(
            """SELECT execution.*, step.id AS step_id
               FROM kubernetes_change_executions execution
               JOIN kubernetes_change_execution_steps step ON step.execution_id = execution.id
               WHERE step.command_id = ?""",
            (command_id,),
        ).fetchone()
        if row is None:
            return
        error_code = str(result["error_code"]) if result.get("error_code") else None
        outcome = (
            "succeeded" if result["status"] == "succeeded"
            else "stale" if error_code == "stale_change"
            else "post_check_failed" if error_code == "post_check_failed"
            else "failed"
        )
        conn.execute(
            "UPDATE kubernetes_change_execution_steps SET status = ?, result_json = ?, completed_at = ? "
            "WHERE id = ?", (outcome, _json(result), now, row["step_id"]),
        )
        conn.execute(
            "UPDATE kubernetes_change_executions SET status = ?, result_json = ?, completed_at = ? "
            "WHERE id = ?", (outcome, _json(result), now, row["id"]),
        )
        self._phases.record_execution_finished_in(
            conn, change_request_id=str(row["change_request_id"]), phase_id=str(row["phase_id"]),
            execution_id=str(row["id"]), command_id=command_id,
            outcome=outcome, error_code=error_code, now=now,
        )

    def _fail_before_dispatch(
        self, row: Any, code: str, request_id: str, now: float,
    ) -> None:
        result = {"error_code": code, "error_message": code}
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                "UPDATE kubernetes_change_execution_steps SET status = 'failed', "
                "result_json = ?, completed_at = ? WHERE id = ? AND status = 'queued'",
                (_json(result), now, row["step_id"]),
            )
            if updated.rowcount:
                conn.execute(
                    "UPDATE kubernetes_change_executions SET status = 'failed', result_json = ?, "
                    "completed_at = ? WHERE id = ? AND status = 'queued'",
                    (_json(result), now, row["id"]),
                )
                self._phases.record_execution_finished_in(
                    conn, change_request_id=str(row["change_request_id"]), phase_id=str(row["phase_id"]),
                    execution_id=str(row["id"]), command_id=str(row["command_id"]),
                    outcome="failed", error_code=code, now=now,
                )
                insert_admin_audit(
                    conn, actor_id=None, target_type="kubernetes_change_executions",
                    target_id=str(row["id"]), action="kubernetes_change_execution_dispatch",
                    reason="Execution authority was not valid at dispatch",
                    before={"status": "queued"}, after={"status": "failed", "error_code": code},
                    result=code, request_id=request_id,
                )
            conn.commit()

    def _reconcile_transport_failures(self, *, now: float, request_id: str) -> None:
        with self._database.connect() as conn:
            rows = conn.execute(
                """
                SELECT execution.*, step.id AS step_id, step.command_id,
                       step.status AS step_status,
                       command.status AS command_status
                FROM kubernetes_change_executions execution
                JOIN kubernetes_change_execution_steps step ON step.execution_id = execution.id
                JOIN connector_commands command ON command.id = step.command_id
                WHERE (
                    step.status = 'dispatched' AND command.status = 'leased'
                    AND command.lease_expires_at <= ?
                ) OR (
                    step.status = 'started' AND command.status = 'unknown_outcome'
                )
                """,
                (now,),
            ).fetchall()
            if not rows:
                return
            conn.execute("BEGIN IMMEDIATE")
            for row in rows:
                unknown = row["step_status"] == "started"
                code = (
                    "execution_delivery_expired"
                    if not unknown else "execution_outcome_unknown"
                )
                result = {"error_code": code, "error_message": code}
                outcome = "unknown_outcome" if unknown else "failed"
                updated = conn.execute(
                    "UPDATE kubernetes_change_execution_steps SET status = ?, result_json = ?, "
                    "completed_at = ? WHERE id = ? AND status = ?",
                    (outcome, _json(result), now, row["step_id"], row["step_status"]),
                )
                if not updated.rowcount:
                    continue
                conn.execute(
                    "UPDATE kubernetes_change_executions SET status = ?, result_json = ?, "
                    "completed_at = ? WHERE id = ?",
                    (outcome, _json(result), now, row["id"]),
                )
                if row["step_status"] == "dispatched":
                    conn.execute(
                        "UPDATE connector_commands SET status = 'rejected', result_json = ?, "
                        "result_received_at = ?, updated_at = ? WHERE id = ? AND status = 'leased'",
                        (_json({
                            "status": "rejected", "stdout": "", "stderr": "", "exit_code": None,
                            "truncated": False, "error_code": code, "error_message": code,
                        }), now, now, row["command_id"]),
                    )
                self._phases.record_execution_finished_in(
                    conn, change_request_id=str(row["change_request_id"]),
                    phase_id=str(row["phase_id"]), execution_id=str(row["id"]),
                    command_id=str(row["command_id"]), outcome=outcome, error_code=code, now=now,
                )
                insert_admin_audit(
                    conn, actor_id=None, target_type="kubernetes_change_executions",
                    target_id=str(row["id"]), action="kubernetes_change_execution_reconcile",
                    reason="Connector transport did not produce a trustworthy terminal outcome",
                    before={"status": row["step_status"]},
                    after={"status": outcome, "error_code": code},
                    result=code, request_id=request_id,
                )
            conn.commit()

    @staticmethod
    def _record_in(conn: sqlite3.Connection, row: Any, *, idempotent: bool) -> dict[str, object]:
        step = conn.execute(
            """SELECT * FROM kubernetes_change_execution_steps
               WHERE execution_id = ? AND direction = 'forward'
               ORDER BY ordinal DESC LIMIT 1""",
            (row["id"],),
        ).fetchone()
        grant = conn.execute(
            "SELECT * FROM kubernetes_execution_grants WHERE step_id = ?", (step["id"],),
        ).fetchone()
        return {
            "id": str(row["id"]), "change_request_id": str(row["change_request_id"]),
            "phase_id": str(row["phase_id"]), "approval_id": str(row["approval_id"]),
            "command_id": str(step["command_id"]), "status": str(row["status"]),
            "execution_timeout_seconds": int(row["execution_timeout_seconds"]),
            "started_at": float(row["started_at"]) if row["started_at"] is not None else None,
            "completed_at": float(row["completed_at"]) if row["completed_at"] is not None else None,
            "result": json.loads(str(row["result_json"])) if row["result_json"] else None,
            "grant": {
                "id": str(grant["id"]), "issued_at": float(grant["issued_at"]),
                "expires_at": float(grant["expires_at"]),
                "consumed_at": float(grant["consumed_at"]) if grant["consumed_at"] is not None else None,
            },
            "idempotent": idempotent,
        }


def _single_frozen_change(approval: dict[str, object]) -> tuple[dict[str, object], str]:
    frozen = approval.get("frozen_changes")
    if not isinstance(frozen, list) or len(frozen) != 1 or not isinstance(frozen[0], dict):
        raise KubernetesChangeExecutionError("invalid_execution_phase", "K04 executes exactly one frozen Change")
    change = frozen[0].get("canonical_change")
    if not isinstance(change, dict):
        raise KubernetesChangeExecutionError("invalid_execution_phase", "Frozen canonical Change is missing")
    return change, _digest(change)


def _change_namespace(change: dict[str, object]) -> str:
    target = change.get("target")
    namespace = target.get("namespace") if isinstance(target, dict) else None
    return namespace if isinstance(namespace, str) and namespace else "default"


def _execution_timeout(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 300 <= value <= 1800:
        raise KubernetesChangeExecutionError(
            "invalid_execution_timeout", "execution_timeout_seconds must be between 300 and 1800",
        )
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise KubernetesChangeExecutionError("invalid_request", f"{field} is required")
    return value.strip()


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _command_record(row: Any) -> dict[str, object]:
    return {
        "id": str(row["id"]), "cluster_id": str(row["cluster_id"]),
        "namespace": str(row["namespace"]), "action": str(row["action"]),
        "parameters": json.loads(str(row["parameters_json"])),
        "execution_grant_id": str(row["kubernetes_execution_grant_id"]),
        "execution_grant_expires_at": float(row["execution_grant_expires_at"]),
        "action_hash": str(row["action_hash"]), "status": str(row["status"]),
        "attempt_count": int(row["attempt_count"]), "lease_id": str(row["lease_id"]),
        "lease_expires_at": float(row["lease_expires_at"]), "created_at": float(row["created_at"]),
        "result": None,
    }
