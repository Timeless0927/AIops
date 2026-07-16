"""Gateway owner for sequential execution of one frozen Kubernetes Change Plan."""

from __future__ import annotations

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
from .kubernetes_execution_cancellation import (
    KubernetesExecutionCancellationError,
    cancel_execution,
)
from .kubernetes_execution_codec import canonical_digest as _digest, canonical_json as _json
from .kubernetes_execution_grants import queue_pending_step
from .kubernetes_execution_progress import (
    active_step,
    create_rollback_steps_in,
    project_steps_in,
    result_outcome,
    validate_declared_execution_result,
)
from .kubernetes_inverse_changes import KubernetesInverseChangeError
from .kubernetes_phase_approvals import KubernetesPhaseApprovalError
from .kubernetes_reconciliation import KubernetesReconciliations
from .kubernetes_unknown_outcomes import reconcile_transport_failures
from .secure_inputs import SecureInputError, SecureInputs
from . import secure_input_execution


_schema.register_plan_execution_migrations()


class KubernetesChangeExecutionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class KubernetesChangeExecutions:
    """Issues per-step grants and projects one frozen Phase execution."""

    def __init__(
        self,
        database: GatewayDatabase,
        *,
        approvals: Any,
        enrollments: ConnectorEnrollments,
        secure_inputs: SecureInputs | None = None,
        phases: ChangePlanPhases | None = None,
        reconciliations: KubernetesReconciliations | None = None,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._database = database
        self._approvals = approvals
        self._enrollments = enrollments
        self._secure_inputs = secure_inputs
        self._phases = phases or ChangePlanPhases()
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")
        self._reconciliations = reconciliations or KubernetesReconciliations(
            database, approvals=approvals, secure_inputs=secure_inputs,
            clock=clock, id_factory=self._id_factory,
        )

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
        frozen_changes = _frozen_changes(approval)
        change = frozen_changes[0]["canonical_change"]
        assert isinstance(change, dict)
        change_hash = _digest(change)
        cluster_id = _text(approval.get("cluster_id"), "cluster_id")
        namespace = _change_namespace(change)
        execution_id = self._id_factory("kubernetes-execution")
        grant_id = self._id_factory("kubernetes-grant")
        command_ids = [self._id_factory("command") for _ in frozen_changes]
        command_id = command_ids[0]
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
            try:
                for frozen in frozen_changes:
                    metadata = frozen.get("secure_inputs", [])
                    if metadata and self._secure_inputs is None:
                        raise SecureInputError(
                            "secure_input_unavailable", "Secure Input owner is unavailable",
                        )
                    if self._secure_inputs is not None:
                        self._secure_inputs.encrypted_refs_for_metadata_in(conn, metadata)
            except SecureInputError as exc:
                self._phases.record_secure_input_unavailable_in(
                    conn,
                    change_request_id=change_request_id,
                    phase_id=phase_id,
                    execution_id=None,
                    command_id=None,
                    now=now,
                )
                if self._secure_inputs is not None:
                    self._secure_inputs.release_revision_in(
                        conn, str(approval["revision_id"]), released_at=now, delete_after=now,
                    )
                conn.commit()
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
            for index, frozen in enumerate(frozen_changes):
                canonical = frozen["canonical_change"]
                assert isinstance(canonical, dict)
                conn.execute(
                    """
                    INSERT INTO kubernetes_change_execution_steps (
                        id, execution_id, ordinal, direction, command_id, change_hash,
                        change_json, inverse_change_json, secure_inputs_json, status, created_at
                    ) VALUES (?, ?, ?, 'forward', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"{execution_id}:forward:{index + 1}", execution_id, index + 1,
                        command_ids[index], _digest(canonical), _json(canonical),
                        _json(frozen["inverse_change"]) if frozen.get("inverse_change") else None,
                        _json(frozen.get("secure_inputs", [])),
                        "queued" if index == 0 else "pending", now,
                    ),
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

    def cancel(
        self,
        change_request_id: str,
        *,
        phase_id: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        change_request_id = _text(change_request_id, "change_request_id")
        phase_id = _text(phase_id, "phase_id")
        actor_id = _text(actor_id, "actor_id")
        reason = _text(reason, "reason")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        try:
            execution_id, idempotent = cancel_execution(
                self._database, approvals=self._approvals, phases=self._phases,
                change_request_id=change_request_id, phase_id=phase_id, actor_id=actor_id,
                reason=reason, idempotency_key=idempotency_key, request_id=request_id,
                now=self._clock(),
            )
        except (KubernetesPhaseApprovalError, KubernetesExecutionCancellationError) as exc:
            raise KubernetesChangeExecutionError(exc.code, exc.message) from exc
        with self._database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM kubernetes_change_executions WHERE id = ?", (execution_id,),
            ).fetchone()
            if row is not None and row["status"] == "cancelled":
                self._schedule_terminal_cleanup_in(conn, row, now=self._clock())
                conn.commit()
            return self._record_in(conn, row, idempotent=idempotent)

    def dispatch_next(self, connector_id: str, cluster_id: str, *, request_id: str) -> dict[str, object] | None:
        connector_id = _text(connector_id, "connector_id")
        cluster_id = _text(cluster_id, "cluster_id")
        now = self._clock()
        try:
            with self._database.connect() as conn:
                available_connector = self._enrollments.execution_connector_in(conn, cluster_id)
        except IdentityError as exc:
            raise KubernetesChangeExecutionError(exc.code, exc.message) from exc
        if available_connector != connector_id:
            raise KubernetesChangeExecutionError("cluster_not_ready", "Connector mismatch")
        if self._secure_inputs is not None:
            self._secure_inputs.cleanup_expired(now=now)
        reconcile_transport_failures(
            self._database, self.record_result_in, now=now, request_id=request_id,
        )
        self._reconciliations.reconcile_failed_observations(now=now)
        queue_pending_step(
            self._database, approvals=self._approvals, phases=self._phases,
            id_factory=self._id_factory, connector_id=connector_id, cluster_id=cluster_id,
            request_id=request_id, now=now, on_failure=self._fail_pending_before_grant,
        )
        with self._database.connect() as conn:
            candidate = conn.execute(
                """
                SELECT execution.*, step.id AS step_id, step.command_id, step.change_hash,
                       step.change_json, step.secure_inputs_json, step.direction, step.ordinal,
                       grant.id AS grant_id, grant.issued_at, grant.expires_at
                FROM kubernetes_change_executions execution
                JOIN kubernetes_change_execution_steps step ON step.execution_id = execution.id
                JOIN kubernetes_execution_grants grant ON grant.execution_id = execution.id
                WHERE execution.connector_id = ? AND execution.cluster_id = ?
                  AND execution.status IN ('queued', 'started', 'rolling_back') AND step.status = 'queued'
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
        if (
            approval.get("id") != candidate["approval_id"]
            or approval.get("cluster_id") != cluster_id
            or _digest(json.loads(str(candidate["change_json"]))) != candidate["change_hash"]
        ):
            self._fail_before_dispatch(candidate, "phase_stale", request_id, now)
            return None
        change = json.loads(str(candidate["change_json"]))
        change_hash = str(candidate["change_hash"])
        metadata = json.loads(str(candidate["secure_inputs_json"]))
        try:
            if metadata and self._secure_inputs is None:
                raise SecureInputError("secure_input_unavailable", "Secure Input owner is unavailable")
            with self._database.connect() as conn:
                secure_refs = (
                    self._secure_inputs.encrypted_refs_for_metadata_in(conn, metadata)
                    if self._secure_inputs is not None else []
                )
        except SecureInputError:
            secure_input_execution.mark_secure_input_unavailable(
                self._database, self._phases, self._secure_inputs, candidate,
                request_id=request_id, now=now,
            )
            return None
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
            **({"secure_inputs": secure_refs} if secure_refs else {}),
        }
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            consumed = conn.execute(
                """
                UPDATE kubernetes_execution_grants SET consumed_at = ?
                WHERE id = ? AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > ?
                  AND EXISTS (
                      SELECT 1
                      FROM kubernetes_change_execution_steps step
                      JOIN kubernetes_change_executions execution ON execution.id = step.execution_id
                      WHERE step.id = kubernetes_execution_grants.step_id
                        AND step.status = 'queued'
                        AND execution.status IN ('queued', 'started', 'rolling_back')
                  )
                """,
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
            dispatched = conn.execute(
                "UPDATE kubernetes_change_execution_steps SET status = 'dispatched' "
                "WHERE id = ? AND status = 'queued'", (candidate["step_id"],),
            )
            if dispatched.rowcount != 1:
                conn.rollback()
                return None
            if candidate["direction"] == "forward" and candidate["status"] == "queued":
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
            """SELECT execution.*, step.id AS step_id, step.status AS step_status,
                      step.direction, step.ordinal AS step_ordinal
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
        if row["direction"] == "forward":
            conn.execute(
                "UPDATE kubernetes_change_executions SET status = 'started', "
                "started_at = COALESCE(started_at, ?) WHERE id = ?",
                (now, row["id"]),
            )
        conn.execute(
            "UPDATE connector_commands SET execution_expires_at = ? WHERE id = ?",
            (now + int(row["execution_timeout_seconds"]), command_id),
        )
        self._phases.record_step_started_in(
            conn, change_request_id=str(row["change_request_id"]), phase_id=str(row["phase_id"]),
            execution_id=str(row["id"]), step_id=str(row["step_id"]), command_id=command_id,
            direction=str(row["direction"]),
            ordinal=int(row["step_ordinal"]), now=now,
        )
        if row["direction"] == "forward" and row["started_at"] is None:
            self._phases.record_execution_started_in(
                conn, change_request_id=str(row["change_request_id"]), phase_id=str(row["phase_id"]),
                execution_id=str(row["id"]), command_id=command_id, now=now,
            )

    def record_result_in(
        self, conn: sqlite3.Connection, command_id: str,
        result: dict[str, object], now: float,
    ) -> None:
        self._reconciliations.record_result_in(conn, command_id, result, now)
        row = conn.execute(
            """SELECT execution.*, step.id AS step_id, step.command_id AS step_command_id,
                      step.direction, step.ordinal, step.source_step_id,
                      step.change_json, step.secure_inputs_json
               FROM kubernetes_change_executions execution
               JOIN kubernetes_change_execution_steps step ON step.execution_id = execution.id
               WHERE step.command_id = ?""",
            (command_id,),
        ).fetchone()
        if row is None:
            return
        validate_declared_execution_result(str(row["change_json"]), result)
        outcome, error_code = result_outcome(result)
        reconciliation_state = None
        if error_code != "execution_outcome_unknown":
            reconciliation_state = self._reconciliations.record_terminal_in(
                conn, command_id, now=now,
            )
        if secure_input_execution.handle_terminal_result_in(
            conn, self._phases, self._secure_inputs, row, result,
            command_id=command_id, error_code=error_code, now=now,
        ):
            return
        conn.execute(
            "UPDATE kubernetes_change_execution_steps SET status = ?, result_json = ?, completed_at = ? "
            "WHERE id = ?", (outcome, _json(result), now, row["step_id"]),
        )
        self._phases.record_step_finished_in(
            conn, change_request_id=str(row["change_request_id"]), phase_id=str(row["phase_id"]),
            execution_id=str(row["id"]), step_id=str(row["step_id"]), command_id=command_id,
            direction=str(row["direction"]), ordinal=int(row["ordinal"]),
            outcome=outcome, error_code=error_code, now=now,
        )
        if outcome == "unknown_outcome":
            self._reconciliations.record_unknown_in(conn, row, now=now)
        if reconciliation_state == "accepted":
            conn.execute(
                "UPDATE kubernetes_change_executions SET result_json = ? WHERE id = ?",
                (_json(result), row["id"]),
            )
            return
        if row["status"] == "cancel_requested":
            self._finish_cancel_requested_in(
                conn, row, result, outcome, error_code, command_id, now,
            )
        elif row["direction"] == "rollback":
            self._finish_rollback_step_in(conn, row, result, outcome, error_code, now)
        elif outcome == "succeeded":
            pending = conn.execute(
                "SELECT 1 FROM kubernetes_change_execution_steps "
                "WHERE execution_id = ? AND direction = 'forward' AND status = 'pending'",
                (row["id"],),
            ).fetchone()
            if pending is not None:
                conn.execute(
                    "UPDATE kubernetes_change_executions SET status = 'started', result_json = ? "
                    "WHERE id = ?", (_json(result), row["id"]),
                )
            else:
                self._finish_plan_in(conn, row, result, "succeeded", error_code, command_id, now)
        else:
            if outcome != "unknown_outcome":
                conn.execute(
                    "UPDATE kubernetes_change_execution_steps SET status = 'cancelled', completed_at = ? "
                    "WHERE execution_id = ? AND direction = 'forward' AND status = 'pending'",
                    (now, row["id"]),
                )
            rollback_count = 0
            rollback_error: str | None = None
            if row["rollback_policy"] == "rollback_completed" and outcome != "unknown_outcome":
                try:
                    rollback_count = create_rollback_steps_in(
                        conn, execution_id=str(row["id"]), failed_step_id=str(row["step_id"]),
                        failed_outcome=outcome, now=now, id_factory=self._id_factory,
                    )
                except KubernetesInverseChangeError as exc:
                    rollback_error = str(exc)
            if rollback_error is not None:
                rollback_result = {**result, "rollback_error": rollback_error}
                conn.execute(
                    "UPDATE kubernetes_change_executions SET status = 'rollback_failed', "
                    "result_json = ?, completed_at = ? WHERE id = ?",
                    (_json(rollback_result), now, row["id"]),
                )
                self._phases.record_rollback_finished_in(
                    conn, change_request_id=str(row["change_request_id"]),
                    phase_id=str(row["phase_id"]), execution_id=str(row["id"]),
                    outcome="rollback_failed", error_code="rollback_binding_failed", now=now,
                )
                self._schedule_terminal_cleanup_in(conn, row, now=now)
            elif rollback_count:
                conn.execute(
                    "UPDATE kubernetes_change_executions SET status = 'rolling_back', "
                    "result_json = ?, completed_at = NULL WHERE id = ?",
                    (_json(result), row["id"]),
                )
                self._phases.record_rollback_started_in(
                    conn, change_request_id=str(row["change_request_id"]),
                    phase_id=str(row["phase_id"]), execution_id=str(row["id"]),
                    failed_step_id=str(row["step_id"]), step_count=rollback_count, now=now,
                )
            else:
                self._finish_plan_in(conn, row, result, outcome, error_code, command_id, now)

    def _finish_cancel_requested_in(
        self, conn: sqlite3.Connection, row: Any, result: dict[str, object],
        outcome: str, error_code: str | None, command_id: str, now: float,
    ) -> None:
        if outcome == "unknown_outcome":
            conn.execute(
                "UPDATE kubernetes_change_executions SET status = 'unknown_outcome', "
                "result_json = ?, completed_at = ? WHERE id = ?",
                (_json(result), now, row["id"]),
            )
            self._phases.record_execution_finished_in(
                conn, change_request_id=str(row["change_request_id"]),
                phase_id=str(row["phase_id"]), execution_id=str(row["id"]),
                command_id=command_id, outcome=outcome, error_code=error_code, now=now,
            )
            return
        conn.execute(
            "UPDATE kubernetes_change_executions SET status = 'cancelled', result_json = ?, "
            "cancelled_at = ?, completed_at = ? WHERE id = ?",
            (_json(result), now, now, row["id"]),
        )
        cancellation = conn.execute(
            "SELECT * FROM kubernetes_execution_cancellations WHERE execution_id = ?", (row["id"],),
        ).fetchone()
        self._phases.record_cancel_in(
            conn, change_request_id=str(row["change_request_id"]), phase_id=str(row["phase_id"]),
            execution_id=str(row["id"]), actor_id=str(cancellation["actor_id"]),
            reason=str(cancellation["reason"]), status="cancelled",
            request_id=str(cancellation["request_id"]), now=now,
        )
        self._schedule_terminal_cleanup_in(conn, row, now=now)

    def _finish_rollback_step_in(
        self, conn: sqlite3.Connection, row: Any, result: dict[str, object],
        outcome: str, error_code: str | None, now: float,
    ) -> None:
        if outcome == "unknown_outcome":
            conn.execute(
                "UPDATE kubernetes_change_execution_steps SET status = 'cancelled', completed_at = ? "
                "WHERE execution_id = ? AND direction = 'rollback' AND status = 'pending'",
                (now, row["id"]),
            )
            conn.execute(
                "UPDATE kubernetes_change_executions SET status = 'unknown_outcome', "
                "result_json = ?, completed_at = ? WHERE id = ?",
                (_json(result), now, row["id"]),
            )
            self._phases.record_execution_finished_in(
                conn, change_request_id=str(row["change_request_id"]),
                phase_id=str(row["phase_id"]), execution_id=str(row["id"]),
                command_id=str(row["step_command_id"]), outcome=outcome,
                error_code=error_code, now=now,
            )
            return
        if outcome == "succeeded":
            conn.execute(
                "UPDATE kubernetes_change_execution_steps SET status = 'rolled_back' "
                "WHERE id = ?", (row["source_step_id"],),
            )
            pending = conn.execute(
                "SELECT 1 FROM kubernetes_change_execution_steps "
                "WHERE execution_id = ? AND direction = 'rollback' AND status = 'pending'",
                (row["id"],),
            ).fetchone()
            if pending is not None:
                return
            status = "rolled_back"
        else:
            status = "rollback_failed"
            conn.execute(
                "UPDATE kubernetes_change_execution_steps SET status = 'cancelled', completed_at = ? "
                "WHERE execution_id = ? AND direction = 'rollback' AND status = 'pending'",
                (now, row["id"]),
            )
        conn.execute(
            "UPDATE kubernetes_change_executions SET status = ?, result_json = ?, completed_at = ? "
            "WHERE id = ?", (status, _json(result), now, row["id"]),
        )
        self._phases.record_rollback_finished_in(
            conn, change_request_id=str(row["change_request_id"]), phase_id=str(row["phase_id"]),
            execution_id=str(row["id"]), outcome=status, error_code=error_code, now=now,
        )
        self._schedule_terminal_cleanup_in(conn, row, now=now)

    def _finish_plan_in(
        self, conn: sqlite3.Connection, row: Any, result: dict[str, object],
        outcome: str, error_code: str | None, command_id: str, now: float,
    ) -> None:
        conn.execute(
            "UPDATE kubernetes_change_executions SET status = ?, result_json = ?, completed_at = ? "
            "WHERE id = ?", (outcome, _json(result), now, row["id"]),
        )
        self._phases.record_execution_finished_in(
            conn, change_request_id=str(row["change_request_id"]), phase_id=str(row["phase_id"]),
            execution_id=str(row["id"]), command_id=command_id,
            outcome=outcome, error_code=error_code, now=now,
        )
        if outcome != "unknown_outcome":
            self._schedule_terminal_cleanup_in(conn, row, now=now)

    def _schedule_terminal_cleanup_in(
        self, conn: sqlite3.Connection, row: Any, *, now: float,
    ) -> None:
        if self._secure_inputs is None:
            return
        secure_input_execution.schedule_secure_input_cleanup_in(
            conn, self._secure_inputs, execution_id=str(row["id"]),
            revision_id=str(row["revision_id"]),
            released_at=now, delete_after=now + int(row["execution_timeout_seconds"]),
        )

    def _fail_before_dispatch(
        self, row: Any, code: str, request_id: str, now: float,
    ) -> None:
        self._fail_unstarted_step(row, code, request_id, now, action="dispatch")

    def _fail_pending_before_grant(
        self, row: Any, code: str, request_id: str, now: float,
    ) -> None:
        self._fail_unstarted_step(row, code, request_id, now, action="grant")

    def _fail_unstarted_step(
        self, row: Any, code: str, request_id: str, now: float, *, action: str,
    ) -> None:
        result = {
            "status": "rejected", "stdout": "", "stderr": "", "exit_code": None,
            "truncated": False, "error_code": code, "error_message": code,
        }
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status FROM kubernetes_change_execution_steps WHERE id = ?", (row["step_id"],),
            ).fetchone()
            if current is not None and current["status"] in {"pending", "queued"}:
                self.record_result_in(conn, str(row["command_id"]), result, now)
                insert_admin_audit(
                    conn, actor_id=None, target_type="kubernetes_change_execution_steps",
                    target_id=str(row["step_id"]),
                    action=f"kubernetes_change_execution_{action}",
                    reason="Execution authority was not valid before Connector start",
                    before={"status": current["status"]},
                    after={"status": "failed", "error_code": code},
                    result=code, request_id=request_id,
                )
            conn.commit()

    def _record_in(
        self, conn: sqlite3.Connection, row: Any, *, idempotent: bool,
    ) -> dict[str, object]:
        steps = project_steps_in(conn, str(row["id"]))
        reconciliation = self._reconciliations.for_execution_in(conn, str(row["id"]))
        if reconciliation is not None and reconciliation["state"] != "resolved":
            steps = [
                {**step, "status": reconciliation["classification"]}
                if step["id"] == reconciliation["step_id"]
                else step
                for step in steps
            ]
        step = active_step(steps)
        effective_status = (
            str(reconciliation["classification"])
            if reconciliation is not None and reconciliation["state"] != "resolved"
            else str(row["availability_status"] or row["status"])
        )
        return {
            "id": str(row["id"]), "change_request_id": str(row["change_request_id"]),
            "phase_id": str(row["phase_id"]), "revision_id": str(row["revision_id"]),
            "approval_id": str(row["approval_id"]),
            "command_id": str(step["command_id"]),
            "status": effective_status,
            "rollback_policy": str(row["rollback_policy"]),
            "execution_timeout_seconds": int(row["execution_timeout_seconds"]),
            "started_at": float(row["started_at"]) if row["started_at"] is not None else None,
            "completed_at": float(row["completed_at"]) if row["completed_at"] is not None else None,
            "result": json.loads(str(row["result_json"])) if row["result_json"] else None,
            "grant": step["grant"], "current_step": step, "steps": steps,
            "reconciliation": reconciliation,
            "idempotent": idempotent,
        }


def _frozen_changes(approval: dict[str, object]) -> list[dict[str, object]]:
    frozen = approval.get("frozen_changes")
    if not isinstance(frozen, list) or not frozen or any(
        not isinstance(item, dict) or not isinstance(item.get("canonical_change"), dict)
        for item in frozen
    ):
        raise KubernetesChangeExecutionError("invalid_execution_phase", "Frozen canonical Changes are missing")
    return frozen


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
