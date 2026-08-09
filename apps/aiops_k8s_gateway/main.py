"""AIOps Gateway V1 process entry point."""

from __future__ import annotations

import argparse
import os
import secrets
import sqlite3
import time
import uuid
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote, urlparse

from aiops.domain.identity import Actor, AuthSession, IdentityConfig, IdentityError, IdentityProvider, SQLiteIdentityStore
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
from .connector_identity import ConnectorIdentity
from .connector_validation_commands import ConnectorValidationCommands
from .diagnosis_delivery import DiagnosisDelivery
from .diagnosis_delivery_runtime import start_diagnosis_delivery
from .gateway_audit import GatewayAudit
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
from .v1_store import GatewayV1Store

_GATEWAY = GatewayV1Store()


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
        _GATEWAY.database,
        clock=time.time,
        token_factory=lambda: secrets.token_urlsafe(32),
        active_actor_lookup=_active_actor,
    )


def _gateway_audit() -> GatewayAudit:
    return GatewayAudit(_GATEWAY.database, clock=time.time)


def _identity_administration() -> IdentityAdministration:
    return IdentityAdministration(
        _GATEWAY.database,
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
    return incident_service(_GATEWAY.database)


def _change_requests(validation: KubernetesChangeValidation | None = None) -> ChangeRequests:
    return ChangeRequests(_GATEWAY.database, validation=validation or _kubernetes_change_validation())


def _secure_inputs() -> SecureInputs:
    return SecureInputs(
        _GATEWAY.database,
        key_path=os.getenv("AIOPS_CHANGE_ENCRYPTION_KEY_PATH", "/var/run/secrets/aiops-change/key"),
    )


def _kubernetes_change_validation() -> KubernetesChangeValidation:
    return KubernetesChangeValidation(
        commands=ConnectorValidationCommands(), enrollments=_GATEWAY.connector_enrollments,
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
    catalog = catalog or ResourceCatalog(_GATEWAY.database)
    return KubernetesPhaseApprovals(
        _GATEWAY.database,
        enrollments=_GATEWAY.connector_enrollments,
        authorities=authorities or _kubernetes_change_authorities(catalog=catalog),
        phases=ChangePlanPhases(),
        validation=validation,
    )


def _kubernetes_change_authorities(
    *, catalog: ResourceCatalog | None = None,
) -> KubernetesChangeAuthorities:
    return KubernetesChangeAuthorities(
        _GATEWAY.database, user_active_in=IdentityAdministration.user_active_in,
        enrollments=_GATEWAY.connector_enrollments,
        catalog=catalog or ResourceCatalog(_GATEWAY.database),
    )


def _kubernetes_change_executions(
    approvals: KubernetesPhaseApprovals | None = None,
    reconciliations: KubernetesReconciliations | None = None,
) -> KubernetesChangeExecutions:
    resolved_approvals = approvals or _kubernetes_phase_approvals()
    return KubernetesChangeExecutions(
        _GATEWAY.database,
        approvals=resolved_approvals,
        enrollments=_GATEWAY.connector_enrollments,
        secure_inputs=_secure_inputs(),
        reconciliations=reconciliations,
    )


def _kubernetes_reconciliations(
    approvals: KubernetesPhaseApprovals,
) -> KubernetesReconciliations:
    return KubernetesReconciliations(
        _GATEWAY.database, approvals=approvals, secure_inputs=_secure_inputs(),
    )


def _chat_http() -> chat_http.ChatHTTPAdapter:
    attachments = ChatAttachments(_GATEWAY.database, scanner=scan_with_clamav)
    identity = _identity_http()
    sessions = _gateway_sessions()
    return chat_http.ChatHTTPAdapter(
        chats=ChatSessions(_GATEWAY.database, attachment_source=attachments), attachments=attachments, handoffs=ChatHandoffs(_GATEWAY.database),
        mcp_registry=MCPRegistry(_GATEWAY.database), skill_registry=SkillRegistry(_GATEWAY.database), actor_view=sessions.actor_view, catalog=ResourceCatalog(_GATEWAY.database), incidents=_incident_service(),
        connector_status=_GATEWAY.connector_enrollments.public_status, request_session=identity.request_session, csrf_valid=identity.csrf_valid, request_id_for=_request_id, error_payload=_error_payload, model_provider_status=model_provider_http.read_status,
    )


def _request_http_adapters() -> tuple[Any, ...]:
    identity = _identity_http()
    sessions = _gateway_sessions()
    audit = _gateway_audit()
    catalog = ResourceCatalog(_GATEWAY.database)
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
        PlatformSetupDecisions(_GATEWAY.database),
        model_status=model_provider_http.read_status,
        notification_status=notification_admin_http.read_status,
        connector_status=_GATEWAY.connector_enrollments.admin_state,
        observability_status=read_observability_status,
    )
    return (
        identity,
        _chat_http(),
        skill_registry_http.SkillRegistryHTTPAdapter(SkillRegistry(_GATEWAY.database), MCPRegistry(_GATEWAY.database), identity.authorize_admin, identity.require_fresh_auth, _request_id, _error_payload),
        mcp_registry_http.MCPRegistryHTTPAdapter(MCPRegistry(_GATEWAY.database), identity.authorize_admin, identity.require_fresh_auth, _request_id, _error_payload),
        model_provider_http.ModelProviderHTTPAdapter(audit.record, identity.authorize_admin, identity.require_fresh_auth, identity.request_session, _request_id, _error_payload),
        notification_admin_http.NotificationAdminHTTPAdapter(audit.unresolved_request, audit.record, identity.authorize_admin, identity.require_fresh_auth, identity.request_session, _request_id, _error_payload, PlatformSetupDecisions(_GATEWAY.database)),
        platform_status_http.PlatformStatusHTTPAdapter(platform_status, identity.authorize_admin, identity.require_fresh_auth, identity.request_session, _request_id, _error_payload),
        secure_input_http.SecureInputHTTPAdapter(sessions.actor_view, _secure_inputs(), identity.request_session, identity.csrf_valid, _request_id, _error_payload),
        resource_catalog_http.ResourceCatalogHTTPAdapter(sessions.actor_view, audit.record, catalog, ConnectorIdentity(_GATEWAY.database), identity.request_session, incidents.team_ids_for_actor, _GATEWAY.connector_enrollments.public_status, identity.authorize_admin, identity.require_fresh_auth, _request_id, _extract_bearer_token, _error_payload),
        kubernetes_phase_approval_http.KubernetesPhaseApprovalHTTPAdapter(sessions.is_fresh, audit.record, changes, authorities, approvals, identity.authorize_admin, identity.require_fresh_auth, identity.request_session, identity.csrf_valid, _request_id, _error_payload),
        kubernetes_change_execution_http.KubernetesChangeExecutionHTTPAdapter(sessions.is_fresh, changes, approvals, executions, reconciliations, identity.request_session, identity.csrf_valid, _request_id, _error_payload),
        change_request_http.ChangeRequestHTTPAdapter(sessions.actor_view, incidents, changes, authorities, approvals, identity.request_session, identity.csrf_valid, _request_id, _error_payload),
        change_center_http.ChangeCenterHTTPAdapter(sessions.actor_view, incidents, ChangeCenter(changes), approvals, identity.request_session, _request_id, _error_payload),
        incident_http.IncidentHTTPAdapter(sessions.actor_view, incidents, changes, approvals, identity.request_session, _request_id, _error_payload),
        incident_report_http.IncidentReportHTTPAdapter(sessions.actor_view, IncidentReports(_GATEWAY.database), incidents, identity.request_session, identity.csrf_valid, _request_id, _error_payload),
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


def _connector_admin_route(path: str) -> tuple[str, str | None] | None:
    prefix = "/api/v1/admin/"
    if not path.startswith(prefix):
        return None
    parts = path[len(prefix) :].strip("/").split("/")
    if not parts or len(parts) > 2 or parts[0] not in {"connector-enrollments", "clusters"}:
        return None
    return parts[0], unquote(parts[1]) if len(parts) == 2 else None


def _handle_connector_admin_get(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    if _identity_http().authorize_admin(handler, request_id) is None:
        return
    state = connector_command_http.admin_state(
        _GATEWAY.connector_enrollments.admin_state(), ConnectorCommands(_GATEWAY.database)
    )
    handler.write_json(HTTPStatus.OK, {"request_id": request_id, **state})


def _handle_connector_admin_mutation(handler: JsonHandler, collection: str, target_id: str | None) -> None:
    request_id = _request_id(handler)
    action = f"{collection}_{'update' if target_id else 'create'}"
    audit_target = (collection, target_id, action)
    identity = _identity_http()
    audit = _gateway_audit()
    session = identity.authorize_admin(handler, request_id, audit_target=audit_target)
    if session is None:
        return
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        audit.denial(session.actor.actor_id, audit_target, "invalid_request", request_id)
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    raw_reason = payload.pop("reason", "")
    reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
    if not reason:
        audit.denial(session.actor.actor_id, audit_target, "reason_required", request_id, "missing")
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("reason_required", "reason is required", request_id))
        return
    if not identity.require_fresh_auth(handler, session, request_id, audit_target=audit_target, reason=reason):
        return
    _apply_connector_admin_mutation(handler, collection, target_id, payload, session, reason, request_id)


def _apply_connector_admin_mutation(
    handler: JsonHandler,
    collection: str,
    target_id: str | None,
    payload: dict[str, Any],
    session: AuthSession,
    reason: str,
    request_id: str,
) -> None:
    try:
        if collection == "connector-enrollments" and target_id is None:
            if (
                set(payload) != {"connector_id", "cluster_id", "expected_revision"}
                or not all(isinstance(payload[field], str) for field in ("connector_id", "cluster_id"))
                or payload["expected_revision"] is not None
            ):
                raise IdentityError(
                    "invalid_enrollment",
                    "connector_id, cluster_id and null expected_revision are required",
                )
            enrollment, credential = _GATEWAY.connector_enrollments.create(
                connector_id=payload["connector_id"],
                cluster_id=payload["cluster_id"],
                actor_id=session.actor.actor_id,
                reason=reason,
                request_id=request_id,
            )
            handler.write_json(HTTPStatus.CREATED, {"request_id": request_id, "connector_enrollment": enrollment, "credential": credential})
            return
        if collection == "connector-enrollments" and target_id is not None:
            if not payload or set(payload) - {"active", "rotate_credential", "retry_read_verification"} or any(not isinstance(value, bool) for value in payload.values()):
                raise IdentityError("invalid_enrollment", "Enrollment update fields must be booleans")
            enrollment, credential = _GATEWAY.connector_enrollments.update(
                target_id,
                active=payload.get("active"),
                rotate_credential=bool(payload.get("rotate_credential")),
                retry_read_verification=bool(payload.get("retry_read_verification")),
                commands=ConnectorCommands(_GATEWAY.database),
                actor_id=session.actor.actor_id,
                reason=reason,
                request_id=request_id,
            )
            response: dict[str, Any] = {"request_id": request_id, "connector_enrollment": enrollment}
            if credential:
                response["credential"] = credential
            handler.write_json(HTTPStatus.OK, response)
            return
        if collection == "clusters" and target_id is not None:
            allowed = {"display_name", "environment", "governance_notes", "mutation_enabled"}
            if not payload or set(payload) - allowed or any(
                not isinstance(value, bool if key == "mutation_enabled" else str) for key, value in payload.items()
            ):
                raise IdentityError("invalid_cluster", "invalid Cluster administration fields")
            cluster = _GATEWAY.connector_enrollments.update_cluster(
                target_id,
                payload=payload,
                actor_id=session.actor.actor_id,
                reason=reason,
                request_id=request_id,
            )
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, "cluster": cluster})
            return
        raise IdentityError("not_found", "administration resource not found")
    except IdentityError as exc:
        attempted_target = target_id or ":".join(str(payload.get(field) or "").strip() for field in ("connector_id", "cluster_id")).strip(":") or None
        _gateway_audit().record(
            actor_id=session.actor.actor_id,
            target_type=collection,
            target_id=attempted_target,
            action=f"{collection}_{'update' if target_id else 'create'}",
            reason=reason,
            before=None,
            after=None,
            result=exc.code,
            request_id=request_id,
        )
        status = (
            HTTPStatus.NOT_FOUND if exc.code == "not_found"
            else HTTPStatus.CONFLICT
            if exc.code in {"enrollment_exists", "rotation_pending", "rotation_blocked"}
            else HTTPStatus.BAD_REQUEST
        )
        handler.write_json(status, _error_payload(exc.code, exc.message, request_id))


def _handle_connector_request(handler: JsonHandler, action: str) -> None:
    request_id = _request_id(handler)
    credential = _extract_bearer_token(handler.headers.get("Authorization")) or ""
    connector_id = ""
    cluster_id = ""
    try:
        if not credential:
            raise IdentityError("invalid_connector_credential", "Connector credential is invalid or revoked")
        payload = handler.read_json_body()
        allowed = {"connector_id", "cluster_id", "namespace_scope", "capabilities"} if action == "register" else {"connector_id", "cluster_id", "status", "failure_summary"}
        required = {"connector_id", "cluster_id"} if action == "register" else {"connector_id", "cluster_id", "status"}
        if set(payload) - allowed or not required <= set(payload):
            raise IdentityError("invalid_request", "invalid Connector request fields")
        connector_id = payload["connector_id"]
        cluster_id = payload["cluster_id"]
        if not isinstance(connector_id, str) or not isinstance(cluster_id, str) or not connector_id.strip() or not cluster_id.strip():
            raise IdentityError("invalid_request", "connector_id and cluster_id are required")
        connector_id = connector_id.strip()
        cluster_id = cluster_id.strip()
        if action == "register":
            for field in ("namespace_scope", "capabilities"):
                value = payload.get(field, [])
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise IdentityError("invalid_request", f"{field} must be an array of strings")
            cluster, created = _GATEWAY.connector_enrollments.register(
                credential,
                connector_id,
                cluster_id,
                namespace_scope=payload.get("namespace_scope", []),
                capabilities=payload.get("capabilities", []),
                commands=ConnectorCommands(_GATEWAY.database),
                request_id=request_id,
            )
            handler.write_json(HTTPStatus.CREATED if created else HTTPStatus.OK, {"request_id": request_id, "status": "registered", "cluster": cluster})
            return
        if not isinstance(payload["status"], str) or not isinstance(payload.get("failure_summary", ""), str):
            raise IdentityError("invalid_request", "status and failure_summary must be strings")
        cluster = _GATEWAY.connector_enrollments.heartbeat(
            credential,
            connector_id,
            cluster_id,
            status=payload["status"],
            failure_summary=payload.get("failure_summary", ""),
            request_id=request_id,
        )
        handler.write_json(HTTPStatus.OK, {"request_id": request_id, "status": "accepted", "cluster": cluster})
    except IdentityError as exc:
        _gateway_audit().record(
            actor_id=None,
            target_type="connectors",
            target_id=connector_id or None,
            action=f"connector_{action}",
            reason="Connector authentication or payload validation",
            before=None,
            after={"cluster_id": cluster_id} if cluster_id else None,
            result=exc.code,
            request_id=request_id,
        )
        status = {
            "invalid_connector_credential": HTTPStatus.UNAUTHORIZED,
            "identity_mismatch": HTTPStatus.FORBIDDEN,
            "not_registered": HTTPStatus.CONFLICT,
        }.get(exc.code, HTTPStatus.BAD_REQUEST)
        handler.write_json(status, _error_payload(exc.code, exc.message, request_id))
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))


class GatewayHandler(JsonHandler):
    """Gateway operational and V1 HTTP surface."""

    service_name = APP_NAME

    def _dispatch(self, route_path: str) -> bool:
        return any(adapter.dispatch(self, route_path) for adapter in _request_http_adapters())

    def do_GET(self) -> None:  # noqa: N802
        route_path = urlparse(self.path).path
        if self.is_metrics_request():
            self.write_metrics_body(gateway_metrics_body(_GATEWAY.database, handler_type=type(self)))
            return
        if self._dispatch(route_path):
            return
        identity = _identity_http()
        if investigation_event_http.dispatch_get(self, route_path, _gateway_sessions().actor_view, _incident_service(), InvestigationEvents(_GATEWAY.database), identity.request_session, _request_id, _error_payload):
            return
        if connector_enrollment_http.dispatch_get(
            self, route_path, _GATEWAY.connector_enrollments,
            identity.request_session, _request_id, _error_payload,
        ):
            return
        admin_route = _connector_admin_route(route_path)
        if admin_route and admin_route[1] is None:
            _handle_connector_admin_get(self)
            return
        if route_path == "/healthz":
            self.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok"})
            return
        if route_path == "/readyz":
            state = _GATEWAY.connector_enrollments.admin_state()
            registered = sum(
                1 for enrollment in state["connector_enrollments"]
                if enrollment["state"] == "online" and enrollment["read_verification"] == "verified"
            )
            self.write_json(
                HTTPStatus.OK,
                {"service": APP_NAME, "status": "ok", "registered_connectors": registered},
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
            self, route_path, ConnectorCommands(_GATEWAY.database), ConnectorIdentity(_GATEWAY.database),
            identity.authorize_admin, _request_id, _extract_bearer_token, _error_payload,
            _record_connector_command_result,
            _kubernetes_change_executions(),
        ):
            return
        if diagnosis_delivery_http.dispatch(self, route_path, DiagnosisDelivery(_GATEWAY.database)):
            return
        if investigation_event_http.dispatch_post(self, route_path, _gateway_sessions().actor_view, _incident_service(), InvestigationEvents(_GATEWAY.database), identity.request_session, identity.csrf_valid, _request_id, _error_payload):
            return
        admin_route = _connector_admin_route(route_path)
        if admin_route and admin_route[1] is None:
            _handle_connector_admin_mutation(self, admin_route[0], None)
            return
        if route_path in {"/api/v1/connectors/register", "/api/v1/connectors/heartbeat"}:
            _handle_connector_request(self, route_path.rsplit("/", 1)[-1])
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
        admin_route = _connector_admin_route(route_path)
        if admin_route and admin_route[1] is not None:
            _handle_connector_admin_mutation(self, admin_route[0], admin_route[1])
            return
        self.write_not_found()

    def do_PUT(self) -> None:  # noqa: N802
        if not self._dispatch(urlparse(self.path).path):
            self.write_not_found()

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._dispatch(urlparse(self.path).path):
            self.write_not_found()


def _record_connector_command_result(
    conn: sqlite3.Connection,
    command_id: str,
    result: dict[str, object],
    now: float,
) -> None:
    _GATEWAY.connector_enrollments.record_verification_result_in(conn, command_id, result, now)
    _change_requests().record_validation_result_in(conn, command_id, result, now)
    _kubernetes_change_executions().record_result_in(conn, command_id, result, now)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIOps Gateway service")
    parser.add_argument("--host", default=os.getenv("AIOPS_GATEWAY_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AIOPS_GATEWAY_PORT", "8080")))
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    start_incident_reconciler(_incident_service(), connector_commands=ConnectorCommands(_GATEWAY.database))
    start_diagnosis_delivery(DiagnosisDelivery(_GATEWAY.database, capability_snapshot=MCPRegistry(_GATEWAY.database).authorized_snapshot, skill_bindings=SkillRegistry(_GATEWAY.database).authorized_bindings))
    notification_requests.start_notification_handoff(
        notification_requests.NotificationOutbox(_GATEWAY.database),
        sender=notification_handoff_http.send_notification_request,
        change_reconciler=lambda: reconcile_change_notifications(_GATEWAY.database),
    )
    serve(GatewayHandler, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
