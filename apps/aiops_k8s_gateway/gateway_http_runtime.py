"""Gateway V1 HTTP route composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import (
    change_center_http,
    change_request_http,
    chat_http,
    incident_http,
    incident_report_http,
    kubernetes_change_execution_http,
    kubernetes_phase_approval_http,
    mcp_registry_http,
    model_provider_http,
    notification_admin_http,
    platform_status_http,
    resource_catalog_http,
    secure_input_http,
    skill_registry_http,
)
from .change_center import ChangeCenter
from .chat_attachments import ChatAttachments
from .chat_handoffs import ChatHandoffs
from .chat_sessions import ChatSessions
from .connector_identity import ConnectorIdentity
from .mcp_registry import MCPRegistry
from .platform_status import PlatformSetupDecisions
from .resource_catalog import ResourceCatalog
from .skill_registry import SkillRegistry
from .v1_store import GatewayV1Store


@dataclass(frozen=True)
class GatewayHTTPHelpers:
    authorize_admin: Callable[..., Any]
    require_fresh_auth: Callable[..., bool]
    request_session: Callable[[Any], tuple[Any, str | None]]
    csrf_valid: Callable[[Any, str], bool]
    request_id: Callable[[Any], str]
    extract_bearer_token: Callable[[str | None], str | None]
    error_payload: Callable[[str, str, str], dict[str, object]]


@dataclass(frozen=True)
class GatewayModuleFactories:
    incident_service: Callable[[], Any]
    change_requests: Callable[[Any], Any]
    chat_attachments: Callable[[Any], ChatAttachments]
    secure_inputs: Callable[[], Any]
    kubernetes_change_validation: Callable[[], Any]
    kubernetes_change_authorities: Callable[..., Any]
    kubernetes_phase_approvals: Callable[..., Any]
    kubernetes_reconciliations: Callable[[Any], Any]
    kubernetes_change_executions: Callable[[Any, Any], Any]


@dataclass(frozen=True)
class GatewayRouteDispatcher:
    sessions: GatewayV1Store
    helpers: GatewayHTTPHelpers
    factories: GatewayModuleFactories

    def dispatch(self, handler: Any, route_path: str) -> bool:
        sessions = self.sessions
        helpers = self.helpers
        factories = self.factories
        catalog = ResourceCatalog(sessions.database)
        identity = ConnectorIdentity(sessions.database)
        incidents = factories.incident_service()
        validation = factories.kubernetes_change_validation()
        changes = factories.change_requests(validation)
        authorities = factories.kubernetes_change_authorities(catalog=catalog)
        phase_approvals = factories.kubernetes_phase_approvals(
            authorities=authorities,
            validation=validation,
            catalog=catalog,
        )
        reconciliations = factories.kubernetes_reconciliations(phase_approvals)
        executions = factories.kubernetes_change_executions(phase_approvals, reconciliations)
        attachments = factories.chat_attachments(sessions.database)
        chats = ChatSessions(sessions.database, attachment_source=attachments)
        return (
            chat_http.ChatHTTPAdapter(
                chats=chats,
                attachments=attachments,
                handoffs=ChatHandoffs(sessions.database),
                mcp_registry=MCPRegistry(sessions.database),
                skill_registry=SkillRegistry(sessions.database),
                sessions=sessions,
                catalog=catalog,
                incidents=incidents,
                connector_status=sessions.connector_enrollments.public_status,
                request_session=helpers.request_session,
                csrf_valid=helpers.csrf_valid,
                request_id_for=helpers.request_id,
                error_payload=helpers.error_payload,
                model_provider_status=model_provider_http.read_status,
            ).dispatch(handler, route_path)
            or skill_registry_http.dispatch(
                handler,
                route_path,
                SkillRegistry(sessions.database),
                MCPRegistry(sessions.database),
                helpers.authorize_admin,
                helpers.require_fresh_auth,
                helpers.request_id,
                helpers.error_payload,
            )
            or mcp_registry_http.dispatch(
                handler,
                route_path,
                MCPRegistry(sessions.database),
                helpers.authorize_admin,
                helpers.require_fresh_auth,
                helpers.request_id,
                helpers.error_payload,
            )
            or model_provider_http.dispatch(
                handler,
                route_path,
                sessions,
                helpers.authorize_admin,
                helpers.require_fresh_auth,
                helpers.request_session,
                helpers.request_id,
                helpers.error_payload,
            )
            or notification_admin_http.dispatch(
                handler,
                route_path,
                sessions,
                helpers.authorize_admin,
                helpers.require_fresh_auth,
                helpers.request_session,
                helpers.request_id,
                helpers.error_payload,
                PlatformSetupDecisions(sessions.database),
            )
            or platform_status_http.dispatch(
                handler,
                route_path,
                sessions,
                sessions.connector_enrollments,
                helpers.authorize_admin,
                helpers.require_fresh_auth,
                helpers.request_session,
                helpers.request_id,
                helpers.error_payload,
            )
            or secure_input_http.dispatch(
                handler,
                route_path,
                sessions,
                factories.secure_inputs(),
                helpers.request_session,
                helpers.csrf_valid,
                helpers.request_id,
                helpers.error_payload,
            )
            or resource_catalog_http.dispatch(
                handler,
                route_path,
                sessions,
                catalog,
                identity,
                helpers.request_session,
                incidents.team_ids_for_actor,
                sessions.connector_enrollments.public_status,
                helpers.authorize_admin,
                helpers.require_fresh_auth,
                helpers.request_id,
                helpers.extract_bearer_token,
                helpers.error_payload,
            )
            or kubernetes_phase_approval_http.dispatch(
                handler,
                route_path,
                sessions,
                changes,
                authorities,
                phase_approvals,
                helpers.authorize_admin,
                helpers.require_fresh_auth,
                helpers.request_session,
                helpers.csrf_valid,
                helpers.request_id,
                helpers.error_payload,
            )
            or kubernetes_change_execution_http.dispatch(
                handler,
                route_path,
                sessions,
                changes,
                phase_approvals,
                executions,
                reconciliations,
                helpers.request_session,
                helpers.csrf_valid,
                helpers.request_id,
                helpers.error_payload,
            )
            or change_request_http.dispatch(
                handler,
                route_path,
                sessions,
                incidents,
                changes,
                authorities,
                phase_approvals,
                helpers.request_session,
                helpers.csrf_valid,
                helpers.request_id,
                helpers.error_payload,
            )
            or change_center_http.dispatch(
                handler,
                route_path,
                sessions,
                incidents,
                ChangeCenter(changes),
                phase_approvals,
                helpers.request_session,
                helpers.request_id,
                helpers.error_payload,
            )
            or incident_http.dispatch(
                handler,
                route_path,
                sessions,
                incidents,
                changes,
                phase_approvals,
                helpers.request_session,
                helpers.request_id,
                helpers.error_payload,
            )
            or incident_report_http.dispatch(
                handler,
                route_path,
                sessions,
                incidents,
                helpers.request_session,
                helpers.csrf_valid,
                helpers.request_id,
                helpers.error_payload,
            )
        )
