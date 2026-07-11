"""Kubernetes Discovery Candidate collection."""

from __future__ import annotations

from collections.abc import Callable

from toolsets.topology_store import KubernetesInventory, KubernetesService, KubernetesWorkload

from .stream_client import ConnectorRegistration


def discover_candidates(
    registration: ConnectorRegistration,
    *,
    inventory_factory: Callable[..., KubernetesInventory] | None = None,
) -> list[dict[str, str | None]] | None:
    load_inventory = inventory_factory or KubernetesInventory.from_kubernetes_client
    candidates: list[dict[str, str | None]] = []
    try:
        for namespace in registration.namespace_scope:
            inventory = load_inventory(
                registration.cluster_id,
                namespace=None if namespace == "*" else namespace,
            )
            candidates.extend(_candidate(workload, inventory.services) for workload in inventory.workloads)
    except Exception:
        return None
    return candidates


def _candidate(
    workload: KubernetesWorkload,
    services: tuple[KubernetesService, ...],
) -> dict[str, str | None]:
    pod_labels = workload.pod_labels or workload.labels
    matches = [
        service
        for service in services
        if service.namespace == workload.namespace
        and service.selector
        and all(pod_labels.get(key) == value for key, value in service.selector.items())
    ]
    return {
        "namespace": workload.namespace,
        "workload_kind": workload.kind,
        "workload_name": workload.name,
        "service_name": matches[0].name if len(matches) == 1 else None,
        "service_hint": pod_labels.get("app.kubernetes.io/name") or pod_labels.get("app"),
        "team_hint": pod_labels.get("aiops.io/team"),
    }
