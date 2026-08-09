import sqlite3
import json
import threading
from pathlib import Path

import pytest

from aiops.contracts.connector_journal import terminal_journal_evidence
from apps.aiops_k8s_gateway.connector_enrollment_http import connector_admin_state
from apps.aiops_k8s_gateway.connector_commands import ConnectorCommandError, ConnectorCommands
from apps.aiops_k8s_gateway.connector_enrollments import ConnectorEnrollments
from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase
from apps.aiops_k8s_gateway.kubernetes_change_executions import KubernetesChangeExecutions
from apps.aiops_k8s_gateway.kubernetes_execution_codec import canonical_digest


class _ExecutionApprovalFacts:
    def authorize_start(self, phase_id: str, *, request_id: str, stage: str) -> dict[str, object]:
        assert (phase_id, stage) == ("phase-1", "dispatch")
        return {"id": "approval-1", "cluster_id": "cluster-prod"}


class _ExecutionConnectorFacts:
    @staticmethod
    def execution_connector_in(_conn: sqlite3.Connection, cluster_id: str) -> str:
        assert cluster_id == "cluster-prod"
        return "connector-prod"


def _seed_mutation_claim(
    database: GatewayDatabase,
    *,
    command_id: str,
    execution_id: str,
    grant_id: str,
    created_at: float,
    revoked_at: float | None = None,
) -> None:
    change = {
        "target": {
            "api_version": "apps/v1", "kind": "Deployment", "namespace": "payments",
            "name": "checkout-api", "uid": "uid-1", "resource_version": "41",
        },
        "operation": "patch",
        "payload": [
            {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
            {"op": "replace", "path": "/spec/replicas", "value": 5},
        ],
        "post_checks": [
            {"type": "json_pointer", "path": "/spec/replicas", "operator": "eq", "value": 5},
        ],
    }
    encoded = json.dumps(change, sort_keys=True, separators=(",", ":"))
    with database.connect() as conn:
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """
            INSERT INTO kubernetes_change_executions (
                id, change_request_id, phase_id, revision_id, approval_id, connector_id,
                cluster_id, actor_id, reason, request_id, idempotency_key, request_hash,
                execution_timeout_seconds, rollback_policy, status, created_at
            ) VALUES (?, 'change-1', 'phase-1', 'revision-1', 'approval-1',
                      'connector-prod', 'cluster-prod', 'actor-1', 'test', 'request-1', ?, ?,
                      300, 'stop_only', 'queued', ?)
            """,
            (execution_id, execution_id, "a" * 64, created_at),
        )
        conn.execute(
            """
            INSERT INTO kubernetes_change_execution_steps (
                id, execution_id, ordinal, direction, command_id, change_hash,
                change_json, status, created_at
            ) VALUES (?, ?, 1, 'forward', ?, ?, ?, 'queued', ?)
            """,
            (
                f"{execution_id}:forward:1", execution_id, command_id,
                canonical_digest(change), encoded, created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO kubernetes_execution_grants (
                id, execution_id, step_id, phase_id, approval_id, command_id,
                change_hash, issued_at, expires_at, revoked_at
            ) VALUES (?, ?, ?, 'phase-1', 'approval-1', ?, ?, ?, ?, ?)
            """,
            (
                grant_id, execution_id, f"{execution_id}:forward:1", command_id,
                canonical_digest(change), created_at, created_at + 60, revoked_at,
            ),
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")


def _mutation_commands(database: GatewayDatabase, now: list[float]) -> ConnectorCommands:
    return ConnectorCommands(
        database,
        clock=lambda: now[0],
        id_factory=lambda prefix: f"{prefix}-{int(now[0])}",
    )


def _register_mutation_connector(database: GatewayDatabase, commands: ConnectorCommands) -> None:
    enrollments = ConnectorEnrollments(database, credential_factory=lambda: "credential")
    enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="test", request_id="request-enroll",
    )
    enrollments.register(
        "credential", "connector-prod", "cluster-prod",
        commands=commands, request_id="request-register",
    )


def test_read_command_requeues_unstarted_retries_started_and_accepts_late_result(tmp_path: Path) -> None:
    now = [100.0]
    sequence = iter(f"id-{index}" for index in range(20))
    database = GatewayDatabase(tmp_path / "gateway.db")
    enrollments = ConnectorEnrollments(
        database,
        clock=lambda: now[0],
        credential_factory=lambda: "credential",
        id_factory=lambda _: next(sequence),
    )
    enrollments.create(
        connector_id="connector-prod",
        cluster_id="cluster-prod",
        actor_id="admin",
        reason="test",
        request_id="request-enroll",
    )
    commands = ConnectorCommands(
        database,
        clock=lambda: now[0],
        id_factory=lambda _: next(sequence),
        lease_seconds=5,
        available_connector_in=enrollments.require_available_connector_in,
        lease_identity_matches_in=enrollments.lease_identity_matches_in,
    )
    enrollments.register(
        "credential", "connector-prod", "cluster-prod",
        commands=commands, request_id="request-register",
    )
    enrollments.heartbeat(
        "credential", "connector-prod", "cluster-prod", status="online",
        failure_summary="", request_id="request-heartbeat",
    )
    verification = commands.poll("connector-prod", "cluster-prod", 0)
    assert verification
    commands.start(
        str(verification["id"]), "connector-prod", "cluster-prod", str(verification["lease_id"])
    )
    commands.submit_result(
        str(verification["id"]),
        "connector-prod",
        "cluster-prod",
        str(verification["lease_id"]),
        {
            "status": "succeeded",
            "stdout": '{"apiVersion":"v1","kind":"PodList","items":[]}',
            "stderr": "",
            "exit_code": 0,
            "truncated": False,
            "error_code": None,
            "error_message": None,
        },
        request_id="request-verification",
        result_handler=enrollments.record_verification_result_in,
    )
    queued = commands.queue_read(
        cluster_id="cluster-prod",
        namespace="payments",
        action="get_resource",
        parameters={"resource_kind": "pods", "output": "json"},
        actor_id="admin",
        reason="test",
        request_id="request-queue",
    )

    first = commands.poll("connector-prod", "cluster-prod", 0)
    now[0] += 6
    second = commands.poll("connector-prod", "cluster-prod", 0)
    assert first and second and first["lease_id"] != second["lease_id"]
    assert second["attempt_count"] == 0
    commands.start(queued["id"], "connector-prod", "cluster-prod", second["lease_id"])
    now[0] += 6
    third = commands.poll("connector-prod", "cluster-prod", 0)
    assert third and third["attempt_count"] == 1
    commands.start(queued["id"], "connector-prod", "cluster-prod", third["lease_id"])
    result = {
        "status": "succeeded",
        "stdout": "{}",
        "stderr": "",
        "exit_code": 0,
        "truncated": False,
        "error_code": None,
        "error_message": None,
    }
    accepted = commands.submit_result(
        queued["id"],
        "connector-prod",
        "cluster-prod",
        second["lease_id"],
        result,
        request_id="request-result",
    )
    assert accepted["late"] is True
    now[0] += 6
    assert commands.cleanup_expired_leases() == 2
    assert commands.submit_result(
        queued["id"], "connector-prod", "cluster-prod", second["lease_id"], result,
        request_id="request-result-replay",
    )["idempotent"] is True

    bounded = commands.queue_read(
        cluster_id="cluster-prod",
        namespace="payments",
        action="get_resource",
        parameters={"resource_kind": "deployments", "output": "wide"},
        actor_id="admin",
        reason="test",
        request_id="request-bounded",
    )
    cluster = connector_admin_state(database, enrollments, commands)["clusters"][0]
    assert cluster["last_read_command"]["id"] == bounded["id"]
    assert cluster["last_read_result"]["id"] == queued["id"]
    for attempt in range(3):
        leased = commands.poll("connector-prod", "cluster-prod", 0)
        assert leased and leased["id"] == bounded["id"] and leased["attempt_count"] == attempt
        commands.start(bounded["id"], "connector-prod", "cluster-prod", leased["lease_id"])
        now[0] += 6
    assert commands.poll("connector-prod", "cluster-prod", 0) is None
    assert commands.get(str(bounded["id"]))["status"] == "failed"
    assert "aiops_gateway_expired_command_leases 0" in commands.metrics()
    assert commands.submit_result(
        str(bounded["id"]), "connector-prod", "cluster-prod", str(leased["lease_id"]), result,
        request_id="request-bounded-late-result",
    )["late"] is True
    assert commands.get(str(bounded["id"]))["status"] == "succeeded"


def test_stale_connector_rejects_new_reads_and_command_dispatch(tmp_path: Path) -> None:
    now = [100.0]
    sequence = iter(f"id-{index}" for index in range(20))
    database = GatewayDatabase(tmp_path / "gateway.db")
    enrollments = ConnectorEnrollments(
        database, clock=lambda: now[0],
        credential_factory=lambda: "credential", id_factory=lambda _: next(sequence),
    )
    enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="test", request_id="request-enroll",
    )
    commands = ConnectorCommands(
        database, clock=lambda: now[0], id_factory=lambda _: next(sequence),
        available_connector_in=enrollments.require_available_connector_in,
        lease_identity_matches_in=enrollments.lease_identity_matches_in,
    )
    enrollments.register(
        "credential", "connector-prod", "cluster-prod",
        commands=commands, request_id="request-register",
    )
    enrollments.heartbeat(
        "credential", "connector-prod", "cluster-prod", status="online",
        failure_summary="", request_id="request-heartbeat",
    )
    verification = commands.poll("connector-prod", "cluster-prod", 0)
    assert verification is not None
    commands.start(
        str(verification["id"]), "connector-prod", "cluster-prod", str(verification["lease_id"]),
    )
    commands.submit_result(
        str(verification["id"]), "connector-prod", "cluster-prod", str(verification["lease_id"]),
        {"status": "succeeded", "stdout": '{"kind":"PodList","items":[]}',
         "stderr": "", "exit_code": 0, "truncated": False,
         "error_code": None, "error_message": None},
        request_id="request-verification",
        result_handler=enrollments.record_verification_result_in,
    )
    now[0] += 121
    with pytest.raises(ConnectorCommandError) as read_unavailable:
        commands.queue_read(
            cluster_id="cluster-prod", namespace="payments", action="get_resource",
            parameters={"resource_kind": "pods", "output": "json"}, actor_id="admin",
            reason="test", request_id="request-read",
        )
    dispatched: list[tuple[str, str]] = []
    with pytest.raises(ConnectorCommandError) as dispatch_unavailable:
        commands.poll(
            "connector-prod", "cluster-prod", 0,
            dispatcher=lambda connector, cluster: dispatched.append((connector, cluster)) or None,
        )
    assert read_unavailable.value.code == dispatch_unavailable.value.code == "cluster_not_ready"
    assert dispatched == []


def test_lease_identity_fact_mismatch_returns_no_command(tmp_path: Path) -> None:
    database = GatewayDatabase(tmp_path / "gateway.db")
    enrollments = ConnectorEnrollments(
        database, credential_factory=lambda: "credential",
    )
    enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="test", request_id="request-enroll",
    )
    enrollments.register(
        "credential", "connector-prod", "cluster-prod",
        commands=ConnectorCommands(database), request_id="request-register",
    )
    commands = ConnectorCommands(
        database,
        available_connector_in=lambda *_args, **_kwargs: "connector-prod",
        lease_identity_matches_in=lambda *_args: False,
    )

    assert commands.poll("connector-prod", "cluster-prod", 0) is None


def test_cluster_summary_excludes_verification_replaced_during_read(tmp_path: Path) -> None:
    database = GatewayDatabase(tmp_path / "gateway.db")
    with database.connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    enrollments = ConnectorEnrollments(
        database,
        credential_factory=lambda: "credential",
        id_factory=lambda _prefix: "enrollment",
    )
    enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="test", request_id="request-enroll",
    )
    command_ids = iter(("verification-old", "verification-new"))
    command_writer = ConnectorCommands(
        database, id_factory=lambda _prefix: next(command_ids),
    )
    enrollments.register(
        "credential", "connector-prod", "cluster-prod",
        commands=command_writer, request_id="request-register",
    )
    verification_command_ids_in = enrollments.verification_command_ids_in

    def replace_verification_after_read(conn: sqlite3.Connection) -> set[str]:
        verification_ids = verification_command_ids_in(conn)
        with database.connect() as writer:
            replacement_id = command_writer.queue_verification_in(
                writer,
                connector_id="connector-prod",
                cluster_id="cluster-prod",
                namespace="default",
                now=101.0,
            )
            writer.execute(
                "UPDATE connector_read_verifications SET command_id = ? WHERE cluster_id = ?",
                (replacement_id, "cluster-prod"),
            )
            writer.commit()
        return verification_ids

    enrollments.verification_command_ids_in = replace_verification_after_read  # type: ignore[method-assign]

    cluster = connector_admin_state(
        database, enrollments, ConnectorCommands(database),
    )["clusters"][0]
    assert cluster["cluster_id"] == "cluster-prod"
    assert cluster["pending_read_commands"] == 0
    assert cluster["last_read_command"] is None
    assert cluster["last_read_result"] is None


def test_mutation_claim_is_single_use_and_lifecycle_owned(tmp_path: Path) -> None:
    now = [100.0]
    database = GatewayDatabase(tmp_path / "gateway.db")
    commands = _mutation_commands(database, now)
    _register_mutation_connector(database, commands)
    executions = KubernetesChangeExecutions(
        database,
        approvals=_ExecutionApprovalFacts(),
        enrollments=_ExecutionConnectorFacts(),  # type: ignore[arg-type]
        commands=commands,
        clock=lambda: now[0],
    )
    _seed_mutation_claim(
        database,
        command_id="command-1",
        execution_id="execution-1",
        grant_id="grant-1",
        created_at=now[0],
    )

    claimed = executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="request-dispatch",
    )

    assert claimed is not None
    assert claimed == commands.get("command-1")
    assert claimed["status"] == "leased" and claimed["lease_id"]
    assert executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="request-replay",
    ) is None

    revoked_database = GatewayDatabase(tmp_path / "revoked.db")
    revoked_commands = _mutation_commands(revoked_database, now)
    _register_mutation_connector(revoked_database, revoked_commands)
    revoked_executions = KubernetesChangeExecutions(
        revoked_database,
        approvals=_ExecutionApprovalFacts(),
        enrollments=_ExecutionConnectorFacts(),  # type: ignore[arg-type]
        commands=revoked_commands,
        clock=lambda: now[0],
    )
    _seed_mutation_claim(
        revoked_database,
        command_id="command-2",
        execution_id="execution-2",
        grant_id="grant-2",
        created_at=now[0] + 1,
        revoked_at=now[0],
    )
    assert revoked_executions.dispatch_next(
        "connector-prod", "cluster-prod", request_id="request-cancel-race",
    ) is None
    with pytest.raises(ConnectorCommandError) as missing:
        revoked_commands.get("command-2")
    assert missing.value.code == "command_not_found"


def test_started_mutation_expiry_requires_exact_journal_reconciliation(tmp_path: Path) -> None:
    now = [100.0]
    database = GatewayDatabase(tmp_path / "gateway.db")
    commands = _mutation_commands(database, now)
    _register_mutation_connector(database, commands)
    _seed_mutation_claim(
        database,
        command_id="command-1",
        execution_id="execution-1",
        grant_id="grant-1",
        created_at=now[0],
    )
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        command = commands.lease_mutation_in(
            conn,
            command_id="command-1",
            connector_id="connector-prod",
            cluster_id="cluster-prod",
            namespace="payments",
            parameters={"grant": {"execution_timeout_seconds": 300}, "change": {}},
            grant_id="grant-1",
            grant_expires_at=160.0,
            action_hash="b" * 64,
            now=now[0],
        )
        conn.commit()
    commands.start(
        "command-1", "connector-prod", "cluster-prod", str(command["lease_id"]),
        start_handler=lambda _conn, _command_id, started_at: started_at + 300,
    )
    now[0] = 401.0

    assert commands.reconcile_unknown_outcomes() == 1
    unknown = commands.get("command-1")
    assert unknown["status"] == "unknown_outcome" and unknown["attempt_count"] == 1

    terminal = {
        "status": "succeeded", "stdout": "", "stderr": "", "exit_code": 0,
        "truncated": False, "error_code": None, "error_message": None,
        "execution": {
            "operation": "patch",
            "target": {"exists": True, "uid": "uid-1", "resource_version": "42"},
            "post_checks": [{"type": "json_pointer", "status": "succeeded"}],
        },
    }
    with pytest.raises(ConnectorCommandError) as untrusted:
        commands.submit_result(
            "command-1", "connector-prod", "cluster-prod", str(command["lease_id"]),
            terminal, request_id="request-untrusted",
        )
    assert untrusted.value.code == "untrusted_terminal_result"

    accepted = commands.submit_result(
        "command-1", "connector-prod", "cluster-prod", str(command["lease_id"]),
        terminal,
        journal_evidence=terminal_journal_evidence(
            "command-1", terminal, recorded_at=110.0,
        ),
        request_id="request-late",
    )
    assert accepted == {
        "id": "command-1", "status": "succeeded", "idempotent": False, "late": True,
    }
    assert commands.get("command-1")["status"] == "succeeded"


def test_validation_command_queue_is_lifecycle_owned(tmp_path: Path) -> None:
    database = GatewayDatabase(tmp_path / "gateway.db")
    ids = iter(("verification-1", "validation-1"))
    commands = ConnectorCommands(database, id_factory=lambda _prefix: next(ids))
    _register_mutation_connector(database, commands)
    change = {
        "target": {
            "api_version": "apps/v1", "kind": "Deployment",
            "namespace": "payments", "name": "checkout-api",
        },
        "operation": "patch",
        "payload": [{"op": "replace", "path": "/spec/replicas", "value": 5}],
        "post_checks": [
            {"type": "json_pointer", "path": "/spec/replicas", "operator": "eq", "value": 5},
        ],
    }

    with database.connect() as conn:
        command_id = commands.queue_validation_in(
            conn,
            connector_id="connector-prod",
            cluster_id="cluster-prod",
            change=change,
            now=100.0,
        )

    assert command_id == "validation-1"
    assert commands.get(command_id)["parameters"] == {"change": change}


def test_secure_transport_redaction_is_lifecycle_owned(tmp_path: Path) -> None:
    database = GatewayDatabase(tmp_path / "gateway.db")
    ids = iter(("verification-1", "validation-1"))
    commands = ConnectorCommands(database, id_factory=lambda _prefix: next(ids))
    _register_mutation_connector(database, commands)
    change = {
        "target": {
            "api_version": "apps/v1", "kind": "Deployment",
            "namespace": "payments", "name": "checkout-api",
        },
        "operation": "patch", "payload": [], "post_checks": [],
    }
    secure_ref = {
        "id": "input-1", "key_name": "TOKEN", "placeholder": "{{secure:input-1}}",
        "sha256": "a" * 64, "ciphertext": "secret-transport",
    }
    with database.connect() as conn:
        command_id = commands.queue_validation_in(
            conn, connector_id="connector-prod", cluster_id="cluster-prod",
            change=change, secure_inputs=[secure_ref], now=100.0,
        )
        commands.redact_secure_inputs_in(conn, command_id)

    assert commands.get(command_id)["parameters"]["secure_inputs"] == [
        {"key_name": "TOKEN", "sha256": "a" * 64},
    ]


def test_unstarted_command_rejection_is_lifecycle_owned(tmp_path: Path) -> None:
    database = GatewayDatabase(tmp_path / "gateway.db")
    ids = iter(("verification-1", "validation-1"))
    commands = ConnectorCommands(database, id_factory=lambda _prefix: next(ids))
    _register_mutation_connector(database, commands)
    with database.connect() as conn:
        command_id = commands.queue_validation_in(
            conn,
            connector_id="connector-prod",
            cluster_id="cluster-prod",
            change={
                "target": {"namespace": "payments"},
                "operation": "patch", "payload": [], "post_checks": [],
            },
            now=100.0,
        )
        commands.reject_in(
            conn,
            [command_id],
            result={"status": "rejected", "error_code": "superseded"},
            now=101.0,
        )

    rejected = commands.get(command_id)
    assert rejected["status"] == "rejected"
    assert rejected["result"] == {"status": "rejected", "error_code": "superseded"}


def test_reconciliation_command_queue_is_lifecycle_owned(tmp_path: Path) -> None:
    database = GatewayDatabase(tmp_path / "gateway.db")
    commands = ConnectorCommands(database, id_factory=lambda _prefix: "verification-1")
    _register_mutation_connector(database, commands)
    with database.connect() as conn:
        commands.queue_reconciliation_in(
            conn,
            command_id="mutation-1:reconcile",
            connector_id="connector-prod",
            cluster_id="cluster-prod",
            namespace="payments",
            parameters={"change": {"operation": "patch"}},
            now=100.0,
        )

    command = commands.get("mutation-1:reconcile")
    assert command["action"] == "reconcile_kubernetes_change"
    assert command["parameters"] == {"change": {"operation": "patch"}}


def test_transport_projection_claim_locks_before_classification(tmp_path: Path) -> None:
    now = [100.0]
    database = GatewayDatabase(tmp_path / "gateway.db")
    commands = _mutation_commands(database, now)
    _register_mutation_connector(database, commands)
    _seed_mutation_claim(
        database,
        command_id="command-1",
        execution_id="execution-1",
        grant_id="grant-1",
        created_at=now[0],
    )
    with database.connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        command = commands.lease_mutation_in(
            conn,
            command_id="command-1",
            connector_id="connector-prod",
            cluster_id="cluster-prod",
            namespace="payments",
            parameters={"grant": {"execution_timeout_seconds": 300}, "change": {}},
            grant_id="grant-1",
            grant_expires_at=160.0,
            action_hash="b" * 64,
            now=now[0],
        )
        conn.commit()
    commands.start(
        "command-1", "connector-prod", "cluster-prod", str(command["lease_id"]),
        start_handler=lambda _conn, _command_id, started_at: started_at + 300,
    )
    now[0] = 401.0
    commands.reconcile_unknown_outcomes()
    claimed: list[list[tuple[str, str]]] = []
    failures: list[BaseException] = []
    ready = threading.Event()
    proceed = threading.Event()
    completed = threading.Event()

    def claim_projection() -> None:
        with database.connect() as conn:
            ready.set()
            proceed.wait(timeout=2)
            try:
                claimed.append(commands.claim_transport_failures_in(
                    conn, [("command-1", "started")], now=now[0],
                ))
                conn.commit()
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)
            finally:
                completed.set()

    worker = threading.Thread(target=claim_projection)
    worker.start()
    assert ready.wait(timeout=2)
    with database.connect() as terminal_conn:
        terminal_conn.execute("BEGIN IMMEDIATE")
        terminal_conn.execute(
            "UPDATE connector_commands SET status = 'succeeded', updated_at = ? WHERE id = ?",
            (now[0], "command-1"),
        )
        proceed.set()
        assert completed.wait(0.1) is False
        terminal_conn.commit()
        worker.join(timeout=2)

    assert completed.is_set() and failures == []
    assert claimed == [[]]
