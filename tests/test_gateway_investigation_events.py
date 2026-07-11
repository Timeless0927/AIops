"""T10 durable Investigation Event and Human Input behavior."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway.connector_identity import ConnectorIdentity
from apps.aiops_k8s_gateway.diagnosis_delivery import DiagnosisDelivery
from apps.aiops_k8s_gateway.gateway_db import GatewayDatabase
from apps.aiops_k8s_gateway.incident import AlertSignal, IncidentService
from apps.aiops_k8s_gateway.investigation_events import InvestigationEventError, InvestigationEvents
from apps.aiops_k8s_gateway.resource_catalog import ResourceCatalog
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


@dataclass
class Clock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


def _investigation(tmp_path: Path) -> tuple[IncidentService, InvestigationEvents, str, str]:
    db_path = tmp_path / "gateway.db"
    store = GatewayV1Store(db_path, credential_factory=lambda: "connector-secret")
    _, credential = store.create_connector_enrollment(
        connector_id="connector-prod",
        cluster_id="cluster-prod",
        actor_id="admin",
        reason="接入生产集群",
        request_id="req-enroll",
    )
    store.register_connector(credential, "connector-prod", "cluster-prod", request_id="req-register")
    clock = Clock()
    ids = itertools.count(1)
    database = GatewayDatabase(db_path)
    incidents = IncidentService(
        database,
        ResourceCatalog(database),
        ConnectorIdentity(database),
        clock=clock,
        id_factory=lambda prefix: f"{prefix}-{next(ids)}",
    )
    incident = incidents.ingest(
        AlertSignal(
            fingerprint="fp-1",
            alertname="HighErrorRate",
            status="firing",
            severity="critical",
            cluster_id="cluster-prod",
            namespace="payments",
        )
    )["incident"]
    incident_id = str(incident["id"])  # type: ignore[index]
    snapshot = incidents.workbench(incident_id, team_ids=None, actor_capabilities=["view_incident"])
    assert snapshot is not None
    investigation_id = str(snapshot["investigation"]["id"])  # type: ignore[index]
    return incidents, InvestigationEvents(database, clock=clock), incident_id, investigation_id


def test_events_are_ordered_pageable_and_human_input_is_append_only(tmp_path: Path) -> None:
    incidents, events, incident_id, investigation_id = _investigation(tmp_path)

    first_page = events.list(investigation_id, after=0, limit=1)
    assert [event["type"] for event in first_page["events"]] == ["investigation.lifecycle"]
    assert first_page["events"][0]["payload"] == {"from": None, "to": "queued", "reason": "alert_signal"}
    assert first_page["next_cursor"] == 1

    assertion = events.submit_human_input(
        investigation_id,
        kind="assertion",
        content="发布发生在 10:00",
        actor_id="sre-1",
        idempotency_key="input-1",
    )
    correction = events.submit_human_input(
        investigation_id,
        kind="correction",
        content="发布发生在 10:05",
        actor_id="sre-1",
        idempotency_key="input-2",
        target_event_id=int(assertion["id"]),
    )
    retraction = events.submit_human_input(
        investigation_id,
        kind="retraction",
        content="时间未经核实",
        actor_id="sre-1",
        idempotency_key="input-3",
        target_event_id=int(correction["id"]),
    )

    replay = events.list(investigation_id, after=1, limit=10)
    assert [event["id"] for event in replay["events"]] == [2, 3, 4]
    assert [event["type"] for event in replay["events"]] == [
        "human_input.assertion",
        "human_input.correction",
        "human_input.retraction",
    ]
    assert replay["events"][1]["payload"]["target_event_id"] == assertion["id"]
    assert replay["events"][2]["payload"]["target_event_id"] == correction["id"]
    assert replay["next_cursor"] == 4
    assert events.submit_human_input(
        investigation_id,
        kind="assertion",
        content="发布发生在 10:00",
        actor_id="sre-1",
        idempotency_key="input-1",
    ) == assertion

    snapshot = incidents.workbench(incident_id, team_ids=None, actor_capabilities=["view_incident"])
    assert snapshot is not None
    assert snapshot["event_cursor"] == 4


def test_human_input_rejects_invalid_reference_and_idempotency_conflict(tmp_path: Path) -> None:
    _, events, _, investigation_id = _investigation(tmp_path)
    assertion = events.submit_human_input(
        investigation_id,
        kind="assertion",
        content="可能是刚发布引起",
        actor_id="sre-1",
        idempotency_key="input-1",
    )

    with pytest.raises(InvestigationEventError, match="same request key"):
        events.submit_human_input(
            investigation_id,
            kind="assertion",
            content="不同内容",
            actor_id="sre-1",
            idempotency_key="input-1",
        )
    with pytest.raises(InvestigationEventError, match="target Human Input"):
        events.submit_human_input(
            investigation_id,
            kind="correction",
            content="修正",
            actor_id="sre-1",
            idempotency_key="input-2",
            target_event_id=int(assertion["id"]) + 100,
        )


def test_controls_are_auditable_and_terminal_investigation_cannot_be_revived(tmp_path: Path) -> None:
    incidents, events, incident_id, investigation_id = _investigation(tmp_path)

    paused = events.control(investigation_id, action="pause", actor_id="sre-1", idempotency_key="ctl-1")
    takeover = events.control(investigation_id, action="takeover", actor_id="sre-1", idempotency_key="ctl-2")
    terminated = events.control(investigation_id, action="terminate", actor_id="sre-1", idempotency_key="ctl-3")

    assert [paused["payload"]["to"], takeover["payload"]["to"], terminated["payload"]["to"]] == [
        "paused",
        "human_led",
        "terminated",
    ]
    assert all(event["actor_id"] == "sre-1" for event in (paused, takeover, terminated))
    terminal_page, terminal_status = events.poll(investigation_id, after=int(takeover["id"]))
    assert [event["id"] for event in terminal_page["events"]] == [terminated["id"]]
    assert terminal_status == "terminated"
    snapshot = incidents.workbench(incident_id, team_ids=None, actor_capabilities=["view_incident"])
    assert snapshot is not None
    assert snapshot["investigation"]["status"] == "terminated"  # type: ignore[index]

    with pytest.raises(InvestigationEventError, match="not allowed"):
        events.control(investigation_id, action="pause", actor_id="sre-1", idempotency_key="ctl-4")

    sent: list[dict[str, object]] = []
    delivery = DiagnosisDelivery(tmp_path / "gateway.db", send=lambda payload: (sent.append(payload) or 202, {}))
    assert delivery.reconcile_due() == 1
    assert delivery.reconcile_due() == 0
    assert sent == []


def test_reinvestigate_is_idempotent_after_the_new_round_is_terminal(tmp_path: Path) -> None:
    incidents, events, incident_id, investigation_id = _investigation(tmp_path)
    events.control(investigation_id, action="terminate", actor_id="sre-1", idempotency_key="ctl-1")

    created = incidents.reinvestigate(incident_id, idempotency_key="retry-safe")
    events.control(str(created["id"]), action="terminate", actor_id="sre-1", idempotency_key="ctl-2")
    duplicate = incidents.reinvestigate(incident_id, idempotency_key="retry-safe")

    assert duplicate == {**created, "status": "terminated"}
