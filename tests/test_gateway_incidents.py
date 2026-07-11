"""T07 Gateway-owned Incident module behavior."""

from __future__ import annotations

import itertools
from dataclasses import replace
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway.incident import AlertSignal, IncidentError, IncidentService
from apps.aiops_k8s_gateway.connector_identity import ConnectorIdentity
from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase
from apps.aiops_k8s_gateway.resource_catalog import DiscoveryObservation, ResourceCatalog
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _registered_cluster(db_path: Path) -> tuple[GatewayV1Store, str]:
    store = GatewayV1Store(
        db_path,
        credential_factory=lambda: "connector-secret",
        id_factory=lambda prefix: f"{prefix}-fixed",
    )
    _, credential = store.create_connector_enrollment(
        connector_id="connector-prod",
        cluster_id="cluster-prod",
        actor_id="admin-1",
        reason="接入生产集群",
        request_id="req-enroll",
    )
    store.register_connector(
        credential,
        "connector-prod",
        "cluster-prod",
        request_id="req-register",
    )
    return store, credential


def _bound_checkout(db_path: Path) -> tuple[str, str]:
    store, _ = _registered_cluster(db_path)
    _, team = store.mutate_admin(
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
    assert snapshot["event_cursor"] == 0
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

    GatewayV1Store(db_path).record_connector_heartbeat(
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
