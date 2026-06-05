from __future__ import annotations

import time
from pathlib import Path

import pytest

from toolsets.topology_store import (
    KubernetesConfigMap,
    KubernetesInventory,
    KubernetesService,
    KubernetesWorkload,
    ServiceEdge,
    ServiceRecord,
    TopologyStore,
    get_service_topology,
    normalize_service_identity,
)


def _store(tmp_path: Path, *, stale_after_seconds: int = 300) -> TopologyStore:
    return TopologyStore(tmp_path / "topology.db", stale_after_seconds=stale_after_seconds)


def test_service_identity_normalizes_cluster_namespace_and_service() -> None:
    identity = normalize_service_identity("Prod Cluster", "Payments_NS", "Checkout API")

    assert identity.cluster_id == "prod-cluster"
    assert identity.namespace == "payments-ns"
    assert identity.service == "checkout-api"
    assert identity.service_id == "prod-cluster/payments-ns/checkout-api"
    assert identity.warnings == (
        "cluster_id_normalized",
        "namespace_normalized",
        "service_normalized",
    )


def test_service_identity_defaults_missing_values() -> None:
    identity = normalize_service_identity(None, "", None)

    assert identity.service_id == "default/default/unknown-service"
    assert "cluster_id_missing" in identity.warnings
    assert "namespace_missing" in identity.warnings
    assert "service_missing" in identity.warnings


def test_upsert_service_records_app_label_and_workload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_service(
        ServiceRecord(
            cluster_id="cluster-a",
            namespace="default",
            service="checkout",
            service_category="clusterip",
            workload_kind="Deployment",
            workload_name="checkout-api",
            app_label="checkout",
            source="kubernetes",
        )
    )

    topology = store.get_service_topology("cluster-a", "default", "checkout")

    assert topology["service"]["found"] is True
    assert topology["service"]["service_category"] == "clusterip"
    assert topology["service"]["workload_kind"] == "Deployment"
    assert topology["service"]["workload_name"] == "checkout-api"
    assert topology["service"]["app_label"] == "checkout"
    assert topology["freshness"]["stale"] is False


def test_kubernetes_inventory_matches_service_and_deployment_app_label(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inventory = KubernetesInventory(
        cluster_id="cluster-a",
        services=(
            KubernetesService(
                name="checkout",
                namespace="default",
                labels={"app": "checkout"},
                selector={"app": "checkout"},
            ),
        ),
        workloads=(
            KubernetesWorkload(
                kind="Deployment",
                name="checkout-api",
                namespace="default",
                labels={"app": "checkout"},
            ),
        ),
    )

    store.ingest_kubernetes_inventory(inventory)
    topology = store.get_service_topology("cluster-a", "default", "checkout")

    assert topology["service"]["workload_kind"] == "Deployment"
    assert topology["service"]["workload_name"] == "checkout-api"
    assert topology["service"]["identity_warnings"] == []


def test_kubernetes_inventory_matches_recommended_app_label_without_mismatch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inventory = KubernetesInventory(
        cluster_id="cluster-a",
        services=(
            KubernetesService(
                name="checkout",
                namespace="default",
                selector={"app.kubernetes.io/name": "checkout"},
            ),
            KubernetesService(
                name="payments",
                namespace="default",
                selector={"app.kubernetes.io/name": "payments"},
            ),
        ),
        workloads=(
            KubernetesWorkload(
                kind="Deployment",
                name="checkout-api",
                namespace="default",
                labels={"app.kubernetes.io/name": "checkout"},
                env={"PAYMENTS_URL": "http://payments.default.svc.cluster.local"},
            ),
            KubernetesWorkload(
                kind="Deployment",
                name="payments-api",
                namespace="default",
                labels={"app.kubernetes.io/name": "payments"},
            ),
        ),
    )

    store.ingest_kubernetes_inventory(inventory)
    topology = store.get_service_topology("cluster-a", "default", "checkout")

    assert topology["service"]["workload_kind"] == "Deployment"
    assert topology["service"]["identity_warnings"] == []
    assert topology["edges"]["outgoing"][0]["to"]["service"] == "payments"


def test_kubernetes_inventory_warns_for_unmatched_app_label(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inventory = KubernetesInventory(
        cluster_id="cluster-a",
        services=(
            KubernetesService(
                name="checkout",
                namespace="default",
                labels={"app": "checkout"},
                selector={"app": "checkout"},
            ),
        ),
        workloads=(
            KubernetesWorkload(
                kind="Deployment",
                name="payments-api",
                namespace="default",
                labels={"app": "payments"},
            ),
        ),
    )

    store.ingest_kubernetes_inventory(inventory)
    topology = store.get_service_topology("cluster-a", "default", "checkout")

    assert topology["service"]["workload_kind"] is None
    assert "service_app_label_without_matching_workload" in topology["service"]["identity_warnings"]


def test_manual_edge_round_trips_with_confidence_source_and_freshness(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_service(ServiceRecord("cluster-a", "default", "checkout"))
    store.upsert_service(ServiceRecord("cluster-a", "default", "payments"))
    store.add_edge(
        ServiceEdge(
            from_cluster_id="cluster-a",
            from_namespace="default",
            from_service="checkout",
            to_cluster_id="cluster-a",
            to_namespace="default",
            to_service="payments",
            edge_type="depends_on",
            source="manual",
            confidence=0.9,
        )
    )

    topology = store.get_service_topology("cluster-a", "default", "checkout")

    assert topology["edges"]["incoming"] == []
    assert len(topology["edges"]["outgoing"]) == 1
    edge = topology["edges"]["outgoing"][0]
    assert edge["to"]["service"] == "payments"
    assert edge["source"] == "manual"
    assert edge["confidence"] == 0.9
    assert edge["freshness"]["stale"] is False


def test_k8s_config_edge_is_weak_evidence_with_warning(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inventory = KubernetesInventory(
        cluster_id="cluster-a",
        services=(
            KubernetesService("checkout", "default", selector={"app": "checkout"}),
            KubernetesService("payments", "default", selector={"app": "payments"}),
        ),
        workloads=(
            KubernetesWorkload(
                kind="Deployment",
                name="checkout-api",
                namespace="default",
                labels={"app": "checkout"},
                env={"PAYMENTS_URL": "http://payments.default.svc.cluster.local"},
            ),
            KubernetesWorkload(
                kind="Deployment",
                name="payments-api",
                namespace="default",
                labels={"app": "payments"},
            ),
        ),
    )

    store.ingest_kubernetes_inventory(inventory)
    topology = store.get_service_topology("cluster-a", "default", "checkout")

    assert len(topology["edges"]["outgoing"]) == 1
    edge = topology["edges"]["outgoing"][0]
    assert edge["source"] == "k8s_config"
    assert edge["confidence"] == 0.35
    assert "weak_evidence_config_reference" in edge["warnings"]


def test_k8s_config_edge_can_read_config_map_values(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inventory = KubernetesInventory(
        cluster_id="cluster-a",
        services=(
            KubernetesService("checkout", "default", selector={"app": "checkout"}),
            KubernetesService("inventory", "default", selector={"app": "inventory"}),
        ),
        workloads=(
            KubernetesWorkload(
                kind="Deployment",
                name="checkout-api",
                namespace="default",
                labels={"app": "checkout"},
                config_map_refs=("checkout-config",),
            ),
            KubernetesWorkload(
                kind="Deployment",
                name="inventory-api",
                namespace="default",
                labels={"app": "inventory"},
            ),
        ),
        config_maps=(
            KubernetesConfigMap(
                name="checkout-config",
                namespace="default",
                data={"INVENTORY_URL": "http://inventory:8080"},
            ),
        ),
    )

    store.ingest_kubernetes_inventory(inventory)
    topology = store.get_service_topology("cluster-a", "default", "checkout")

    assert topology["edges"]["outgoing"][0]["to"]["service"] == "inventory"
    assert topology["edges"]["outgoing"][0]["source"] == "k8s_config"


def test_k8s_config_explicit_dns_namespace_does_not_match_same_name_service_elsewhere(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inventory = KubernetesInventory(
        cluster_id="cluster-a",
        services=(
            KubernetesService("checkout", "default", selector={"app": "checkout"}),
            KubernetesService("payments", "default", selector={"app": "payments"}),
            KubernetesService("payments", "staging", selector={"app": "payments"}),
        ),
        workloads=(
            KubernetesWorkload(
                kind="Deployment",
                name="checkout-api",
                namespace="default",
                labels={"app": "checkout"},
                env={"PAYMENTS_URL": "http://payments.default.svc.cluster.local"},
            ),
            KubernetesWorkload(
                kind="Deployment",
                name="payments-api",
                namespace="default",
                labels={"app": "payments"},
            ),
            KubernetesWorkload(
                kind="Deployment",
                name="payments-api",
                namespace="staging",
                labels={"app": "payments"},
            ),
        ),
    )

    store.ingest_kubernetes_inventory(inventory)
    topology = store.get_service_topology("cluster-a", "default", "checkout")

    targets = {
        (edge["to"]["namespace"], edge["to"]["service"])
        for edge in topology["edges"]["outgoing"]
    }
    assert targets == {("default", "payments")}


def test_k8s_config_short_name_stays_in_workload_namespace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inventory = KubernetesInventory(
        cluster_id="cluster-a",
        services=(
            KubernetesService("checkout", "staging", selector={"app": "checkout"}),
            KubernetesService("payments", "default", selector={"app": "payments"}),
            KubernetesService("payments", "staging", selector={"app": "payments"}),
        ),
        workloads=(
            KubernetesWorkload(
                kind="Deployment",
                name="checkout-api",
                namespace="staging",
                labels={"app": "checkout"},
                env={"PAYMENTS_URL": "http://payments:8080"},
            ),
            KubernetesWorkload(
                kind="Deployment",
                name="payments-api",
                namespace="default",
                labels={"app": "payments"},
            ),
            KubernetesWorkload(
                kind="Deployment",
                name="payments-api",
                namespace="staging",
                labels={"app": "payments"},
            ),
        ),
    )

    store.ingest_kubernetes_inventory(inventory)
    topology = store.get_service_topology("cluster-a", "staging", "checkout")

    targets = {
        (edge["to"]["namespace"], edge["to"]["service"])
        for edge in topology["edges"]["outgoing"]
    }
    assert targets == {("staging", "payments")}


def test_stale_service_and_edge_are_reported(tmp_path: Path) -> None:
    store = _store(tmp_path, stale_after_seconds=1)
    old_ts = time.time() - 60
    store.upsert_service(ServiceRecord("cluster-a", "default", "checkout", observed_at=old_ts))
    store.add_edge(
        ServiceEdge(
            "cluster-a",
            "default",
            "checkout",
            "cluster-a",
            "default",
            "payments",
            "depends_on",
            "manual",
            0.5,
            observed_at=old_ts,
        )
    )

    topology = store.get_service_topology("cluster-a", "default", "checkout")

    assert topology["freshness"]["stale"] is True
    assert topology["freshness"]["service"]["stale"] is True
    assert topology["edges"]["outgoing"][0]["freshness"]["stale"] is True


def test_new_edge_does_not_hide_stale_service_catalog(tmp_path: Path) -> None:
    store = _store(tmp_path, stale_after_seconds=1)
    old_ts = time.time() - 60
    store.upsert_service(ServiceRecord("cluster-a", "default", "checkout", observed_at=old_ts))
    store.add_edge(
        ServiceEdge(
            "cluster-a",
            "default",
            "checkout",
            "cluster-a",
            "default",
            "payments",
            "depends_on",
            "manual",
            0.5,
        )
    )

    topology = store.get_service_topology("cluster-a", "default", "checkout")

    assert topology["freshness"]["stale"] is True
    assert topology["freshness"]["observed_at"] == pytest.approx(old_ts)
    assert topology["freshness"]["service"]["stale"] is True
    assert topology["freshness"]["edges"]["stale"] is False
    assert topology["edges"]["outgoing"][0]["freshness"]["stale"] is False


def test_get_service_topology_module_function_uses_db_path(tmp_path: Path) -> None:
    db_path = tmp_path / "topology.db"
    store = TopologyStore(db_path)
    store.upsert_service(ServiceRecord("cluster-a", "default", "checkout"))
    store.close()

    topology = get_service_topology("cluster-a", "default", "checkout", db_path=db_path)

    assert topology["ok"] is True
    assert topology["service"]["service_id"] == "cluster-a/default/checkout"


def test_module_get_service_topology_does_not_grow_fd_count(tmp_path: Path) -> None:
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.exists():
        pytest.skip("/proc/self/fd is unavailable")

    db_path = tmp_path / "topology.db"
    with TopologyStore(db_path) as store:
        store.upsert_service(ServiceRecord("cluster-a", "default", "checkout"))

    baseline = len(list(fd_dir.iterdir()))
    for _ in range(200):
        topology = get_service_topology("cluster-a", "default", "checkout", db_path=db_path)
        assert topology["ok"] is True

    assert len(list(fd_dir.iterdir())) <= baseline + 2


def test_missing_service_returns_controlled_envelope(tmp_path: Path) -> None:
    topology = _store(tmp_path).get_service_topology("cluster-a", "default", "missing")

    assert topology["ok"] is True
    assert topology["service"]["found"] is False
    assert "service_not_found" in topology["warnings"]


def test_kubernetes_client_missing_dependency_is_controlled() -> None:
    with pytest.raises(RuntimeError, match="backend_unavailable"):
        KubernetesInventory.from_kubernetes_client("cluster-a")
