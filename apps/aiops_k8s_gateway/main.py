"""AIOps Gateway V1 process entry point."""

from __future__ import annotations

import argparse
import os
import secrets
import time
import uuid
from http import HTTPStatus
from typing import Any
from urllib.parse import urlparse

from aiops.domain.identity import Actor, IdentityConfig, IdentityProvider, SQLiteIdentityStore
from apps.service_http import JsonHandler, connectivity_payload, serve

from . import APP_NAME, mcp_registry_http, skill_registry_http
from . import (
    change_center_http,
    change_request_http,
    chat_http,
    connector_command_http,
    connector_enrollment_http,
    diagnosis_delivery_http,
    incident_http,
    incident_report_http,
    investigation_event_http,
    kubernetes_change_execution_http,
    kubernetes_phase_approval_http,
    model_provider_http,
    notification_admin_http,
    notification_handoff_http,
    notification_requests,
    platform_status_http,
    resource_catalog_http,
    secure_input_http,
)
from .alertmanager_webhook import handle_http_request as handle_alertmanager_request
from .change_plan_phases import ChangePlanPhases, reconcile_change_notifications
from .change_center import ChangeCenter
from .change_requests import ChangeRequests
from .chat_sessions import ChatSessions
from .chat_attachments import ChatAttachments, scan_with_clamav
from .chat_handoffs import ChatHandoffs
from .kubernetes_change_authorities import KubernetesChangeAuthorities
from .kubernetes_change_validation import KubernetesChangeValidation
from .kubernetes_phase_approvals import KubernetesPhaseApprovals
from .kubernetes_change_executions import KubernetesChangeExecutions
from .kubernetes_reconciliation import KubernetesReconciliations
from .connector_commands import ConnectorCommands
from .connector_enrollments import ConnectorEnrollments
from .connector_identity import ConnectorIdentity
from .diagnosis_delivery import DiagnosisDelivery
from .diagnosis_delivery_runtime import start_diagnosis_delivery
from .gateway_audit import GatewayAudit
from .gateway_db import GatewayDatabase
from .gateway_sessions import GatewaySessions
from .identity_administration import IdentityAdministration
from .identity_http import IdentityHTTPAdapter
from .incident_runtime import incident_service, start_incident_reconciler
from .incident_reports import IncidentReports
from .investigation_events import InvestigationEvents
from .mcp_registry import MCPRegistry
from .observability import metrics_body as gateway_metrics_body
from .platform_status import PlatformSetupDecisions, PlatformStatus
from .platform_status_observability import read_status as read_observability_status
from .resource_catalog import ResourceCatalog
from .secure_inputs import SecureInputs
from .skill_registry import SkillRegistry
_DATABASE = GatewayDatabase()
_CONNECTOR_ENROLLMENTS = ConnectorEnrollments(_DATABASE)
_CONNECTOR_COMMANDS = ConnectorCommands(
    _DATABASE,
    available_connector_in=_CONNECTOR_ENROLLMENTS.require_available_connector_in,
    lease_identity_matches_in=_CONNECTOR_ENROLLMENTS.lease_identity_matches_in,
)


def _login_identity(username: str, password: str) -> Actor:
    provider = IdentityProvider(IdentityConfig.load())
    try:
        return provider.login(username, password)
    finally:
        provider.store.close()


def _active_actor(username: str) -> Actor | None:
    store = SQLiteIdentityStore(IdentityConfig.load().store_path)
    try:
        return store.get_actor(username)
    finally:
        store.close()


def _gateway_sessions() -> GatewaySessions:
    return GatewaySessions(
        _DATABASE,
        clock=time.time,
        token_factory=lambda: secrets.token_urlsafe(32),
        active_actor_lookup=_active_actor,
    )


def _gateway_audit() -> GatewayAudit:
    return GatewayAudit(_DATABASE, clock=time.time)


def _identity_administration() -> IdentityAdministration:
    return IdentityAdministration(
        _DATABASE,
        audit_insert=_gateway_audit().insert_in,
        revoke_actor_in=GatewaySessions.revoke_actor_in,
        clock=time.time,
        id_factory=lambda prefix: f"{prefix}-{uuid.uuid4().hex}",
    )


def _identity_http() -> IdentityHTTPAdapter:
    return IdentityHTTPAdapter(
        _gateway_sessions(),
        _identity_administration(),
        _gateway_audit(),
        _login_identity,
        _request_id,
        _error_payload,
    )


def _incident_service():
    return incident_service(_DATABASE)


def _change_requests(validation: KubernetesChangeValidation | None = None) -> ChangeRequests:
    return ChangeRequests(_DATABASE, validation=validation or _kubernetes_change_validation())


def _secure_inputs() -> SecureInputs:
    return SecureInputs(
        _DATABASE,
        key_path=os.getenv("AIOPS_CHANGE_ENCRYPTION_KEY_PATH", "/var/run/secrets/aiops-change/key"),
    )


def _kubernetes_change_validation() -> KubernetesChangeValidation:
    return KubernetesChangeValidation(
        commands=_CONNECTOR_COMMANDS, enrollments=_CONNECTOR_ENROLLMENTS,
        secure_inputs=_secure_inputs(),
        availability_recorder=ChangePlanPhases().record_secure_input_unavailable_in,
    )


def _kubernetes_phase_approvals(
    *,
    authorities: KubernetesChangeAuthorities | None = None,
    validation: KubernetesChangeValidation | None = None,
    catalog: ResourceCatalog | None = None,
) -> KubernetesPhaseApprovals:
    validation = validation or _kubernetes_change_validation()
    catalog = catalog or ResourceCatalog(_DATABASE)
    return KubernetesPhaseApprovals(
        _DATABASE,
        enrollments=_CONNECTOR_ENROLLMENTS,
        authorities=authorities or _kubernetes_change_authorities(catalog=catalog),
        phases=ChangePlanPhases(),
        validation=validation,
    )


def _kubernetes_change_authorities(
    *, catalog: ResourceCatalog | None = None,
) -> KubernetesChangeAuthorities:
    return KubernetesChangeAuthorities(
        _DATABASE, user_active_in=IdentityAdministration.user_active_in,
        enrollments=_CONNECTOR_ENROLLMENTS,
        catalog=catalog or ResourceCatalog(_DATABASE),
    )


def _kubernetes_change_executions(
    approvals: KubernetesPhaseApprovals | None = None,
    reconciliations: KubernetesReconciliations | None = None,
) -> KubernetesChangeExecutions:
    resolved_approvals = approvals or _kubernetes_phase_approvals()
    return KubernetesChangeExecutions(
        _DATABASE,
        approvals=resolved_approvals,
        enrollments=_CONNECTOR_ENROLLMENTS,
        commands=_CONNECTOR_COMMANDS,
        secure_inputs=_secure_inputs(),
        reconciliations=reconciliations,
    )


def _kubernetes_reconciliations(
    approvals: KubernetesPhaseApprovals,
) -> KubernetesReconciliations:
    return KubernetesReconciliations(
        _DATABASE, approvals=approvals, commands=_CONNECTOR_COMMANDS,
        secure_inputs=_secure_inputs(),
    )


def _chat_http() -> chat_http.ChatHTTPAdapter:
    attachments = ChatAttachments(_DATABASE, scanner=scan_with_clamav)
    identity = _identity_http()
    sessions = _gateway_sessions()
    return chat_http.ChatHTTPAdapter(
        chats=ChatSessions(_DATABASE, attachment_source=attachments), attachments=attachments, handoffs=ChatHandoffs(_DATABASE),
        mcp_registry=MCPRegistry(_DATABASE), skill_registry=SkillRegistry(_DATABASE), actor_view=sessions.actor_view, catalog=ResourceCatalog(_DATABASE), incidents=_incident_service(),
        connector_status=_CONNECTOR_ENROLLMENTS.public_status, request_session=identity.request_session, csrf_valid=identity.csrf_valid, request_id_for=_request_id, error_payload=_error_payload, model_provider_status=model_provider_http.read_status,
    )


def _request_http_adapters() -> tuple[Any, ...]:
    identity = _identity_http()
    sessions = _gateway_sessions()
    audit = _gateway_audit()
    catalog = ResourceCatalog(_DATABASE)
    incidents = _incident_service()
    validation = _kubernetes_change_validation()
    changes = _change_requests(validation)
    authorities = _kubernetes_change_authorities(catalog=catalog)
    approvals = _kubernetes_phase_approvals(
        authorities=authorities, validation=validation, catalog=catalog,
    )
    reconciliations = _kubernetes_reconciliations(approvals)
    executions = _kubernetes_change_executions(approvals, reconciliations)
    platform_status = PlatformStatus(
        PlatformSetupDecisions(_DATABASE),
        model_status=model_provider_http.read_status,
        notification_status=notification_admin_http.read_status,
        connector_status=_CONNECTOR_ENROLLMENTS.admin_state,
        observability_status=read_observability_status,
    )
    return (
        identity,
        connector_enrollment_http.ConnectorEnrollmentHTTPAdapter(
            _DATABASE,
            _CONNECTOR_ENROLLMENTS,
            _CONNECTOR_COMMANDS,
            identity.authorize_admin,
            identity.require_fresh_auth,
            identity.request_session,
            audit.denial,
            audit.record,
            _request_id,
            _extract_bearer_token,
            _error_payload,
        ),
        _chat_http(),
        skill_registry_http.SkillRegistryHTTPAdapter(SkillRegistry(_DATABASE), MCPRegistry(_DATABASE), identity.authorize_admin, identity.require_fresh_auth, _request_id, _error_payload),
        mcp_registry_http.MCPRegistryHTTPAdapter(MCPRegistry(_DATABASE), identity.authorize_admin, identity.require_fresh_auth, _request_id, _error_payload),
        model_provider_http.ModelProviderHTTPAdapter(audit.record, identity.authorize_admin, identity.require_fresh_auth, identity.request_session, _request_id, _error_payload),
        notification_admin_http.NotificationAdminHTTPAdapter(audit.unresolved_request, audit.record, identity.authorize_admin, identity.require_fresh_auth, identity.request_session, _request_id, _error_payload, PlatformSetupDecisions(_DATABASE)),
        platform_status_http.PlatformStatusHTTPAdapter(platform_status, identity.authorize_admin, identity.require_fresh_auth, identity.request_session, _request_id, _error_payload),
        secure_input_http.SecureInputHTTPAdapter(sessions.actor_view, _secure_inputs(), identity.request_session, identity.csrf_valid, _request_id, _error_payload),
        resource_catalog_http.ResourceCatalogHTTPAdapter(sessions.actor_view, audit.record, catalog, ConnectorIdentity(_DATABASE), identity.request_session, incidents.team_ids_for_actor, _CONNECTOR_ENROLLMENTS.public_status, identity.authorize_admin, identity.require_fresh_auth, _request_id, _extract_bearer_token, _error_payload),
        kubernetes_phase_approval_http.KubernetesPhaseApprovalHTTPAdapter(sessions.is_fresh, audit.record, changes, authorities, approvals, identity.authorize_admin, identity.require_fresh_auth, identity.request_session, identity.csrf_valid, _request_id, _error_payload),
        kubernetes_change_execution_http.KubernetesChangeExecutionHTTPAdapter(sessions.is_fresh, changes, approvals, executions, reconciliations, identity.request_session, identity.csrf_valid, _request_id, _error_payload),
        change_request_http.ChangeRequestHTTPAdapter(sessions.actor_view, incidents, changes, authorities, approvals, identity.request_session, identity.csrf_valid, _request_id, _error_payload),
        change_center_http.ChangeCenterHTTPAdapter(sessions.actor_view, incidents, ChangeCenter(changes), approvals, identity.request_session, _request_id, _error_payload),
        incident_http.IncidentHTTPAdapter(sessions.actor_view, incidents, changes, approvals, identity.request_session, _request_id, _error_payload),
        incident_report_http.IncidentReportHTTPAdapter(sessions.actor_view, IncidentReports(_DATABASE), incidents, identity.request_session, identity.csrf_valid, _request_id, _error_payload),
    )


def _request_id(handler: JsonHandler) -> str:
    return handler.request_id()


def _error_payload(code: str, message: str, request_id: str) -> dict[str, Any]:
    return {
        "service": APP_NAME,
        "status": "failed",
        "request_id": request_id,
        "error": {"code": code, "message": message},
    }


def _extract_bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


class GatewayHandler(JsonHandler):
    """Gateway operational and V1 HTTP surface."""

    service_name = APP_NAME

    def _dispatch(self, route_path: str) -> bool:
        return any(adapter.dispatch(self, route_path) for adapter in _request_http_adapters())

    def do_GET(self) -> None:  # noqa: N802
        route_path = urlparse(self.path).path
        if self.is_metrics_request():
            self.write_metrics_body(gateway_metrics_body(_DATABASE, handler_type=type(self)))
            return
        if self._dispatch(route_path):
            return
        identity = _identity_http()
        if investigation_event_http.dispatch_get(self, route_path, _gateway_sessions().actor_view, _incident_service(), InvestigationEvents(_DATABASE), identity.request_session, _request_id, _error_payload):
            return
        if route_path == "/healthz":
            self.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok"})
            return
        if route_path == "/readyz":
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "registered_connectors": _CONNECTOR_ENROLLMENTS.online_verified_count(),
                },
            )
            return
        if route_path == "/connectivity/connector":
            connector_url = os.getenv("AIOPS_CONNECTOR_URL", "")
            if not connector_url:
                self.write_json(HTTPStatus.SERVICE_UNAVAILABLE, {"service": APP_NAME, "status": "unavailable", "peer": "connector", "error": "AIOPS_CONNECTOR_URL is not set"})
            else:
                self.write_json(*connectivity_payload(service=APP_NAME, peer_name="connector", peer_url=connector_url))
            return
        self.write_not_found()

    def do_POST(self) -> None:  # noqa: N802
        route_path = urlparse(self.path).path
        if self._dispatch(route_path):
            return
        identity = _identity_http()
        if connector_command_http.dispatch(
            self, route_path, _CONNECTOR_COMMANDS, ConnectorIdentity(_DATABASE),
            identity.authorize_admin, _request_id, _extract_bearer_token, _error_payload,
            _record_connector_command_result,
            _kubernetes_change_executions(),
        ):
            return
        if diagnosis_delivery_http.dispatch(self, route_path, DiagnosisDelivery(_DATABASE)):
            return
        if investigation_event_http.dispatch_post(self, route_path, _gateway_sessions().actor_view, _incident_service(), InvestigationEvents(_DATABASE), identity.request_session, identity.csrf_valid, _request_id, _error_payload):
            return
        if route_path == "/webhooks/alertmanager":
            length = int(self.headers.get("Content-Length", "0") or "0")
            status, payload = handle_alertmanager_request(self.rfile.read(length) if length else b"", dict(self.headers), _incident_service())
            self.write_json(status, payload)
            return
        self.write_not_found()

    def do_PATCH(self) -> None:  # noqa: N802
        route_path = urlparse(self.path).path
        if self._dispatch(route_path):
            return
        self.write_not_found()

    def do_PUT(self) -> None:  # noqa: N802
        if not self._dispatch(urlparse(self.path).path):
            self.write_not_found()

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._dispatch(urlparse(self.path).path):
            self.write_not_found()


def _record_connector_command_result(
    conn: Any,
    command_id: str,
    result: dict[str, object],
    now: float,
) -> None:
    _CONNECTOR_ENROLLMENTS.record_verification_result_in(conn, command_id, result, now)
    _change_requests().record_validation_result_in(conn, command_id, result, now)
    _kubernetes_change_executions().record_result_in(conn, command_id, result, now)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIOps Gateway service")
    parser.add_argument("--host", default=os.getenv("AIOPS_GATEWAY_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AIOPS_GATEWAY_PORT", "8080")))
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    start_incident_reconciler(_incident_service(), connector_commands=_CONNECTOR_COMMANDS)
    start_diagnosis_delivery(DiagnosisDelivery(_DATABASE, capability_snapshot=MCPRegistry(_DATABASE).authorized_snapshot, skill_bindings=SkillRegistry(_DATABASE).authorized_bindings))
    notification_requests.start_notification_handoff(
        notification_requests.NotificationOutbox(_DATABASE),
        sender=notification_handoff_http.send_notification_request,
        change_reconciler=lambda: reconcile_change_notifications(_DATABASE),
    )
    serve(GatewayHandler, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
