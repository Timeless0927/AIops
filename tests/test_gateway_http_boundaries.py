"""Gateway HTTP adapter boundary checks."""

import inspect
from pathlib import Path

from apps.aiops_k8s_gateway import (
    change_center_http,
    change_request_http,
    chat_http,
    incident_http,
    incident_report_http,
    identity_http,
    kubernetes_change_execution_http,
    kubernetes_phase_approval_http,
    mcp_registry_http,
    model_provider_http,
    notification_admin_http,
    notification_requests,
    platform_status_http,
    resource_catalog_http,
    secure_input_http,
    skill_registry_http,
)
from apps.aiops_k8s_gateway import main as gateway_main
from apps.aiops_k8s_gateway.v1_store import GatewayV1Store


def test_chat_adapter_exposes_a_narrow_dispatch_seam() -> None:
    assert tuple(inspect.signature(chat_http.ChatHTTPAdapter.dispatch).parameters) == (
        "self",
        "handler",
        "route_path",
    )
    assert tuple(inspect.signature(gateway_main._chat_http).parameters) == ()
    dependencies = set(inspect.signature(chat_http.ChatHTTPAdapter).parameters)
    assert "sessions" not in dependencies
    assert {"actor_view", "connector_status"} <= dependencies
    assert not Path(chat_http.__file__).with_name("gateway_http_runtime.py").exists()


def test_remaining_gateway_adapters_expose_narrow_dispatch_seams() -> None:
    adapters = (
        (skill_registry_http, "SkillRegistryHTTPAdapter"),
        (mcp_registry_http, "MCPRegistryHTTPAdapter"),
        (model_provider_http, "ModelProviderHTTPAdapter"),
        (notification_admin_http, "NotificationAdminHTTPAdapter"),
        (platform_status_http, "PlatformStatusHTTPAdapter"),
        (secure_input_http, "SecureInputHTTPAdapter"),
        (resource_catalog_http, "ResourceCatalogHTTPAdapter"),
        (kubernetes_phase_approval_http, "KubernetesPhaseApprovalHTTPAdapter"),
        (kubernetes_change_execution_http, "KubernetesChangeExecutionHTTPAdapter"),
        (change_request_http, "ChangeRequestHTTPAdapter"),
        (change_center_http, "ChangeCenterHTTPAdapter"),
        (incident_http, "IncidentHTTPAdapter"),
        (incident_report_http, "IncidentReportHTTPAdapter"),
    )
    for module, class_name in adapters:
        adapter_type = getattr(module, class_name)
        assert tuple(inspect.signature(adapter_type.dispatch).parameters) == (
            "self",
            "handler",
            "route_path",
        )
        assert "sessions" not in inspect.signature(adapter_type).parameters
        assert not hasattr(module, "dispatch")

    assert tuple(inspect.signature(gateway_main._request_http_adapters).parameters) == ()


def test_identity_http_adapter_owns_auth_admin_and_audit_dispatch() -> None:
    assert tuple(inspect.signature(identity_http.IdentityHTTPAdapter.dispatch).parameters) == (
        "self",
        "handler",
        "route_path",
    )
    assert tuple(inspect.signature(gateway_main._identity_http).parameters) == ()
    assert not {
        "issue",
        "lookup",
        "is_fresh",
        "mark_fresh",
        "revoke",
        "actor_view",
        "mutate_admin",
        "record_admin_audit",
        "list_admin_audit",
        "unresolved_admin_request",
    } & set(dir(GatewayV1Store))


def test_notification_change_reconciliation_is_composed_at_the_gateway_boundary() -> None:
    handoff_source = inspect.getsource(notification_requests.start_notification_handoff)
    assert "change_plan_phases" not in handoff_source
    assert "outbox._database" not in handoff_source
    assert (
        inspect.signature(notification_requests.start_notification_handoff)
        .parameters["change_reconciler"]
        .default
        is inspect.Parameter.empty
    )
