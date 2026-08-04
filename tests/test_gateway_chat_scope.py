from __future__ import annotations

import pytest

from apps.aiops_k8s_gateway.chat_scope import ChatScopeError, freeze_chat_scope


def _workspace() -> dict[str, object]:
    return {
        "services": [
            {"id": "service-checkout", "name": "checkout", "team_id": "team-a"},
            {"id": "service-payment", "name": "payment", "team_id": "team-a"},
        ],
        "resources": [
            {
                "id": "target-checkout",
                "cluster_id": "cluster-prod",
                "namespace": "shop",
                "kind": "Deployment",
                "name": "checkout-api",
                "service_id": "service-checkout",
                "team_id": "team-a",
                "binding_state": "bound",
                "availability": "available",
            },
            {
                "id": "target-payment",
                "cluster_id": "cluster-prod",
                "namespace": "shop",
                "kind": "Deployment",
                "name": "payment-api",
                "service_id": "service-payment",
                "team_id": "team-a",
                "binding_state": "bound",
                "availability": "unavailable",
            },
        ],
    }


def test_freezes_only_actor_visible_resources_and_supports_scope_narrowing() -> None:
    cluster = freeze_chat_scope({"cluster_id": "cluster-prod"}, workspace=_workspace())
    assert cluster["selection"] == {"cluster_id": "cluster-prod"}
    assert [item["deployment_target_id"] for item in cluster["resources"]] == [
        "target-checkout",
        "target-payment",
    ]
    assert cluster["time_range"] == {"type": "relative", "value": "30m"}
    assert len(cluster["revision"]) == 64

    target = freeze_chat_scope(
        {"cluster_id": "cluster-prod", "deployment_target_id": "target-checkout"},
        workspace=_workspace(),
    )
    assert target["resources"] == [
        {
            "deployment_target_id": "target-checkout",
            "cluster_id": "cluster-prod",
            "namespace": "shop",
            "service_id": "service-checkout",
            "service_name": "checkout",
            "workload_kind": "Deployment",
            "workload_name": "checkout-api",
        }
    ]


def test_unknown_and_unauthorized_scope_have_the_same_non_enumerating_result() -> None:
    for selection in (
        {"cluster_id": "cluster-secret"},
        {"cluster_id": "cluster-prod", "deployment_target_id": "target-secret"},
    ):
        with pytest.raises(ChatScopeError) as exc_info:
            freeze_chat_scope(selection, workspace=_workspace())
        assert exc_info.value.code == "chat_scope_not_found"


def test_incident_scope_is_frozen_from_the_authorized_workbench_projection() -> None:
    scope = freeze_chat_scope(
        {"incident_id": "incident-1"},
        workspace={"services": [], "resources": []},
        incident={
            "incident": {"id": "incident-1"},
            "resource_context": {
                "cluster_id": "cluster-prod",
                "namespace": "shop",
                "deployment_target_id": "target-checkout",
                "service_id": "service-checkout",
                "service_name": "checkout",
                "workload_kind": "Deployment",
                "workload_name": "checkout-api",
                "resource_binding_id": "binding-1",
            },
        },
    )
    assert scope["selection"] == {"incident_id": "incident-1"}
    assert scope["resources"][0]["deployment_target_id"] == "target-checkout"
