"""T09 Gateway-owned durable Diagnosis Request behavior."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway.connector_identity import ConnectorIdentity
from apps.aiops_k8s_gateway.diagnosis_delivery import DiagnosisDelivery, DiagnosisDeliveryError
from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase
from apps.aiops_k8s_gateway.incident import AlertSignal, IncidentService
from apps.aiops_k8s_gateway.resource_catalog import ResourceCatalog
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


@dataclass
class Clock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


def _incident_service(db_path: Path, clock: Clock) -> IncidentService:
    store = GatewayV1Store(db_path, credential_factory=lambda: "connector-secret")
    _, credential = store.create_connector_enrollment(
        connector_id="connector-prod",
        cluster_id="cluster-prod",
        actor_id="admin",
        reason="接入生产集群",
        request_id="req-enroll",
    )
    store.register_connector(credential, "connector-prod", "cluster-prod", request_id="req-register")
    ids = itertools.count(1)
    database = GatewayDatabase(db_path)
    return IncidentService(
        database,
        ResourceCatalog(database),
        ConnectorIdentity(database),
        clock=clock,
        id_factory=lambda prefix: f"{prefix}-{next(ids)}",
        diagnosis_request_ttl_seconds=30,
    )


def _signal(fingerprint: str = "fp-1") -> AlertSignal:
    return AlertSignal(
        fingerprint=fingerprint,
        alertname="HighErrorRate",
        status="firing",
        severity="critical",
        cluster_id="cluster-prod",
        namespace="payments",
        summary="checkout error rate is above 10%",
        workload_kind="Deployment",
        workload_name="checkout-api",
    )


def _investigation(service: IncidentService, incident_id: str) -> dict[str, object]:
    snapshot = service.workbench(incident_id, team_ids=None, actor_capabilities=["view_incident"])
    assert snapshot is not None
    return snapshot["investigation"]  # type: ignore[return-value]


def test_request_retries_until_diagnosis_durably_accepts(tmp_path: Path) -> None:
    clock = Clock()
    db_path = tmp_path / "gateway.db"
    incidents = _incident_service(db_path, clock)
    incident_id = str(incidents.ingest(_signal())["incident"]["id"])  # type: ignore[index]
    calls: list[dict[str, object]] = []

    def unavailable(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        calls.append(payload)
        return 503, {"status": "unavailable"}

    delivery = DiagnosisDelivery(db_path, send=unavailable, clock=clock, retry_base_seconds=1)
    assert delivery.reconcile_due() == 1
    assert _investigation(incidents, incident_id)["status"] == "queued"
    assert calls[0]["incident_id"] == incident_id
    assert "investigation_id" in calls[0]

    clock.now += 2
    delivery = DiagnosisDelivery(
        db_path,
        send=lambda payload: (202, {"status": "accepted", "request_id": payload["request_id"]}),
        clock=clock,
        retry_base_seconds=1,
    )
    assert delivery.reconcile_due() == 1
    assert _investigation(incidents, incident_id)["status"] == "running"

    incidents.ingest(_signal("fp-2"))
    assert delivery.reconcile_due() == 0


def test_rejected_or_expired_request_fails_investigation(tmp_path: Path) -> None:
    clock = Clock()
    rejected_db = tmp_path / "rejected.db"
    rejected_incidents = _incident_service(rejected_db, clock)
    rejected_id = str(rejected_incidents.ingest(_signal())["incident"]["id"])  # type: ignore[index]
    rejected = DiagnosisDelivery(rejected_db, send=lambda _: (422, {"status": "rejected"}), clock=clock)

    assert rejected.reconcile_due() == 1
    assert _investigation(rejected_incidents, rejected_id)["status"] == "failed"

    expired_db = tmp_path / "expired.db"
    expired_incidents = _incident_service(expired_db, clock)
    expired_id = str(expired_incidents.ingest(_signal())["incident"]["id"])  # type: ignore[index]
    clock.now += 31
    expired = DiagnosisDelivery(expired_db, send=lambda _: (202, {"status": "accepted"}), clock=clock)

    assert expired.reconcile_due() == 1
    assert _investigation(expired_incidents, expired_id)["status"] == "failed"


def test_cancel_stops_delivery_and_terminates_queued_investigation(tmp_path: Path) -> None:
    clock = Clock()
    db_path = tmp_path / "gateway.db"
    incidents = _incident_service(db_path, clock)
    incident_id = str(incidents.ingest(_signal())["incident"]["id"])  # type: ignore[index]
    investigation_id = str(_investigation(incidents, incident_id)["id"])
    delivery = DiagnosisDelivery(db_path, send=lambda _: (500, {}), clock=clock)

    assert delivery.cancel(investigation_id) is True
    assert delivery.reconcile_due() == 0
    assert _investigation(incidents, incident_id)["status"] == "terminated"


def test_late_writeback_cannot_revive_expired_request(tmp_path: Path) -> None:
    clock = Clock()
    db_path = tmp_path / "gateway.db"
    incidents = _incident_service(db_path, clock)
    incident_id = str(incidents.ingest(_signal())["incident"]["id"])  # type: ignore[index]
    investigation = _investigation(incidents, incident_id)
    sent: list[dict[str, object]] = []
    delivery = DiagnosisDelivery(
        db_path,
        send=lambda payload: (sent.append(payload) or 503, {"status": "unavailable"}),
        clock=clock,
    )
    delivery.reconcile_due()
    clock.now += 31
    delivery.reconcile_due()

    with pytest.raises(DiagnosisDeliveryError, match="already terminal"):
        delivery.accept_writeback(
            {
                "request_id": sent[0]["request_id"],
                "incident_id": incident_id,
                "investigation_id": investigation["id"],
                "status": "failed",
                "diagnosis": {"summary": "late"},
                "missing_evidence": [],
            }
        )


def test_writeback_is_idempotent_and_does_not_expose_job_identity(tmp_path: Path) -> None:
    clock = Clock()
    db_path = tmp_path / "gateway.db"
    incidents = _incident_service(db_path, clock)
    incident_id = str(incidents.ingest(_signal())["incident"]["id"])  # type: ignore[index]
    sent: list[dict[str, object]] = []
    delivery = DiagnosisDelivery(
        db_path,
        send=lambda payload: (
            sent.append(payload) or 202,
            {"status": "accepted", "request_id": payload["request_id"]},
        ),
        clock=clock,
    )
    delivery.reconcile_due()
    result = {
        "request_id": sent[0]["request_id"],
        "incident_id": incident_id,
        "investigation_id": sent[0]["investigation_id"],
        "status": "needs_human",
        "diagnosis": {"summary": "metrics unavailable"},
        "missing_evidence": [{"source_type": "prometheus"}],
    }

    assert delivery.accept_writeback(result) == {"ok": True, "duplicate": False}
    assert delivery.accept_writeback(result) == {"ok": True, "duplicate": True}
    investigation = _investigation(incidents, incident_id)
    assert investigation["status"] == "completed"
    assert "request_id" not in investigation
    assert "session_id" not in investigation

    incidents.ingest(_signal("fp-2"))
    assert _investigation(incidents, incident_id)["sequence"] == 1
    assert incidents.reinvestigate(incident_id)["sequence"] == 2
    assert _investigation(incidents, incident_id)["status"] == "queued"
