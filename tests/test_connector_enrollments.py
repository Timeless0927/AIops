"""Connector Enrollment owner rotation and restart behavior."""

from pathlib import Path

import pytest

from aiops.domain.identity import IdentityError
from apps.aiops_k8s_gateway.connector_commands import ConnectorCommands
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _verify(
    store: GatewayV1Store,
    commands: ConnectorCommands,
    stdout: str = '{"apiVersion":"v1","kind":"PodList","items":[]}',
) -> None:
    command = commands.poll("connector-a", "cluster-a", 0)
    assert command
    commands.start(str(command["id"]), "connector-a", "cluster-a", str(command["lease_id"]))
    commands.submit_result(
        str(command["id"]),
        "connector-a",
        "cluster-a",
        str(command["lease_id"]),
        {
            "status": "succeeded",
            "stdout": stdout,
            "stderr": "",
            "exit_code": 0,
            "truncated": False,
            "error_code": None,
            "error_message": None,
        },
        request_id="verify-result",
        result_handler=store.connector_enrollments.record_verification_result_in,
    )


def _registered_store(
    db_path: Path,
    now: list[float],
    verification_stdout: str = '{"apiVersion":"v1","kind":"PodList","items":[]}',
) -> tuple[GatewayV1Store, ConnectorCommands]:
    credentials = iter(("current-credential", "candidate-credential"))
    store = GatewayV1Store(
        db_path,
        clock=lambda: now[0],
        credential_factory=lambda: next(credentials),
    )
    store.connector_enrollments.create(
        connector_id="connector-a",
        cluster_id="cluster-a",
        actor_id="admin",
        reason="test",
        request_id="enroll",
    )
    commands = ConnectorCommands(store.database, clock=lambda: now[0])
    store.connector_enrollments.register(
        "current-credential",
        "connector-a",
        "cluster-a",
        commands=commands,
        request_id="register",
    )
    store.connector_enrollments.heartbeat(
        "current-credential",
        "connector-a",
        "cluster-a",
        status="online",
        failure_summary="",
        request_id="heartbeat",
    )
    _verify(store, commands, verification_stdout)
    return store, commands


def test_generic_kubernetes_list_of_pods_completes_read_verification(tmp_path: Path) -> None:
    store, _commands = _registered_store(
        tmp_path / "gateway.db",
        [100.0],
        '{"apiVersion":"v1","kind":"List","items":[{"apiVersion":"v1","kind":"Pod"}]}',
    )

    state = store.connector_enrollments.admin_state()
    assert state["connector_enrollments"][0]["read_verification"] == "verified"
    assert state["clusters"][0]["read_verification"]["discovery"] == {
        "api_version": "v1",
        "kind": "PodList",
    }


def test_pod_list_with_non_pod_item_fails_read_verification(tmp_path: Path) -> None:
    store, _commands = _registered_store(
        tmp_path / "gateway.db",
        [100.0],
        '{"apiVersion":"v1","kind":"PodList","items":[{"apiVersion":"v1","kind":"Secret"}]}',
    )

    state = store.connector_enrollments.admin_state()
    assert state["connector_enrollments"][0]["read_verification"] == "failed"
    assert state["clusters"][0]["read_verification"]["reason_code"] == (
        "invalid_verification_response"
    )


def test_rotation_survives_restart_and_switches_only_on_candidate_registration(tmp_path: Path) -> None:
    now = [100.0]
    db_path = tmp_path / "gateway.db"
    store, commands = _registered_store(db_path, now)
    enrollment_id = str(store.connector_enrollments.admin_state()["connector_enrollments"][0]["id"])

    _, candidate = store.connector_enrollments.update(
        enrollment_id,
        active=None,
        rotate_credential=True,
        commands=commands,
        actor_id="admin",
        reason="rotate",
        request_id="rotate",
    )
    assert candidate == "candidate-credential"
    restarted = GatewayV1Store(db_path, clock=lambda: now[0])
    assert restarted.connector_enrollments.admin_state()["connector_enrollments"][0]["state"] == "rotation_pending"
    restarted.connector_enrollments.heartbeat(
        "current-credential", "connector-a", "cluster-a",
        status="online", failure_summary="", request_id="old-still-current",
    )

    restarted.connector_enrollments.register(
        candidate,
        "connector-a",
        "cluster-a",
        commands=ConnectorCommands(restarted.database, clock=lambda: now[0]),
        request_id="candidate-register",
    )
    with pytest.raises(IdentityError, match="invalid or revoked"):
        restarted.connector_enrollments.heartbeat(
            "current-credential", "connector-a", "cluster-a",
            status="online", failure_summary="", request_id="old-rejected",
        )


def test_expired_candidate_keeps_current_credential(tmp_path: Path) -> None:
    now = [100.0]
    store, commands = _registered_store(tmp_path / "gateway.db", now)
    enrollment_id = str(store.connector_enrollments.admin_state()["connector_enrollments"][0]["id"])
    _, candidate = store.connector_enrollments.update(
        enrollment_id,
        active=None,
        rotate_credential=True,
        commands=commands,
        actor_id="admin",
        reason="rotate",
        request_id="rotate",
    )
    now[0] += 901
    with pytest.raises(IdentityError):
        store.connector_enrollments.register(
            candidate or "", "connector-a", "cluster-a", commands=commands, request_id="expired"
        )
    store.connector_enrollments.heartbeat(
        "current-credential", "connector-a", "cluster-a",
        status="online", failure_summary="", request_id="current-preserved",
    )
    assert store.connector_enrollments.admin_state()["connector_enrollments"][0]["state"] == "online"


def test_failed_read_verification_requires_explicit_retry(tmp_path: Path) -> None:
    now = [100.0]
    store = GatewayV1Store(
        tmp_path / "gateway.db",
        clock=lambda: now[0],
        credential_factory=lambda: "credential",
    )
    enrollment, _ = store.connector_enrollments.create(
        connector_id="connector-a", cluster_id="cluster-a", actor_id="admin",
        reason="test", request_id="enroll",
    )
    commands = ConnectorCommands(store.database, clock=lambda: now[0])
    store.connector_enrollments.register(
        "credential", "connector-a", "cluster-a", commands=commands, request_id="register"
    )
    command = commands.poll("connector-a", "cluster-a", 0)
    assert command
    commands.start(str(command["id"]), "connector-a", "cluster-a", str(command["lease_id"]))
    commands.submit_result(
        str(command["id"]), "connector-a", "cluster-a", str(command["lease_id"]),
        {
            "status": "failed", "stdout": "", "stderr": "forbidden", "exit_code": 1,
            "truncated": False, "error_code": "kubernetes_forbidden",
            "error_message": "read denied",
        },
        request_id="failed",
        result_handler=store.connector_enrollments.record_verification_result_in,
    )
    assert store.connector_enrollments.admin_state()["connector_enrollments"][0]["read_verification"] == "failed"

    store.connector_enrollments.update(
        str(enrollment["id"]), active=None, rotate_credential=False,
        retry_read_verification=True, commands=commands, actor_id="admin",
        reason="permissions fixed", request_id="retry",
    )
    state = store.connector_enrollments.admin_state()
    assert state["connector_enrollments"][0]["read_verification"] == "verifying"
    assert commands.poll("connector-a", "cluster-a", 0) is not None


@pytest.mark.parametrize(
    ("status", "result_json"),
    [
        ("started", None),
        ("unknown_outcome", None),
        ("failed", '{"error_code":"rollback_required"}'),
    ],
)
def test_unfinished_command_states_block_rotation(
    tmp_path: Path, status: str, result_json: str | None
) -> None:
    now = [100.0]
    store, commands = _registered_store(tmp_path / "gateway.db", now)
    state = store.connector_enrollments.admin_state()
    enrollment_id = str(state["connector_enrollments"][0]["id"])
    with store.database.connect() as conn:
        conn.execute(
            """UPDATE connector_commands
               SET status = ?, result_json = ?, result_hash = NULL, result_received_at = NULL
               WHERE id = (SELECT command_id FROM connector_read_verifications WHERE cluster_id = 'cluster-a')""",
            (status, result_json),
        )

    with pytest.raises(IdentityError) as error:
        store.connector_enrollments.update(
            enrollment_id, active=None, rotate_credential=True, commands=commands,
            actor_id="admin", reason="rotate", request_id="blocked",
        )
    assert error.value.code == "rotation_blocked"
