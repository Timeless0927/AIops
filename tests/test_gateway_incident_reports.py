"""T15 Gateway-owned immutable Incident Report behavior."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aiops.domain.identity import SQLiteIdentityStore
from apps.aiops_k8s_gateway import main as gateway_main  # noqa: F401 - register complete schema
from apps.aiops_k8s_gateway.connector_identity import ConnectorIdentity
from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase
from apps.aiops_k8s_gateway.incident import AlertSignal, IncidentService
from apps.aiops_k8s_gateway.incident_reports import IncidentReportError, IncidentReports
from apps.aiops_k8s_gateway.resource_catalog import DiscoveryObservation, ResourceCatalog
from apps.aiops_k8s_gateway.identity_administration import IdentityAdministration
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _incident(db_path: Path) -> tuple[GatewayDatabase, str, str, str]:
    SQLiteIdentityStore(db_path).close()
    store = GatewayV1Store(db_path, credential_factory=lambda: "connector-secret")
    _, user = IdentityAdministration(store.database).mutate(
        collection="users", target_id=None,
        payload={"username": "reporter", "display_name": "Reporter", "password": "strong-password"},
        actor_id="admin", reason="test", action="users_create", request_id="req-user",
    )
    _, team = IdentityAdministration(store.database).mutate(
        collection="teams", target_id=None, payload={"name": "Payments", "description": ""},
        actor_id="admin", reason="test", action="teams_create", request_id="req-team",
    )
    _, credential = store.connector_enrollments.create(
        connector_id="connector-prod", cluster_id="cluster-prod", actor_id="admin",
        reason="test", request_id="req-enroll",
    )
    store.connector_enrollments.register(credential, "connector-prod", "cluster-prod", request_id="req-register")
    database = store.database
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
    ), webhook_request_id="firing-report-1")
    incident_id = str(created["incident"]["id"])
    with database.connect() as conn:
        investigation_id = str(conn.execute(
            "SELECT id FROM investigations WHERE incident_id = ?", (incident_id,)
        ).fetchone()[0])
    return database, incident_id, investigation_id, str(user["id"])


def _resolve(database: GatewayDatabase, incident_id: str, investigation_id: str) -> None:
    with database.connect() as conn:
        incident = conn.execute(
            "SELECT evidence_revision, updated_at FROM incidents WHERE id = ?", (incident_id,),
        ).fetchone()
        evidence_revision = int(incident["evidence_revision"]) + 1
        observed_at = float(incident["updated_at"]) + 1
        resolved_at = observed_at + 300
        conn.execute("UPDATE investigations SET status = 'completed', updated_at = updated_at + 1 WHERE id = ?", (investigation_id,))
        conn.execute(
            "UPDATE alert_signals SET status = 'recovered', recovered_webhook_request_id = ?, updated_at = ? WHERE incident_id = ?",
            (f"resolved-report-{evidence_revision}", observed_at, incident_id),
        )
        conn.execute(
            "INSERT INTO recovery_observations (id, incident_id, evidence_revision, observed_at, stabilizes_at, resolved_at, resolved_webhook_request_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"recovery-report-{evidence_revision}", incident_id, evidence_revision,
                observed_at, resolved_at, resolved_at, f"resolved-report-{evidence_revision}",
            ),
        )
        conn.execute(
            "UPDATE incidents SET status = 'resolved', lifecycle_state = 'resolved', resolved_at = ?, updated_at = ?, evidence_revision = ?, revision = revision + 1 WHERE id = ?",
            (resolved_at, resolved_at, evidence_revision, incident_id),
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
    blocked = reports.get(incident_id, team_ids=None, actor_id=actor_id)
    assert blocked and blocked["not_ready_reason"] == "Incident recovery must prove 300-second stabilization"
    _resolve(database, incident_id, investigation_id)
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
    assert published["facts"]["alert_signals"][0]["firing_webhook_request_id"] == "firing-report-1"
    assert published["facts"]["alert_signals"][0]["recovered_webhook_request_id"] == "resolved-report-1"
    assert published["facts"]["recovery_observations"][0]["resolved_webhook_request_id"] == "resolved-report-1"
    assert published["facts"]["recovery_observations"][0]["stabilizes_at"] - published["facts"]["recovery_observations"][0]["observed_at"] == 300
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
    _resolve(database, incident_id, investigation_id)
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


def test_report_library_projects_scoped_summary_and_latest_version(tmp_path: Path) -> None:
    database, incident_id, investigation_id, actor_id = _incident(tmp_path / "gateway.db")
    ids = iter(("draft-1", "publication-1", "draft-2", "publication-2"))
    reports = IncidentReports(database, clock=lambda: 2_000.0, id_factory=lambda _prefix: next(ids))
    assert reports.list_for_actor(team_ids=None) == []
    _resolve(database, incident_id, investigation_id)

    [draft_summary] = reports.list_for_actor(team_ids=None)
    assert draft_summary["state"] == "draft"
    assert draft_summary["draft"] is None
    assert draft_summary["latest_publication"] is None
    assert "narrative" not in str(draft_summary)
    assert "decision_action_history" not in str(draft_summary)
    reports.get(incident_id, team_ids=None, actor_id=actor_id)
    [persisted_draft] = reports.list_for_actor(team_ids=None)
    assert persisted_draft["draft"]["id"] == "draft-1"  # type: ignore[index]
    reports.publish(incident_id, team_ids=None, actor_id=actor_id)

    with database.connect() as conn:
        conn.execute(
            "UPDATE incidents SET status = 'active', lifecycle_state = 'reopened', "
            "resolved_at = NULL, updated_at = updated_at + 1, revision = revision + 1 WHERE id = ?",
            (incident_id,),
        )
    [reopened] = reports.list_for_actor(team_ids=None)
    assert reopened["state"] == "reopened"
    assert reopened["draft"] is None
    assert reopened["latest_publication"]["version"] == 1  # type: ignore[index]
    assert reports.list_for_actor(team_ids=set()) == []

    _resolve(database, incident_id, investigation_id)
    reports.get(incident_id, team_ids=None, actor_id=actor_id)
    reports.publish(incident_id, team_ids=None, actor_id=actor_id)
    [published] = reports.list_for_actor(team_ids=None)
    assert published["state"] == "published"
    assert published["publication_count"] == 2
    assert published["latest_publication"]["version"] == 2  # type: ignore[index]
    assert published["incident"]["id"] == incident_id  # type: ignore[index]
    assert published["service"]["name"] == "Checkout"  # type: ignore[index]


def test_report_freezes_generic_change_governance_history(tmp_path: Path) -> None:
    database, incident_id, investigation_id, actor_id = _incident(tmp_path / "gateway.db")
    change = {
        "target": {
            "api_version": "apps/v1", "kind": "Deployment", "namespace": "payments",
            "name": "checkout-api", "uid": "uid-1", "resource_version": "41",
        },
        "operation": "patch",
        "payload": [{"op": "test", "path": "/metadata/uid", "value": "uid-1"}],
        "post_checks": [{"type": "workload_rollout"}],
    }
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO change_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("change-1", incident_id, actor_id, "restore availability", "recommendation action-1", "idem-change", 10, 20),
        )
        conn.execute(
            "INSERT INTO change_plan_phases (id, change_request_id, sequence, status, approval_status, orchestration_status, created_at, updated_at) VALUES (?, ?, 1, 'awaiting_approval', 'approved', 'rolled_back', 10, 20)",
            ("phase-1", "change-1"),
        )
        conn.execute(
            "INSERT INTO change_plan_revisions VALUES (?, ?, ?, 1, 'validating', NULL, ?, 11, NULL)",
            ("revision-1", "change-1", "phase-1", json.dumps({"summary": "restart rollout", "changes": [change]})),
        )
        conn.execute(
            "INSERT INTO kubernetes_phase_approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "approval-1", "phase-1", "revision-1", actor_id, '["authority-1"]',
                "idem-approval", "a" * 64, "restore service", "req-approval",
                "rollback_completed", '["apps/v1:Deployment:payments/checkout-api"]',
                json.dumps([{"canonical_change": change, "inverse_change": change}]), 30, 21, 40,
            ),
        )
        conn.execute(
            "INSERT INTO kubernetes_change_executions (id, change_request_id, phase_id, revision_id, approval_id, connector_id, cluster_id, actor_id, reason, request_id, idempotency_key, request_hash, execution_timeout_seconds, rollback_policy, status, result_json, created_at, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 300, 'rollback_completed', 'rolled_back', ?, 22, 23, 26)",
            (
                "execution-1", "change-1", "phase-1", "revision-1", "approval-1",
                "connector-prod", "cluster-prod", actor_id, "restore", "req-execution",
                "idem-execution", "b" * 64,
                json.dumps({"status": "failed", "stdout": "raw secret", "stderr": "raw error"}),
            ),
        )
        conn.execute(
            "INSERT INTO kubernetes_change_execution_steps (id, execution_id, ordinal, direction, source_step_id, command_id, change_hash, change_json, inverse_change_json, status, result_json, created_at, started_at, completed_at) VALUES (?, ?, 1, 'forward', NULL, ?, ?, ?, ?, 'failed', ?, 22, 23, 24)",
            ("step-forward", "execution-1", "command-forward", "c" * 64, json.dumps(change), json.dumps(change), json.dumps({"status": "failed", "stdout": "raw step"})),
        )
        conn.execute(
            "INSERT INTO kubernetes_change_execution_steps (id, execution_id, ordinal, direction, source_step_id, command_id, change_hash, change_json, inverse_change_json, status, result_json, created_at, started_at, completed_at) VALUES (?, ?, 1, 'rollback', ?, ?, ?, ?, NULL, 'rolled_back', ?, 24, 25, 26)",
            ("step-rollback", "execution-1", "step-forward", "command-rollback", "d" * 64, json.dumps(change), json.dumps({"status": "succeeded", "stdout": "raw rollback"})),
        )
        conn.execute(
            "INSERT INTO connector_commands (id, connector_id, cluster_id, namespace, action, parameters_json, status, result_json, created_at, updated_at) VALUES (?, ?, ?, ?, 'reconcile_kubernetes_change', '{}', 'succeeded', '{}', 27, 27)",
            ("command-observation", "connector-prod", "cluster-prod", "payments"),
        )
        conn.execute(
            "INSERT INTO kubernetes_change_reconciliations (id, execution_id, step_id, mutation_command_id, observation_command_id, classification, state, evidence_json, evidence_sha256, observed_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'effect_observed', 'observed', ?, ?, 27, 27, 27)",
            ("reconciliation-1", "execution-1", "step-forward", "command-forward", "command-observation", json.dumps({"post_checks_passed": True}), "e" * 64),
        )
        conn.execute(
            "INSERT INTO change_request_events (change_request_id, type, actor_id, payload_json, created_at) VALUES (?, 'change_request.rollback_finished', NULL, ?, 26)",
            ("change-1", json.dumps({"phase_id": "phase-1", "outcome": "rolled_back"})),
        )
    _resolve(database, incident_id, investigation_id)

    report = IncidentReports(database).get(incident_id, team_ids=None, actor_id=actor_id)
    history = report["draft"]["decision_action_history"]  # type: ignore[index]
    [request] = history["change_requests"]
    [phase] = request["phases"]

    assert phase["approval"]["id"] == "approval-1"
    assert phase["execution"]["status"] == "rolled_back"
    assert [step["direction"] for step in phase["execution"]["steps"]] == ["forward", "rollback"]
    assert phase["execution"]["reconciliations"][0]["classification"] == "effect_observed"
    assert "raw secret" not in json.dumps(report)
    assert "raw step" not in json.dumps(report)
