"""T07 Gateway-owned Incident module behavior."""

from __future__ import annotations

import itertools
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway.incident import AlertSignal, IncidentError, IncidentService
from apps.aiops_k8s_gateway.incident_runtime import start_incident_reconciler
from apps.aiops_k8s_gateway.connector_identity import ConnectorIdentity
from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase
from apps.aiops_k8s_gateway.notification_requests import NotificationOutbox
from apps.aiops_k8s_gateway.resource_catalog import DiscoveryObservation, ResourceCatalog
from apps.aiops_k8s_gateway.identity_administration import IdentityAdministration
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _registered_cluster(db_path: Path) -> tuple[GatewayV1Store, str]:
    store = GatewayV1Store(
        db_path,
        credential_factory=lambda: "connector-secret",
        id_factory=lambda prefix: f"{prefix}-fixed",
    )
    _, credential = store.connector_enrollments.create(
        connector_id="connector-prod",
        cluster_id="cluster-prod",
        actor_id="admin-1",
        reason="接入生产集群",
        request_id="req-enroll",
    )
    store.connector_enrollments.register(
        credential,
        "connector-prod",
        "cluster-prod",
        request_id="req-register",
    )
    return store, credential


def _bound_checkout(db_path: Path) -> tuple[str, str]:
    store, _ = _registered_cluster(db_path)
    _, team = IdentityAdministration(store.database).mutate(
        collection="teams",
        target_id=None,
        payload={"name": "Payments", "description": "支付责任团队"},
        actor_id="admin-1",
        reason="建立责任团队",
        action="teams_create",
        request_id="req-team",
    )
    catalog = ResourceCatalog(db_path)
    [candidate] = catalog.refresh_discovery(
        "cluster-prod",
        [DiscoveryObservation(namespace="payments", workload_kind="Deployment", workload_name="checkout-api")],
    )
    service = catalog.create_service(
        team_id=str(team["id"]),
        name="Checkout",
        description="结账服务",
        actor_id="admin-1",
        reason="登记服务",
        request_id="req-service",
    )
    catalog.confirm_binding(
        candidate_id=str(candidate["id"]),
        service_id=str(service["id"]),
        actor_id="admin-1",
        reason="确认生产工作负载归属",
        request_id="req-binding",
    )
    return str(team["id"]), str(service["id"])


def _signal(fingerprint: str, *, workload_name: str | None = "checkout-api") -> AlertSignal:
    return AlertSignal(
        fingerprint=fingerprint,
        alertname="HighErrorRate",
        status="firing",
        severity="critical",
        cluster_id="cluster-prod",
        namespace="payments",
        summary="checkout error rate is above 10%",
        workload_kind="Deployment" if workload_name else None,
        workload_name=workload_name,
    )


def _warning_signal(fingerprint: str) -> AlertSignal:
    return replace(_signal(fingerprint), severity="medium")


def _service(db_path: Path) -> IncidentService:
    ids = itertools.count(1)
    database = GatewayDatabase(db_path)
    return IncidentService(
        database,
        ResourceCatalog(database),
        ConnectorIdentity(database),
        clock=lambda: 1000.0 + next(ids),
        id_factory=lambda prefix: f"{prefix}-{next(ids)}",
    )


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _lifecycle_service(db_path: Path, clock: _Clock) -> IncidentService:
    ids = itertools.count(1)
    database = GatewayDatabase(db_path)
    return IncidentService(
        database,
        ResourceCatalog(database),
        ConnectorIdentity(database),
        clock=clock,
        id_factory=lambda prefix: f"{prefix}-{next(ids)}",
        stabilization_seconds=30,
        reopen_seconds=120,
    )


def test_ingress_rejects_alert_for_unregistered_cluster(tmp_path: Path) -> None:
    service = _service(tmp_path / "gateway.db")

    with pytest.raises(IncidentError, match="registered Cluster") as error:
        service.ingest(_signal("fp-1"))

    assert error.value.code == "cluster_not_registered"


def test_bound_signals_correlate_and_create_one_queued_investigation(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway.db"
    team_id, service_id = _bound_checkout(db_path)
    incidents = _service(db_path)

    first = incidents.ingest(_signal("fp-pod-a"))
    second = incidents.ingest(_signal("fp-pod-b"))
    snapshot = incidents.workbench(
        str(first["incident"]["id"]),
        team_ids={team_id},
        actor_capabilities=["view_incident"],
    )

    assert first["created"] is True
    assert second["created"] is False
    assert second["incident"]["id"] == first["incident"]["id"]
    assert snapshot is not None
    assert snapshot["incident"]["binding_status"] == "bound"
    assert snapshot["incident"]["signal_count"] == 2
    assert snapshot["resource_context"]["service_id"] == service_id
    assert snapshot["responsibility"] == {
        "status": "assigned",
        "team_id": team_id,
        "team_name": "Payments",
    }
    assert [signal["fingerprint"] for signal in snapshot["alert_signals"]] == ["fp-pod-a", "fp-pod-b"]
    assert snapshot["investigation"]["sequence"] == 1
    assert snapshot["investigation"]["status"] == "queued"
    assert len(snapshot["snapshot_revision"]) == 16
    assert snapshot["event_cursor"] == 1
    assert snapshot["actor_capabilities"] == ["view_incident"]
    assert snapshot["evidence_steps"] == []
    assert snapshot["judgment"] is None
    assert snapshot["recommended_actions"] == []
    assert incidents.list_incidents(team_ids={"team-other"}) == []
    assert incidents.workbench(
        str(first["incident"]["id"]),
        team_ids={"team-other"},
        actor_capabilities=["view_incident"],
    ) is None
    requests = NotificationOutbox(GatewayDatabase(db_path)).list_requests()
    assert [request["event_type"] for request in requests] == ["incident.opened"]
    assert requests[0]["subject"]["id"] == first["incident"]["id"]

    GatewayV1Store(db_path).connector_enrollments.heartbeat(
        "connector-secret",
        "connector-prod",
        "cluster-prod",
        status="degraded",
        failure_summary="API latency",
        request_id="req-heartbeat",
    )
    changed = incidents.workbench(
        str(first["incident"]["id"]),
        team_ids={team_id},
        actor_capabilities=["view_incident"],
    )
    assert changed is not None
    assert changed["snapshot_revision"] != snapshot["snapshot_revision"]
    assert changed["resource_context"]["runtime_status"] == "degraded"


def test_new_signal_severity_change_is_recorded_in_notification_outbox(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway.db"
    _bound_checkout(db_path)
    incidents = _service(db_path)
    incident_id = str(incidents.ingest(_warning_signal("fp-warning"))["incident"]["id"])

    incidents.ingest(_signal("fp-critical"))

    requests = NotificationOutbox(GatewayDatabase(db_path)).list_requests()
    assert [request["event_type"] for request in requests] == [
        "incident.opened",
        "incident.severity_changed",
    ]
    assert requests[1]["facts"] == {
        "incident_id": incident_id,
        "previous_severity": "medium",
        "severity": "critical",
        "status": "severity_changed",
    }


def test_unbound_workload_correlates_without_claiming_ownership(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway.db"
    _registered_cluster(db_path)
    incidents = _service(db_path)

    first = incidents.ingest(_signal("fp-worker-a", workload_name="unknown-worker"))
    second = incidents.ingest(_signal("fp-worker-b", workload_name="unknown-worker"))
    snapshot = incidents.workbench(str(first["incident"]["id"]), team_ids=None, actor_capabilities=[])

    assert second["incident"]["id"] == first["incident"]["id"]
    assert snapshot is not None
    assert snapshot["incident"]["binding_status"] == "unbound"
    assert snapshot["resource_context"]["workload_name"] == "unknown-worker"
    assert snapshot["resource_context"]["service_id"] is None
    assert snapshot["responsibility"]["status"] == "unassigned"

    named_only = incidents.ingest(replace(_signal("fp-app-a", workload_name="catalog-api"), workload_kind=None))
    named_only_again = incidents.ingest(replace(_signal("fp-app-b", workload_name="catalog-api"), workload_kind=None))
    assert named_only_again["incident"]["id"] == named_only["incident"]["id"]


def test_alert_without_resource_identity_isolated_by_fingerprint(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway.db"
    _registered_cluster(db_path)
    incidents = _service(db_path)

    first = incidents.ingest(_signal("fp-isolated-a", workload_name=None))
    second = incidents.ingest(_signal("fp-isolated-b", workload_name=None))

    assert second["incident"]["id"] != first["incident"]["id"]
    assert len(incidents.list_incidents(team_ids=None)) == 2
    assert len(incidents.list_incidents(team_ids=set())) == 2


def test_incident_resolves_only_after_every_signal_remains_recovered(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway.db"
    _bound_checkout(db_path)
    clock = _Clock()
    incidents = _lifecycle_service(db_path, clock)
    first = incidents.ingest(_signal("fp-pod-a"))
    incidents.ingest(_signal("fp-pod-b"))
    incident_id = str(first["incident"]["id"])

    incidents.ingest(
        replace(_signal("fp-pod-a"), status="recovered"),
        webhook_request_id="resolved-fp-pod-a-1",
    )
    still_firing = incidents.workbench(incident_id, team_ids=None, actor_capabilities=[])
    assert still_firing is not None
    assert still_firing["incident"]["lifecycle_state"] == "firing"
    assert still_firing["recovery_observation"] is None

    incidents.ingest(
        replace(_signal("fp-pod-b"), status="recovered"),
        webhook_request_id="resolved-fp-pod-b-1",
    )
    stabilizing = incidents.workbench(incident_id, team_ids=None, actor_capabilities=[])
    assert stabilizing is not None
    assert stabilizing["incident"]["lifecycle_state"] == "stabilizing"
    assert stabilizing["incident"]["evidence_revision"] == 1
    assert stabilizing["recovery_observation"]["status"] == "stabilizing"
    assert stabilizing["recovery_observation"]["resolved_webhook_request_id"] == "resolved-fp-pod-b-1"

    incidents.ingest(_signal("fp-pod-c"))
    interrupted = incidents.workbench(incident_id, team_ids=None, actor_capabilities=[])
    assert interrupted is not None
    assert interrupted["incident"]["lifecycle_state"] == "firing"
    assert interrupted["incident"]["evidence_revision"] == 2
    assert interrupted["recovery_observation"]["status"] == "cancelled"
    incidents.ingest(
        replace(_signal("fp-pod-c"), status="recovered"),
        webhook_request_id="resolved-fp-pod-c-1",
    )

    clock.advance(29)
    assert incidents.list_incidents(team_ids=None)[0]["status"] == "active"
    clock.advance(1)
    resolved = incidents.workbench(incident_id, team_ids=None, actor_capabilities=[])
    assert resolved is not None
    assert resolved["incident"]["status"] == "resolved"
    assert resolved["incident"]["lifecycle_state"] == "resolved"
    assert resolved["recovery_observation"]["status"] == "resolved"
    assert resolved["investigation"]["status"] == "queued"
    resolved_request = next(
        item for item in NotificationOutbox(GatewayDatabase(db_path)).list_requests()
        if item["event_type"] == "incident.resolved"
    )
    assert resolved_request["facts"] == {
        "incident_id": incident_id,
        "status": "resolved",
        "recovery_observation_id": resolved["recovery_observation"]["id"],
        "resolved_webhook_request_id": "resolved-fp-pod-c-1",
        "recovery_observed_at": resolved["recovery_observation"]["observed_at"],
        "stabilizes_at": resolved["recovery_observation"]["stabilizes_at"],
        "resolved_at": resolved["recovery_observation"]["resolved_at"],
    }


def test_refire_cancels_stabilization_and_reopens_only_within_window(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway.db"
    _bound_checkout(db_path)
    clock = _Clock()
    incidents = _lifecycle_service(db_path, clock)
    original = incidents.ingest(_signal("fp-pod-a"))
    incident_id = str(original["incident"]["id"])

    incidents.ingest(
        replace(_signal("fp-pod-a"), status="recovered"),
        webhook_request_id="resolved-fp-pod-a-1",
    )
    clock.advance(10)
    refire = incidents.ingest(_signal("fp-pod-a"))
    assert refire["incident"]["id"] == incident_id
    cancelled = incidents.workbench(incident_id, team_ids=None, actor_capabilities=[])
    assert cancelled is not None
    assert cancelled["incident"]["lifecycle_state"] == "firing"
    assert cancelled["recovery_observation"]["status"] == "cancelled"

    incidents.ingest(
        replace(_signal("fp-pod-a"), status="recovered"),
        webhook_request_id="resolved-fp-pod-a-2",
    )
    clock.advance(30)
    incidents.list_incidents(team_ids=None)
    clock.advance(119)
    reopened = incidents.ingest(_signal("fp-pod-b"))
    assert reopened["incident"]["id"] == incident_id
    assert reopened["incident"]["lifecycle_state"] == "reopened"

    incidents.ingest(
        replace(_signal("fp-pod-b"), status="recovered"),
        webhook_request_id="resolved-fp-pod-b-1",
    )
    clock.advance(30)
    incidents.list_incidents(team_ids=None)
    clock.advance(121)
    new_occurrence = incidents.ingest(_signal("fp-pod-a"))
    assert new_occurrence["created"] is True
    assert new_occurrence["incident"]["id"] != incident_id
    old = incidents.workbench(incident_id, team_ids=None, actor_capabilities=[])
    assert old is not None
    assert old["incident"]["status"] == "resolved"
    assert old["incident"]["signal_count"] == 2


def test_runtime_reconciler_resolves_without_another_request(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "gateway.db"
    _bound_checkout(db_path)
    ids = itertools.count(1)
    database = GatewayDatabase(db_path)
    clock = _Clock()
    incidents = IncidentService(
        database,
        ResourceCatalog(database),
        ConnectorIdentity(database),
        clock=clock,
        stabilization_seconds=30,
        id_factory=lambda prefix: f"{prefix}-{next(ids)}",
    )
    incident_id = str(incidents.ingest(_signal("fp-runtime"))["incident"]["id"])
    incidents.ingest(
        replace(_signal("fp-runtime"), status="recovered"),
        webhook_request_id="resolved-fp-runtime-1",
    )
    clock.advance(30)
    stop = threading.Event()
    reconciled = threading.Event()
    reconcile_due = incidents.reconcile_due

    def reconcile_once() -> int:
        try:
            return reconcile_due()
        finally:
            reconciled.set()

    monkeypatch.setattr(incidents, "reconcile_due", reconcile_once)
    thread = start_incident_reconciler(incidents, interval_seconds=60, stop_event=stop)

    try:
        assert reconciled.wait(timeout=1)
        with database.connect() as conn:
            assert conn.execute("SELECT status FROM incidents WHERE id = ?", (incident_id,)).fetchone()[0] == "resolved"
    finally:
        stop.set()
        thread.join(timeout=1)


def test_recovery_without_resolved_webhook_identity_does_not_resolve(tmp_path: Path) -> None:
    db_path = tmp_path / "gateway.db"
    _bound_checkout(db_path)
    clock = _Clock()
    incidents = _lifecycle_service(db_path, clock)
    incident_id = str(incidents.ingest(_signal("fp-no-webhook"))["incident"]["id"])

    incidents.ingest(replace(_signal("fp-no-webhook"), status="recovered"))
    clock.advance(30)

    assert incidents.reconcile_due() == 0
    snapshot = incidents.workbench(incident_id, team_ids=None, actor_capabilities=[])
    assert snapshot is not None
    assert snapshot["incident"]["status"] == "active"
    assert snapshot["incident"]["lifecycle_state"] == "firing"
    assert snapshot["recovery_observation"] is None
