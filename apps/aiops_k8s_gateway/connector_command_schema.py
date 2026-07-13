"""Connector Command schema extension for read-like Kubernetes validation."""

from __future__ import annotations

from .connector_command_migration_history import LEGACY_MIGRATIONS
from .gateway_db import register_migrations


_SCHEMA_VERSION = 19
_SCHEMA = """
ALTER TABLE connector_commands RENAME TO connector_commands_v13;
ALTER TABLE command_leases RENAME TO command_leases_v13;
DROP INDEX connector_commands_poll;

CREATE TABLE connector_commands (
    id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN (
        'get_resource', 'validate_kubernetes_change',
        'restart_deployment', 'scale_deployment', 'rollback_deployment'
    )),
    parameters_json TEXT NOT NULL CHECK (json_valid(parameters_json)),
    rollback_plan_json TEXT CHECK (rollback_plan_json IS NULL OR json_valid(rollback_plan_json)),
    execution_grant_id TEXT UNIQUE,
    execution_grant_expires_at REAL,
    action_hash TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'leased', 'started', 'succeeded', 'failed', 'rejected', 'unknown_outcome'
    )),
    lease_id TEXT,
    lease_expires_at REAL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    result_hash TEXT,
    result_received_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK (
        (action IN ('get_resource', 'validate_kubernetes_change')
         AND execution_grant_id IS NULL AND execution_grant_expires_at IS NULL AND action_hash IS NULL)
        OR (action NOT IN ('get_resource', 'validate_kubernetes_change')
            AND execution_grant_id IS NOT NULL AND execution_grant_expires_at IS NOT NULL
            AND length(action_hash) = 64)
    ),
    FOREIGN KEY (connector_id) REFERENCES connector_enrollments(connector_id),
    FOREIGN KEY (cluster_id) REFERENCES clusters(cluster_id),
    FOREIGN KEY (execution_grant_id) REFERENCES execution_grants(id)
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
FROM connector_commands_v13;
CREATE INDEX connector_commands_poll
    ON connector_commands(connector_id, cluster_id, status, lease_expires_at, created_at);

CREATE TABLE command_leases (
    lease_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL,
    connector_id TEXT NOT NULL,
    granted_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    started_at REAL,
    FOREIGN KEY (command_id) REFERENCES connector_commands(id) ON DELETE CASCADE
);
INSERT INTO command_leases SELECT * FROM command_leases_v13;
DROP TABLE command_leases_v13;
DROP TABLE connector_commands_v13;
"""
register_migrations((*LEGACY_MIGRATIONS, (_SCHEMA_VERSION, _SCHEMA)))
