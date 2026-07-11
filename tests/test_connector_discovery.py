"""Connector Kubernetes discovery module."""

from __future__ import annotations

from apps.cluster_connector.discovery import discover_candidates
from apps.cluster_connector.stream_client import ConnectorRegistration
from toolsets.topology_store import KubernetesInventory, KubernetesService, KubernetesWorkload


def test_service_selector_matches_pod_template_labels() -> None:
    registration = ConnectorRegistration(
        connector_id="connector-prod",
        cluster_id="cluster-prod",
        namespace_scope=("payments",),
        capabilities=(),
    )
    inventory = KubernetesInventory(
        cluster_id="cluster-prod",
        services=(
            KubernetesService(name="checkout", namespace="payments", selector={"app": "checkout"}),
        ),
        workloads=(
            KubernetesWorkload(
                kind="Deployment",
                name="checkout-api",
                namespace="payments",
                labels={"app": "misleading-workload-metadata"},
                pod_labels={"app": "checkout", "app.kubernetes.io/name": "checkout", "aiops.io/team": "payments"},
            ),
        ),
    )

    candidates = discover_candidates(registration, inventory_factory=lambda cluster_id, namespace: inventory)

    assert candidates == [
        {
            "namespace": "payments",
            "workload_kind": "Deployment",
            "workload_name": "checkout-api",
            "service_name": "checkout",
            "service_hint": "checkout",
            "team_hint": "payments",
        }
    ]
