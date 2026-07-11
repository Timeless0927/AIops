from pathlib import Path

from apps.aiops_k8s_gateway.connector_commands import ConnectorCommands
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def test_read_command_requeues_unstarted_retries_started_and_accepts_late_result(tmp_path: Path) -> None:
    now = [100.0]
    sequence = iter(f"id-{index}" for index in range(20))
    store = GatewayV1Store(
        tmp_path / "gateway.db",
        clock=lambda: now[0],
        credential_factory=lambda: "credential",
        id_factory=lambda _: next(sequence),
    )
    store.create_connector_enrollment(
        connector_id="connector-prod",
        cluster_id="cluster-prod",
        actor_id="admin",
        reason="test",
        request_id="request-enroll",
    )
    store.register_connector(
        "credential", "connector-prod", "cluster-prod", request_id="request-register"
    )
    commands = ConnectorCommands(
        store.database,
        clock=lambda: now[0],
        id_factory=lambda _: next(sequence),
        lease_seconds=5,
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
    accepted = commands.submit_result(
        queued["id"],
        "connector-prod",
        "cluster-prod",
        second["lease_id"],
        {
            "status": "succeeded",
            "stdout": "{}",
            "stderr": "",
            "exit_code": 0,
            "truncated": False,
            "error_code": None,
            "error_message": None,
        },
        request_id="request-result",
    )
    assert accepted["late"] is True

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
