"""Focused migration coverage for Generic Kubernetes Change state."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

from aiops.domain.identity import SQLiteIdentityStore
from apps.aiops_k8s_gateway import gateway_db
from apps.aiops_k8s_gateway.connector_commands import ConnectorCommands
from apps.aiops_k8s_gateway.identity_administration import IdentityAdministration
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store
from test_gateway_kubernetes_change_executions import _change, _json, _store


def test_upgrade_preserves_typed_commands_until_final_retirement(tmp_path: Path) -> None:
    future = {  # noqa: SLF001 - build an actual v13 database
        version: gateway_db._MIGRATIONS.pop(version)  # noqa: SLF001
        for version in tuple(gateway_db._MIGRATIONS)  # noqa: SLF001
        if version > 13
    }
    database = gateway_db.GatewayDatabase(tmp_path / "gateway.db")
    try:
        database.connect().close()
    finally:
        gateway_db._MIGRATIONS.update(future)  # noqa: SLF001

    with sqlite3.connect(database.db_path) as conn:
        conn.executescript(f"""
            INSERT INTO connector_enrollments (
                id, connector_id, cluster_id, credential_hash, active, created_at, updated_at
            ) VALUES ('enrollment-old', 'connector-old', 'cluster-old', '{'c' * 64}', 1, 900, 900);
            INSERT INTO clusters (
                cluster_id, connector_id, display_name, environment, last_heartbeat,
                created_at, updated_at
            ) VALUES ('cluster-old', 'connector-old', 'Old Cluster', 'prod', 900, 900, 900);
            INSERT INTO approvals (
                id, incident_id, investigation_id, action_id, action_version, action_hash,
                approver_id, authority_id, idempotency_key, request_hash,
                frozen_action_json, created_at
            ) VALUES (
                'approval-old', 'incident-old', 'investigation-old', 'action-old', 1,
                '{'a' * 64}', 'user-old', 'authority-old', 'approval-old', '{'b' * 64}',
                '{{}}', 950
            );
            INSERT INTO execution_grants (
                id, approval_id, action_hash, scope_json, expires_at, consumed_at, command_id
            ) VALUES (
                'grant-old', 'approval-old', '{'a' * 64}', '{{}}', 1200, 1000,
                'legacy-restart'
            );
        """)
        conn.execute(
            """
            INSERT INTO connector_commands (
                id, connector_id, cluster_id, namespace, action, parameters_json,
                rollback_plan_json, execution_grant_id, execution_grant_expires_at,
                action_hash, status, created_at, updated_at
            ) VALUES ('legacy-restart', 'connector-old', 'cluster-old', 'payments',
                      'restart_deployment', '{}', NULL, 'grant-old', 1200, ?,
                      'succeeded', 1000, 1001)
            """,
            ("a" * 64,),
        )

    with database.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM connector_commands WHERE id = 'legacy-restart'",
        ).fetchone() is None
        assert {19, 37}.issubset({
            int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")
        })
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_preserves_existing_validation_command_foreign_keys(tmp_path: Path) -> None:
    migrations = {  # noqa: SLF001 - exercise the v25 -> current upgrade
        version: gateway_db._MIGRATIONS.pop(version)  # noqa: SLF001
        for version in (26, 27, 28, 29, 32, 34, 37)
    }
    try:
        store = GatewayV1Store(
            tmp_path / "gateway.db", credential_factory=lambda: "connector-secret",
        )
        SQLiteIdentityStore(store.db_path).close()
        _, approver = IdentityAdministration(store.database).mutate(
            collection="users", target_id=None,
            payload={
                "username": "approver", "display_name": "Approver",
                "password": "strong-password",
            },
            actor_id="admin", reason="test", action="users_create",
            request_id="req-user",
        )
        _, credential = store.connector_enrollments.create(
            connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
            reason="test", request_id="req-enroll",
        )
        store.connector_enrollments.register(
            credential, "connector-prod", "cluster-prod", namespace_scope=["*"],
            capabilities=["validate", "execute"],
            commands=ConnectorCommands(store.database), request_id="req-register",
        )
        with store.database.connect() as conn:
            command_id = str(conn.execute(
                "SELECT id FROM connector_commands WHERE action = 'get_resource' LIMIT 1",
            ).fetchone()[0])
            conn.execute(
                "INSERT INTO incidents (id, title, severity, status, created_at, updated_at) "
                "VALUES ('incident-1', 'Checkout', 'critical', 'active', 1000, 1000)",
            )
            conn.execute(
                "INSERT INTO change_requests "
                "(id, incident_id, actor_id, desired_outcome, context, idempotency_key, "
                "created_at, updated_at) VALUES "
                "('change-1', 'incident-1', ?, 'scale', '', 'change-1', 1000, 1000)",
                (approver["id"],),
            )
            conn.execute(
                "INSERT INTO change_plan_phases "
                "(id, change_request_id, sequence, status, created_at, updated_at) "
                "VALUES ('phase-1', 'change-1', 1, 'validating', 1000, 1000)",
            )
            conn.execute(
                "INSERT INTO change_plan_revisions "
                "(id, change_request_id, phase_id, revision, status, plan_json, created_at) "
                "VALUES ('revision-1', 'change-1', 'phase-1', 1, 'validating', ?, 1000)",
                (_json({"summary": "scale", "changes": []}),),
            )
            conn.execute(
                """
                INSERT INTO kubernetes_change_validations (
                    id, change_request_id, phase_id, revision_id, ordinal, cluster_id,
                    command_id, draft_json, status, created_at, updated_at
                ) VALUES ('validation-1', 'change-1', 'phase-1', 'revision-1', 1,
                          'cluster-prod', ?, '{}', 'pending', 1000, 1000)
                """,
                (command_id,),
            )
    finally:
        gateway_db._MIGRATIONS.update(migrations)  # noqa: SLF001
    with store.database.connect() as conn:
        assert conn.execute(
            "SELECT command_id FROM kubernetes_change_validations WHERE id = 'validation-1'",
        ).fetchone()[0] == command_id
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        targets = {
            row["table"]
            for row in conn.execute("PRAGMA foreign_key_list(kubernetes_change_validations)")
        }
        assert "connector_commands" in targets and "connector_commands_v19" not in targets


def test_v28_migrates_single_execution_to_plan_step_without_behavior_loss(
    tmp_path: Path,
) -> None:
    migrations = {  # noqa: SLF001
        version: gateway_db._MIGRATIONS.pop(version)  # noqa: SLF001
        for version in (28, 29, 32, 34, 37)
    }
    try:
        store, approver_id = _store(tmp_path, verify_connector=False)
        change_hash = hashlib.sha256(_json(_change()).encode()).hexdigest()
        with store.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO kubernetes_change_executions (
                    id, change_request_id, phase_id, revision_id, approval_id,
                    connector_id, cluster_id, command_id, actor_id, reason, request_id,
                    idempotency_key, request_hash, change_hash, execution_timeout_seconds,
                    status, created_at
                ) VALUES ('execution-old', 'change-1', 'phase-1', 'revision-1', 'approval-1',
                          'connector-prod', 'cluster-prod', 'command-old', ?, 'start', 'req-old',
                          'start-old', ?, ?, 300, 'queued', 1001)
                """,
                (approver_id, "b" * 64, change_hash),
            )
            conn.execute(
                """
                INSERT INTO kubernetes_execution_grants (
                    id, execution_id, phase_id, approval_id, command_id,
                    change_hash, issued_at, expires_at
                ) VALUES ('grant-old', 'execution-old', 'phase-1', 'approval-1',
                          'command-old', ?, 1001, 1061)
                """,
                (change_hash,),
            )
    finally:
        gateway_db._MIGRATIONS.update(migrations)  # noqa: SLF001

    with store.database.connect() as conn:
        plan = conn.execute(
            "SELECT id, status, rollback_policy FROM kubernetes_change_executions",
        ).fetchone()
        step = conn.execute(
            "SELECT execution_id, ordinal, direction, command_id, change_hash, status "
            "FROM kubernetes_change_execution_steps",
        ).fetchone()
        grant = conn.execute(
            "SELECT execution_id, step_id, command_id FROM kubernetes_execution_grants",
        ).fetchone()
        assert tuple(plan) == ("execution-old", "queued", "stop_only")
        assert tuple(step) == (
            "execution-old", 1, "forward", "command-old", change_hash, "queued",
        )
        assert tuple(grant) == (
            "execution-old", "execution-old:forward:1", "command-old",
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
