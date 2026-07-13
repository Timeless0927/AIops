"""K04 Gateway single Kubernetes Change execution tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aiops.domain.identity import SQLiteIdentityStore
from apps.aiops_k8s_gateway.connector_commands import ConnectorCommands
from apps.aiops_k8s_gateway.kubernetes_change_executions import (
    KubernetesChangeExecutionError,
    KubernetesChangeExecutions,
)
from apps.aiops_k8s_gateway.kubernetes_phase_approvals import KubernetesPhaseApprovalError
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store
from apps.aiops_k8s_gateway import gateway_db


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _change() -> dict[str, object]:
    return {
        "target": {
            "api_version": "apps/v1", "kind": "Deployment", "namespace": "payments",
            "name": "checkout-api", "uid": "uid-1", "resource_version": "41",
        },
        "operation": "patch",
        "payload": [
            {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "41"},
            {"op": "test", "path": "/spec/replicas", "value": 3},
            {"op": "replace", "path": "/spec/replicas", "value": 5},
        ],
        "post_checks": [
            {"type": "json_pointer", "path": "/spec/replicas", "operator": "eq", "value": 5},
        ],
    }


class ApprovalBoundary:
    def __init__(self, approver_id: str, *, deny_dispatch: bool = False) -> None:
        self.approver_id = approver_id
        self.deny_dispatch = deny_dispatch
        self.calls: list[tuple[str, str]] = []

    def authorize_start(self, phase_id: str, *, request_id: str, stage: str) -> dict[str, object]:
        self.calls.append((stage, request_id))
        if stage == "dispatch" and self.deny_dispatch:
            raise KubernetesPhaseApprovalError("authority_revoked", "Authority revoked")
        change = _change()
        return {
            "id": "approval-1", "phase_id": phase_id, "revision_id": "revision-1",
            "approver_id": self.approver_id, "change_request_id": "change-1",
            "cluster_id": "cluster-prod", "environment": "prod",
            "frozen_changes": [{
                "ordinal": 1, "canonical_change": change, "dry_run_hash": "d" * 64,
            }],
        }


def _store(tmp_path: Path) -> tuple[GatewayV1Store, str]:
    store = GatewayV1Store(tmp_path / "gateway.db", credential_factory=lambda: "connector-secret")
    SQLiteIdentityStore(store.db_path).close()
    _, approver = store.mutate_admin(
        collection="users", target_id=None,
        payload={"username": "approver", "display_name": "Approver", "password": "strong-password"},
        actor_id="admin", reason="test", action="users_create", request_id="req-user",
    )
    _, credential = store.connector_enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="test", request_id="req-enroll",
    )
    commands = ConnectorCommands(store.database)
    store.connector_enrollments.register(
        credential, "connector-prod", "cluster-prod", namespace_scope=["*"],
        capabilities=["validate", "execute"], commands=commands, request_id="req-register",
    )
    verification = commands.poll("connector-prod", "cluster-prod", 0)
    assert verification is not None
    commands.start(
        str(verification["id"]), "connector-prod", "cluster-prod", str(verification["lease_id"]),
    )
    commands.submit_result(
        str(verification["id"]), "connector-prod", "cluster-prod", str(verification["lease_id"]),
        {
            "status": "succeeded", "stdout": '{"apiVersion":"v1","kind":"PodList","items":[]}',
            "stderr": "", "exit_code": 0, "truncated": False,
            "error_code": None, "error_message": None,
        },
        request_id="req-verify", result_handler=store.connector_enrollments.record_verification_result_in,
    )
    now = 1_000.0
    frozen = [{
        "ordinal": 1, "canonical_change": _change(), "dry_run_hash": "d" * 64,
    }]
    with store.database.connect() as conn:
        conn.execute(
            "INSERT INTO incidents (id, title, severity, status, created_at, updated_at) "
            "VALUES ('incident-1', 'Checkout', 'critical', 'active', ?, ?)", (now, now),
        )
        conn.execute(
            "INSERT INTO change_requests "
            "(id, incident_id, actor_id, desired_outcome, context, idempotency_key, created_at, updated_at) "
            "VALUES ('change-1', 'incident-1', ?, 'scale', '', 'change-1', ?, ?)",
            (approver["id"], now, now),
        )
        conn.execute(
            "INSERT INTO change_plan_phases "
            "(id, change_request_id, sequence, status, approval_status, created_at, updated_at) "
            "VALUES ('phase-1', 'change-1', 1, 'awaiting_approval', 'approved', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO change_plan_revisions "
            "(id, change_request_id, phase_id, revision, status, plan_json, created_at) "
            "VALUES ('revision-1', 'change-1', 'phase-1', 1, 'validating', ?, ?)",
            (_json({"summary": "scale", "changes": []}), now),
        )
        conn.execute(
            """
            INSERT INTO kubernetes_phase_approvals (
                id, phase_id, revision_id, approver_id, authority_ids_json,
                idempotency_key, request_hash, reason, request_id, rollback_policy,
                target_confirmations_json, frozen_changes_json, dry_run_expires_at,
                approved_at, start_expires_at
            ) VALUES ('approval-1', 'phase-1', 'revision-1', ?, '[]', 'approval-1', ?,
                      'approved', 'req-approval', 'stop_only', '[]', ?, 1500, 1000, 1900)
            """,
            (approver["id"], "a" * 64, _json(frozen)),
        )
    return store, str(approver["id"])


def _executions(
    store: GatewayV1Store,
    boundary: ApprovalBoundary,
    *,
    now: float = 1_001.0,
) -> KubernetesChangeExecutions:
    counts: dict[str, int] = {}

    def next_id(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}-{counts[prefix]}"

    return KubernetesChangeExecutions(
        store.database, approvals=boundary, enrollments=store.connector_enrollments,
        clock=lambda: now, id_factory=next_id,
    )


def test_start_issues_one_60_second_grant_and_is_idempotent(tmp_path: Path) -> None:
    store, approver_id = _store(tmp_path)
    boundary = ApprovalBoundary(approver_id)
    executions = _executions(store, boundary)

    started = executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start approved change", idempotency_key="start-1",
        request_id="req-start", execution_timeout_seconds=300,
    )
    replay = executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start approved change", idempotency_key="start-1",
        request_id="req-replay", execution_timeout_seconds=300,
    )

    assert started["status"] == "queued"
    assert started["grant"]["expires_at"] == 1_061.0  # type: ignore[index]
    assert started["grant"]["consumed_at"] is None  # type: ignore[index]
    assert replay["id"] == started["id"] and replay["idempotent"] is True
    assert boundary.calls == [("grant", "req-start")]
    with pytest.raises(KubernetesChangeExecutionError) as second:
        executions.start(
            "change-1", phase_id="phase-1", actor_id=approver_id,
            reason="second start", idempotency_key="start-2",
            request_id="req-second", execution_timeout_seconds=300,
        )
    assert second.value.code == "execution_exists"


def test_dispatch_rechecks_authority_and_consumes_grant_once(tmp_path: Path) -> None:
    store, approver_id = _store(tmp_path)
    boundary = ApprovalBoundary(approver_id)
    executions = _executions(store, boundary)
    started = executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start", idempotency_key="start", request_id="req-start",
        execution_timeout_seconds=300,
    )

    command = executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-dispatch",
    )
    repeated = executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-dispatch-again",
    )

    assert command is not None and command["action"] == "execute_kubernetes_change"
    assert command["execution_grant_id"] == started["grant"]["id"]  # type: ignore[index]
    assert repeated is None
    assert boundary.calls == [("grant", "req-start"), ("dispatch", "req-dispatch")]
    projected = executions.for_phase("phase-1")
    assert projected is not None and projected["grant"]["consumed_at"] == 1_001.0  # type: ignore[index]


def test_revoked_dispatch_never_delivers_command(tmp_path: Path) -> None:
    store, approver_id = _store(tmp_path)
    boundary = ApprovalBoundary(approver_id, deny_dispatch=True)
    executions = _executions(store, boundary)
    executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start", idempotency_key="start", request_id="req-start",
        execution_timeout_seconds=300,
    )

    assert executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-dispatch",
    ) is None
    projected = executions.for_phase("phase-1")
    assert projected is not None and projected["status"] == "failed"
    assert projected["result"]["error_code"] == "authority_revoked"  # type: ignore[index]
    assert projected["grant"]["consumed_at"] is None  # type: ignore[index]


@pytest.mark.parametrize("timeout", [299, 1801, True])
def test_execution_timeout_is_bounded(tmp_path: Path, timeout: object) -> None:
    store, approver_id = _store(tmp_path)
    executions = _executions(store, ApprovalBoundary(approver_id))
    with pytest.raises(KubernetesChangeExecutionError) as invalid:
        executions.start(
            "change-1", phase_id="phase-1", actor_id=approver_id,
            reason="start", idempotency_key="start", request_id="req-start",
            execution_timeout_seconds=timeout,
        )
    assert invalid.value.code == "invalid_execution_timeout"


def test_start_requires_execute_capability(tmp_path: Path) -> None:
    store, approver_id = _store(tmp_path)
    with store.database.connect() as conn:
        row = conn.execute(
            "SELECT identity_json FROM connector_read_verifications WHERE cluster_id = 'cluster-prod'",
        ).fetchone()
        identity = json.loads(str(row["identity_json"]))
        identity["capabilities"] = ["validate"]
        conn.execute(
            "UPDATE connector_read_verifications SET identity_json = ? WHERE cluster_id = 'cluster-prod'",
            (_json(identity),),
        )
    executions = _executions(store, ApprovalBoundary(approver_id))

    with pytest.raises(KubernetesChangeExecutionError) as unavailable:
        executions.start(
            "change-1", phase_id="phase-1", actor_id=approver_id,
            reason="start", idempotency_key="start", request_id="req-start",
            execution_timeout_seconds=300,
        )
    assert unavailable.value.code == "cluster_not_ready"


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"status": "succeeded", "stdout": '{"post_checks":[]}', "stderr": "", "exit_code": 0,
          "truncated": False, "error_code": None, "error_message": None}, "succeeded"),
        ({"status": "rejected", "stdout": "", "stderr": "", "exit_code": None,
          "truncated": False, "error_code": "stale_change", "error_message": "stale"}, "stale"),
        ({"status": "failed", "stdout": '{"post_checks":[]}', "stderr": "", "exit_code": 1,
          "truncated": False, "error_code": "post_check_failed", "error_message": "failed"}, "post_check_failed"),
        ({"status": "failed", "stdout": "", "stderr": "", "exit_code": None,
          "truncated": False, "error_code": "kubernetes_api_rejected", "error_message": "rejected"}, "failed"),
    ],
)
def test_started_and_terminal_results_update_execution_and_phase(
    tmp_path: Path, result: dict[str, object], expected: str,
) -> None:
    store, approver_id = _store(tmp_path)
    executions = _executions(store, ApprovalBoundary(approver_id))
    executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start", idempotency_key="start", request_id="req-start",
        execution_timeout_seconds=300,
    )
    command = executions.dispatch_next("connector-prod", "cluster-prod", request_id="req-dispatch")
    assert command is not None
    commands = ConnectorCommands(store.database, clock=lambda: 1_002.0)
    commands.start(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        start_handler=executions.record_started_in,
    )
    commands.submit_result(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        result, request_id="req-result", result_handler=executions.record_result_in,
    )

    projected = executions.for_phase("phase-1")
    assert projected is not None and projected["status"] == expected
    assert projected["started_at"] == 1_002.0
    with store.database.connect() as conn:
        phase_status = conn.execute(
            "SELECT execution_status FROM change_plan_phases WHERE id = 'phase-1'",
        ).fetchone()[0]
        execution_expires_at = conn.execute(
            "SELECT execution_expires_at FROM connector_commands WHERE id = ?",
            (command["id"],),
        ).fetchone()[0]
    assert phase_status == ("succeeded" if expected == "succeeded" else "failed")
    assert execution_expires_at == 1_302.0


def test_started_timeout_remains_unknown_outcome_until_reconciled(tmp_path: Path) -> None:
    store, approver_id = _store(tmp_path)
    boundary = ApprovalBoundary(approver_id)
    executions = _executions(store, boundary)
    executions.start(
        "change-1", phase_id="phase-1", actor_id=approver_id,
        reason="start", idempotency_key="start", request_id="req-start",
        execution_timeout_seconds=300,
    )
    command = executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-dispatch",
    )
    assert command is not None
    ConnectorCommands(store.database, clock=lambda: 1_002.0).start(
        str(command["id"]), "connector-prod", "cluster-prod", str(command["lease_id"]),
        start_handler=executions.record_started_in,
    )
    ConnectorCommands(store.database, clock=lambda: 1_303.0).reconcile_unknown_outcomes()
    later = _executions(store, boundary, now=1_303.0)

    assert later.dispatch_next(
        "connector-prod", "cluster-prod", request_id="req-reconcile",
    ) is None
    projected = later.for_phase("phase-1")
    assert projected is not None and projected["status"] == "unknown_outcome"
    with store.database.connect() as conn:
        assert conn.execute(
            "SELECT execution_status FROM change_plan_phases WHERE id = 'phase-1'",
        ).fetchone()[0] == "unknown_outcome"
        assert conn.execute(
            "SELECT type FROM change_request_events WHERE change_request_id = 'change-1' "
            "ORDER BY event_id DESC LIMIT 1",
        ).fetchone()[0] == "change_request.execution_outcome_unknown"


def test_migration_preserves_existing_validation_command_foreign_keys(tmp_path: Path) -> None:
    migration = gateway_db._MIGRATIONS.pop(26)  # noqa: SLF001 - exercise the v25 -> current upgrade
    status_migration = gateway_db._MIGRATIONS.pop(27)  # noqa: SLF001
    plan_migration = gateway_db._MIGRATIONS.pop(28)  # noqa: SLF001
    try:
        store = GatewayV1Store(tmp_path / "gateway.db", credential_factory=lambda: "connector-secret")
        SQLiteIdentityStore(store.db_path).close()
        _, approver = store.mutate_admin(
            collection="users", target_id=None,
            payload={"username": "approver", "display_name": "Approver", "password": "strong-password"},
            actor_id="admin", reason="test", action="users_create", request_id="req-user",
        )
        _, credential = store.connector_enrollments.create(
            connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
            reason="test", request_id="req-enroll",
        )
        store.connector_enrollments.register(
            credential, "connector-prod", "cluster-prod", namespace_scope=["*"],
            capabilities=["validate", "execute"], commands=ConnectorCommands(store.database),
            request_id="req-register",
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
                "(id, incident_id, actor_id, desired_outcome, context, idempotency_key, created_at, updated_at) "
                "VALUES ('change-1', 'incident-1', ?, 'scale', '', 'change-1', 1000, 1000)",
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
        gateway_db._MIGRATIONS[26] = migration  # noqa: SLF001
        gateway_db._MIGRATIONS[27] = status_migration  # noqa: SLF001
        gateway_db._MIGRATIONS[28] = plan_migration  # noqa: SLF001

    with store.database.connect() as conn:
        assert conn.execute(
            "SELECT command_id FROM kubernetes_change_validations WHERE id = 'validation-1'",
        ).fetchone()[0] == command_id
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        targets = {
            row["table"] for row in conn.execute("PRAGMA foreign_key_list(kubernetes_change_validations)")
        }
        assert "connector_commands" in targets and "connector_commands_v19" not in targets


def test_v28_migrates_single_execution_to_plan_step_without_behavior_loss(tmp_path: Path) -> None:
    migration = gateway_db._MIGRATIONS.pop(28, None)  # noqa: SLF001
    assert migration is not None
    try:
        store, approver_id = _store(tmp_path)
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
        gateway_db._MIGRATIONS[28] = migration  # noqa: SLF001

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
