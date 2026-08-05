from __future__ import annotations

from pathlib import Path

from apps.aiops_k8s_gateway.diagnosis_delivery import DiagnosisDelivery
from tests.test_gateway_diagnosis_delivery import Clock, _incident_service, _signal


def test_diagnosis_request_carries_registry_snapshot_for_incident_scope(tmp_path: Path) -> None:
    clock = Clock()
    db_path = tmp_path / "gateway.db"
    incidents = _incident_service(db_path, clock)
    incident_id = str(incidents.ingest(_signal())["incident"]["id"])  # type: ignore[index]
    resolutions: list[tuple[object, str, str]] = []
    sent: list[dict[str, object]] = []

    def snapshot(scope, *, actor_id: str, request_id: str):
        resolutions.append((scope, actor_id, request_id))
        return {
            "query_metrics": {
                "name": "query_metrics", "version": "prometheus-query-v1",
                "enabled": True, "read_only": True, "mutation": False,
                "integration_id": "mcp-metrics", "integration_revision": "mcp-revision:1",
            },
        }

    delivery = DiagnosisDelivery(
        db_path, capability_snapshot=snapshot,
        send=lambda payload: (
            sent.append(payload) or 202,
            {"status": "accepted", "request_id": payload["request_id"]},
        ),
        clock=clock,
    )
    assert delivery.reconcile_due() == 1

    request_id = str(sent[0]["request_id"])
    assert sent[0]["incident_id"] == incident_id
    assert sent[0]["capabilities"]["query_metrics"]["integration_id"] == "mcp-metrics"  # type: ignore[index]
    assert resolutions == [(
        {"resources": [{"cluster_id": "cluster-prod", "namespace": "payments"}]},
        "system:gateway", request_id,
    )]
