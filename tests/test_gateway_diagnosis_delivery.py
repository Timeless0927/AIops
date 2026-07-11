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
from apps.aiops_k8s_gateway.investigation_events import InvestigationEvents
from apps.aiops_k8s_gateway.resource_catalog import DiscoveryObservation, ResourceCatalog
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
    _, team = store.mutate_admin(
        collection="teams",
        target_id=None,
        payload={"name": "Payments", "description": "支付责任团队"},
        actor_id="admin",
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
        actor_id="admin",
        reason="登记服务",
        request_id="req-service",
    )
    catalog.confirm_binding(
        candidate_id=str(candidate["id"]),
        service_id=str(service["id"]),
        actor_id="admin",
        reason="确认生产工作负载归属",
        request_id="req-binding",
    )
    ids = itertools.count(1)
    database = GatewayDatabase(db_path)
    return IncidentService(
        database,
        catalog,
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
        "steps": [
            {
                "tool": "query_metrics",
                "status": "partial",
                "source_type": "metrics",
                "evidence_ref": {"ref_id": "prometheus:partial"},
                "summary": "metrics query returned a partial window",
            }
        ],
        "missing_evidence": [{"source_type": "prometheus"}],
    }

    assert delivery.accept_writeback(result) == {"ok": True, "duplicate": False}
    assert delivery.accept_writeback(result) == {"ok": True, "duplicate": True}
    investigation = _investigation(incidents, incident_id)
    assert investigation["status"] == "completed"
    assert "request_id" not in investigation
    assert "session_id" not in investigation
    replay = InvestigationEvents(db_path).list(str(investigation["id"]))["events"]
    assert [event["type"] for event in replay] == [
        "investigation.lifecycle",
        "investigation.lifecycle",
        "diagnosis.output",
        "evidence_step.changed",
        "investigation.lifecycle",
    ]
    assert replay[1]["payload"] == {"from": "queued", "to": "running", "reason": "diagnosis_accepted"}
    assert replay[2]["payload"]["status"] == "needs_human"
    assert replay[3]["payload"]["source"] == "prometheus"
    assert replay[4]["payload"] == {"from": "running", "to": "completed", "reason": "diagnosis_result"}

    incidents.ingest(_signal("fp-2"))
    assert _investigation(incidents, incident_id)["sequence"] == 1
    assert incidents.reinvestigate(incident_id)["sequence"] == 2
    assert _investigation(incidents, incident_id)["status"] == "queued"


def test_correction_invalidates_diagnosis_that_depended_on_human_input(tmp_path: Path) -> None:
    clock = Clock()
    db_path = tmp_path / "gateway.db"
    incidents = _incident_service(db_path, clock)
    incident_id = str(incidents.ingest(_signal())["incident"]["id"])  # type: ignore[index]
    investigation_id = str(_investigation(incidents, incident_id)["id"])
    events = InvestigationEvents(db_path, clock=clock)
    assertion = events.submit_human_input(
        investigation_id,
        kind="assertion",
        content="发布发生在告警前五分钟",
        actor_id="sre-1",
        idempotency_key="input-1",
    )
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
    assert sent[0]["human_inputs"] == [
        {
            "event_id": assertion["id"],
            "kind": "assertion",
            "actor_id": "sre-1",
            "payload": {"content": "发布发生在告警前五分钟"},
            "created_at": clock.now,
        }
    ]
    delivery.accept_writeback(
        {
            "request_id": sent[0]["request_id"],
            "incident_id": incident_id,
            "investigation_id": investigation_id,
            "status": "diagnosed",
            "diagnosis": {
                "summary": "发布可能导致错误率升高",
                "human_input_event_ids": [assertion["id"]],
                "recommended_action_ids": ["action-1"],
                "recommended_actions": [
                    {
                        "id": "action-1",
                        "action_type": "restart_deployment",
                        "summary": "重启 checkout-api Deployment",
                        "parameters": {},
                        "evidence_step_ids": ["step-k8s"],
                        "safeguards": ["一次只重启一个 Deployment"],
                    }
                ],
            },
            "evidence_steps": [
                {
                    "id": "step-k8s",
                    "purpose": "确认 Deployment 当前状态",
                    "source": "k8s",
                    "scope": {
                        "cluster_id": "cluster-prod",
                        "namespace": "payments",
                        "workload_kind": "Deployment",
                        "workload_name": "checkout-api",
                    },
                    "state": "succeeded",
                    "result": "revision 42, 3/3 replicas ready",
                    "impact": "目标存在且健康",
                    "evidence_references": ["k8s:deployment/checkout-api@42"],
                    "observed_at": 990.0,
                    "expires_at": 1290.0,
                }
            ],
            "missing_evidence": [],
        }
    )

    correction = events.submit_human_input(
        investigation_id,
        kind="correction",
        content="发布实际发生在告警后",
        actor_id="sre-1",
        idempotency_key="input-2",
        target_event_id=int(assertion["id"]),
    )
    replay = events.list(investigation_id)["events"]

    assert [event["type"] for event in replay[-3:]] == [
        "human_input.correction",
        "judgment.invalidated",
        "recommended_action.stale",
    ]
    assert replay[-2]["payload"]["triggered_by_event_id"] == correction["id"]
    assert replay[-1]["payload"]["recommended_action_id"] == "action-1"
    snapshot = incidents.workbench(incident_id, team_ids=None, actor_capabilities=[])
    assert snapshot is not None
    assert snapshot["judgment"]["valid"] is False  # type: ignore[index]
    assert snapshot["recommended_actions"][0]["stale"] is True  # type: ignore[index]
    assert snapshot["recommended_actions"][0]["gate"]["approvable"] is False  # type: ignore[index]


def test_writeback_projects_canonical_evidence_and_gated_restart_action(tmp_path: Path) -> None:
    clock = Clock()
    db_path = tmp_path / "gateway.db"
    incidents = _incident_service(db_path, clock)
    incident_id = str(incidents.ingest(_signal())["incident"]["id"])  # type: ignore[index]
    investigation = _investigation(incidents, incident_id)
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
        "investigation_id": investigation["id"],
        "status": "diagnosed",
        "diagnosis": {
            "summary": "错误率上升与当前 Deployment revision 相关",
            "next_verification": [],
            "recommended_actions": [
                {
                    "id": "action-restart",
                    "action_type": "restart_deployment",
                    "summary": "重启 checkout-api Deployment",
                    "parameters": {},
                    "evidence_step_ids": ["step-metrics", "step-k8s"],
                    "safeguards": ["一次只重启一个 Deployment"],
                    "rollback_plan": {"type": "none", "reason": "restart 不改变 revision"},
                }
            ],
        },
        "evidence_steps": [
            {
                "id": "step-metrics",
                "purpose": "确认用户错误率仍然异常",
                "source": "prometheus",
                "scope": {
                    "cluster_id": "cluster-prod",
                    "namespace": "payments",
                    "workload_kind": "Deployment",
                    "workload_name": "checkout-api",
                },
                "state": "succeeded",
                "result": "5xx rate 18.7%",
                "impact": "确认故障仍在持续",
                "evidence_references": ["prometheus:checkout-5xx"],
                "observed_at": 990.0,
                "expires_at": 1290.0,
            },
            {
                "id": "step-k8s",
                "purpose": "确认 Deployment 当前状态",
                "source": "k8s",
                "scope": {
                    "cluster_id": "cluster-prod",
                    "namespace": "payments",
                    "workload_kind": "Deployment",
                    "workload_name": "checkout-api",
                },
                "state": "succeeded",
                "result": "revision 42, 3/3 replicas ready",
                "impact": "目标存在且 scope 与 Incident 一致",
                "evidence_references": ["k8s:deployment/checkout-api@42"],
                "observed_at": 995.0,
                "expires_at": 1295.0,
            },
        ],
        "missing_evidence": [],
    }

    assert delivery.accept_writeback(result) == {"ok": True, "duplicate": False}
    snapshot = incidents.workbench(incident_id, team_ids=None, actor_capabilities=["view_incident"])

    assert snapshot is not None
    assert [step["id"] for step in snapshot["evidence_steps"]] == ["step-metrics", "step-k8s"]  # type: ignore[index]
    assert snapshot["evidence_steps"][0] == {  # type: ignore[index]
        "id": "step-metrics",
        "sequence": 1,
        "purpose": "确认用户错误率仍然异常",
        "source": "prometheus",
        "scope": {
            "cluster_id": "cluster-prod",
            "namespace": "payments",
            "workload_kind": "Deployment",
            "workload_name": "checkout-api",
        },
        "state": "succeeded",
        "result": "5xx rate 18.7%",
        "impact": "确认故障仍在持续",
        "evidence_references": ["prometheus:checkout-5xx"],
        "missing_guidance": None,
        "observed_at": 990.0,
        "expires_at": 1290.0,
    }
    assert snapshot["judgment"] == {
        "summary": "错误率上升与当前 Deployment revision 相关",
        "valid": True,
        "evidence_gate_status": "complete",
        "next_evidence_guidance": [],
    }
    [action] = snapshot["recommended_actions"]  # type: ignore[misc]
    assert action["id"] == "action-restart"
    assert action["version"] == 1
    assert action["action_type"] == "restart_deployment"
    assert action["gate"] == {"status": "complete", "approvable": True, "reasons": []}
    assert action["stale"] is False
    assert len(action["hash"]) == 64

    clock.now = 1300.0
    expired = incidents.workbench(incident_id, team_ids=None, actor_capabilities=[])
    assert expired is not None
    assert expired["recommended_actions"][0]["stale"] is True  # type: ignore[index]
    assert expired["recommended_actions"][0]["gate"]["approvable"] is False  # type: ignore[index]

    incidents.ingest(AlertSignal(**{**_signal().__dict__, "status": "recovered"}))
    recovered = incidents.workbench(incident_id, team_ids=None, actor_capabilities=[])
    assert recovered is not None
    assert recovered["recommended_actions"][0]["stale"] is True  # type: ignore[index]
    assert recovered["recommended_actions"][0]["gate"]["status"] == "incomplete"  # type: ignore[index]


def test_incomplete_evidence_keeps_judgment_but_blocks_mutation(tmp_path: Path) -> None:
    clock = Clock()
    db_path = tmp_path / "gateway.db"
    incidents = _incident_service(db_path, clock)
    incident_id = str(incidents.ingest(_signal())["incident"]["id"])  # type: ignore[index]
    investigation = _investigation(incidents, incident_id)
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

    delivery.accept_writeback(
        {
            "request_id": sent[0]["request_id"],
            "incident_id": incident_id,
            "investigation_id": investigation["id"],
            "status": "partial",
            "diagnosis": {
                "summary": "日志支持回归判断，但 Kubernetes 状态尚未确认",
                "next_verification": ["重新读取 Deployment revision"],
                "recommended_actions": [
                    {
                        "id": "action-blocked",
                        "action_type": "restart_deployment",
                        "summary": "重启 checkout-api Deployment",
                        "parameters": {},
                        "evidence_step_ids": ["step-k8s"],
                        "safeguards": ["一次只重启一个 Deployment"],
                    }
                ],
            },
            "evidence_steps": [
                {
                    "id": "step-k8s",
                    "purpose": "确认 Deployment 当前状态",
                    "source": "k8s",
                    "scope": {
                        "cluster_id": "cluster-prod",
                        "namespace": "payments",
                        "workload_kind": "Deployment",
                        "workload_name": "checkout-api",
                    },
                    "state": "partial",
                    "result": "API Server timeout",
                    "impact": "无法确认当前 revision 与 replicas",
                    "evidence_references": [],
                    "missing_guidance": "Connector 恢复后重新读取 Deployment",
                    "observed_at": 990.0,
                    "expires_at": 995.0,
                }
            ],
            "missing_evidence": [{"source_type": "k8s", "reason": "Deployment observation incomplete"}],
        }
    )
    snapshot = incidents.workbench(incident_id, team_ids=None, actor_capabilities=[])

    assert snapshot is not None
    assert snapshot["evidence_steps"][0]["missing_guidance"] == "Connector 恢复后重新读取 Deployment"  # type: ignore[index]
    assert snapshot["judgment"]["summary"] == "日志支持回归判断，但 Kubernetes 状态尚未确认"  # type: ignore[index]
    assert snapshot["judgment"]["evidence_gate_status"] == "incomplete"  # type: ignore[index]
    assert "重新读取 Deployment revision" in snapshot["judgment"]["next_evidence_guidance"]  # type: ignore[index]
    [action] = snapshot["recommended_actions"]  # type: ignore[misc]
    assert action["gate"]["status"] == "incomplete"
    assert action["gate"]["approvable"] is False
    assert "all referenced Evidence Steps must succeed" in action["gate"]["reasons"]
    assert "referenced evidence is stale" in action["gate"]["reasons"]
    assert "referenced Evidence Step has no evidence reference" in action["gate"]["reasons"]
