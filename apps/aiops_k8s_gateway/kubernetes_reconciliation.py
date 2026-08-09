"""Gateway-owned reconciliation evidence for Kubernetes execution outcomes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from typing import Any

from .connector_commands import ConnectorCommands
from .gateway_audit import insert_admin_audit
from .gateway_db import GatewayDatabase, register_migrations
from .kubernetes_execution_codec import canonical_json
from .secure_inputs import SecureInputError, SecureInputs

_SCHEMA_VERSION = 34
_SCHEMA = """
PRAGMA legacy_alter_table = ON;
ALTER TABLE command_leases RENAME TO command_leases_v33;
ALTER TABLE kubernetes_change_validation_secure_inputs
    RENAME TO kubernetes_change_validation_secure_inputs_v33;
ALTER TABLE kubernetes_change_validations RENAME TO kubernetes_change_validations_v33;
ALTER TABLE connector_commands RENAME TO connector_commands_v33;
DROP INDEX connector_commands_poll;
DROP INDEX kubernetes_change_validations_by_command;

CREATE TABLE connector_commands (
    id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN (
        'get_resource', 'validate_kubernetes_change', 'execute_kubernetes_change',
        'reconcile_kubernetes_change', 'restart_deployment', 'scale_deployment',
        'rollback_deployment'
    )),
    parameters_json TEXT NOT NULL CHECK (json_valid(parameters_json)),
    rollback_plan_json TEXT CHECK (rollback_plan_json IS NULL OR json_valid(rollback_plan_json)),
    execution_grant_id TEXT UNIQUE,
    kubernetes_execution_grant_id TEXT UNIQUE,
    execution_grant_expires_at REAL,
    action_hash TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'leased', 'started', 'succeeded', 'failed', 'rejected', 'unknown_outcome'
    )),
    lease_id TEXT,
    lease_expires_at REAL,
    execution_expires_at REAL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    result_hash TEXT,
    result_received_at REAL,
    journal_recorded_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK (
        (action IN ('get_resource', 'validate_kubernetes_change', 'reconcile_kubernetes_change')
         AND execution_grant_id IS NULL AND kubernetes_execution_grant_id IS NULL
         AND execution_grant_expires_at IS NULL AND action_hash IS NULL)
        OR (action IN ('restart_deployment', 'scale_deployment', 'rollback_deployment')
            AND execution_grant_id IS NOT NULL AND kubernetes_execution_grant_id IS NULL
            AND execution_grant_expires_at IS NOT NULL AND length(action_hash) = 64)
        OR (action = 'execute_kubernetes_change'
            AND execution_grant_id IS NULL AND kubernetes_execution_grant_id IS NOT NULL
            AND execution_grant_expires_at IS NOT NULL AND length(action_hash) = 64)
    ),
    FOREIGN KEY (connector_id) REFERENCES connector_enrollments(connector_id),
    FOREIGN KEY (cluster_id) REFERENCES clusters(cluster_id),
    FOREIGN KEY (execution_grant_id) REFERENCES execution_grants(id),
    FOREIGN KEY (kubernetes_execution_grant_id) REFERENCES kubernetes_execution_grants(id)
);
INSERT INTO connector_commands (
    id, connector_id, cluster_id, namespace, action, parameters_json,
    rollback_plan_json, execution_grant_id, kubernetes_execution_grant_id,
    execution_grant_expires_at, action_hash, status, lease_id, lease_expires_at,
    execution_expires_at, attempt_count, result_json, result_hash,
    result_received_at, created_at, updated_at
)
SELECT id, connector_id, cluster_id, namespace, action, parameters_json,
       rollback_plan_json, execution_grant_id, kubernetes_execution_grant_id,
       execution_grant_expires_at, action_hash, status, lease_id, lease_expires_at,
       execution_expires_at, attempt_count, result_json, result_hash,
       result_received_at, created_at, updated_at
FROM connector_commands_v33;
CREATE INDEX connector_commands_poll
    ON connector_commands(connector_id, cluster_id, status, lease_expires_at, created_at);

CREATE TABLE kubernetes_change_validations (
    id TEXT PRIMARY KEY,
    change_request_id TEXT NOT NULL REFERENCES change_requests(id) ON DELETE CASCADE,
    phase_id TEXT NOT NULL REFERENCES change_plan_phases(id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL REFERENCES change_plan_revisions(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    cluster_id TEXT NOT NULL REFERENCES clusters(cluster_id),
    command_id TEXT UNIQUE REFERENCES connector_commands(id),
    draft_json TEXT NOT NULL CHECK (json_valid(draft_json)),
    status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed', 'superseded')),
    policy_error_json TEXT CHECK (policy_error_json IS NULL OR json_valid(policy_error_json)),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(revision_id, ordinal)
);
INSERT INTO kubernetes_change_validations SELECT * FROM kubernetes_change_validations_v33;
CREATE INDEX kubernetes_change_validations_by_command
    ON kubernetes_change_validations(command_id) WHERE command_id IS NOT NULL;

CREATE TABLE kubernetes_change_validation_secure_inputs (
    validation_id TEXT NOT NULL REFERENCES kubernetes_change_validations(id) ON DELETE CASCADE,
    secure_input_id TEXT NOT NULL REFERENCES secure_inputs(id),
    PRIMARY KEY (validation_id, secure_input_id)
);
INSERT INTO kubernetes_change_validation_secure_inputs
SELECT * FROM kubernetes_change_validation_secure_inputs_v33;

CREATE TABLE command_leases (
    lease_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL,
    connector_id TEXT NOT NULL,
    granted_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    started_at REAL,
    FOREIGN KEY (command_id) REFERENCES connector_commands(id) ON DELETE CASCADE
);
INSERT INTO command_leases SELECT * FROM command_leases_v33;

CREATE TABLE kubernetes_change_reconciliations (
    id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES kubernetes_change_executions(id),
    step_id TEXT NOT NULL UNIQUE REFERENCES kubernetes_change_execution_steps(id),
    mutation_command_id TEXT NOT NULL UNIQUE,
    observation_command_id TEXT NOT NULL UNIQUE REFERENCES connector_commands(id),
    classification TEXT NOT NULL CHECK (classification IN ('unknown_outcome', 'effect_observed')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'observed', 'accepted', 'resolved')),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    observed_at REAL,
    accepted_by TEXT REFERENCES users(id),
    acceptance_reason TEXT,
    acceptance_request_id TEXT,
    acceptance_idempotency_key TEXT,
    acceptance_request_hash TEXT CHECK (
        acceptance_request_hash IS NULL OR length(acceptance_request_hash) = 64
    ),
    accepted_at REAL,
    replanning_phase_id TEXT UNIQUE REFERENCES change_plan_phases(id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(accepted_by, acceptance_idempotency_key)
);
CREATE INDEX kubernetes_change_reconciliations_by_command
    ON kubernetes_change_reconciliations(observation_command_id);
CREATE INDEX kubernetes_change_reconciliations_by_state
    ON kubernetes_change_reconciliations(state, updated_at);

DROP TABLE command_leases_v33;
DROP TABLE kubernetes_change_validation_secure_inputs_v33;
DROP TABLE kubernetes_change_validations_v33;
DROP TABLE connector_commands_v33;
PRAGMA legacy_alter_table = OFF;
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))

_PHASE_SCHEMA_VERSION = 35
_PHASE_SCHEMA = """
ALTER TABLE change_plan_phases ADD COLUMN reconciliation_status TEXT
    CHECK (reconciliation_status IS NULL OR reconciliation_status IN (
        'unknown_outcome', 'effect_observed'
    ));
"""
register_migrations(((_PHASE_SCHEMA_VERSION, _PHASE_SCHEMA),))

_RETIREMENT_SCHEMA_VERSION = 37
_RETIREMENT_SCHEMA = """
PRAGMA legacy_alter_table = ON;
ALTER TABLE command_leases RENAME TO command_leases_v36;
ALTER TABLE kubernetes_change_validation_secure_inputs
    RENAME TO kubernetes_change_validation_secure_inputs_v36;
ALTER TABLE kubernetes_change_validations RENAME TO kubernetes_change_validations_v36;
ALTER TABLE kubernetes_change_reconciliations RENAME TO kubernetes_change_reconciliations_v36;
ALTER TABLE connector_commands RENAME TO connector_commands_v36;
DROP INDEX IF EXISTS connector_commands_poll;
DROP INDEX IF EXISTS kubernetes_change_validations_by_command;
DROP INDEX IF EXISTS kubernetes_change_reconciliations_by_command;
DROP INDEX IF EXISTS kubernetes_change_reconciliations_by_state;

CREATE TABLE connector_commands (
    id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN (
        'get_resource', 'validate_kubernetes_change', 'execute_kubernetes_change',
        'reconcile_kubernetes_change'
    )),
    parameters_json TEXT NOT NULL CHECK (json_valid(parameters_json)),
    kubernetes_execution_grant_id TEXT UNIQUE,
    execution_grant_expires_at REAL,
    action_hash TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'leased', 'started', 'succeeded', 'failed', 'rejected', 'unknown_outcome'
    )),
    lease_id TEXT,
    lease_expires_at REAL,
    execution_expires_at REAL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    result_hash TEXT,
    result_received_at REAL,
    journal_recorded_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK (
        (action IN ('get_resource', 'validate_kubernetes_change', 'reconcile_kubernetes_change')
         AND kubernetes_execution_grant_id IS NULL
         AND execution_grant_expires_at IS NULL AND action_hash IS NULL)
        OR (action = 'execute_kubernetes_change'
            AND kubernetes_execution_grant_id IS NOT NULL
            AND execution_grant_expires_at IS NOT NULL AND length(action_hash) = 64)
    ),
    FOREIGN KEY (connector_id) REFERENCES connector_enrollments(connector_id),
    FOREIGN KEY (cluster_id) REFERENCES clusters(cluster_id),
    FOREIGN KEY (kubernetes_execution_grant_id) REFERENCES kubernetes_execution_grants(id)
);
INSERT INTO connector_commands (
    id, connector_id, cluster_id, namespace, action, parameters_json,
    kubernetes_execution_grant_id, execution_grant_expires_at, action_hash,
    status, lease_id, lease_expires_at, execution_expires_at, attempt_count,
    result_json, result_hash, result_received_at, journal_recorded_at,
    created_at, updated_at
)
SELECT id, connector_id, cluster_id, namespace, action, parameters_json,
       kubernetes_execution_grant_id, execution_grant_expires_at, action_hash,
       status, lease_id, lease_expires_at, execution_expires_at, attempt_count,
       result_json, result_hash, result_received_at, journal_recorded_at,
       created_at, updated_at
FROM connector_commands_v36
WHERE action IN (
    'get_resource', 'validate_kubernetes_change', 'execute_kubernetes_change',
    'reconcile_kubernetes_change'
);
CREATE INDEX connector_commands_poll
    ON connector_commands(connector_id, cluster_id, status, lease_expires_at, created_at);

CREATE TABLE kubernetes_change_validations (
    id TEXT PRIMARY KEY,
    change_request_id TEXT NOT NULL REFERENCES change_requests(id) ON DELETE CASCADE,
    phase_id TEXT NOT NULL REFERENCES change_plan_phases(id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL REFERENCES change_plan_revisions(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    cluster_id TEXT NOT NULL REFERENCES clusters(cluster_id),
    command_id TEXT UNIQUE REFERENCES connector_commands(id),
    draft_json TEXT NOT NULL CHECK (json_valid(draft_json)),
    status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed', 'superseded')),
    policy_error_json TEXT CHECK (policy_error_json IS NULL OR json_valid(policy_error_json)),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(revision_id, ordinal)
);
INSERT INTO kubernetes_change_validations
SELECT validation.* FROM kubernetes_change_validations_v36 validation
LEFT JOIN connector_commands command ON command.id = validation.command_id
WHERE validation.command_id IS NULL OR command.id IS NOT NULL;
CREATE INDEX kubernetes_change_validations_by_command
    ON kubernetes_change_validations(command_id) WHERE command_id IS NOT NULL;

CREATE TABLE kubernetes_change_validation_secure_inputs (
    validation_id TEXT NOT NULL REFERENCES kubernetes_change_validations(id) ON DELETE CASCADE,
    secure_input_id TEXT NOT NULL REFERENCES secure_inputs(id),
    PRIMARY KEY (validation_id, secure_input_id)
);
INSERT INTO kubernetes_change_validation_secure_inputs
SELECT binding.* FROM kubernetes_change_validation_secure_inputs_v36 binding
JOIN kubernetes_change_validations validation ON validation.id = binding.validation_id;

CREATE TABLE command_leases (
    lease_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL,
    connector_id TEXT NOT NULL,
    granted_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    started_at REAL,
    FOREIGN KEY (command_id) REFERENCES connector_commands(id) ON DELETE CASCADE
);
INSERT INTO command_leases
SELECT lease.* FROM command_leases_v36 lease
JOIN connector_commands command ON command.id = lease.command_id;

CREATE TABLE kubernetes_change_reconciliations (
    id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES kubernetes_change_executions(id),
    step_id TEXT NOT NULL UNIQUE REFERENCES kubernetes_change_execution_steps(id),
    mutation_command_id TEXT NOT NULL UNIQUE,
    observation_command_id TEXT NOT NULL UNIQUE REFERENCES connector_commands(id),
    classification TEXT NOT NULL CHECK (classification IN ('unknown_outcome', 'effect_observed')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'observed', 'accepted', 'resolved')),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    observed_at REAL,
    accepted_by TEXT REFERENCES users(id),
    acceptance_reason TEXT,
    acceptance_request_id TEXT,
    acceptance_idempotency_key TEXT,
    acceptance_request_hash TEXT CHECK (
        acceptance_request_hash IS NULL OR length(acceptance_request_hash) = 64
    ),
    accepted_at REAL,
    replanning_phase_id TEXT UNIQUE REFERENCES change_plan_phases(id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(accepted_by, acceptance_idempotency_key)
);
INSERT INTO kubernetes_change_reconciliations
SELECT reconciliation.* FROM kubernetes_change_reconciliations_v36 reconciliation
JOIN connector_commands command ON command.id = reconciliation.observation_command_id;
CREATE INDEX kubernetes_change_reconciliations_by_command
    ON kubernetes_change_reconciliations(observation_command_id);
CREATE INDEX kubernetes_change_reconciliations_by_state
    ON kubernetes_change_reconciliations(state, updated_at);

DROP TABLE command_leases_v36;
DROP TABLE kubernetes_change_validation_secure_inputs_v36;
DROP TABLE kubernetes_change_validations_v36;
DROP TABLE kubernetes_change_reconciliations_v36;
DROP TABLE connector_commands_v36;
DROP TABLE IF EXISTS execution_grants;
DROP TABLE IF EXISTS approvals;
DROP TABLE IF EXISTS approval_authorities;
PRAGMA legacy_alter_table = OFF;
"""
register_migrations(((_RETIREMENT_SCHEMA_VERSION, _RETIREMENT_SCHEMA),))


class KubernetesReconciliationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class KubernetesReconciliations:
    """Owns immutable observation evidence and explicit User acceptance."""

    def __init__(
        self,
        database: GatewayDatabase,
        *,
        approvals: Any,
        commands: ConnectorCommands,
        secure_inputs: SecureInputs | None = None,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._database = database
        self._approvals = approvals
        self._commands = commands
        self._secure_inputs = secure_inputs
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")

    def record_unknown_in(self, conn: sqlite3.Connection, row: Any, *, now: float) -> None:
        if conn.execute(
            "SELECT 1 FROM kubernetes_change_reconciliations WHERE step_id = ?", (row["step_id"],),
        ).fetchone() is not None:
            return
        change = json.loads(str(row["change_json"]))
        metadata = json.loads(str(row["secure_inputs_json"]))
        secure_refs: list[dict[str, object]] = []
        if metadata and self._secure_inputs is not None:
            try:
                secure_refs = self._secure_inputs.encrypted_refs_for_metadata_in(conn, metadata)
            except SecureInputError:
                secure_refs = []
        observation_command_id = f"{row['step_command_id']}:reconcile"
        reconciliation_id = f"{row['step_id']}:reconciliation"
        evidence = {
            "classification": "unknown_outcome",
            "mutation_command_id": str(row["step_command_id"]),
            "observation": "pending",
        }
        encoded = canonical_json(evidence)
        self._commands.queue_reconciliation_in(
            conn,
            command_id=observation_command_id,
            connector_id=str(row["connector_id"]),
            cluster_id=str(row["cluster_id"]),
            namespace=_change_namespace(change),
            parameters={
                "change": change,
                **({"secure_inputs": secure_refs} if secure_refs else {}),
            },
            now=now,
        )
        conn.execute(
            """
            INSERT INTO kubernetes_change_reconciliations (
                id, execution_id, step_id, mutation_command_id, observation_command_id,
                classification, state, evidence_json, evidence_sha256, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'unknown_outcome', 'pending', ?, ?, ?, ?)
            """,
            (
                reconciliation_id, row["id"], row["step_id"], row["step_command_id"],
                observation_command_id, encoded, _digest(encoded), now, now,
            ),
        )

    def record_result_in(
        self, conn: sqlite3.Connection, command_id: str,
        result: dict[str, object], now: float,
    ) -> None:
        row = conn.execute(
            "SELECT * FROM kubernetes_change_reconciliations WHERE observation_command_id = ?",
            (command_id,),
        ).fetchone()
        if row is None:
            return
        self._commands.redact_secure_inputs_in(conn, command_id)
        if row["state"] in {"accepted", "resolved"}:
            return
        evidence = _observation_evidence(result)
        classification = str(evidence["classification"])
        encoded = canonical_json(evidence)
        conn.execute(
            """
            UPDATE kubernetes_change_reconciliations
            SET classification = ?, state = 'observed', evidence_json = ?,
                evidence_sha256 = ?, observed_at = ?, updated_at = ?
            WHERE id = ? AND state = 'pending'
            """,
            (classification, encoded, _digest(encoded), now, now, row["id"]),
        )
        execution = conn.execute(
            "SELECT change_request_id, phase_id FROM kubernetes_change_executions WHERE id = ?",
            (row["execution_id"],),
        ).fetchone()
        if execution is None:
            return
        conn.execute(
            "UPDATE change_plan_phases SET reconciliation_status = ?, updated_at = ? WHERE id = ?",
            (classification, now, execution["phase_id"]),
        )
        _append_event(
            conn, str(execution["change_request_id"]),
            "change_request.reconciliation_observed", None,
            {
                "phase_id": str(execution["phase_id"]),
                "execution_id": str(row["execution_id"]),
                "step_id": str(row["step_id"]),
                "classification": classification,
                "evidence_sha256": _digest(encoded),
            },
            now,
        )

    def record_terminal_in(
        self, conn: sqlite3.Connection, command_id: str, *, now: float,
    ) -> str | None:
        row = conn.execute(
            """
            SELECT observation_command_id, execution_id, state
            FROM kubernetes_change_reconciliations
            WHERE mutation_command_id = ? AND state IN ('pending', 'observed', 'accepted')
            """,
            (command_id,),
        ).fetchone()
        if row is not None:
            self._commands.redact_secure_inputs_in(conn, str(row["observation_command_id"]))
            result = {
                "status": "rejected", "stdout": "", "stderr": "", "exit_code": None,
                "truncated": False, "error_code": "terminal_result_confirmed",
                "error_message": "terminal_result_confirmed",
            }
            self._commands.reject_in(
                conn, [str(row["observation_command_id"])], result=result, now=now,
            )
            conn.execute(
                """
                UPDATE change_plan_phases SET reconciliation_status = NULL, updated_at = ?
                WHERE id = (
                    SELECT phase_id FROM kubernetes_change_executions WHERE id = ?
                )
                """,
                (now, row["execution_id"]),
            )
        conn.execute(
            """
            UPDATE kubernetes_change_reconciliations
            SET state = 'resolved', updated_at = ?
            WHERE mutation_command_id = ? AND state IN ('pending', 'observed', 'accepted')
            """,
            (now, command_id),
        )
        return str(row["state"]) if row is not None else None

    def reconcile_failed_observations(self, *, now: float) -> int:
        with self._database.connect() as conn:
            return self.reconcile_failed_observations_in(conn, now=now)

    def reconcile_failed_observations_in(
        self, conn: sqlite3.Connection, *, now: float,
    ) -> int:
        conn.execute("BEGIN IMMEDIATE")
        command_ids = [
            str(row["observation_command_id"])
            for row in conn.execute(
                "SELECT observation_command_id FROM kubernetes_change_reconciliations "
                "WHERE state = 'pending'",
            ).fetchall()
        ]
        failed = [
            (command_id, result)
            for command_id in command_ids
            if (result := self._commands.failed_result_in(conn, command_id)) is not None
        ]
        for command_id, result in failed:
            self.record_result_in(conn, command_id, result, now)
        conn.commit()
        return len(failed)

    def for_execution_in(
        self, conn: sqlite3.Connection, execution_id: str,
    ) -> dict[str, object] | None:
        row = conn.execute(
            "SELECT * FROM kubernetes_change_reconciliations WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        return _projection(row) if row is not None else None

    def accept(
        self,
        change_request_id: str,
        phase_id: str,
        *,
        actor_id: str,
        evidence_sha256: str,
        reason: str,
        idempotency_key: str,
        request_id: str,
    ) -> dict[str, object]:
        change_request_id = _text(change_request_id, "change_request_id")
        phase_id = _text(phase_id, "phase_id")
        actor_id = _text(actor_id, "actor_id")
        reason = _text(reason, "reason")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        if not isinstance(evidence_sha256, str) or len(evidence_sha256) != 64:
            raise KubernetesReconciliationError("invalid_request", "evidence_sha256 is invalid")
        request_hash = _digest(canonical_json({
            "phase_id": phase_id,
            "change_request_id": change_request_id,
            "actor_id": actor_id,
            "evidence_sha256": evidence_sha256,
            "reason": reason,
        }))
        now = self._clock()
        try:
            self._approvals.authorize_reconciliation(
                phase_id, actor_id=actor_id, request_id=request_id,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "reconciliation_forbidden"))
            message = str(getattr(exc, "message", str(exc)))
            raise KubernetesReconciliationError(code, message) from exc
        with self._database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT reconciliation.*, execution.change_request_id,
                       execution.revision_id, execution.execution_timeout_seconds
                FROM kubernetes_change_reconciliations reconciliation
                JOIN kubernetes_change_executions execution
                  ON execution.id = reconciliation.execution_id
                WHERE execution.phase_id = ? AND execution.change_request_id = ?
                """,
                (phase_id, change_request_id),
            ).fetchone()
            if row is None:
                raise KubernetesReconciliationError("not_found", "Reconciliation evidence not found")
            if row["state"] == "accepted":
                if (
                    row["accepted_by"] != actor_id
                    or row["acceptance_idempotency_key"] != idempotency_key
                    or row["acceptance_request_hash"] != request_hash
                ):
                    raise KubernetesReconciliationError(
                        "idempotency_conflict", "Reconciliation was already accepted",
                    )
                conn.rollback()
                projected = _projection(row)
                projected["idempotent"] = True
                return projected
            if row["state"] != "observed" or row["evidence_sha256"] != evidence_sha256:
                raise KubernetesReconciliationError(
                    "reconciliation_stale", "Reconciliation evidence is not current",
                )
            sequence = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM change_plan_phases WHERE change_request_id = ?",
                (row["change_request_id"],),
            ).fetchone()[0]
            replanning_phase_id = self._id_factory("change-phase")
            conn.execute(
                """
                INSERT INTO change_plan_phases (
                    id, change_request_id, sequence, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'planning', ?, ?)
                """,
                (replanning_phase_id, row["change_request_id"], sequence, now, now),
            )
            conn.execute(
                """
                UPDATE kubernetes_change_reconciliations
                SET state = 'accepted', accepted_by = ?, acceptance_reason = ?,
                    acceptance_request_id = ?, acceptance_idempotency_key = ?,
                    acceptance_request_hash = ?, accepted_at = ?, replanning_phase_id = ?,
                    updated_at = ? WHERE id = ?
                """,
                (
                    actor_id, reason, request_id, idempotency_key, request_hash, now,
                    replanning_phase_id, now, row["id"],
                ),
            )
            conn.execute(
                """
                UPDATE kubernetes_change_execution_steps
                SET status = 'cancelled', completed_at = ?
                WHERE execution_id = ? AND status = 'pending'
                """,
                (now, row["execution_id"]),
            )
            conn.execute(
                "UPDATE change_requests SET updated_at = ? WHERE id = ?",
                (now, row["change_request_id"]),
            )
            if self._secure_inputs is not None:
                self._secure_inputs.release_revision_in(
                    conn, str(row["revision_id"]), released_at=now,
                    delete_after=now + int(row["execution_timeout_seconds"]),
                )
            _append_event(
                conn, str(row["change_request_id"]),
                "change_request.reconciliation_accepted", actor_id,
                {
                    "phase_id": phase_id,
                    "execution_id": str(row["execution_id"]),
                    "classification": str(row["classification"]),
                    "evidence_sha256": evidence_sha256,
                    "replanning_phase_id": replanning_phase_id,
                    "reason": reason,
                    "request_id": request_id,
                },
                now,
            )
            insert_admin_audit(
                conn, actor_id=actor_id, target_type="kubernetes_change_reconciliations",
                target_id=str(row["id"]), action="kubernetes_reconciliation_accept",
                reason=reason,
                before={"state": row["state"], "classification": row["classification"]},
                after={
                    "state": "accepted", "classification": row["classification"],
                    "evidence_sha256": evidence_sha256,
                },
                result="accepted", request_id=request_id,
            )
            accepted = conn.execute(
                "SELECT * FROM kubernetes_change_reconciliations WHERE id = ?", (row["id"],),
            ).fetchone()
            conn.commit()
        projected = _projection(accepted)
        projected["idempotent"] = False
        return projected


def _observation_evidence(result: dict[str, object]) -> dict[str, object]:
    if result.get("status") == "succeeded" and isinstance(result.get("stdout"), str):
        try:
            value = json.loads(str(result["stdout"]))
        except json.JSONDecodeError:
            value = None
        if (
            isinstance(value, dict)
            and set(value) == {
                "classification", "target", "effect_matches", "post_checks",
                "observed_at", "evidence_sha256",
            }
            and value.get("classification") in {"unknown_outcome", "effect_observed"}
            and isinstance(value.get("target"), dict)
            and isinstance(value.get("effect_matches"), bool)
            and isinstance(value.get("post_checks"), list)
            and value.get("evidence_sha256") == _digest(canonical_json({
                key: value[key] for key in value if key != "evidence_sha256"
            }))
        ):
            return value
    return {
        "classification": "unknown_outcome",
        "observation": "unavailable",
        "error_code": str(result.get("error_code") or "reconciliation_unavailable"),
    }


def _projection(row: Any) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "step_id": str(row["step_id"]),
        "classification": str(row["classification"]),
        "state": str(row["state"]),
        "evidence": json.loads(str(row["evidence_json"])),
        "evidence_sha256": str(row["evidence_sha256"]),
        "observed_at": float(row["observed_at"]) if row["observed_at"] is not None else None,
        "accepted_by": str(row["accepted_by"]) if row["accepted_by"] else None,
        "acceptance_reason": str(row["acceptance_reason"]) if row["acceptance_reason"] else None,
        "accepted_at": float(row["accepted_at"]) if row["accepted_at"] is not None else None,
        "replanning_phase_id": str(row["replanning_phase_id"]) if row["replanning_phase_id"] else None,
    }


def _change_namespace(change: dict[str, object]) -> str:
    target = change.get("target")
    namespace = target.get("namespace") if isinstance(target, dict) else None
    return namespace if isinstance(namespace, str) and namespace else "default"


def _append_event(
    conn: sqlite3.Connection,
    change_request_id: str,
    event_type: str,
    actor_id: str | None,
    payload: dict[str, object],
    now: float,
) -> None:
    conn.execute(
        """
        INSERT INTO change_request_events (
            change_request_id, type, actor_id, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (change_request_id, event_type, actor_id, canonical_json(payload), now),
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise KubernetesReconciliationError("invalid_request", f"{field} is required")
    return value.strip()
