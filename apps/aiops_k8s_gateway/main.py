"""AIOps Gateway V1 process entry point."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import sqlite3
import time
import uuid
from http import HTTPStatus
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import unquote, urlparse

from aiops.domain.identity import AuthSession, IdentityConfig, IdentityError, IdentityProvider, ROLE_ADMIN
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
from .change_plan_phases import ChangePlanPhases
from .change_center import ChangeCenter
from .change_requests import ChangeRequests
from .chat_sessions import ChatSessions
from .chat_attachments import ChatAttachments
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
from .incident_runtime import incident_service, start_incident_reconciler
from .investigation_events import InvestigationEvents
from .mcp_registry import MCPRegistry
from .observability import metrics_body as gateway_metrics_body
from .platform_status import PlatformSetupDecisions
from .resource_catalog import ResourceCatalog
from .secure_inputs import SecureInputs
from .skill_registry import SkillRegistry
from .v1_store import GatewayV1Store

_SESSIONS = GatewayV1Store()
_SESSION_COOKIE_NAME = "aiops_session"
_CSRF_HEADER_NAME = "X-CSRF-Token"
_CSRF_MESSAGE = b"aiops-console-csrf"


def _identity_provider() -> IdentityProvider:
    return IdentityProvider(IdentityConfig.load())


def _incident_service():
    return incident_service(_SESSIONS.database)


def _change_requests(validation: KubernetesChangeValidation | None = None) -> ChangeRequests:
    return ChangeRequests(_SESSIONS.database, validation=validation or _kubernetes_change_validation())


def _secure_inputs() -> SecureInputs:
    return SecureInputs(
        _SESSIONS.database,
        key_path=os.getenv("AIOPS_CHANGE_ENCRYPTION_KEY_PATH", "/var/run/secrets/aiops-change/key"),
    )


def _kubernetes_change_validation() -> KubernetesChangeValidation:
    return KubernetesChangeValidation(
        commands=ConnectorValidationCommands(), enrollments=_SESSIONS.connector_enrollments,
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
    catalog = catalog or ResourceCatalog(_SESSIONS.database)
    return KubernetesPhaseApprovals(
        _SESSIONS.database,
        enrollments=_SESSIONS.connector_enrollments,
        authorities=authorities or _kubernetes_change_authorities(catalog=catalog),
        phases=ChangePlanPhases(),
        validation=validation,
    )


def _kubernetes_change_authorities(
    *, catalog: ResourceCatalog | None = None,
) -> KubernetesChangeAuthorities:
    return KubernetesChangeAuthorities(
        _SESSIONS.database, users=_SESSIONS, enrollments=_SESSIONS.connector_enrollments,
        catalog=catalog or ResourceCatalog(_SESSIONS.database),
    )


def _kubernetes_change_executions(
    approvals: KubernetesPhaseApprovals | None = None,
    reconciliations: KubernetesReconciliations | None = None,
) -> KubernetesChangeExecutions:
    resolved_approvals = approvals or _kubernetes_phase_approvals()
    return KubernetesChangeExecutions(
        _SESSIONS.database,
        approvals=resolved_approvals,
        enrollments=_SESSIONS.connector_enrollments,
        secure_inputs=_secure_inputs(),
        reconciliations=reconciliations,
    )


def _kubernetes_reconciliations(
    approvals: KubernetesPhaseApprovals,
) -> KubernetesReconciliations:
    return KubernetesReconciliations(
        _SESSIONS.database, approvals=approvals, secure_inputs=_secure_inputs(),
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


def _extract_session_cookie(header: str | None) -> str | None:
    if not header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(header)
    except Exception:
        return None
    morsel = cookie.get(_SESSION_COOKIE_NAME)
    return morsel.value.strip() if morsel and morsel.value.strip() else None


def _request_session(handler: JsonHandler) -> tuple[AuthSession | None, str | None]:
    bearer = _extract_bearer_token(handler.headers.get("Authorization"))
    session = _SESSIONS.get(bearer or "")
    if session is not None:
        return session, "bearer"
    session = _SESSIONS.get(_extract_session_cookie(handler.headers.get("Cookie")) or "")
    return (session, "cookie") if session is not None else (None, None)


def _csrf_token(session_token: str) -> str:
    return hmac.new(session_token.encode(), _CSRF_MESSAGE, hashlib.sha256).hexdigest()


def _csrf_valid(handler: JsonHandler, session_token: str) -> bool:
    supplied = handler.headers.get(_CSRF_HEADER_NAME, "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, _csrf_token(session_token))


def _secure_session_cookie(handler: JsonHandler) -> bool:
    configured = os.getenv("AIOPS_SECURE_SESSION_COOKIE", "").lower() in {"1", "true", "yes"}
    return configured or handler.headers.get("X-Forwarded-Proto", "").lower() == "https"


def _session_cookie_header(handler: JsonHandler, token: str, max_age: int) -> str:
    secure = "; Secure" if _secure_session_cookie(handler) else ""
    return f"{_SESSION_COOKIE_NAME}={token}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax{secure}"


def _clear_session_cookie_header(handler: JsonHandler) -> str:
    secure = "; Secure" if _secure_session_cookie(handler) else ""
    return f"{_SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax{secure}"


def _record_admin_denial(
    actor_id: str | None,
    audit_target: tuple[str, str | None, str] | None,
    result: str,
    request_id: str,
    reason: str = "unavailable_before_authorization",
) -> None:
    if audit_target is None:
        return
    target_type, target_id, action = audit_target
    _SESSIONS.record_admin_audit(
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        action=action,
        reason=reason,
        before=None,
        after=None,
        result=result,
        request_id=request_id,
    )


def _authorize_v1_admin(
    handler: JsonHandler,
    request_id: str,
    *,
    audit_target: tuple[str, str | None, str] | None = None,
) -> AuthSession | None:
    session, auth_mode = _request_session(handler)
    if session is None:
        _record_admin_denial(None, audit_target, "unauthorized", request_id)
        handler.write_json(HTTPStatus.UNAUTHORIZED, _error_payload("unauthorized", "authentication required", request_id))
        return None
    if auth_mode == "cookie" and handler.command not in {"GET", "HEAD", "OPTIONS"} and not _csrf_valid(handler, session.token):
        _record_admin_denial(session.actor.actor_id, audit_target, "csrf_required", request_id)
        handler.write_json(HTTPStatus.FORBIDDEN, _error_payload("csrf_required", "missing or invalid CSRF token", request_id))
        return None
    if not _SESSIONS.is_platform_administrator(session.actor.actor_id):
        _record_admin_denial(session.actor.actor_id, audit_target, "forbidden", request_id)
        handler.write_json(HTTPStatus.FORBIDDEN, _error_payload("forbidden", "Platform Administrator access required", request_id))
        return None
    return session


def _require_fresh_auth(
    handler: JsonHandler,
    session: AuthSession,
    request_id: str,
    *,
    audit_target: tuple[str, str | None, str],
    reason: str,
) -> bool:
    if _SESSIONS.is_fresh(session.token):
        return True
    _record_admin_denial(session.actor.actor_id, audit_target, "fresh_auth_required", request_id, reason)
    handler.write_json(
        HTTPStatus.FORBIDDEN,
        _error_payload("fresh_auth_required", "re-authentication is required", request_id),
    )
    return False


def _v1_admin_route(path: str) -> tuple[str, str | None] | None:
    prefix = "/api/v1/admin/"
    if not path.startswith(prefix):
        return None
    parts = path[len(prefix) :].strip("/").split("/")
    collections = {
        "users",
        "teams",
        "team-memberships",
        "role-bindings",
        "connector-enrollments",
        "clusters",
        "audit",
    }
    if not parts or len(parts) > 2 or parts[0] not in collections:
        return None
    return parts[0], unquote(parts[1]) if len(parts) == 2 else None


def _handle_v1_admin_get(handler: JsonHandler, collection: str) -> None:
    request_id = _request_id(handler)
    if _authorize_v1_admin(handler, request_id) is None:
        return
    if collection == "audit":
        handler.write_json(HTTPStatus.OK, {"request_id": request_id, "audit": _SESSIONS.list_admin_audit()})
    elif collection in {"connector-enrollments", "clusters"}:
        state = connector_command_http.admin_state(
            _SESSIONS.connector_enrollments.admin_state(), ConnectorCommands(_SESSIONS.database)
        )
        handler.write_json(HTTPStatus.OK, {"request_id": request_id, **state})
    else:
        handler.write_json(HTTPStatus.OK, {"request_id": request_id, **_SESSIONS.admin_state()})


def _handle_v1_admin_mutation(handler: JsonHandler, collection: str, target_id: str | None) -> None:
    request_id = _request_id(handler)
    action = f"{collection}_{'update' if target_id else 'create'}"
    audit_target = (collection, target_id, action)
    session = _authorize_v1_admin(handler, request_id, audit_target=audit_target)
    if session is None:
        return
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        _record_admin_denial(session.actor.actor_id, audit_target, "invalid_request", request_id)
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    raw_reason = payload.pop("reason", "")
    reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
    if not reason:
        _record_admin_denial(session.actor.actor_id, audit_target, "reason_required", request_id, "missing")
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("reason_required", "reason is required", request_id))
        return
    if not _require_fresh_auth(handler, session, request_id, audit_target=audit_target, reason=reason):
        return
    if collection in {"connector-enrollments", "clusters"}:
        _handle_connector_admin_mutation(handler, collection, target_id, payload, session, reason, request_id)
        return
    allowed_fields = {
        "users": ({"display_name", "email", "password", "active"} if target_id else {"username", "display_name", "email", "password"}),
        "teams": {"name", "description", "active"},
        "team-memberships": ({"active"} if target_id else {"user_id", "team_id"}),
        "role-bindings": ({"active"} if target_id else {"user_id", "role", "scope_type", "scope_id"}),
    }[collection]
    unknown = set(payload) - allowed_fields
    text_fields = set(payload) - {"active", "scope_id"}
    invalid = (
        not payload
        or bool(unknown)
        or any(not isinstance(payload[field], str) for field in text_fields)
        or ("scope_id" in payload and payload["scope_id"] is not None and not isinstance(payload["scope_id"], str))
        or ("active" in payload and not isinstance(payload["active"], bool))
    )
    if invalid:
        _record_admin_denial(session.actor.actor_id, audit_target, "invalid_request", request_id, reason)
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", "invalid administration fields", request_id))
        return
    try:
        response_key, after = _SESSIONS.mutate_admin(
            collection=collection,
            target_id=target_id,
            payload=payload,
            actor_id=session.actor.actor_id,
            reason=reason,
            action=action,
            request_id=request_id,
        )
    except IdentityError as exc:
        status = HTTPStatus.NOT_FOUND if exc.code == "not_found" else HTTPStatus.CONFLICT if exc.code.endswith("exists") or exc.code == "last_admin" else HTTPStatus.BAD_REQUEST
        _record_admin_denial(session.actor.actor_id, audit_target, exc.code, request_id, reason)
        handler.write_json(status, _error_payload(exc.code, exc.message, request_id))
        return
    except sqlite3.Error:
        handler.write_json(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            _error_payload("administration_write_failed", "administration change was not committed", request_id),
        )
        return
    handler.write_json(HTTPStatus.OK if target_id else HTTPStatus.CREATED, {"request_id": request_id, response_key: after})


def _handle_connector_admin_mutation(
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
            enrollment, credential = _SESSIONS.connector_enrollments.create(
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
            enrollment, credential = _SESSIONS.connector_enrollments.update(
                target_id,
                active=payload.get("active"),
                rotate_credential=bool(payload.get("rotate_credential")),
                retry_read_verification=bool(payload.get("retry_read_verification")),
                commands=ConnectorCommands(_SESSIONS.database),
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
            cluster = _SESSIONS.connector_enrollments.update_cluster(
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
        _SESSIONS.record_admin_audit(
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
            cluster, created = _SESSIONS.connector_enrollments.register(
                credential,
                connector_id,
                cluster_id,
                namespace_scope=payload.get("namespace_scope", []),
                capabilities=payload.get("capabilities", []),
                commands=ConnectorCommands(_SESSIONS.database),
                request_id=request_id,
            )
            handler.write_json(HTTPStatus.CREATED if created else HTTPStatus.OK, {"request_id": request_id, "status": "registered", "cluster": cluster})
            return
        if not isinstance(payload["status"], str) or not isinstance(payload.get("failure_summary", ""), str):
            raise IdentityError("invalid_request", "status and failure_summary must be strings")
        cluster = _SESSIONS.connector_enrollments.heartbeat(
            credential,
            connector_id,
            cluster_id,
            status=payload["status"],
            failure_summary=payload.get("failure_summary", ""),
            request_id=request_id,
        )
        handler.write_json(HTTPStatus.OK, {"request_id": request_id, "status": "accepted", "cluster": cluster})
    except IdentityError as exc:
        _SESSIONS.record_admin_audit(
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
        catalog = ResourceCatalog(_SESSIONS.database)
        identity = ConnectorIdentity(_SESSIONS.database)
        incidents = _incident_service()
        common = (_SESSIONS, _authorize_v1_admin, _require_fresh_auth, _request_id, _error_payload)
        validation = _kubernetes_change_validation()
        changes = _change_requests(validation)
        authorities = _kubernetes_change_authorities(catalog=catalog)
        phase_approvals = _kubernetes_phase_approvals(
            authorities=authorities, validation=validation, catalog=catalog,
        )
        reconciliations = _kubernetes_reconciliations(phase_approvals)
        executions = _kubernetes_change_executions(phase_approvals, reconciliations)
        return (
            chat_http.dispatch(self, route_path, ChatSessions(_SESSIONS.database), ChatAttachments(_SESSIONS.database), ChatHandoffs(_SESSIONS.database), MCPRegistry(_SESSIONS.database), SkillRegistry(_SESSIONS.database), _SESSIONS, catalog, incidents, _SESSIONS.connector_enrollments.public_status, _request_session, _csrf_valid, _request_id, _error_payload)
            or skill_registry_http.dispatch(self, route_path, SkillRegistry(_SESSIONS.database), MCPRegistry(_SESSIONS.database), _authorize_v1_admin, _require_fresh_auth, _request_id, _error_payload)
            or mcp_registry_http.dispatch(self, route_path, MCPRegistry(_SESSIONS.database), _authorize_v1_admin, _require_fresh_auth, _request_id, _error_payload)
            or model_provider_http.dispatch(
                self, route_path, _SESSIONS, _authorize_v1_admin, _require_fresh_auth,
                _request_session, _request_id, _error_payload,
            )
            or notification_admin_http.dispatch(
                self, route_path, _SESSIONS, _authorize_v1_admin, _require_fresh_auth,
                _request_session, _request_id, _error_payload, PlatformSetupDecisions(_SESSIONS.database),
            )
            or platform_status_http.dispatch(self, route_path, _SESSIONS, _SESSIONS.connector_enrollments, _authorize_v1_admin, _require_fresh_auth, _request_session, _request_id, _error_payload)
            or secure_input_http.dispatch(
                self, route_path, _SESSIONS, _secure_inputs(),
                _request_session, _csrf_valid, _request_id, _error_payload,
            )
            or resource_catalog_http.dispatch(self, route_path, _SESSIONS, catalog, identity, _request_session, incidents.team_ids_for_actor, _SESSIONS.connector_enrollments.public_status, _authorize_v1_admin, _require_fresh_auth, _request_id, _extract_bearer_token, _error_payload)
            or kubernetes_phase_approval_http.dispatch(
                self, route_path, _SESSIONS, changes, authorities, phase_approvals,
                _authorize_v1_admin, _require_fresh_auth, _request_session, _csrf_valid,
                _request_id, _error_payload,
            )
            or kubernetes_change_execution_http.dispatch(
                self, route_path, _SESSIONS, changes, phase_approvals, executions,
                reconciliations,
                _request_session, _csrf_valid, _request_id, _error_payload,
            )
            or change_request_http.dispatch(
                self, route_path, _SESSIONS, incidents, changes, authorities, phase_approvals,
                _request_session, _csrf_valid, _request_id, _error_payload,
            )
            or change_center_http.dispatch(
                self, route_path, _SESSIONS, incidents, ChangeCenter(changes), phase_approvals,
                _request_session, _request_id, _error_payload,
            )
            or incident_http.dispatch(
                self, route_path, _SESSIONS, incidents, changes,
                phase_approvals, _request_session, _request_id, _error_payload,
            )
            or incident_report_http.dispatch(self, route_path, _SESSIONS, incidents, _request_session, _csrf_valid, _request_id, _error_payload)
        )

    def do_GET(self) -> None:  # noqa: N802
        route_path = urlparse(self.path).path
        if self.is_metrics_request():
            self.write_metrics_body(gateway_metrics_body(_SESSIONS.database, handler_type=type(self)))
            return
        if self._dispatch(route_path):
            return
        if investigation_event_http.dispatch_get(self, route_path, _SESSIONS, _incident_service(), InvestigationEvents(_SESSIONS.database), _request_session, _request_id, _error_payload):
            return
        if connector_enrollment_http.dispatch_get(
            self, route_path, _SESSIONS.connector_enrollments,
            _request_session, _request_id, _error_payload,
        ):
            return
        admin_route = _v1_admin_route(route_path)
        if admin_route and admin_route[1] is None:
            _handle_v1_admin_get(self, admin_route[0])
            return
        if route_path == "/api/v1/actor":
            request_id = _request_id(self)
            session, _ = _request_session(self)
            if session is None:
                self.write_json(HTTPStatus.UNAUTHORIZED, _error_payload("unauthorized", "authentication required", request_id))
            else:
                self.write_json(HTTPStatus.OK, {"request_id": request_id, "actor": _SESSIONS.actor_view(session.actor)})
            return
        if route_path == "/auth/csrf":
            request_id = _request_id(self)
            session, _ = _request_session(self)
            if session is None:
                self.write_json(HTTPStatus.UNAUTHORIZED, _error_payload("unauthorized", "missing or invalid session", request_id))
            else:
                self.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "csrf_token": _csrf_token(session.token)})
            return
        if route_path == "/healthz":
            self.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok"})
            return
        if route_path == "/readyz":
            state = _SESSIONS.connector_enrollments.admin_state()
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
        if connector_command_http.dispatch(
            self, route_path, ConnectorCommands(_SESSIONS.database), ConnectorIdentity(_SESSIONS.database),
            _authorize_v1_admin, _request_id, _extract_bearer_token, _error_payload,
            _record_connector_command_result,
            _kubernetes_change_executions(),
        ):
            return
        if diagnosis_delivery_http.dispatch(self, route_path, DiagnosisDelivery(_SESSIONS.database)):
            return
        if investigation_event_http.dispatch_post(self, route_path, _SESSIONS, _incident_service(), InvestigationEvents(_SESSIONS.database), _request_session, _csrf_valid, _request_id, _error_payload):
            return
        admin_route = _v1_admin_route(route_path)
        if admin_route and admin_route[1] is None and admin_route[0] != "audit":
            _handle_v1_admin_mutation(self, admin_route[0], None)
            return
        if route_path in {"/api/v1/connectors/register", "/api/v1/connectors/heartbeat"}:
            _handle_connector_request(self, route_path.rsplit("/", 1)[-1])
            return
        if route_path == "/auth/login":
            self._login()
            return
        if route_path == "/auth/reauth":
            self._reauth()
            return
        if route_path == "/auth/logout":
            self._logout()
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
        admin_route = _v1_admin_route(route_path)
        if admin_route and admin_route[1] is not None and admin_route[0] != "audit":
            _handle_v1_admin_mutation(self, admin_route[0], admin_route[1])
            return
        self.write_not_found()

    def do_PUT(self) -> None:  # noqa: N802
        if not self._dispatch(urlparse(self.path).path):
            self.write_not_found()

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._dispatch(urlparse(self.path).path):
            self.write_not_found()

    def _login(self) -> None:
        request_id = _request_id(self)
        try:
            payload = self.read_json_body()
            actor = _identity_provider().login(str(payload.get("username") or ""), str(payload.get("password") or ""))
            if (
                payload.get("session_mode") == "cookie"
                and actor.auth_source == "local"
                and actor.has_role(ROLE_ADMIN)
                and os.getenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD")
                and actor.username == (os.getenv("AIOPS_BOOTSTRAP_ADMIN_USERNAME", "admin").strip() or "admin")
            ):
                _SESSIONS.ensure_platform_administrator(actor.actor_id)
            session = _SESSIONS.issue(actor)
        except IdentityError as exc:
            status = HTTPStatus.SERVICE_UNAVAILABLE if exc.code == "ldap_unavailable" else HTTPStatus.UNAUTHORIZED
            self.write_json(status, _error_payload(exc.code, exc.message, request_id))
            return
        except (TypeError, ValueError) as exc:
            self.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
            return
        response = {
            "service": APP_NAME,
            "status": "ok",
            "request_id": request_id,
            "expires_at": session.expires_at,
            "actor": _SESSIONS.actor_view(actor),
        }
        if payload.get("session_mode") != "cookie":
            response["token"] = session.token
        self.write_json(HTTPStatus.OK, response, headers={"Set-Cookie": _session_cookie_header(self, session.token, _SESSIONS.ttl_seconds)})

    def _reauth(self) -> None:
        request_id = _request_id(self)
        session, auth_mode = _request_session(self)
        if session is None:
            self.write_json(HTTPStatus.UNAUTHORIZED, _error_payload("unauthorized", "authentication required", request_id))
            return
        if auth_mode == "cookie" and not _csrf_valid(self, session.token):
            self.write_json(HTTPStatus.FORBIDDEN, _error_payload("csrf_required", "missing or invalid CSRF token", request_id))
            return
        try:
            actor = _identity_provider().login(session.actor.username, str(self.read_json_body().get("password") or ""))
        except IdentityError as exc:
            self.write_json(HTTPStatus.UNAUTHORIZED, _error_payload(exc.code, exc.message, request_id))
            return
        if actor.actor_id != session.actor.actor_id:
            self.write_json(HTTPStatus.UNAUTHORIZED, _error_payload("invalid_credentials", "identity mismatch", request_id))
            return
        _SESSIONS.mark_fresh(session.token)
        self.write_json(HTTPStatus.OK, {"status": "ok", "request_id": request_id})

    def _logout(self) -> None:
        request_id = _request_id(self)
        session, auth_mode = _request_session(self)
        if session is not None:
            if auth_mode == "cookie" and not _csrf_valid(self, session.token):
                self.write_json(HTTPStatus.FORBIDDEN, _error_payload("csrf_required", "missing or invalid CSRF token", request_id))
                return
            _SESSIONS.revoke(session.token)
        self.write_json(
            HTTPStatus.OK,
            {"service": APP_NAME, "status": "ok", "request_id": request_id},
            headers={"Set-Cookie": _clear_session_cookie_header(self)},
        )


def _record_connector_command_result(
    conn: sqlite3.Connection,
    command_id: str,
    result: dict[str, object],
    now: float,
) -> None:
    _SESSIONS.connector_enrollments.record_verification_result_in(conn, command_id, result, now)
    _change_requests().record_validation_result_in(conn, command_id, result, now)
    _kubernetes_change_executions().record_result_in(conn, command_id, result, now)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIOps Gateway service")
    parser.add_argument("--host", default=os.getenv("AIOPS_GATEWAY_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AIOPS_GATEWAY_PORT", "8080")))
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    start_incident_reconciler(_incident_service(), connector_commands=ConnectorCommands(_SESSIONS.database))
    start_diagnosis_delivery(DiagnosisDelivery(_SESSIONS.database, capability_snapshot=MCPRegistry(_SESSIONS.database).authorized_snapshot, skill_bindings=SkillRegistry(_SESSIONS.database).authorized_bindings))
    notification_requests.start_notification_handoff(
        notification_requests.NotificationOutbox(_SESSIONS.database),
        sender=notification_handoff_http.send_notification_request,
    )
    serve(GatewayHandler, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
