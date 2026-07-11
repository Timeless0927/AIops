"""T15 Gateway-owned immutable Incident Report behavior."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aiops.domain.identity import SQLiteIdentityStore
from apps.aiops_k8s_gateway.approval import Approvals
from apps.aiops_k8s_gateway.connector_identity import ConnectorIdentity
from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase
from apps.aiops_k8s_gateway.incident import AlertSignal, IncidentService
from apps.aiops_k8s_gateway.incident_reports import IncidentReportError, IncidentReports
from apps.aiops_k8s_gateway.resource_catalog import DiscoveryObservation, ResourceCatalog
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _incident(db_path: Path) -> tuple[GatewayDatabase, str, str, str]:
    SQLiteIdentityStore(db_path).close()
    store = GatewayV1Store(db_path, credential_factory=lambda: "connector-secret")
    _, user = store.mutate_admin(
        collection="users", target_id=None,
        payload={"username": "reporter", "display_name": "Reporter", "password": "strong-password"},
        actor_id="admin", reason="test", action="users_create", request_id="req-user",
    )
    _, team = store.mutate_admin(
        collection="teams", target_id=None, payload={"name": "Payments", "description": ""},
        actor_id="admin", reason="test", action="teams_create", request_id="req-team",
    )
    _, credential = store.create_connector_enrollment(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="test", request_id="req-enroll",
    )
    store.register_connector(credential, "connector-prod", "cluster-prod", request_id="req-register")
    database = store.database
    Approvals(database)
    catalog = ResourceCatalog(database)
    [candidate] = catalog.refresh_discovery(
        "cluster-prod",
        [DiscoveryObservation(namespace="payments", workload_kind="Deployment", workload_name="checkout-api")],
    )
    service = catalog.create_service(
        team_id=str(team["id"]), name="Checkout", description="", actor_id="admin",
        reason="test", request_id="req-service",
    )
    catalog.confirm_binding(
        candidate_id=str(candidate["id"]), service_id=str(service["id"]), actor_id="admin",
        reason="test", request_id="req-binding",
    )
    incidents = IncidentService(database, catalog, ConnectorIdentity(database))
    created = incidents.ingest(AlertSignal(
        fingerprint="fp-report", alertname="HighErrorRate", cluster_id="cluster-prod", namespace="payments",
        status="firing", severity="critical", summary="error rate above 10%",
        workload_kind="Deployment", workload_name="checkout-api",
    ))
    incident_id = str(created["incident"]["id"])
    with database.connect() as conn:
        investigation_id = str(conn.execute(
            "SELECT id FROM investigations WHERE incident_id = ?", (incident_id,)
        ).fetchone()[0])
    return database, incident_id, investigation_id, str(user["id"])


def _resolve(database: GatewayDatabase, incident_id: str, investigation_id: str) -> None:
    with database.connect() as conn:
        conn.execute("UPDATE investigations SET status = 'completed', updated_at = updated_at + 1 WHERE id = ?", (investigation_id,))
        conn.execute(
            "UPDATE incidents SET status = 'resolved', lifecycle_state = 'resolved', resolved_at = updated_at + 2, updated_at = updated_at + 2, revision = revision + 1 WHERE id = ?",
            (incident_id,),
        )


def test_report_requires_resolved_incident_and_terminal_investigations(tmp_path: Path) -> None:
    database, incident_id, investigation_id, actor_id = _incident(tmp_path / "gateway.db")
    reports = IncidentReports(database, id_factory=lambda prefix: f"{prefix}-1")

    unavailable = reports.get(incident_id, team_ids=None, actor_id=actor_id)
    assert unavailable == {
        "availability": "not_ready", "not_ready_reason": "Incident must be resolved",
        "draft": None, "publications": [],
    }

    with database.connect() as conn:
        conn.execute(
            "UPDATE incidents SET status = 'resolved', lifecycle_state = 'resolved', resolved_at = updated_at + 1, updated_at = updated_at + 1, revision = revision + 1 WHERE id = ?",
            (incident_id,),
        )
    waiting = reports.get(incident_id, team_ids=None, actor_id=actor_id)
    assert waiting and waiting["not_ready_reason"] == "All Investigations must be terminal"

    with database.connect() as conn:
        conn.execute("UPDATE investigations SET status = 'completed' WHERE id = ?", (investigation_id,))
    ready = reports.get(incident_id, team_ids=None, actor_id=actor_id)
    assert ready and ready["availability"] == "ready"
    assert ready["draft"]["included_investigation_ids"] == [investigation_id]  # type: ignore[index]


def test_publish_freezes_version_and_reresolve_creates_new_draft(tmp_path: Path) -> None:
    database, incident_id, investigation_id, actor_id = _incident(tmp_path / "gateway.db")
    _resolve(database, incident_id, investigation_id)
    ids = iter(("report-draft-1", "report-version-1", "report-draft-2", "report-version-2"))
    reports = IncidentReports(database, clock=lambda: 2000.0, id_factory=lambda _prefix: next(ids))
    first = reports.get(incident_id, team_ids=None, actor_id=actor_id)
    assert first and first["draft"]["status"] == "draft"  # type: ignore[index]

    narrative = {
        "impact": "Checkout failed for 12 minutes.",
        "root_cause": "A bad Deployment revision saturated workers.",
        "resolution_summary": "Rolled back to revision 41.",
        "follow_up": "Add a rollout saturation alert.",
    }
    updated = reports.update(incident_id, narrative, team_ids=None, actor_id=actor_id)
    published = reports.publish(incident_id, team_ids=None, actor_id=actor_id)
    replay = reports.publish(incident_id, team_ids=None, actor_id=actor_id)
    assert published == replay
    assert published["version"] == 1 and published["narrative"] == narrative
    assert published["facts"] == updated["facts"]
    assert "html" not in published and "session_id" not in str(published)

    with pytest.raises(IncidentReportError, match="cannot be edited"):
        reports.update(incident_id, narrative, team_ids=None, actor_id=actor_id)
    with database.connect() as conn, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE incident_report_publications SET version = 9 WHERE id = ?", (published["id"],))

    with database.connect() as conn:
        conn.execute(
            "UPDATE incidents SET status = 'active', lifecycle_state = 'reopened', resolved_at = NULL, revision = revision + 1 WHERE id = ?",
            (incident_id,),
        )
        conn.execute(
            "UPDATE incidents SET status = 'resolved', lifecycle_state = 'resolved', resolved_at = updated_at + 3, updated_at = updated_at + 3, revision = revision + 1 WHERE id = ?",
            (incident_id,),
        )
    second = reports.get(incident_id, team_ids=None, actor_id=actor_id)
    assert second and second["draft"]["id"] == "report-draft-2"  # type: ignore[index]
    assert second["draft"]["narrative"] == {field: "" for field in narrative}  # type: ignore[index]
    assert [version["version"] for version in second["publications"]] == [1]  # type: ignore[union-attr]


def test_report_scope_and_narrative_field_boundary(tmp_path: Path) -> None:
    database, incident_id, investigation_id, actor_id = _incident(tmp_path / "gateway.db")
    _resolve(database, incident_id, investigation_id)
    reports = IncidentReports(database)

    assert reports.get(incident_id, team_ids=set(), actor_id=actor_id) is None
    with pytest.raises(IncidentReportError, match="Only Incident Report narrative fields"):
        reports.update(incident_id, {"impact": "edited frozen facts"}, team_ids=None, actor_id=actor_id)
    with database.connect() as conn:
        conn.execute(
            "UPDATE incidents SET binding_status = 'unbound', resource_binding_id = NULL, service_id = NULL, team_id = NULL WHERE id = ?",
            (incident_id,),
        )
    assert reports.get(incident_id, team_ids=set(), actor_id=actor_id) is not None
