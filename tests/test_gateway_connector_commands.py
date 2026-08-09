import sqlite3
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway.connector_commands import ConnectorCommandError, ConnectorCommands
from apps.aiops_k8s_gateway.connector_enrollments import ConnectorEnrollments
from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase


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
        verification_command_ids_in=enrollments.verification_command_ids_in,
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
    cluster = commands.summarize_clusters([{"cluster_id": "cluster-prod"}])[0]
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
        verification_command_ids_in=enrollments.verification_command_ids_in,
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
        verification_command_ids_in=enrollments.verification_command_ids_in,
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

    def replace_verification_after_read(conn: sqlite3.Connection) -> set[str]:
        verification_ids = enrollments.verification_command_ids_in(conn)
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

    commands = ConnectorCommands(
        database,
        verification_command_ids_in=replace_verification_after_read,
    )

    assert commands.summarize_clusters([{"cluster_id": "cluster-prod"}]) == [
        {
            "cluster_id": "cluster-prod",
            "pending_read_commands": 0,
            "last_read_command": None,
            "last_read_result": None,
        }
    ]
