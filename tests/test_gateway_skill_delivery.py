from __future__ import annotations

from pathlib import Path

from apps.aiops_k8s_gateway.diagnosis_delivery import DiagnosisDelivery
from apps.aiops_k8s_gateway.mcp_registry import MCPRegistry
from apps.aiops_k8s_gateway.skill_registry import SkillRegistry
from tests.test_gateway_diagnosis_delivery import Clock, _incident_service, _signal


def test_investigation_delivery_freezes_exact_skill_and_mcp_versions(tmp_path: Path) -> None:
    clock = Clock()
    db_path = tmp_path / "gateway.db"
    incidents = _incident_service(db_path, clock)
    incidents.ingest(_signal())
    mcp = MCPRegistry(
        db_path,
        id_factory=lambda: "mcp-metrics",
        revision_id=lambda: "mcp-revision:metrics",
    )
    mcp.create(
        name="Prometheus",
        endpoint="https://mcp.example.test",
        credential=None,
        capabilities=[{"name": "query_metrics", "version": "prometheus-query-v1", "read_only": True}],
        allowed_scope=[{"cluster_id": "cluster-prod", "namespace": "payments"}],
        enabled=True,
        actor_id="admin",
        reason="register metrics",
        request_id="mcp-create",
    )
    mcp.verify(
        "mcp-metrics",
        actor_id="admin",
        reason="verify metrics",
        request_id="mcp-verify",
        probe=lambda *_args: {
            "status": "ok",
            "capabilities": [{
                "name": "query_metrics", "version": "prometheus-query-v1",
                "read_only": True, "mutation": False, "path": "/query_metrics",
            }],
        },
    )
    skills = SkillRegistry(db_path, id_factory=lambda: "skill-payments")
    skills.create(
        name="Payments triage",
        instruction="Check the error-rate Observation first.",
        workflow=[],
        applicable_scope=[{"cluster_id": "cluster-prod", "namespace": "payments"}],
        required_mcp=[{
            "integration_id": "mcp-metrics", "integration_revision": "mcp-revision:metrics",
            "name": "query_metrics", "version": "prometheus-query-v1",
        }],
        actor_id="admin",
        reason="create triage practice",
        request_id="skill-create",
    )
    skills.set_enabled(
        "skill-payments", version=1, expected_active_version=None,
        mcp_integrations=mcp.list(), actor_id="admin", reason="enable practice",
        request_id="skill-enable",
    )
    sent: list[dict[str, object]] = []
    delivery = DiagnosisDelivery(
        db_path,
        capability_snapshot=mcp.authorized_snapshot,
        skill_bindings=skills.authorized_bindings,
        send=lambda payload: (
            sent.append(payload) or 202,
            {"status": "accepted", "request_id": payload["request_id"]},
        ),
        clock=clock,
    )

    assert delivery.reconcile_due() == 1
    assert sent[0]["skills"] == [{
        "id": "skill-payments", "name": "Payments triage", "version": 1,
        "instruction": "Check the error-rate Observation first.", "workflow": [],
        "required_mcp": [{
            "integration_id": "mcp-metrics", "integration_revision": "mcp-revision:metrics",
            "name": "query_metrics", "version": "prometheus-query-v1",
        }],
    }]
