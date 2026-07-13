"""Shared Gateway schema for generic Kubernetes Change execution transport."""

from __future__ import annotations

from .gateway_db import register_migrations


_SCHEMA_VERSION = 26
_SCHEMA = """
ALTER TABLE change_plan_phases ADD COLUMN execution_status TEXT
    CHECK (execution_status IS NULL OR execution_status IN (
        'executing', 'succeeded', 'failed', 'unknown_outcome'
    ));

CREATE TABLE kubernetes_change_executions (
    id TEXT PRIMARY KEY,
    change_request_id TEXT NOT NULL REFERENCES change_requests(id),
    phase_id TEXT NOT NULL UNIQUE REFERENCES change_plan_phases(id),
    revision_id TEXT NOT NULL REFERENCES change_plan_revisions(id),
    approval_id TEXT NOT NULL UNIQUE REFERENCES kubernetes_phase_approvals(id),
    connector_id TEXT NOT NULL REFERENCES connector_enrollments(connector_id),
    cluster_id TEXT NOT NULL REFERENCES clusters(cluster_id),
    command_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    request_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    change_hash TEXT NOT NULL CHECK (length(change_hash) = 64),
    execution_timeout_seconds INTEGER NOT NULL CHECK (execution_timeout_seconds BETWEEN 300 AND 1800),
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'dispatched', 'started', 'succeeded', 'failed', 'stale',
        'post_check_failed', 'unknown_outcome'
    )),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    UNIQUE(actor_id, idempotency_key)
);
CREATE INDEX kubernetes_change_executions_by_status
    ON kubernetes_change_executions(connector_id, cluster_id, status, created_at, id);

CREATE TABLE kubernetes_execution_grants (
    id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL UNIQUE REFERENCES kubernetes_change_executions(id),
    phase_id TEXT NOT NULL REFERENCES change_plan_phases(id),
    approval_id TEXT NOT NULL REFERENCES kubernetes_phase_approvals(id),
    command_id TEXT NOT NULL UNIQUE,
    change_hash TEXT NOT NULL CHECK (length(change_hash) = 64),
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL,
    CHECK (expires_at > issued_at)
);

PRAGMA legacy_alter_table = ON;
ALTER TABLE connector_commands RENAME TO connector_commands_v19;
ALTER TABLE command_leases RENAME TO command_leases_v19;
DROP INDEX connector_commands_poll;

CREATE TABLE connector_commands (
    id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN (
        'get_resource', 'validate_kubernetes_change', 'execute_kubernetes_change',
        'restart_deployment', 'scale_deployment', 'rollback_deployment'
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
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK (
        (action IN ('get_resource', 'validate_kubernetes_change')
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
    id, connector_id, cluster_id, namespace, action, parameters_json, rollback_plan_json,
    execution_grant_id, execution_grant_expires_at, action_hash, status, lease_id,
    lease_expires_at, attempt_count, result_json, result_hash, result_received_at,
    created_at, updated_at
)
SELECT id, connector_id, cluster_id, namespace, action, parameters_json, rollback_plan_json,
       execution_grant_id, execution_grant_expires_at, action_hash, status, lease_id,
       lease_expires_at, attempt_count, result_json, result_hash, result_received_at,
       created_at, updated_at
FROM connector_commands_v19;
CREATE INDEX connector_commands_poll
    ON connector_commands(connector_id, cluster_id, status, lease_expires_at, created_at);

DROP INDEX kubernetes_change_validations_by_command;
ALTER TABLE kubernetes_change_validations RENAME TO kubernetes_change_validations_v21;
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
INSERT INTO kubernetes_change_validations SELECT * FROM kubernetes_change_validations_v21;
CREATE INDEX kubernetes_change_validations_by_command
    ON kubernetes_change_validations(command_id) WHERE command_id IS NOT NULL;

CREATE TABLE command_leases (
    lease_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL,
    connector_id TEXT NOT NULL,
    granted_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    started_at REAL,
    FOREIGN KEY (command_id) REFERENCES connector_commands(id) ON DELETE CASCADE
);
INSERT INTO command_leases SELECT * FROM command_leases_v19;
DROP TABLE command_leases_v19;
DROP TABLE kubernetes_change_validations_v21;
DROP TABLE connector_commands_v19;
PRAGMA legacy_alter_table = OFF;
"""
register_migrations(((_SCHEMA_VERSION, _SCHEMA),))


_ORCHESTRATION_STATUS_VERSION = 27
_ORCHESTRATION_STATUS_SCHEMA = """
ALTER TABLE change_plan_phases ADD COLUMN orchestration_status TEXT
    CHECK (orchestration_status IS NULL OR orchestration_status IN (
        'cancel_requested', 'cancelled', 'rolling_back', 'rolled_back', 'rollback_failed'
    ));
"""
register_migrations(((_ORCHESTRATION_STATUS_VERSION, _ORCHESTRATION_STATUS_SCHEMA),))


_PLAN_SCHEMA_VERSION = 28
_PLAN_SCHEMA = """
PRAGMA legacy_alter_table = ON;
ALTER TABLE connector_commands RENAME TO connector_commands_v26;
ALTER TABLE command_leases RENAME TO command_leases_v26;
ALTER TABLE kubernetes_change_validations RENAME TO kubernetes_change_validations_v26;
ALTER TABLE kubernetes_execution_grants RENAME TO kubernetes_execution_grants_v26;
ALTER TABLE kubernetes_change_executions RENAME TO kubernetes_change_executions_v26;
DROP INDEX connector_commands_poll;
DROP INDEX kubernetes_change_validations_by_command;
DROP INDEX kubernetes_change_executions_by_status;

CREATE TABLE kubernetes_change_executions (
    id TEXT PRIMARY KEY,
    change_request_id TEXT NOT NULL REFERENCES change_requests(id),
    phase_id TEXT NOT NULL UNIQUE REFERENCES change_plan_phases(id),
    revision_id TEXT NOT NULL REFERENCES change_plan_revisions(id),
    approval_id TEXT NOT NULL UNIQUE REFERENCES kubernetes_phase_approvals(id),
    connector_id TEXT NOT NULL REFERENCES connector_enrollments(connector_id),
    cluster_id TEXT NOT NULL REFERENCES clusters(cluster_id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    request_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    execution_timeout_seconds INTEGER NOT NULL CHECK (execution_timeout_seconds BETWEEN 300 AND 1800),
    rollback_policy TEXT NOT NULL CHECK (rollback_policy IN ('stop_only', 'rollback_completed')),
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'dispatched', 'started', 'succeeded', 'failed', 'stale',
        'post_check_failed', 'unknown_outcome', 'cancel_requested', 'cancelled',
        'rolling_back', 'rolled_back', 'rollback_failed'
    )),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    cancel_requested_at REAL,
    cancelled_at REAL,
    UNIQUE(actor_id, idempotency_key)
);
CREATE INDEX kubernetes_change_executions_by_status
    ON kubernetes_change_executions(connector_id, cluster_id, status, created_at, id);

CREATE TABLE kubernetes_change_execution_steps (
    id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES kubernetes_change_executions(id),
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    direction TEXT NOT NULL CHECK (direction IN ('forward', 'rollback')),
    source_step_id TEXT REFERENCES kubernetes_change_execution_steps(id),
    command_id TEXT NOT NULL UNIQUE,
    change_hash TEXT NOT NULL CHECK (length(change_hash) = 64),
    change_json TEXT NOT NULL CHECK (json_valid(change_json)),
    inverse_change_json TEXT CHECK (inverse_change_json IS NULL OR json_valid(inverse_change_json)),
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'queued', 'dispatched', 'started', 'succeeded', 'failed',
        'stale', 'post_check_failed', 'unknown_outcome', 'rolled_back', 'cancelled'
    )),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    UNIQUE(execution_id, direction, ordinal),
    UNIQUE(source_step_id)
);
CREATE INDEX kubernetes_change_execution_steps_by_status
    ON kubernetes_change_execution_steps(execution_id, direction, status, ordinal);

CREATE TABLE kubernetes_execution_grants (
    id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES kubernetes_change_executions(id),
    step_id TEXT NOT NULL UNIQUE REFERENCES kubernetes_change_execution_steps(id),
    phase_id TEXT NOT NULL REFERENCES change_plan_phases(id),
    approval_id TEXT NOT NULL REFERENCES kubernetes_phase_approvals(id),
    command_id TEXT NOT NULL UNIQUE,
    change_hash TEXT NOT NULL CHECK (length(change_hash) = 64),
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL,
    revoked_at REAL,
    CHECK (expires_at > issued_at)
);

INSERT INTO kubernetes_change_executions (
    id, change_request_id, phase_id, revision_id, approval_id, connector_id,
    cluster_id, actor_id, reason, request_id, idempotency_key, request_hash,
    execution_timeout_seconds, rollback_policy, status, result_json,
    created_at, started_at, completed_at
)
SELECT execution.id, execution.change_request_id, execution.phase_id,
       execution.revision_id, execution.approval_id, execution.connector_id,
       execution.cluster_id, execution.actor_id, execution.reason,
       execution.request_id, execution.idempotency_key, execution.request_hash,
       execution.execution_timeout_seconds, approval.rollback_policy,
       execution.status, execution.result_json, execution.created_at,
       execution.started_at, execution.completed_at
FROM kubernetes_change_executions_v26 execution
JOIN kubernetes_phase_approvals approval ON approval.id = execution.approval_id;

INSERT INTO kubernetes_change_execution_steps (
    id, execution_id, ordinal, direction, command_id, change_hash, change_json,
    status, result_json, created_at, started_at, completed_at
)
SELECT execution.id || ':forward:1', execution.id, 1, 'forward',
       execution.command_id, execution.change_hash,
       json_extract(approval.frozen_changes_json, '$[0].canonical_change'),
       execution.status, execution.result_json, execution.created_at,
       execution.started_at, execution.completed_at
FROM kubernetes_change_executions_v26 execution
JOIN kubernetes_phase_approvals approval ON approval.id = execution.approval_id;

INSERT INTO kubernetes_execution_grants (
    id, execution_id, step_id, phase_id, approval_id, command_id,
    change_hash, issued_at, expires_at, consumed_at
)
SELECT grant.id, grant.execution_id, grant.execution_id || ':forward:1',
       grant.phase_id, grant.approval_id, grant.command_id, grant.change_hash,
       grant.issued_at, grant.expires_at, grant.consumed_at
FROM kubernetes_execution_grants_v26 grant;

CREATE TABLE connector_commands (
    id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN (
        'get_resource', 'validate_kubernetes_change', 'execute_kubernetes_change',
        'restart_deployment', 'scale_deployment', 'rollback_deployment'
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
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK (
        (action IN ('get_resource', 'validate_kubernetes_change')
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
INSERT INTO connector_commands SELECT * FROM connector_commands_v26;
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
INSERT INTO kubernetes_change_validations SELECT * FROM kubernetes_change_validations_v26;
CREATE INDEX kubernetes_change_validations_by_command
    ON kubernetes_change_validations(command_id) WHERE command_id IS NOT NULL;

CREATE TABLE command_leases (
    lease_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL,
    connector_id TEXT NOT NULL,
    granted_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    started_at REAL,
    FOREIGN KEY (command_id) REFERENCES connector_commands(id) ON DELETE CASCADE
);
INSERT INTO command_leases SELECT * FROM command_leases_v26;

DROP TABLE command_leases_v26;
DROP TABLE kubernetes_change_validations_v26;
DROP TABLE connector_commands_v26;
DROP TABLE kubernetes_execution_grants_v26;
DROP TABLE kubernetes_change_executions_v26;
PRAGMA legacy_alter_table = OFF;
"""
PLAN_EXECUTION_MIGRATIONS = ((_PLAN_SCHEMA_VERSION, _PLAN_SCHEMA),)


def register_plan_execution_migrations() -> None:
    register_migrations(PLAN_EXECUTION_MIGRATIONS)
