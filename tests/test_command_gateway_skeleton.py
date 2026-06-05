"""AIO-57 CommandTask, Grant, and Connector envelope skeleton tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aiops.domain import CommandTask, CommandTaskStatus, CommandTaskStore, Grant
from aiops.k8s import CommandEnvelope, ResultEnvelope
from apps.cluster_connector.kubectl_executor import validate_command_envelope


def _command_envelope(**overrides: object) -> CommandEnvelope:
    payload = {
        "envelope_version": "v1",
        "task_id": "task-1",
        "command_id": "cmd-1",
        "cluster_id": "cluster-a",
        "namespace": "default",
        "action_type": "read",
        "argv": ("kubectl", "get", "pods", "-n", "default"),
        "timeout_seconds": 30,
        "output_limit_bytes": 4096,
        "grant_id": "grant-1",
    }
    payload.update(overrides)
    return CommandEnvelope(**payload)


def test_command_task_state_flow_records_events() -> None:
    store = CommandTaskStore()
    task = store.create(
        CommandTask.create(
            task_id="task-1",
            command_id="cmd-1",
            cluster_id="cluster-a",
            namespace="default",
            action_type="kubectl_get",
        )
    )

    assert task.status == CommandTaskStatus.CREATED

    for status in (
        CommandTaskStatus.PENDING_APPROVAL,
        CommandTaskStatus.APPROVED,
        CommandTaskStatus.QUEUED,
        CommandTaskStatus.DISPATCHED,
        CommandTaskStatus.RUNNING,
        CommandTaskStatus.SUCCEEDED,
    ):
        task = store.transition(task.task_id, status)

    assert task.status == CommandTaskStatus.SUCCEEDED
    assert [event.to_status for event in store.events_for("task-1")] == [
        CommandTaskStatus.CREATED,
        CommandTaskStatus.PENDING_APPROVAL,
        CommandTaskStatus.APPROVED,
        CommandTaskStatus.QUEUED,
        CommandTaskStatus.DISPATCHED,
        CommandTaskStatus.RUNNING,
        CommandTaskStatus.SUCCEEDED,
    ]


def test_command_task_rejects_invalid_transition() -> None:
    task = CommandTask.create(
        task_id="task-1",
        command_id="cmd-1",
        cluster_id="cluster-a",
        namespace="default",
        action_type="kubectl_get",
    )

    with pytest.raises(ValueError, match="invalid task transition"):
        task.transition_to(CommandTaskStatus.RUNNING)


def test_duplicate_command_id_returns_existing_task() -> None:
    store = CommandTaskStore()
    original = store.create(
        CommandTask.create(
            task_id="task-1",
            command_id="cmd-1",
            cluster_id="cluster-a",
            namespace="default",
            action_type="kubectl_get",
        )
    )
    duplicate = store.create(
        CommandTask.create(
            task_id="task-2",
            command_id="cmd-1",
            cluster_id="cluster-a",
            namespace="default",
            action_type="kubectl_get",
        )
    )

    assert duplicate is original
    assert store.get("task-2") is None


def test_grant_expires_and_allows_only_one_use() -> None:
    issued_at = datetime(2026, 6, 5, tzinfo=timezone.utc)
    grant = Grant(
        grant_id="grant-1",
        task_id="task-1",
        command_id="cmd-1",
        cluster_id="cluster-a",
        namespace="default",
        action="kubectl_get",
        risk_level="low",
        issued_by="approval-system",
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=5),
    )

    consumed = grant.consume(issued_at + timedelta(minutes=1))

    assert consumed.uses == 1
    assert consumed.can_use(issued_at + timedelta(minutes=2)) is False
    with pytest.raises(ValueError, match="expired or already consumed"):
        consumed.consume(issued_at + timedelta(minutes=2))
    assert grant.is_expired(issued_at + timedelta(minutes=6)) is True


def test_command_envelope_round_trip_and_invalid_payload() -> None:
    envelope = _command_envelope()

    assert CommandEnvelope.from_dict(envelope.to_dict()) == envelope
    with pytest.raises(ValueError, match="argv"):
        _command_envelope(argv=())


def test_result_envelope_round_trip_and_invalid_status() -> None:
    result = ResultEnvelope(
        envelope_version="v1",
        task_id="task-1",
        command_id="cmd-1",
        connector_id="connector-1",
        cluster_id="cluster-a",
        status="succeeded",
        stdout="ok",
        exit_code=0,
    )

    assert ResultEnvelope.from_dict(result.to_dict()) == result
    with pytest.raises(ValueError, match="invalid result status"):
        ResultEnvelope(
            envelope_version="v1",
            task_id="task-1",
            command_id="cmd-1",
            connector_id="connector-1",
            cluster_id="cluster-a",
            status="unknown",
        )


def test_connector_validation_accepts_scoped_kubectl_envelope() -> None:
    validate_command_envelope(
        _command_envelope(),
        connector_cluster_id="cluster-a",
        allowed_namespaces={"default"},
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"namespace": "kube-system"}, "namespace_out_of_scope"),
        ({"argv": ("bash", "-lc", "kubectl get pods")}, "command_rejected"),
        ({"timeout_seconds": 601}, "timeout exceeds"),
        ({"output_limit_bytes": 1024 * 1024 + 1}, "output limit exceeds"),
        ({"grant_id": None}, "grant_ref is required"),
    ],
)
def test_connector_validation_rejects_invalid_envelope(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_command_envelope(
            _command_envelope(**overrides),
            connector_cluster_id="cluster-a",
            allowed_namespaces={"default"},
        )
