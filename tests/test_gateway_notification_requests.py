from __future__ import annotations

from pathlib import Path
import threading

import pytest

from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase
from apps.aiops_k8s_gateway.notification_requests import NotificationOutbox, start_notification_handoff
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store
from aiops.contracts.notification import EVENT_TYPES, NotificationContractError, notification_request


def _request(event_type: str = "incident.opened") -> dict[str, object]:
    subject_type = event_type.split(".", 1)[0]
    status = event_type.rsplit(".", 1)[-1]
    if subject_type == "incident":
        facts: dict[str, object] = {"incident_id": "incident-1", "status": status}
        if event_type == "incident.severity_changed":
            facts.update(previous_severity="medium", severity="critical")
    elif subject_type == "investigation":
        facts = {
            "incident_id": "incident-1",
            "investigation_id": "subject-1",
            "status": status,
            "reason": "diagnosis outcome",
        }
    elif subject_type == "approval":
        facts = {
            "incident_id": "incident-1",
            "investigation_id": "investigation-1",
            "action_id": "action-1",
            "status": status,
        }
        if event_type not in {"approval.required", "approval.blocked"}:
            facts["approval_id"] = "subject-1"
        if event_type in {"approval.rejected", "approval.expired", "approval.blocked"}:
            facts["reason"] = "approval outcome"
    elif subject_type == "execution":
        facts = {
            "incident_id": "incident-1",
            "command_id": "subject-1",
            "action": "restart_deployment",
            "status": status,
        }
    else:
        facts = {"connector_id": "subject-1", "cluster_id": "cluster-prod", "status": status}
    return notification_request(
        event_id=f"{event_type}:subject-1:1",
        event_type=event_type,
        occurred_at=1_700_000_000,
        severity="critical" if event_type == "incident.opened" else "warning",
        subject={"type": subject_type, "id": "subject-1", "version": 1},
        scope={
            "environment": "prod",
            "team_id": "team-payments",
            "service_id": "service-checkout",
            "cluster_id": "cluster-prod",
            "namespace": "payments",
            "resource_type": "deployment",
            "resource_id": "checkout-api",
        },
        summary="Checkout is unavailable",
        facts=facts,
        console_path="/incidents/incident-1",
    )


def test_contract_accepts_every_specified_domain_event_and_rejects_transport_fields() -> None:
    assert {notification_request(**(_request(event_type) | {"event_id": f"event-{index}"}))["event_type"] for index, event_type in enumerate(EVENT_TYPES)} == set(EVENT_TYPES)

    invalid = _request() | {"destination": "oc-secret"}
    with pytest.raises(NotificationContractError):
        notification_request(**invalid)

    with pytest.raises(NotificationContractError, match="finite"):
        notification_request(**(_request() | {"occurred_at": float("nan")}))

    with pytest.raises(NotificationContractError, match="unexpected facts"):
        notification_request(**(_request() | {"facts": {"incident_id": "incident-1", "status": "opened", "command_id": "wrong-domain"}}))


def test_business_fact_and_notification_request_commit_or_rollback_together(tmp_path: Path) -> None:
    database = GatewayDatabase(tmp_path / "gateway.db")
    outbox = NotificationOutbox(database, clock=lambda: 1_700_000_001)
    with database.connect() as conn:
        conn.execute("CREATE TABLE business_facts (id TEXT PRIMARY KEY)")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO business_facts VALUES ('fact-rolled-back')")
        outbox.enqueue_in(conn, _request())
        conn.rollback()

        assert conn.execute("SELECT COUNT(*) FROM business_facts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM notification_requests").fetchone()[0] == 0

        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO business_facts VALUES ('fact-committed')")
        outbox.enqueue_in(conn, _request())
        conn.commit()

    assert outbox.list_requests()[0]["event_id"] == _request()["event_id"]


def test_handoff_retries_without_changing_committed_business_state(tmp_path: Path) -> None:
    database = GatewayDatabase(tmp_path / "gateway.db")
    outbox = NotificationOutbox(database, clock=lambda: 1_700_000_001, retry_seconds=0)
    with database.connect() as conn:
        conn.execute("CREATE TABLE business_facts (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO business_facts VALUES ('fact-1')")
        outbox.enqueue_in(conn, _request())

    def unavailable(_payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        raise OSError("engine unavailable")

    assert outbox.run_handoff_once(unavailable) is True
    assert outbox.list_requests()[0]["status"] == "pending"
    with database.connect() as conn:
        assert conn.execute("SELECT id FROM business_facts").fetchone()[0] == "fact-1"

    assert outbox.run_handoff_once(
        lambda payload: (202, {"status": "accepted", "event_id": payload["event_id"]})
    ) is True
    accepted = outbox.list_requests()[0]
    assert accepted["status"] == "accepted"
    assert accepted["attempt_count"] == 2


def test_connector_presence_transitions_create_offline_and_recovered_requests(tmp_path: Path) -> None:
    now = [100.0]
    store = GatewayV1Store(
        tmp_path / "gateway.db",
        clock=lambda: now[0],
        credential_factory=lambda: "connector-secret",
    )
    store.create_connector_enrollment(
        connector_id="connector-prod",
        cluster_id="cluster-prod",
        actor_id="admin",
        reason="test",
        request_id="enroll",
    )
    store.register_connector("connector-secret", "connector-prod", "cluster-prod", request_id="register")
    outbox = NotificationOutbox(store.database, clock=lambda: now[0])

    assert outbox.reconcile_connector_presence() == 0
    now[0] += 121
    assert outbox.reconcile_connector_presence() == 1
    store.record_connector_heartbeat(
        "connector-secret",
        "connector-prod",
        "cluster-prod",
        status="online",
        failure_summary="",
        request_id="heartbeat",
    )
    assert outbox.reconcile_connector_presence() == 1

    requests = outbox.list_requests()
    assert [request["event_type"] for request in requests] == ["connector.offline", "connector.recovered"]
    assert all(request["scope"]["cluster_id"] == "cluster-prod" for request in requests)


def test_handoff_worker_survives_one_iteration_failure() -> None:
    stop = threading.Event()

    class FlakyOutbox:
        calls = 0

        def reconcile_connector_presence(self) -> int:
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary sqlite failure")
            stop.set()
            return 0

        def run_handoff_once(self, _sender) -> bool:
            return False

    outbox = FlakyOutbox()
    worker = start_notification_handoff(outbox, sender=lambda _: (202, {}), interval_seconds=0.01, stop_event=stop)  # type: ignore[arg-type]
    worker.join(timeout=1)

    assert outbox.calls == 2
    assert not worker.is_alive()
