"""Resource Catalog module behavior."""

from __future__ import annotations

import json
import itertools
import sqlite3
from pathlib import Path

import pytest

from apps.aiops_k8s_gateway.resource_catalog import DiscoveryObservation, ResourceCatalog
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def _gateway_state(db_path: Path) -> tuple[GatewayV1Store, str, str]:
    store = GatewayV1Store(
        db_path,
        credential_factory=lambda: "connector-secret",
        id_factory=lambda prefix: f"{prefix}-fixed",
    )
    _, credential = store.connector_enrollments.create(
        connector_id="connector-prod",
        cluster_id="cluster-prod",
        actor_id="admin-1",
        reason="接入生产集群",
        request_id="req-enroll",
    )
    store.connector_enrollments.register(
        credential,
        "connector-prod",
        "cluster-prod",
        request_id="req-register",
    )
    _, team = store.mutate_admin(
        collection="teams",
        target_id=None,
        payload={"name": "Payments", "description": "支付责任团队"},
        actor_id="admin-1",
        reason="建立责任团队",
        action="teams_create",
        request_id="req-team",
    )
    return store, credential, str(team["id"])


def test_confirmed_binding_survives_discovery_hints_and_can_be_corrected(tmp_path: Path) -> None:
    store, _, payments_team_id = _gateway_state(tmp_path / "gateway.db")
    ids = itertools.count(1)
    catalog = ResourceCatalog(tmp_path / "gateway.db", id_factory=lambda prefix: f"{prefix}-{next(ids)}")

    [candidate] = catalog.refresh_discovery(
        "cluster-prod",
        [
            DiscoveryObservation(
                namespace="payments",
                workload_kind="Deployment",
                workload_name="checkout-api",
                service_name="checkout",
                service_hint="checkout-label",
                team_hint="payments-label",
            )
        ],
    )
    assert candidate["binding_status"] == "unbound"
    assert catalog.resolve_binding("cluster-prod", "payments", "Deployment", "checkout-api") is None
    unbound = catalog.execution_target_error(
        {"cluster": "cluster-prod", "namespace": "payments", "deployment": "checkout-api", "service": "Checkout", "team": "Payments"}
    )
    assert unbound is not None and unbound.code == "resource_unbound"

    payments_service = catalog.create_service(
        team_id=payments_team_id,
        name="Checkout",
        description="结账服务",
        actor_id="admin-1",
        reason="登记服务",
        request_id="req-service-1",
    )
    first = catalog.confirm_binding(
        candidate_id=str(candidate["id"]),
        service_id=str(payments_service["id"]),
        actor_id="admin-1",
        reason="确认生产工作负载归属",
        request_id="req-bind-1",
    )
    catalog.refresh_discovery(
        "cluster-prod",
        [
            DiscoveryObservation(
                namespace="payments",
                workload_kind="Deployment",
                workload_name="checkout-api",
                service_name="renamed-k8s-service",
                service_hint="wrong-service",
                team_hint="wrong-team",
            ),
            DiscoveryObservation(
                namespace="payments",
                workload_kind="Deployment",
                workload_name="checkout-worker",
                service_hint="checkout",
            ),
            DiscoveryObservation(
                namespace="payments",
                workload_kind="Deployment",
                workload_name="checkout-cron",
                service_hint="checkout",
            ),
        ],
    )
    resolved = catalog.resolve_binding("cluster-prod", "payments", "Deployment", "checkout-api")
    assert resolved is not None
    assert resolved["id"] == first["id"]
    assert resolved["service_id"] == payments_service["id"]
    assert resolved["team_id"] == payments_team_id
    assert resolved["revision"] == 1
    assert catalog.execution_target_error(
        {"cluster": "cluster-prod", "namespace": "payments", "deployment": "checkout-api", "service": "Checkout", "team": "Payments"}
    ) is None
    mismatch = catalog.execution_target_error(
        {"cluster": "cluster-prod", "namespace": "payments", "deployment": "checkout-api", "service": "wrong", "team": "Payments"}
    )
    assert mismatch is not None and mismatch.code == "resource_binding_mismatch"
    worker = next(row for row in catalog.list_state()["discovery_candidates"] if row["workload_name"] == "checkout-worker")
    catalog.confirm_binding(
        candidate_id=str(worker["id"]),
        service_id=str(payments_service["id"]),
        actor_id="admin-1",
        reason="同一 Service 的第二个部署目标",
        request_id="req-bind-worker",
    )
    assert sum(
        binding["service_id"] == payments_service["id"]
        for binding in catalog.list_state()["resource_bindings"]
    ) == 2

    _, platform_team = store.mutate_admin(
        collection="teams",
        target_id=None,
        payload={"name": "Platform", "description": "平台责任团队"},
        actor_id="admin-1",
        reason="建立平台团队",
        action="teams_create",
        request_id="req-team-2",
    )
    platform_service = catalog.create_service(
        team_id=str(platform_team["id"]),
        name="Checkout Platform",
        description="纠正后的服务归属",
        actor_id="admin-1",
        reason="登记纠正目标",
        request_id="req-service-2",
    )
    corrected = catalog.correct_binding(
        binding_id=str(first["id"]),
        service_id=str(platform_service["id"]),
        actor_id="admin-1",
        reason="纠正责任团队",
        request_id="req-bind-2",
    )
    state = catalog.list_state()

    assert corrected["team_id"] == platform_team["id"]
    assert corrected["revision"] == 2
    assert len(state["deployment_targets"]) == 2
    assert sum(binding["service_id"] == payments_service["id"] for binding in state["resource_bindings"]) == 1
    assert next(row for row in state["discovery_candidates"] if row["workload_name"] == "checkout-cron")["binding_status"] == "unbound"
    audit = store.list_admin_audit()
    correction = next(row for row in audit if row["request_id"] == "req-bind-2")
    assert correction["action"] == "resource-bindings_update"
    assert json.loads(str(correction["before_json"]))["service_id"] == payments_service["id"]
    assert json.loads(str(correction["after_json"]))["service_id"] == platform_service["id"]


def test_binding_rolls_back_when_audit_cannot_commit(tmp_path: Path) -> None:
    store, _, team_id = _gateway_state(tmp_path / "gateway.db")
    catalog = ResourceCatalog(tmp_path / "gateway.db")
    [candidate] = catalog.refresh_discovery(
        "cluster-prod",
        [DiscoveryObservation(namespace="payments", workload_kind="Deployment", workload_name="checkout-api")],
    )
    service = catalog.create_service(
        team_id=team_id,
        name="Checkout",
        description="",
        actor_id="admin-1",
        reason="登记服务",
        request_id="req-service",
    )
    with sqlite3.connect(tmp_path / "gateway.db") as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_catalog_audit BEFORE INSERT ON admin_audit
            BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END
            """
        )
    with pytest.raises(sqlite3.IntegrityError):
        catalog.confirm_binding(
            candidate_id=str(candidate["id"]),
            service_id=str(service["id"]),
            actor_id="admin-1",
            reason="验证原子审计",
            request_id="req-bind",
        )
    with sqlite3.connect(tmp_path / "gateway.db") as conn:
        conn.execute("DROP TRIGGER reject_catalog_audit")
    state = catalog.list_state()
    assert state["deployment_targets"] == []
    assert state["resource_bindings"] == []
    assert state["discovery_candidates"][0]["binding_status"] == "unbound"


def test_actor_workspace_is_safe_scoped_and_projects_runtime_states(tmp_path: Path) -> None:
    store, _, team_id = _gateway_state(tmp_path / "gateway.db")
    ids = itertools.count(1)
    catalog = ResourceCatalog(tmp_path / "gateway.db", id_factory=lambda prefix: f"{prefix}-{next(ids)}")
    candidates = catalog.refresh_discovery("cluster-prod", [
        DiscoveryObservation(namespace="payments", workload_kind="Deployment", workload_name="checkout-api"),
        DiscoveryObservation(namespace="payments", workload_kind="Deployment", workload_name="unbound-worker"),
    ])
    service = catalog.create_service(
        team_id=team_id, name="Checkout", description="admin-only description",
        actor_id="admin-1", reason="test", request_id="req-service",
    )
    idle_service = catalog.create_service(
        team_id=team_id, name="Payments Worker", description="",
        actor_id="admin-1", reason="test", request_id="req-idle-service",
    )
    idle_enrollments = GatewayV1Store(
        tmp_path / "gateway.db", credential_factory=lambda: "idle-secret",
    ).connector_enrollments
    _, idle_credential = idle_enrollments.create(
        connector_id="connector-idle", cluster_id="cluster-idle", actor_id="admin-1",
        reason="test", request_id="req-idle-enrollment",
    )
    idle_enrollments.register(
        idle_credential, "connector-idle", "cluster-idle", request_id="req-idle-register",
    )
    catalog.confirm_binding(
        candidate_id=str(candidates[0]["id"]), service_id=str(service["id"]),
        actor_id="admin-1", reason="test", request_id="req-binding",
    )
    status = [{
        "connector_id": "connector-prod", "cluster_id": "cluster-prod",
        "state": "offline", "read_verification": "verified",
    }, {
        "connector_id": "connector-idle", "cluster_id": "cluster-idle",
        "state": "online", "read_verification": "unverified",
    }]

    scoped = catalog.list_for_actor(team_ids={team_id}, connector_status=status)
    assert [item["name"] for item in scoped["resources"]] == ["checkout-api", "unbound-worker"]
    checkout = scoped["resources"][0]
    assert checkout["availability"] == "unavailable"
    assert checkout["binding_state"] == "bound"
    assert scoped["services"] == [{
        "id": service["id"], "team_id": team_id, "team_name": "Payments",
        "name": "Checkout", "active": True,
    }, {
        "id": idle_service["id"], "team_id": team_id, "team_name": "Payments",
        "name": "Payments Worker", "active": True,
    }]
    assert "description" not in str(scoped)

    admin = catalog.list_for_actor(team_ids=None, connector_status=status)
    assert {item["binding_state"] for item in admin["resources"]} == {"bound", "unbound"}
    prod = next(cluster for cluster in admin["clusters"] if cluster["id"] == "cluster-prod")
    assert prod["runtime_status"] == "offline"
    assert prod["read_verification"] == "verified"
    assert {cluster["id"] for cluster in admin["clusters"]} == {"cluster-prod", "cluster-idle"}

    catalog.refresh_discovery("cluster-prod", [
        DiscoveryObservation(
            namespace="payments", workload_kind="Deployment", workload_name="unbound-worker",
        ),
    ])
    deleted = catalog.list_for_actor(team_ids={team_id}, connector_status=status)
    assert next(item for item in deleted["resources"] if item["name"] == "checkout-api")["availability"] == "unavailable"
