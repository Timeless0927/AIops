from __future__ import annotations

from pathlib import Path

from aiops.contracts.governed_tools import approved_capability, capability_denial
from apps.aiops_k8s_gateway.diagnosis_delivery import DiagnosisDelivery
from tests.test_gateway_diagnosis_delivery import Clock, _incident_service, _signal
from tests.test_gateway_mcp_registry import _registry


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
    assert sent[0]["capabilities"]["run_k8s_read"]["owner"] == "gateway"  # type: ignore[index]
    assert resolutions == [(
        {"resources": [{"cluster_id": "cluster-prod", "namespace": "payments"}]},
        "system:gateway", request_id,
    )]


def test_diagnosis_request_always_carries_gateway_owned_k8s_read(tmp_path: Path) -> None:
    clock = Clock()
    db_path = tmp_path / "gateway.db"
    _incident_service(db_path, clock).ingest(_signal())
    sent: list[dict[str, object]] = []
    delivery = DiagnosisDelivery(
        db_path,
        capability_snapshot=lambda _scope, **_context: {},
        send=lambda payload: (
            sent.append(payload) or 202,
            {"status": "accepted", "request_id": payload["request_id"]},
        ),
        clock=clock,
    )

    assert delivery.reconcile_due() == 1
    assert sent[0]["capabilities"] == {
        "run_k8s_read": {
            "name": "run_k8s_read",
            "version": "gateway-k8s-read-v1",
            "enabled": True,
            "read_only": True,
            "mutation": False,
            "owner": "gateway",
        }
    }


def test_registry_cannot_declare_gateway_owned_k8s_read(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    policy = [{"name": "run_k8s_read", "version": "gateway-k8s-read-v1", "read_only": True}]
    registry.create(
        name="Kubernetes read",
        endpoint="https://mcp.example.test",
        credential=None,
        capabilities=policy,
        allowed_scope=[{"cluster_id": "cluster-a", "namespace": "payments"}],
        enabled=True,
        actor_id="user:admin",
        reason="register",
        request_id="mcp-create-1",
    )
    verification = registry.verify(
        "mcp-1",
        actor_id="user:admin",
        reason="verify",
        request_id="mcp-verify-1",
        probe=lambda *_args: {
            "status": "ok",
            "capabilities": [{
                "name": "run_k8s_read",
                "version": "gateway-k8s-read-v1",
                "read_only": True,
                "mutation": False,
                "path": "/run_k8s_read",
            }],
        },
    )
    declared = {
        "run_k8s_read": {
            "name": "run_k8s_read",
            "version": "gateway-k8s-read-v1",
            "enabled": True,
            "read_only": True,
            "mutation": False,
            "integration_id": "mcp-k8s",
            "integration_revision": "mcp-revision:1",
        }
    }

    assert approved_capability("run_k8s_read") is None
    assert verification["verification"]["reason_code"] == "capability_snapshot_changed"  # type: ignore[index]
    assert capability_denial("run_k8s_read", declared) == "tool capability owner changed"
