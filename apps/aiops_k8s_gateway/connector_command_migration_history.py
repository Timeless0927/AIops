"""Retained SQLite history required to upgrade pre-K08 Gateway databases."""

from __future__ import annotations


LEGACY_MIGRATIONS = (
    (
        11,
        """
        CREATE TABLE approval_authorities (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            environment TEXT NOT NULL CHECK (environment IN ('*', 'prod', 'staging', 'dev', 'test')),
            scope_type TEXT NOT NULL CHECK (scope_type IN ('platform', 'team', 'service', 'deployment_target')),
            scope_id TEXT,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            CHECK ((scope_type = 'platform' AND scope_id IS NULL) OR (scope_type != 'platform' AND scope_id IS NOT NULL)),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX approval_authority_identity
            ON approval_authorities(user_id, environment, scope_type, IFNULL(scope_id, ''));

        CREATE TABLE approvals (
            id TEXT PRIMARY KEY,
            incident_id TEXT NOT NULL,
            investigation_id TEXT NOT NULL,
            action_id TEXT NOT NULL,
            action_version INTEGER NOT NULL,
            action_hash TEXT NOT NULL CHECK (length(action_hash) = 64),
            approver_id TEXT NOT NULL,
            authority_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
            frozen_action_json TEXT NOT NULL CHECK (json_valid(frozen_action_json)),
            created_at REAL NOT NULL,
            UNIQUE (approver_id, idempotency_key),
            UNIQUE (investigation_id, action_id, action_version),
            FOREIGN KEY (incident_id) REFERENCES incidents(id),
            FOREIGN KEY (investigation_id) REFERENCES investigations(id),
            FOREIGN KEY (approver_id) REFERENCES users(id),
            FOREIGN KEY (authority_id) REFERENCES approval_authorities(id)
        );

        CREATE TABLE execution_grants (
            id TEXT PRIMARY KEY,
            approval_id TEXT NOT NULL UNIQUE,
            action_hash TEXT NOT NULL CHECK (length(action_hash) = 64),
            scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
            expires_at REAL NOT NULL,
            consumed_at REAL NOT NULL,
            command_id TEXT NOT NULL UNIQUE,
            FOREIGN KEY (approval_id) REFERENCES approvals(id)
        );
        """,
    ),
    (
        12,
        """
        ALTER TABLE connector_commands RENAME TO connector_commands_v10;
        ALTER TABLE command_leases RENAME TO command_leases_v10;
        DROP INDEX connector_commands_poll;

        CREATE TABLE connector_commands (
            id TEXT PRIMARY KEY,
            connector_id TEXT NOT NULL,
            cluster_id TEXT NOT NULL,
            namespace TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('get_resource', 'restart_deployment')),
            parameters_json TEXT NOT NULL CHECK (json_valid(parameters_json)),
            execution_grant_id TEXT UNIQUE,
            execution_grant_expires_at REAL,
            action_hash TEXT,
            status TEXT NOT NULL CHECK (status IN ('queued', 'leased', 'started', 'succeeded', 'failed', 'rejected')),
            lease_id TEXT,
            lease_expires_at REAL,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
            result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
            result_hash TEXT,
            result_received_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            CHECK (
                (action = 'get_resource' AND execution_grant_id IS NULL AND execution_grant_expires_at IS NULL AND action_hash IS NULL)
                OR (action = 'restart_deployment' AND execution_grant_id IS NOT NULL AND execution_grant_expires_at IS NOT NULL AND length(action_hash) = 64)
            ),
            FOREIGN KEY (connector_id) REFERENCES connector_enrollments(connector_id),
            FOREIGN KEY (cluster_id) REFERENCES clusters(cluster_id),
            FOREIGN KEY (execution_grant_id) REFERENCES execution_grants(id)
        );
        INSERT INTO connector_commands (
            id, connector_id, cluster_id, namespace, action, parameters_json, status,
            lease_id, lease_expires_at, attempt_count, result_json, result_hash,
            result_received_at, created_at, updated_at
        )
        SELECT id, connector_id, cluster_id, namespace, action, parameters_json, status,
               lease_id, lease_expires_at, attempt_count, result_json, result_hash,
               result_received_at, created_at, updated_at
        FROM connector_commands_v10;
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
        INSERT INTO command_leases SELECT * FROM command_leases_v10;
        DROP TABLE command_leases_v10;
        DROP TABLE connector_commands_v10;
        """,
    ),
    (
        13,
        """
        ALTER TABLE connector_commands RENAME TO connector_commands_v12;
        ALTER TABLE command_leases RENAME TO command_leases_v12;
        DROP INDEX connector_commands_poll;

        CREATE TABLE connector_commands (
            id TEXT PRIMARY KEY,
            connector_id TEXT NOT NULL,
            cluster_id TEXT NOT NULL,
            namespace TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN (
                'get_resource', 'restart_deployment', 'scale_deployment', 'rollback_deployment'
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
                (action = 'get_resource' AND execution_grant_id IS NULL AND execution_grant_expires_at IS NULL AND action_hash IS NULL)
                OR (action != 'get_resource' AND execution_grant_id IS NOT NULL AND execution_grant_expires_at IS NOT NULL AND length(action_hash) = 64)
            ),
            FOREIGN KEY (connector_id) REFERENCES connector_enrollments(connector_id),
            FOREIGN KEY (cluster_id) REFERENCES clusters(cluster_id),
            FOREIGN KEY (execution_grant_id) REFERENCES execution_grants(id)
        );
        INSERT INTO connector_commands (
            id, connector_id, cluster_id, namespace, action, parameters_json,
            execution_grant_id, execution_grant_expires_at, action_hash, status,
            lease_id, lease_expires_at, attempt_count, result_json, result_hash,
            result_received_at, created_at, updated_at
        )
        SELECT id, connector_id, cluster_id, namespace, action, parameters_json,
               execution_grant_id, execution_grant_expires_at, action_hash, status,
               lease_id, lease_expires_at, attempt_count, result_json, result_hash,
               result_received_at, created_at, updated_at
        FROM connector_commands_v12;
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
        INSERT INTO command_leases SELECT * FROM command_leases_v12;
        DROP TABLE command_leases_v12;
        DROP TABLE connector_commands_v12;
        """,
    ),
)
