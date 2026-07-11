"""Smokeable entry point for the AIOps K8s Gateway process."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import re
import sqlite3
import time
import uuid
from dataclasses import asdict
from http.cookies import SimpleCookie
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from aiops.domain.identity import (
    Actor,
    IdentityConfig,
    IdentityError,
    IdentityProvider,
    AuthSession,
    PERMISSION_APPROVE_ACTION,
    PERMISSION_EXECUTE_MUTATION,
    PERMISSION_K8S_READ,
    PERMISSION_MANAGE_SETTINGS,
    PERMISSION_MANAGE_RUNBOOKS,
    PERMISSION_QUERY_AUDIT,
    PERMISSION_SYNC_LDAP,
    PERMISSION_VIEW_EVIDENCE,
    PERMISSION_VIEW_INCIDENT,
    PERMISSION_MANAGE_USERS,
    PERMISSION_VIEW_USERS,
    PERMISSION_VIEW_POLICY,
    PERMISSION_VIEW_SETTINGS,
    PERMISSION_VIEW_RUNBOOKS,
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_OPERATOR,
    Scope,
    SQLiteIdentityStore,
    resource_scope,
    role_permission_matrix,
)
from aiops.domain import cluster_registry
from apps.internal_auth import enforce_internal_auth
from apps.service_http import JsonHandler, connectivity_payload, serve
from toolsets import audit_log, incident_store

from . import APP_NAME
from . import action_control_service
from . import audit_chain_service
from . import agent_run_service
from . import approval_execution_service
from . import approval_service
from . import evidence_service
from . import notification_center
from . import report_service
from . import runbook_service
from . import settings_service, incident_http, resource_catalog_http, diagnosis_delivery_http, investigation_event_http, connector_command_http, approval_http
from .v1_store import GatewayV1Store
from .alertmanager_webhook import handle_http_request
from .command_service import build_mutation_envelope, build_read_envelope, dispatch_read_envelope
from .connector_router import ConnectorRoute
from .diagnosis_writeback import read_diagnosis_process_view, read_incident_view
from .diagnosis_delivery import DiagnosisDelivery
from .diagnosis_delivery_runtime import start_diagnosis_delivery
from .case_profile_service import apply_case_profile, read_case_profile
from .connector_identity import ConnectorIdentity
from .connector_commands import ConnectorCommands
from .approval import Approvals
from .incident_runtime import incident_service, start_incident_reconciler
from .investigation_events import InvestigationEvents
from .resource_catalog import ResourceCatalog
_ROUTES: dict[str, ConnectorRoute] = {}
_SESSIONS = GatewayV1Store()
_SESSION_COOKIE_NAME = "aiops_session"
_CSRF_HEADER_NAME = "X-CSRF-Token"
_CSRF_MESSAGE = b"aiops-console-csrf"
_PERMISSION_WRITE_INCIDENT_REPORT = "write_incident_report"
_PERMISSION_APPROVE_KB_CANDIDATE = "approve_kb_candidate"
_APP_ROUTE_PREFIXES = (
    "/incidents",
    "/agent-runs",
    "/approvals",
    "/audit",
    "/clusters",
    "/policies",
    "/runbooks",
    "/users",
    "/settings",
    "/search",
)
_APP_ROUTE_EXACT = {"/", "/login"}
_LEGACY_NO_FRONTEND_FALLBACK_EXACT = {
    "/approvals/history",
    "/approvals/pending",
}
_LEGACY_NO_FRONTEND_FALLBACK_PREFIXES = (
    "/approval-center",
    "/evidence",
    "/kb",
    "/notification-center",
    "/notifications",
    "/overview",
)
_NO_FRONTEND_FALLBACK_PREFIXES = (
    "/api/",
    "/auth/",
    "/connectors",
    "/webhooks/",
    "/diagnosis/",
    "/k8s/",
)
_NO_FRONTEND_FALLBACK_EXACT = {"/healthz", "/readyz", "/metrics"}
_DIAGNOSIS_SERVICE_ACTOR = Actor(
    actor_id="aiops-diagnosis",
    username="aiops-diagnosis",
    display_name="AIOps Diagnosis",
    roles=(ROLE_OPERATOR,),
    scope=Scope(clusters=("*",), services=("*",), teams=("*",), namespaces=("*",)),
    auth_source="service_token",
)
_GATEWAY_EXECUTOR_ACTOR = Actor(
    actor_id="gateway",
    username="gateway",
    display_name="AIOps Gateway",
    roles=(ROLE_ADMIN,),
    scope=Scope(clusters=("*",), services=("*",), teams=("*",), namespaces=("*",)),
    auth_source="system",
)
_CHAT_AGENT_ID = "console-next-chat-agent"
def _identity_provider() -> IdentityProvider:
    return IdentityProvider(IdentityConfig.load())


def _incident_service():
    return incident_service(_SESSIONS.database)
def _identity_store() -> SQLiteIdentityStore:
    return SQLiteIdentityStore(IdentityConfig.load().store_path)


def _request_id(handler: JsonHandler) -> str:
    value = handler.headers.get("X-Request-ID") or handler.headers.get("X-Correlation-ID")
    return value.strip() if value and value.strip() else f"req-{uuid.uuid4().hex}"


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
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


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
    cookie_token = _extract_session_cookie(handler.headers.get("Cookie"))
    session = _SESSIONS.get(cookie_token or "")
    return (session, "cookie") if session is not None else (None, None)


def _csrf_token(session_token: str) -> str:
    return hmac.new(session_token.encode("utf-8"), _CSRF_MESSAGE, hashlib.sha256).hexdigest()


def _csrf_valid(handler: JsonHandler, session_token: str) -> bool:
    supplied = handler.headers.get(_CSRF_HEADER_NAME, "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, _csrf_token(session_token))


def _secure_session_cookie(handler: JsonHandler) -> bool:
    configured = os.getenv("AIOPS_SECURE_SESSION_COOKIE", "").lower() in {"1", "true", "yes"}
    forwarded = handler.headers.get("X-Forwarded-Proto", "").lower() == "https"
    return configured or forwarded


def _session_cookie_header(handler: JsonHandler, token: str, max_age: int) -> str:
    secure = "; Secure" if _secure_session_cookie(handler) else ""
    return f"{_SESSION_COOKIE_NAME}={token}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax{secure}"


def _clear_session_cookie_header(handler: JsonHandler) -> str:
    secure = "; Secure" if _secure_session_cookie(handler) else ""
    return f"{_SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax{secure}"


def _resource_scope_from_payload(payload: dict[str, Any]) -> Scope:
    return resource_scope(
        cluster=str(payload.get("cluster") or payload.get("cluster_id") or "").strip() or None,
        service=str(payload.get("service") or "").strip() or None,
        team=str(payload.get("team") or "").strip() or None,
        namespace=str(payload.get("namespace") or "").strip() or None,
    )


def _evidence_scope_from_payload(payload: dict[str, Any]) -> Scope:
    raw = payload.get("scope") if isinstance(payload.get("scope"), dict) else payload
    return resource_scope(
        cluster=_scope_value(raw.get("cluster") or raw.get("cluster_id")),
        service=_scope_value(raw.get("service") or raw.get("service_id")),
        team=_scope_value(raw.get("team") or raw.get("team_id")),
        namespace=_scope_value(raw.get("namespace")),
    )


def _agent_run_scope_from_payload(payload: dict[str, Any]) -> Scope:
    raw = payload.get("scope") if isinstance(payload.get("scope"), dict) else payload
    return resource_scope(
        cluster=_scope_value(raw.get("cluster") or raw.get("cluster_id")),
        service=_scope_value(raw.get("service") or raw.get("service_id")),
        team=_scope_value(raw.get("team") or raw.get("team_id")),
        namespace=_scope_value(raw.get("namespace")),
    )


def _agent_run_scope_from_run(run: dict[str, Any]) -> Scope:
    raw = run.get("scope") if isinstance(run.get("scope"), dict) else {}
    return resource_scope(
        cluster=_scope_value(raw.get("cluster")),
        service=_scope_value(raw.get("service")),
        team=_scope_value(raw.get("team")),
        namespace=_scope_value(raw.get("namespace")),
    )


def _approval_resource_scope(approval: dict[str, Any]) -> Scope:
    raw = approval.get("resource_scope") if isinstance(approval.get("resource_scope"), dict) else {}
    return _approval_scope_from_raw(raw)


def _approval_scope_from_raw(raw: dict[str, Any]) -> Scope:
    return resource_scope(
        cluster=_scope_value(raw.get("cluster_id") or raw.get("cluster")),
        service=_scope_value(raw.get("service_id") or raw.get("service")),
        team=_scope_value(raw.get("team_id") or raw.get("team")),
        namespace=_scope_value(raw.get("namespace")),
    )


def _scope_value(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _incident_resource_scope(incident: dict[str, Any]) -> Scope:
    return resource_scope(
        cluster=_scope_value(incident.get("cluster")),
        service=_scope_value(incident.get("service")),
        team=_scope_value(incident.get("team")),
        namespace=_scope_value(incident.get("namespace")),
    )


def _authorize(handler: JsonHandler, permission: str, scope: Scope, request_id: str) -> Actor | None:
    session, auth_mode = _request_session(handler)
    if session is None:
        _record_gateway_authz_audit(
            actor=None,
            request_id=request_id,
            permission=permission,
            resource_scope=scope,
            decision="deny",
            result="unauthorized",
        )
        handler.write_json(HTTPStatus.UNAUTHORIZED, _error_payload("unauthorized", "missing or invalid bearer token", request_id))
        return None
    if auth_mode == "cookie" and handler.command not in {"GET", "HEAD", "OPTIONS"} and not _csrf_valid(handler, session.token):
        _record_gateway_authz_audit(
            actor=session.actor,
            request_id=request_id,
            permission=permission,
            resource_scope=scope,
            decision="deny",
            result="csrf_required",
        )
        handler.write_json(HTTPStatus.FORBIDDEN, _error_payload("csrf_required", "missing or invalid CSRF token", request_id))
        return None
    actor = session.actor
    auth_scope = None if scope.is_empty() else scope
    if not actor.can(permission, auth_scope):
        _record_gateway_authz_audit(
            actor=actor,
            request_id=request_id,
            permission=permission,
            resource_scope=scope,
            decision="deny",
            result="forbidden",
        )
        handler.write_json(HTTPStatus.FORBIDDEN, _error_payload("forbidden", f"permission denied: {permission}", request_id))
        return None
    return actor


def _authorize_resource(handler: JsonHandler, permission: str, scope: Scope, request_id: str) -> Actor | None:
    actor = _authorize(handler, permission, scope, request_id)
    if actor is None:
        return None
    if not actor.has_role("admin") and not scope.is_complete():
        _record_gateway_authz_audit(
            actor=actor,
            request_id=request_id,
            permission=permission,
            resource_scope=scope,
            decision="deny",
            result="missing_scope",
        )
        handler.write_json(HTTPStatus.FORBIDDEN, _error_payload("forbidden", f"permission denied: {permission}", request_id))
        return None
    return actor


def _authorize_incident_report_write(handler: JsonHandler, scope: Scope, request_id: str) -> Actor | None:
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return None
    if actor.has_role(ROLE_OPERATOR) or actor.has_role(ROLE_APPROVER) or actor.has_role(ROLE_ADMIN):
        return actor
    _record_gateway_authz_audit(
        actor=actor,
        request_id=request_id,
        permission=_PERMISSION_WRITE_INCIDENT_REPORT,
        resource_scope=scope,
        decision="deny",
        result="read_only_role",
    )
    handler.write_json(HTTPStatus.FORBIDDEN, _error_payload("forbidden", "incident report writes require operator, approver, or admin", request_id))
    return None


def _console_dist_dir() -> Path | None:
    raw = os.getenv("AIOPS_CONSOLE_DIST_DIR", "").strip()
    if not raw:
        return None
    root = Path(raw).expanduser()
    return root if root.is_dir() else None


def _is_app_route(path: str) -> bool:
    if path in _APP_ROUTE_EXACT:
        return True
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in _APP_ROUTE_PREFIXES)


def _is_frontend_fallback_route(path: str) -> bool:
    return not Path(path).suffix


def _is_service_route(path: str) -> bool:
    if path in _NO_FRONTEND_FALLBACK_EXACT:
        return True
    if path in _LEGACY_NO_FRONTEND_FALLBACK_EXACT:
        return True
    if any(path == prefix or path.startswith(f"{prefix}/") for prefix in _LEGACY_NO_FRONTEND_FALLBACK_PREFIXES):
        return True
    return any(path.startswith(prefix) for prefix in _NO_FRONTEND_FALLBACK_PREFIXES)


def _serve_console_asset(handler: JsonHandler, route_path: str) -> bool:
    root = _console_dist_dir()
    if root is None or _is_service_route(route_path):
        return False
    relative = route_path.lstrip("/") or "index.html"
    candidate = (root / relative).resolve()
    root_resolved = root.resolve()
    if candidate.is_file():
        try:
            candidate.relative_to(root_resolved)
        except ValueError:
            return False
        _write_static_file(handler, candidate)
        return True
    if _is_app_route(route_path) or _is_frontend_fallback_route(route_path):
        index = root_resolved / "index.html"
        if index.is_file():
            _write_static_file(handler, index)
            return True
    return False


def _write_static_file(handler: JsonHandler, path: Path) -> None:
    body = path.read_bytes()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _write_html(handler: JsonHandler, body: str) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def _write_markdown(handler: JsonHandler, body: str) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/markdown; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def _audit_role(actor: Actor) -> str:
    return ",".join(actor.roles)


def _record_gateway_audit(
    actor: Actor,
    *,
    request_id: str,
    action: str,
    result: str,
    cluster: str | None = None,
    namespace: str | None = None,
    incident_id: str | None = None,
    permission: str | None = None,
    decision: str | None = None,
    resource_scope: Scope | None = None,
    approval_id: str | None = None,
    action_proposal_id: str | None = None,
) -> None:
    asyncio.run(
        audit_log.record_audit(
            who=actor.username,
            what=action,
            cluster=cluster,
            namespace=namespace,
            trigger="gateway",
            tool_level="control-plane",
            tool_name="gateway",
            result=result,
            incident_id=incident_id,
            actor=actor.actor_id,
            role=_audit_role(actor),
            scope=actor.scope.to_dict(),
            request_id=request_id,
            permission=permission,
            decision=decision,
            resource_scope=resource_scope.to_dict() if resource_scope else None,
            approval_id=approval_id,
            action_proposal_id=action_proposal_id,
        )
    )


def _record_gateway_authz_audit(
    *,
    actor: Actor | None,
    request_id: str,
    permission: str,
    resource_scope: Scope,
    decision: str,
    result: str,
) -> None:
    asyncio.run(
        audit_log.record_audit(
            who=actor.username if actor else "anonymous",
            what="gateway_authorize",
            trigger="gateway",
            tool_level="control-plane",
            tool_name="gateway",
            result=result,
            actor=actor.actor_id if actor else None,
            role=_audit_role(actor) if actor else None,
            scope=actor.scope.to_dict() if actor else None,
            request_id=request_id,
            permission=permission,
            decision=decision,
            resource_scope=resource_scope.to_dict(),
        )
    )


def _authorize_v1_admin(
    handler: JsonHandler,
    request_id: str,
    *,
    audit_target: tuple[str, str | None, str] | None = None,
) -> AuthSession | None:
    session, auth_mode = _request_session(handler)
    if session is None:
        _record_v1_admin_denial(None, audit_target, "unauthorized", request_id)
        handler.write_json(HTTPStatus.UNAUTHORIZED, _error_payload("unauthorized", "authentication required", request_id))
        return None
    if auth_mode == "cookie" and handler.command not in {"GET", "HEAD", "OPTIONS"} and not _csrf_valid(handler, session.token):
        _record_v1_admin_denial(session.actor.actor_id, audit_target, "csrf_required", request_id)
        handler.write_json(HTTPStatus.FORBIDDEN, _error_payload("csrf_required", "missing or invalid CSRF token", request_id))
        return None
    if not _SESSIONS.is_platform_administrator(session.actor.actor_id):
        _record_v1_admin_denial(session.actor.actor_id, audit_target, "forbidden", request_id)
        handler.write_json(HTTPStatus.FORBIDDEN, _error_payload("forbidden", "Platform Administrator access required", request_id))
        return None
    return session


def _record_v1_admin_denial(
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
    _record_v1_admin_denial(
        session.actor.actor_id,
        audit_target,
        "fresh_auth_required",
        request_id,
        reason=reason,
    )
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
    if not parts or len(parts) > 2 or parts[0] not in {
        "users",
        "teams",
        "team-memberships",
        "role-bindings",
        "connector-enrollments",
        "clusters",
        "audit",
    }:
        return None
    return parts[0], unquote(parts[1]) if len(parts) == 2 else None


def _handle_v1_admin_get(handler: JsonHandler, collection: str) -> None:
    request_id = _request_id(handler)
    if _authorize_v1_admin(handler, request_id) is None:
        return
    if collection == "audit":
        handler.write_json(HTTPStatus.OK, {"request_id": request_id, "audit": _SESSIONS.list_admin_audit()})
        return
    if collection in {"connector-enrollments", "clusters"}:
        handler.write_json(HTTPStatus.OK, {"request_id": request_id, **connector_command_http.admin_state(_SESSIONS.connector_admin_state(), ConnectorCommands(_SESSIONS.database))})
        return
    handler.write_json(HTTPStatus.OK, {"request_id": request_id, **_SESSIONS.admin_state()})


def _handle_v1_admin_mutation(handler: JsonHandler, collection: str, target_id: str | None) -> None:
    request_id = _request_id(handler)
    action = f"{collection}_{'update' if target_id else 'create'}"
    session = _authorize_v1_admin(
        handler,
        request_id,
        audit_target=(collection, target_id, action),
    )
    if session is None:
        return
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        _record_v1_admin_denial(session.actor.actor_id, (collection, target_id, action), "invalid_request", request_id)
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    raw_reason = payload.pop("reason", "")
    reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
    if not reason:
        _record_v1_admin_denial(
            session.actor.actor_id,
            (collection, target_id, action),
            "reason_required",
            request_id,
            reason="missing",
        )
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("reason_required", "reason is required", request_id))
        return
    if not _require_fresh_auth(
        handler,
        session,
        request_id,
        audit_target=(collection, target_id, action),
        reason=reason,
    ):
        return
    if collection in {"connector-enrollments", "clusters"}:
        _handle_v1_connector_admin_mutation(
            handler,
            collection=collection,
            target_id=target_id,
            payload=payload,
            session=session,
            reason=reason,
            request_id=request_id,
        )
        return
    allowed_fields = {
        "users": ({"display_name", "email", "password", "active"} if target_id else {"username", "display_name", "email", "password"}),
        "teams": {"name", "description", "active"},
        "team-memberships": ({"active"} if target_id else {"user_id", "team_id"}),
        "role-bindings": ({"active"} if target_id else {"user_id", "role", "scope_type", "scope_id"}),
    }[collection]
    unknown = sorted(set(payload) - allowed_fields)
    text_fields = set(payload) - {"active", "scope_id"}
    invalid_types = any(not isinstance(payload[field], str) for field in text_fields)
    invalid_scope_id = "scope_id" in payload and payload["scope_id"] is not None and not isinstance(payload["scope_id"], str)
    if not payload or unknown or invalid_types or invalid_scope_id or ("active" in payload and not isinstance(payload["active"], bool)):
        _record_v1_admin_denial(
            session.actor.actor_id,
            (collection, target_id, action),
            "invalid_request",
            request_id,
            reason=reason,
        )
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", "invalid administration fields", request_id))
        return

    audit_target = target_id or str(payload.get("user_id") or payload.get("username") or payload.get("name") or "") or None
    before: dict[str, object] | None = None
    if target_id:
        try:
            before = {
                "users": _SESSIONS.user,
                "teams": _SESSIONS.team,
                "team-memberships": _SESSIONS.team_membership,
                "role-bindings": _SESSIONS.role_binding,
            }[collection](target_id)
        except IdentityError:
            pass
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
        status = {
            "not_found": HTTPStatus.NOT_FOUND,
            "last_admin": HTTPStatus.CONFLICT,
            "user_exists": HTTPStatus.CONFLICT,
            "team_exists": HTTPStatus.CONFLICT,
            "membership_exists": HTTPStatus.CONFLICT,
            "role_binding_exists": HTTPStatus.CONFLICT,
        }.get(exc.code, HTTPStatus.BAD_REQUEST)
        _SESSIONS.record_admin_audit(
            actor_id=session.actor.actor_id,
            target_type=collection,
            target_id=audit_target,
            action=action,
            reason=reason,
            before=before,
            after=None,
            result=exc.code,
            request_id=request_id,
        )
        handler.write_json(status, _error_payload(exc.code, exc.message, request_id))
        return
    except sqlite3.Error:
        handler.write_json(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            _error_payload("administration_write_failed", "administration change was not committed", request_id),
        )
        return
    handler.write_json(HTTPStatus.OK if target_id else HTTPStatus.CREATED, {"request_id": request_id, response_key: after})


def _handle_v1_connector_admin_mutation(
    handler: JsonHandler,
    *,
    collection: str,
    target_id: str | None,
    payload: dict[str, Any],
    session: AuthSession,
    reason: str,
    request_id: str,
) -> None:
    try:
        if collection == "connector-enrollments" and target_id is None:
            if set(payload) != {"connector_id", "cluster_id"} or not all(isinstance(value, str) for value in payload.values()):
                raise IdentityError("invalid_enrollment", "connector_id and cluster_id are required")
            enrollment, credential = _SESSIONS.create_connector_enrollment(
                connector_id=payload["connector_id"],
                cluster_id=payload["cluster_id"],
                actor_id=session.actor.actor_id,
                reason=reason,
                request_id=request_id,
            )
            handler.write_json(
                HTTPStatus.CREATED,
                {"request_id": request_id, "connector_enrollment": enrollment, "credential": credential},
            )
            return
        if collection == "connector-enrollments" and target_id is not None:
            if not payload or set(payload) - {"active", "rotate_credential"} or any(not isinstance(value, bool) for value in payload.values()):
                raise IdentityError("invalid_enrollment", "active and rotate_credential must be booleans")
            enrollment, credential = _SESSIONS.update_connector_enrollment(
                target_id,
                active=payload.get("active"),
                rotate_credential=bool(payload.get("rotate_credential")),
                actor_id=session.actor.actor_id,
                reason=reason,
                request_id=request_id,
            )
            if not enrollment["active"] or credential:
                _ROUTES.pop(str(enrollment["connector_id"]), None)
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
            cluster = _SESSIONS.update_cluster(
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
        attempted_target = target_id or ":".join(
            str(payload.get(field) or "").strip() for field in ("connector_id", "cluster_id")
        ).strip(":") or None
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
        status = {
            "not_found": HTTPStatus.NOT_FOUND,
            "enrollment_exists": HTTPStatus.CONFLICT,
        }.get(exc.code, HTTPStatus.BAD_REQUEST)
        handler.write_json(status, _error_payload(exc.code, exc.message, request_id))


def _handle_v1_connector_request(handler: JsonHandler, action: str) -> None:
    request_id = _request_id(handler)
    credential = _extract_bearer_token(handler.headers.get("Authorization")) or ""
    connector_id = ""
    cluster_id = ""
    try:
        if not credential:
            raise IdentityError("invalid_connector_credential", "Connector credential is invalid or revoked")
        payload = handler.read_json_body()
        allowed = (
            {"connector_id", "cluster_id", "namespace_scope", "capabilities"}
            if action == "register"
            else {"connector_id", "cluster_id", "status", "failure_summary"}
        )
        required = {"connector_id", "cluster_id"} if action == "register" else {"connector_id", "cluster_id", "status"}
        if set(payload) - allowed or not required <= set(payload):
            raise IdentityError("invalid_request", "invalid Connector request fields")
        connector_id = payload["connector_id"]
        cluster_id = payload["cluster_id"]
        if not isinstance(connector_id, str) or not isinstance(cluster_id, str):
            raise IdentityError("invalid_request", "connector_id and cluster_id are required")
        connector_id = connector_id.strip()
        cluster_id = cluster_id.strip()
        if not connector_id or not cluster_id:
            raise IdentityError("invalid_request", "connector_id and cluster_id are required")
        if action == "register":
            for field in ("namespace_scope", "capabilities"):
                value = payload.get(field, [])
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise IdentityError("invalid_request", f"{field} must be an array of strings")
            cluster, created = _SESSIONS.register_connector(
                credential,
                connector_id,
                cluster_id,
                request_id=request_id,
            )
            _ROUTES[connector_id] = ConnectorRoute(
                cluster_id=cluster_id,
                connector_id=connector_id,
                session_id=f"session-{uuid.uuid4().hex}",
            )
            handler.write_json(
                HTTPStatus.CREATED if created else HTTPStatus.OK,
                {"request_id": request_id, "status": "registered", "cluster": cluster},
            )
            return
        if not isinstance(payload["status"], str) or not isinstance(payload.get("failure_summary", ""), str):
            raise IdentityError("invalid_request", "status and failure_summary must be strings")
        cluster = _SESSIONS.record_connector_heartbeat(
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
    """Minimal Gateway HTTP surface used by image and compose smoke tests."""

    def do_GET(self) -> None:  # noqa: N802
        if self.is_metrics_request():
            self.write_metrics(APP_NAME)
            return
        parsed = urlparse(self.path)
        route_path = parsed.path
        query = parse_qs(parsed.query)
        if resource_catalog_http.dispatch(self, route_path, _SESSIONS, ResourceCatalog(_SESSIONS.database), ConnectorIdentity(_SESSIONS.database), _authorize_v1_admin, _require_fresh_auth, _request_id, _extract_bearer_token, _error_payload): return  # noqa: E701
        if approval_http.dispatch(self, route_path, _SESSIONS, Approvals(_SESSIONS.database), _authorize_v1_admin, _require_fresh_auth, _request_session, _csrf_valid, _request_id, _error_payload): return  # noqa: E701
        if incident_http.dispatch(self, route_path, _SESSIONS, _incident_service(), Approvals(_SESSIONS.database), _request_session, _request_id, _error_payload): return  # noqa: E701
        if investigation_event_http.dispatch_get(self, route_path, _SESSIONS, _incident_service(), InvestigationEvents(_SESSIONS.database), _request_session, _request_id, _error_payload): return  # noqa: E701
        admin_route = _v1_admin_route(route_path)
        if admin_route and admin_route[1] is None:
            _handle_v1_admin_get(self, admin_route[0])
            return

        if _serve_console_asset(self, route_path):
            return

        incident_id = _parse_incident_view_route(route_path)

        if incident_id is not None:
            if enforce_internal_auth(
                self,
                service_name=APP_NAME,
                allowed_service_account="aiops-diagnosis",
            ) is None:
                return
            status, payload = asyncio.run(read_incident_view(incident_id))
            self.write_json(status, payload)
            return

        if route_path == "/healthz":
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "connector_url": os.getenv("AIOPS_CONNECTOR_URL", ""),
                },
            )
            return

        if route_path == "/readyz":
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "registered_connectors": len(_ROUTES),
                },
            )
            return

        if route_path == "/connectivity/connector":
            connector_url = os.getenv("AIOPS_CONNECTOR_URL", "")
            if not connector_url:
                self.write_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "service": APP_NAME,
                        "status": "unavailable",
                        "peer": "connector",
                        "error": "AIOPS_CONNECTOR_URL is not set",
                    },
                )
                return
            status, payload = connectivity_payload(
                service=APP_NAME,
                peer_name="connector",
                peer_url=connector_url,
            )
            self.write_json(status, payload)
            return

        if route_path == "/connectors":
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "connectors": [asdict(route) for route in _ROUTES.values()],
                },
            )
            return

        if route_path == "/api/incidents/active":
            request_id = _request_id(self)
            actor = _authorize(self, PERMISSION_VIEW_INCIDENT, Scope(), request_id)
            if actor is None:
                return
            incidents = asyncio.run(incident_store.list_active())
            visible = [
                _overview_incident_row(incident)
                for incident in incidents
                if actor.can(PERMISSION_VIEW_INCIDENT, _incident_resource_scope(incident))
            ]
            _record_gateway_audit(
                actor,
                request_id=request_id,
                action="incident_active_query",
                result="success",
                permission=PERMISSION_VIEW_INCIDENT,
                decision="allow",
                resource_scope=Scope(),
            )
            self.write_json(
                HTTPStatus.OK,
                {"service": APP_NAME, "status": "ok", "request_id": request_id, "incidents": visible},
            )
            return

        if route_path == "/api/v1/actor":
            request_id = _request_id(self)
            session, _ = _request_session(self)
            if session is None:
                self.write_json(HTTPStatus.UNAUTHORIZED, _error_payload("unauthorized", "authentication required", request_id))
                return
            actor_view = _SESSIONS.actor_view(session.actor)
            self.write_json(
                HTTPStatus.OK,
                {"request_id": request_id, "actor": actor_view},
            )
            return

        if route_path == "/api/search":
            _handle_global_search(self, query)
            return

        if route_path == "/api/users":
            _handle_user_list(self)
            return

        if route_path == "/api/settings":
            _handle_settings_get(self)
            return

        if route_path == "/api/clusters":
            _handle_cluster_list(self)
            return

        if route_path == "/api/policies":
            _handle_policies_get(self)
            return

        if route_path == "/api/runbooks":
            _handle_runbook_list(self)
            return

        if route_path == "/api/agent-runs":
            _handle_agent_run_list(self)
            return

        if route_path == "/api/audit/chains":
            _handle_audit_chain_list(self, query)
            return

        audit_chain_id = _audit_chain_detail_id(route_path)
        if audit_chain_id:
            _handle_audit_chain_detail(self, audit_chain_id)
            return

        if route_path == "/api/audit/raw":
            _handle_audit_raw(self, query)
            return

        if route_path == "/api/audit/tombstones":
            _handle_audit_tombstones(self, query)
            return

        if route_path == "/api/notifications":
            _handle_console_notifications(self, query)
            return

        if route_path == "/api/notifications/stream":
            _handle_console_notifications_stream(self, query)
            return

        evidence_incident_id = _incident_evidence_id(route_path)
        if evidence_incident_id:
            _handle_incident_evidence(self, evidence_incident_id)
            return

        agent_run = _agent_run_route(route_path)
        if agent_run is not None:
            run_id, action = agent_run
            if action == "snapshot":
                _handle_agent_run_snapshot(self, run_id)
                return
            if action == "evidence":
                _handle_agent_run_evidence(self, run_id)
                return
            if action == "events":
                _handle_agent_run_events(self, run_id, query)
                return
            if action == "stream":
                _handle_agent_run_stream(self, run_id)
                return
            if action == "feedback":
                _handle_run_feedback(self, run_id)
                return

        if route_path == "/auth/me":
            request_id = _request_id(self)
            session, _ = _request_session(self)
            if session is None:
                self.write_json(HTTPStatus.UNAUTHORIZED, _error_payload("unauthorized", "missing or invalid bearer token", request_id))
                return
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "request_id": request_id,
                    "actor": session.actor.to_dict(),
                    "role_permission_matrix": role_permission_matrix(),
                },
            )
            return

        if route_path == "/auth/csrf":
            request_id = _request_id(self)
            session, _ = _request_session(self)
            if session is None:
                self.write_json(HTTPStatus.UNAUTHORIZED, _error_payload("unauthorized", "missing or invalid bearer token", request_id))
                return
            self.write_json(
                HTTPStatus.OK,
                {"service": APP_NAME, "status": "ok", "request_id": request_id, "csrf_token": _csrf_token(session.token)},
            )
            return

        if route_path in {"/notifications/types", "/api/notifications/types"}:
            self.write_json(HTTPStatus.OK, notification_center.template_catalog())
            return

        if route_path in {"/notifications/deliveries", "/api/notifications/deliveries"}:
            deliveries = notification_center.list_deliveries(
                status=_first_query_value(query, "status"),
                notification_type=_first_query_value(query, "notification_type", "type"),
                limit=_query_limit(query),
            )
            self.write_json(
                HTTPStatus.OK,
                {"service": APP_NAME, "status": "ok", "deliveries": deliveries},
            )
            return

        if route_path == "/api/approval-requests":
            request_id = _request_id(self)
            actor = _authorize(self, PERMISSION_VIEW_INCIDENT, Scope(), request_id)
            if actor is None:
                return
            approvals = approval_service.list_requests(
                status=_first_query_value(query, "status"),
                assigned_to=_first_query_value(query, "assigned_to"),
                team_id=_first_query_value(query, "team_id"),
                incident_id=_first_query_value(query, "incident_id"),
                session_id=_first_query_value(query, "session_id"),
                action_proposal_id=_first_query_value(query, "action_proposal_id"),
                risk_level=_first_query_value(query, "risk_level"),
                created_at_from=_query_float(query, "created_at_from"),
                created_at_to=_query_float(query, "created_at_to"),
                limit=_query_limit(query),
                offset=_query_offset(query),
            )
            visible = [
                approval
                for approval in approvals
                if actor.can(PERMISSION_VIEW_INCIDENT, _approval_resource_scope(approval))
            ]
            _record_approval_audit(
                actor,
                request_id=request_id,
                action="approval_query",
                result="success",
                permission=PERMISSION_VIEW_INCIDENT,
                decision="allow",
                resource_scope=Scope(),
            )
            self.write_json(
                HTTPStatus.OK,
                {"service": APP_NAME, "status": "ok", "request_id": request_id, "approval_requests": visible},
            )
            return

        if route_path == "/api/case-profile":
            request_id = _request_id(self)
            incident_id = _first_query_value(query, "incident_id")
            if not incident_id:
                self.write_json(
                    HTTPStatus.BAD_REQUEST,
                    _error_payload("invalid_request", "incident_id query parameter is required", request_id),
                )
                return
            actor = _authorize(self, PERMISSION_VIEW_INCIDENT, Scope(), request_id)
            if actor is None:
                return
            status, result = asyncio.run(read_case_profile(incident_id))
            self.write_json(status, {"service": APP_NAME, "request_id": request_id, **result})
            return

        process_stream_incident_id = _diagnosis_process_stream_incident_id(route_path)
        if process_stream_incident_id:
            _handle_diagnosis_process_stream(self, process_stream_incident_id)
            return

        process_incident_id = _diagnosis_process_incident_id(route_path)
        if process_incident_id:
            request_id = _request_id(self)
            try:
                incident = asyncio.run(incident_store.get_incident(process_incident_id))
            except ValueError:
                self.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", "incident not found", request_id))
                return
            scope = _incident_resource_scope(incident)
            actor = _authorize_resource(self, PERMISSION_VIEW_INCIDENT, scope, request_id)
            if actor is None:
                return
            status, payload = _read_scoped_diagnosis_process(process_incident_id, incident, actor)
            if status == HTTPStatus.OK:
                _record_gateway_audit(
                    actor,
                    request_id=request_id,
                    action="diagnosis_process_view",
                    result="success",
                    incident_id=process_incident_id,
                    permission=PERMISSION_VIEW_INCIDENT,
                    decision="allow",
                    resource_scope=scope,
                )
                self.write_json(
                    status,
                    {"service": APP_NAME, "request_id": request_id, **payload},
                )
                return
            self.write_json(status, _error_payload(str(payload.get("status") or "failed"), str(payload.get("error") or "diagnosis process not found"), request_id))
            return

        workbench_incident_id = _incident_workbench_id(route_path)
        if workbench_incident_id:
            _handle_incident_workbench(self, workbench_incident_id)
            return

        report_incident_id = _incident_report_id(route_path)
        if report_incident_id:
            _handle_incident_report_get(self, report_incident_id, query)
            return

        execution_approval_id = _approval_execution_detail_id(route_path)
        if execution_approval_id:
            request_id = _request_id(self)
            approval = approval_service.get_request(execution_approval_id)
            if approval is None:
                self.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", "approval request not found", request_id))
                return
            scope = _approval_resource_scope(approval)
            actor = _authorize_resource(self, PERMISSION_VIEW_INCIDENT, scope, request_id)
            if actor is None:
                return
            execution = approval_execution_service.get_execution(execution_approval_id)
            _record_approval_execution_audit(
                actor,
                request_id=request_id,
                action="approval_execution_get",
                result="success",
                decision="allow",
                resource_scope=scope,
                approval=approval,
                execution=execution,
            )
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "request_id": request_id,
                    "execution_grant": approval.get("execution_grant"),
                    "execution": execution,
                },
            )
            return

        action_detail_id = _action_detail_id(route_path)
        if action_detail_id:
            _handle_action_detail(self, action_detail_id)
            return

        detail_id = _approval_detail_id(route_path)
        if detail_id:
            request_id = _request_id(self)
            approval = approval_service.get_request(detail_id)
            if approval is None:
                self.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", "approval request not found", request_id))
                return
            scope = _approval_resource_scope(approval)
            actor = _authorize_resource(self, PERMISSION_VIEW_INCIDENT, scope, request_id)
            if actor is None:
                return
            _record_approval_audit(
                actor,
                request_id=request_id,
                action="approval_get",
                result="success",
                permission=PERMISSION_VIEW_INCIDENT,
                decision="allow",
                resource_scope=scope,
                approval=approval,
            )
            self.write_json(
                HTTPStatus.OK,
                {"service": APP_NAME, "status": "ok", "request_id": request_id, "approval_request": approval},
            )
            return

        self.write_not_found()

    def do_POST(self) -> None:  # noqa: N802
        route_path = urlparse(self.path).path
        if approval_http.dispatch(self, route_path, _SESSIONS, Approvals(_SESSIONS.database), _authorize_v1_admin, _require_fresh_auth, _request_session, _csrf_valid, _request_id, _error_payload): return  # noqa: E701
        if connector_command_http.dispatch(self, route_path, ConnectorCommands(_SESSIONS.database), ConnectorIdentity(_SESSIONS.database), _authorize_v1_admin, _request_id, _extract_bearer_token, _error_payload): return  # noqa: E701
        if diagnosis_delivery_http.dispatch(self, route_path, DiagnosisDelivery(_SESSIONS.database)): return  # noqa: E701
        if resource_catalog_http.dispatch(self, route_path, _SESSIONS, ResourceCatalog(_SESSIONS.database), ConnectorIdentity(_SESSIONS.database), _authorize_v1_admin, _require_fresh_auth, _request_id, _extract_bearer_token, _error_payload): return  # noqa: E701
        if investigation_event_http.dispatch_post(self, route_path, _SESSIONS, _incident_service(), InvestigationEvents(_SESSIONS.database), _request_session, _csrf_valid, _request_id, _error_payload): return  # noqa: E701
        admin_route = _v1_admin_route(route_path)
        if admin_route and admin_route[1] is None and admin_route[0] != "audit":
            _handle_v1_admin_mutation(self, admin_route[0], None)
            return
        if route_path in {"/connectors/register", "/api/v1/connectors/register", "/api/v1/connectors/heartbeat"}:
            _handle_v1_connector_request(self, route_path.rsplit("/", 1)[-1])
            return

        if route_path == "/api/case-profile":
            request_id = _request_id(self)
            try:
                payload = self.read_json_body()
            except (TypeError, ValueError) as exc:
                self.write_json(
                    HTTPStatus.BAD_REQUEST,
                    _error_payload("invalid_request", str(exc), request_id),
                )
                return
            scope = _resource_scope_from_payload(payload)
            actor = _authorize(self, PERMISSION_VIEW_INCIDENT, scope, request_id)
            if actor is None:
                return
            status, result = asyncio.run(apply_case_profile(payload))
            _record_gateway_audit(
                actor,
                request_id=request_id,
                action="case_profile_backfill",
                result="ok" if result.get("ok") else result.get("status", "failed"),
                incident_id=str(payload.get("incident_id") or "").strip() or None,
                permission=PERMISSION_VIEW_INCIDENT,
                decision="allow",
                resource_scope=scope,
            )
            self.write_json(status, {"service": APP_NAME, "request_id": request_id, **result})
            return

        if route_path == "/api/users":
            _handle_user_create(self)
            return

        if route_path == "/api/settings/preview":
            _handle_settings_preview(self)
            return

        if route_path == "/api/settings":
            _handle_settings_save(self)
            return

        if route_path == "/api/settings/rollback":
            _handle_settings_rollback(self)
            return

        if route_path == "/api/clusters":
            _handle_cluster_save(self)
            return

        if route_path == "/api/clusters/runtime":
            _handle_cluster_runtime(self)
            return

        if route_path == "/api/policies/test":
            _handle_policy_test(self)
            return

        runbook_toggle_id = _runbook_toggle_id(route_path)
        if runbook_toggle_id:
            _handle_runbook_toggle(self, runbook_toggle_id)
            return

        if route_path in {"/api/evidence/query", "/api/evidence/agent-query"}:
            _handle_evidence_query(self, agent=route_path.endswith("/agent-query"))
            return

        if route_path == "/api/agent-runs":
            _handle_agent_run_create(self)
            return

        kb_approve = _incident_kb_candidate_approve(route_path)
        if kb_approve is not None:
            incident_id, candidate_id = kb_approve
            _handle_kb_candidate_approve(self, incident_id, candidate_id)
            return

        report_action = _incident_report_action(route_path)
        if report_action is not None:
            incident_id, action = report_action
            if action == "draft":
                _handle_incident_report_draft(self, incident_id)
                return
            if action == "publish":
                _handle_incident_report_publish(self, incident_id)
                return
            if action == "kb-candidates":
                _handle_incident_report_kb_candidates(self, incident_id)
                return

        if route_path == "/api/feedback":
            _handle_feedback_create(self)
            return

        agent_run = _agent_run_route(route_path)
        if agent_run is not None:
            run_id, action = agent_run
            if action == "messages":
                _handle_agent_run_message(self, run_id)
                return
            if action == "promote":
                _handle_agent_run_promote(self, run_id)
                return
            if action == "archive":
                _handle_agent_run_archive(self, run_id)
                return
            if action == "delete":
                _handle_agent_run_delete(self, run_id)
                return
            if action == "conversation":
                _handle_agent_run_conversation(self, run_id)
                return

        disabled_user = _user_action_username(route_path, "disable")
        if disabled_user:
            _handle_user_disable(self, disabled_user)
            return

        reset_user = _user_action_username(route_path, "reset-password")
        if reset_user:
            _handle_user_reset_password(self, reset_user)
            return

        if route_path == "/auth/login":
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

            _record_gateway_audit(actor, request_id=request_id, action=f"{actor.auth_source}_login", result="success")
            platform_administrator = _SESSIONS.is_platform_administrator(actor.actor_id)
            response = {
                "service": APP_NAME,
                "status": "ok",
                "request_id": request_id,
                "expires_at": session.expires_at,
                "actor": _SESSIONS.actor_view(actor) if payload.get("session_mode") == "cookie" or platform_administrator else actor.to_dict(),
            }
            if payload.get("session_mode") != "cookie":
                response["token"] = session.token
                if not platform_administrator:
                    response["role_permission_matrix"] = role_permission_matrix()
            self.write_json(
                HTTPStatus.OK,
                response,
                headers={"Set-Cookie": _session_cookie_header(self, session.token, _SESSIONS.ttl_seconds)},
            )
            return

        if route_path == "/auth/reauth":
            request_id = _request_id(self)
            session, auth_mode = _request_session(self)
            if session is None:
                self.write_json(HTTPStatus.UNAUTHORIZED, _error_payload("unauthorized", "authentication required", request_id))
                return
            if auth_mode == "cookie" and not _csrf_valid(self, session.token):
                self.write_json(HTTPStatus.FORBIDDEN, _error_payload("csrf_required", "missing or invalid CSRF token", request_id))
                return
            try:
                payload = self.read_json_body()
                actor = _identity_provider().login(session.actor.username, str(payload.get("password") or ""))
            except IdentityError as exc:
                self.write_json(HTTPStatus.UNAUTHORIZED, _error_payload(exc.code, exc.message, request_id))
                return
            if actor.actor_id != session.actor.actor_id:
                self.write_json(HTTPStatus.UNAUTHORIZED, _error_payload("invalid_credentials", "identity mismatch", request_id))
                return
            _SESSIONS.mark_fresh(session.token)
            self.write_json(HTTPStatus.OK, {"status": "ok", "request_id": request_id})
            return

        if route_path == "/auth/logout":
            request_id = _request_id(self)
            session, auth_mode = _request_session(self)
            if session is not None:
                if auth_mode == "cookie" and not _csrf_valid(self, session.token):
                    self.write_json(
                        HTTPStatus.FORBIDDEN,
                        _error_payload("csrf_required", "missing or invalid CSRF token", request_id),
                    )
                    return
                _SESSIONS.revoke(session.token)
            self.write_json(
                HTTPStatus.OK,
                {"service": APP_NAME, "status": "ok", "request_id": request_id},
                headers={"Set-Cookie": _clear_session_cookie_header(self)},
            )
            return

        if route_path == "/auth/sync":
            request_id = _request_id(self)
            actor = _authorize(self, PERMISSION_SYNC_LDAP, Scope(), request_id)
            if actor is None:
                return
            try:
                users = _identity_provider().sync_users()
            except IdentityError as exc:
                self.write_json(HTTPStatus.SERVICE_UNAVAILABLE, _error_payload(exc.code, exc.message, request_id))
                _record_gateway_audit(actor, request_id=request_id, action="ldap_sync", result=exc.code)
                return
            _record_gateway_audit(actor, request_id=request_id, action="ldap_sync", result="success")
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "request_id": request_id,
                    "users": [user.to_dict() for user in users],
                },
            )
            return

        if route_path == "/incidents/query":
            request_id = _request_id(self)
            try:
                payload = self.read_json_body()
            except (TypeError, ValueError) as exc:
                self.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
                return
            scope = _resource_scope_from_payload(payload)
            actor = _authorize(self, PERMISSION_VIEW_INCIDENT, scope, request_id)
            if actor is None:
                return
            incidents = asyncio.run(incident_store.list_active())
            filtered = [
                incident
                for incident in incidents
                if actor.can(PERMISSION_VIEW_INCIDENT, _incident_resource_scope(incident))
            ]
            _record_gateway_audit(actor, request_id=request_id, action="incident_query", result="success")
            self.write_json(
                HTTPStatus.OK,
                {"service": APP_NAME, "status": "ok", "request_id": request_id, "incidents": filtered},
            )
            return

        if route_path == "/audit/query":
            request_id = _request_id(self)
            try:
                payload = self.read_json_body()
            except (TypeError, ValueError) as exc:
                self.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
                return
            actor = _authorize(self, PERMISSION_QUERY_AUDIT, _resource_scope_from_payload(payload), request_id)
            if actor is None:
                return
            rows = asyncio.run(
                audit_log.query_audit(
                    who=payload.get("who"),
                    cluster=payload.get("cluster"),
                    namespace=payload.get("namespace"),
                    limit=int(payload.get("limit") or 100),
                )
            )
            _record_gateway_audit(actor, request_id=request_id, action="audit_query", result="success")
            self.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "rows": rows})
            return

        if route_path == "/k8s/read":
            request_id = _request_id(self)
            service_identity = None
            if self.headers.get("Authorization") and _request_session(self)[0] is None:
                service_identity = enforce_internal_auth(
                    self,
                    service_name=APP_NAME,
                    allowed_service_account="aiops-diagnosis",
                )
                if service_identity is None:
                    return
            try:
                payload = self.read_json_body()
                scope = _resource_scope_from_payload(payload)
                actor = (
                    _DIAGNOSIS_SERVICE_ACTOR
                    if service_identity
                    else _authorize_resource(self, PERMISSION_K8S_READ, scope, request_id)
                )
                if actor is None:
                    return
                envelope = build_read_envelope(payload)
            except (TypeError, ValueError) as exc:
                self.write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"service": APP_NAME, "status": "invalid", "error": str(exc)},
                )
                return

            connector_url = os.getenv("AIOPS_CONNECTOR_URL", "")
            if not connector_url:
                self.write_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "service": APP_NAME,
                        "status": "failed",
                        "request_id": request_id,
                        "error": {"code": "connector_offline", "message": "AIOPS_CONNECTOR_URL is not set"},
                    },
                )
                return

            route = next((item for item in _ROUTES.values() if item.cluster_id == envelope.cluster_id), None)
            if route is None:
                self.write_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "service": APP_NAME,
                        "status": "failed",
                        "error": {"code": "connector_offline", "message": "no connector route for cluster"},
                    },
                )
                return
            result = dispatch_read_envelope(envelope, route=route, connector_url=connector_url)
            _record_gateway_audit(
                actor,
                request_id=request_id,
                action="k8s_read",
                result=result.status,
                cluster=envelope.cluster_id,
                namespace=envelope.namespace,
                permission=PERMISSION_K8S_READ,
                decision="allow",
                resource_scope=_resource_scope_from_payload(payload),
            )
            status = HTTPStatus.OK if result.status in {"succeeded", "failed"} else HTTPStatus.BAD_REQUEST
            response_payload = result.to_dict()
            response_payload["audit"] = actor.audit_context(request_id)
            self.write_json(status, response_payload)
            return

        if route_path == "/webhooks/alertmanager":
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length > 0 else b""
            status, payload = handle_http_request(body, dict(self.headers), _incident_service())
            self.write_json(status, payload)
            return

        if route_path in {"/notifications/send", "/api/notifications/send"}:
            try:
                payload = self.read_json_body()
            except (TypeError, ValueError) as exc:
                self.write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": str(exc)})
                return
            status, result = notification_center.handle_send_http_request(payload)
            self.write_json(status, result)
            return

        if route_path == "/api/notifications/retry":
            _handle_console_notification_retry(self)
            return

        if route_path == "/notifications/retry":
            try:
                payload = self.read_json_body()
            except (TypeError, ValueError) as exc:
                self.write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": str(exc)})
                return
            status, result = notification_center.handle_retry_http_request(payload)
            self.write_json(status, result)
            return

        if route_path == "/api/approval-requests":
            _handle_approval_create(self)
            return

        if route_path == "/api/actions/propose":
            _handle_action_propose(self)
            return

        control_incident_id = _incident_control_id(route_path)
        if control_incident_id:
            _handle_incident_control(self, control_incident_id)
            return

        execute_id = _approval_execute_id(route_path)
        if execute_id:
            _handle_approval_execute(self, execute_id)
            return

        action_match = _approval_action(route_path)
        if action_match:
            approval_id, action = action_match
            _handle_approval_decision(self, approval_id, action)
            return

        self.write_not_found()

    def do_PATCH(self) -> None:  # noqa: N802
        route_path = urlparse(self.path).path
        if approval_http.dispatch(self, route_path, _SESSIONS, Approvals(_SESSIONS.database), _authorize_v1_admin, _require_fresh_auth, _request_session, _csrf_valid, _request_id, _error_payload): return  # noqa: E701
        if resource_catalog_http.dispatch(self, route_path, _SESSIONS, ResourceCatalog(_SESSIONS.database), ConnectorIdentity(_SESSIONS.database), _authorize_v1_admin, _require_fresh_auth, _request_id, _extract_bearer_token, _error_payload): return  # noqa: E701
        admin_route = _v1_admin_route(route_path)
        if admin_route and admin_route[1] is not None and admin_route[0] != "audit":
            _handle_v1_admin_mutation(self, admin_route[0], admin_route[1])
            return
        cluster_id = _cluster_detail_id(route_path)
        if cluster_id:
            _handle_cluster_save(self, cluster_id)
            return
        username = _user_detail_username(route_path)
        if username:
            _handle_user_update(self, username)
            return
        self.write_not_found()


def _parse_incident_view_route(path: str) -> str | None:
    parts = [part for part in urlparse(path).path.split("/") if part]
    if len(parts) == 2 and parts[0] == "incidents" and parts[1].strip():
        return parts[1].strip()
    return None


def _overview_incident_row(incident: dict[str, Any]) -> dict[str, Any]:
    created_at = incident.get("created_at")
    return {
        "incident_id": incident.get("id"),
        "title": incident.get("summary") or incident.get("alert_name") or incident.get("id"),
        "severity": incident.get("severity") or "unknown",
        "status": incident.get("status") or "unknown",
        "cluster": incident.get("cluster"),
        "namespace": incident.get("namespace"),
        "service": incident.get("service") or incident.get("service_id") or "unknown service",
        "team": incident.get("team") or incident.get("team_id"),
        "impact": incident.get("alert_name") or "-",
        "age": _age_label(created_at),
        "tags": " ".join(
            str(value)
            for value in [
                incident.get("severity"),
                incident.get("status"),
                incident.get("service"),
                incident.get("namespace"),
            ]
            if value
        ),
    }


def _age_label(created_at: Any) -> str:
    try:
        seconds = max(0, int(__import__("time").time() - float(created_at)))
    except (TypeError, ValueError):
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _handle_global_search(handler: JsonHandler, query: dict[str, list[str]]) -> None:
    request_id = _request_id(handler)
    actor = _authorize(handler, PERMISSION_VIEW_EVIDENCE, Scope(), request_id)
    if actor is None:
        return
    q = _first_query_value(query, "q", "query") or ""
    kind = _first_query_value(query, "type", "kind")
    results = _global_search_results(actor, q=q, kind=kind, limit=_query_limit(query))
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="global_search",
        result="success",
        permission=PERMISSION_VIEW_EVIDENCE,
        decision="allow",
        resource_scope=Scope(),
    )
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "query": q, "results": results},
    )


def _global_search_results(actor: Actor, *, q: str, kind: str | None, limit: int) -> list[dict[str, Any]]:
    term = q.strip().lower()
    kind = kind.strip().lower() if kind else None
    results: list[dict[str, Any]] = []
    resources: dict[tuple[str, str], dict[str, Any]] = {}

    def add(result: dict[str, Any]) -> None:
        if len(results) >= limit:
            return
        result_kind = str(result["type"])
        if kind and kind not in {result_kind, f"{result_kind}s"}:
            return
        fields = [result.get("id"), result.get("title"), result.get("subtitle"), result.get("status"), result.get("route")]
        fields.extend((result.get("scope") or {}).values() if isinstance(result.get("scope"), dict) else [])
        if term and not any(term in str(field or "").lower() for field in fields):
            return
        results.append(result)

    def remember(scope: dict[str, Any], *, route_area: str = "incidents") -> None:
        for resource_kind, key in (("cluster", "cluster"), ("namespace", "namespace"), ("service", "service"), ("team", "team")):
            value = str(scope.get(key) or scope.get(f"{key}_id") or "").strip()
            if not value:
                continue
            resources[(resource_kind, value)] = {
                "type": resource_kind,
                "id": value,
                "title": value,
                "subtitle": resource_kind,
                "route": _global_search_resource_route(resource_kind, value, route_area=route_area),
                "status": "visible",
                "scope": {key: value},
            }

    for incident in asyncio.run(incident_store.list_active()):
        scope = _incident_resource_scope(incident)
        if not actor.can(PERMISSION_VIEW_INCIDENT, scope):
            continue
        remember(
            {
                "cluster": incident.get("cluster"),
                "namespace": incident.get("namespace"),
                "service": incident.get("service") or incident.get("service_id"),
                "team": incident.get("team") or incident.get("team_id"),
            }
        )
        incident_id = str(incident.get("id") or "")
        add(
            {
                "type": "incident",
                "id": incident_id,
                "title": incident.get("summary") or incident.get("alert_name") or incident_id,
                "subtitle": incident.get("alert_name") or incident.get("service") or "",
                "route": f"/incidents/{quote(incident_id)}",
                "status": incident.get("status") or "unknown",
                "scope": scope.to_dict(),
            }
        )
    for run in asyncio.run(agent_run_service.list_runs()):
        scope = _agent_run_scope_from_run(run)
        if not actor.can(PERMISSION_VIEW_INCIDENT, scope):
            continue
        raw_scope = run.get("scope") if isinstance(run.get("scope"), dict) else {}
        remember(raw_scope)
        run_id = str(run.get("run_id") or "")
        add(
            {
                "type": "agent_run",
                "id": run_id,
                "title": run.get("title") or run_id,
                "subtitle": run.get("incident_id") or run.get("conversation_status") or "",
                "route": f"/agent-runs/{quote(run_id)}",
                "status": run.get("status") or "unknown",
                "scope": scope.to_dict(),
            }
        )
        conversation_id = str(run.get("conversation_id") or "")
        if conversation_id:
            add(
                {
                    "type": "conversation",
                    "id": conversation_id,
                    "title": run.get("title") or conversation_id,
                    "subtitle": run_id,
                    "route": f"/agent-runs/{quote(run_id)}",
                    "status": run.get("conversation_status") or "unknown",
                    "scope": scope.to_dict(),
                }
            )

    if PERMISSION_APPROVE_ACTION in actor.permissions():
        for approval in approval_service.list_requests(limit=500):
            scope = _approval_resource_scope(approval)
            if not actor.can(PERMISSION_APPROVE_ACTION, scope):
                continue
            raw_scope = approval.get("resource_scope") if isinstance(approval.get("resource_scope"), dict) else {}
            remember(raw_scope)
            approval_id = str(approval.get("approval_id") or "")
            add(
                {
                    "type": "approval",
                    "id": approval_id,
                    "title": approval.get("action_summary") or approval_id,
                    "subtitle": approval.get("requested_by") or "",
                    "route": f"/approvals/{quote(approval_id)}",
                    "status": approval.get("status") or "unknown",
                    "scope": scope.to_dict(),
                }
            )

    if PERMISSION_QUERY_AUDIT in actor.permissions():
        for chain in asyncio.run(audit_chain_service.list_chains(actor, limit=500)):
            raw_scope = chain.get("scope") if isinstance(chain.get("scope"), dict) else {}
            remember(raw_scope, route_area="audit")
            chain_id = str(chain.get("chain_id") or "")
            add(
                {
                    "type": "audit_chain",
                    "id": chain_id,
                    "title": chain.get("requested_action") or chain_id,
                    "subtitle": chain.get("approval_id") or "",
                    "route": f"/audit/{quote(chain_id)}",
                    "status": chain.get("responsibility_status") or "unknown",
                    "scope": raw_scope,
                }
            )

    if actor.can(PERMISSION_VIEW_USERS, Scope()):
        for user in _identity_store().list_user_records():
            scope = user.get("scope") if isinstance(user.get("scope"), dict) else {}
            if not actor.has_role(ROLE_ADMIN) and not actor.scope.matches(Scope.from_mapping(scope)):
                continue
            for key in ("clusters", "namespaces", "services", "teams"):
                for value in scope.get(key, []) or []:
                    remember({key.removesuffix("s"): value}, route_area="users")
            username = str(user.get("username") or "")
            add(
                {
                    "type": "user",
                    "id": username,
                    "title": user.get("display_name") or username,
                    "subtitle": ",".join(user.get("roles") or []),
                    "route": f"/users/{quote(username)}",
                    "status": "disabled" if user.get("disabled") else "enabled",
                    "scope": scope,
                }
            )

    if actor.can(PERMISSION_VIEW_SETTINGS, Scope()):
        for cluster in cluster_registry.list_clusters():
            cluster_scope = resource_scope(
                cluster=_scope_value(cluster.get("cluster_id")),
                namespace=_scope_value(cluster.get("default_namespace_scope")),
                team=_scope_value(cluster.get("owner_team")),
            )
            if not actor.has_role(ROLE_ADMIN) and not actor.scope.matches(cluster_scope):
                continue
            remember(
                {
                    "cluster": cluster.get("cluster_id"),
                    "namespace": cluster.get("default_namespace_scope"),
                    "team": cluster.get("owner_team"),
                },
                route_area="clusters",
            )

    for result in resources.values():
        add(result)
    return results


def _global_search_resource_route(resource_kind: str, value: str, *, route_area: str) -> str:
    encoded_kind = quote(resource_kind)
    encoded_value = quote(value)
    if route_area == "clusters":
        return f"/clusters?{encoded_kind}={encoded_value}"
    if route_area == "users":
        return f"/users?{encoded_kind}={encoded_value}"
    if route_area == "audit":
        return f"/audit?{encoded_kind}={encoded_value}"
    return f"/incidents?{encoded_kind}={encoded_value}"


def _diagnosis_process_incident_id(path: str) -> str | None:
    parts = [part for part in urlparse(path).path.split("/") if part]
    if len(parts) == 4 and parts[:2] == ["api", "incidents"] and parts[3] == "diagnosis-process":
        return parts[2].strip() or None
    return None


def _diagnosis_process_stream_incident_id(path: str) -> str | None:
    parts = [part for part in urlparse(path).path.split("/") if part]
    if len(parts) == 5 and parts[:2] == ["api", "incidents"] and parts[3:] == ["diagnosis-process", "stream"]:
        return parts[2].strip() or None
    return None


def _audit_chain_detail_id(path: str) -> str | None:
    parts = [part for part in urlparse(path).path.split("/") if part]
    if len(parts) == 4 and parts[:3] == ["api", "audit", "chains"]:
        return unquote(parts[3]).strip() or None
    return None


def _first_query_value(query: dict[str, list[str]], *keys: str) -> str | None:
    for key in keys:
        values = query.get(key)
        if values and values[0].strip():
            return values[0].strip()
    return None


def _query_limit(query: dict[str, list[str]]) -> int:
    values = query.get("limit")
    if not values:
        return 100
    try:
        return max(1, min(int(values[0]), 500))
    except ValueError:
        return 100


def _query_offset(query: dict[str, list[str]]) -> int:
    values = query.get("offset")
    if not values:
        return 0
    try:
        return max(0, int(values[0]))
    except ValueError:
        return 0


def _query_float(query: dict[str, list[str]], key: str) -> float | None:
    values = query.get(key)
    if not values or not values[0].strip():
        return None
    try:
        return float(values[0])
    except ValueError:
        return None


def _user_detail_username(route_path: str) -> str | None:
    prefix = "/api/users/"
    if not route_path.startswith(prefix):
        return None
    suffix = route_path[len(prefix):].strip("/")
    if not suffix or "/" in suffix:
        return None
    return unquote(suffix).strip() or None


def _user_action_username(route_path: str, action: str) -> str | None:
    prefix = "/api/users/"
    if not route_path.startswith(prefix):
        return None
    parts = [part for part in route_path[len(prefix):].split("/") if part]
    if len(parts) == 2 and parts[1] == action:
        return unquote(parts[0]).strip() or None
    return None


def _cluster_detail_id(route_path: str) -> str | None:
    prefix = "/api/clusters/"
    if not route_path.startswith(prefix):
        return None
    suffix = route_path[len(prefix) :].strip("/")
    if not suffix or "/" in suffix or suffix == "runtime":
        return None
    return unquote(suffix).strip() or None


def _runbook_toggle_id(route_path: str) -> str | None:
    prefix = "/api/runbooks/"
    suffix = "/toggle"
    if not route_path.startswith(prefix) or not route_path.endswith(suffix):
        return None
    raw = route_path[len(prefix):-len(suffix)].strip("/")
    return unquote(raw).strip() or None


def _identity_error_status(exc: IdentityError) -> HTTPStatus:
    if exc.code in {"invalid_user", "scope_required"}:
        return HTTPStatus.BAD_REQUEST
    if exc.code == "not_found":
        return HTTPStatus.NOT_FOUND
    if exc.code == "ldap_password_reset_forbidden":
        return HTTPStatus.FORBIDDEN
    if exc.code in {"user_exists", "last_admin"}:
        return HTTPStatus.CONFLICT
    return HTTPStatus.BAD_REQUEST


def _recent_user_audits() -> dict[str, dict[str, Any]]:
    rows = asyncio.run(audit_log.query_audit(cluster="identity", limit=200))
    recent: dict[str, dict[str, Any]] = {}
    for row in rows:
        username = str(row.get("namespace") or "").strip()
        action = str(row.get("what") or "").strip()
        if not username or username in recent or not action.startswith("user_"):
            continue
        recent[username] = {
            "action": action,
            "result": row.get("result"),
            "actor": row.get("actor") or row.get("who"),
            "request_id": row.get("request_id"),
            "when_ts": row.get("when_ts"),
        }
    return recent


def _with_recent_user_audit(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recent = _recent_user_audits()
    return [{**record, "recent_permission_audit": recent.get(record["username"])} for record in records]


def _record_user_management_audit(
    actor: Actor,
    *,
    request_id: str,
    username: str,
    action: str,
    result: str,
    decision: str,
) -> None:
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action=action,
        result=result,
        cluster="identity",
        namespace=username,
        permission=PERMISSION_MANAGE_USERS,
        decision=decision,
        resource_scope=Scope(),
    )


def _agent_run_route(route_path: str) -> tuple[str, str] | None:
    prefix = "/api/agent-runs/"
    if not route_path.startswith(prefix):
        return None
    parts = [unquote(part) for part in route_path[len(prefix) :].split("/") if part]
    if len(parts) == 1:
        return parts[0], "snapshot"
    if len(parts) == 2 and parts[1] in {"events", "stream", "messages", "promote", "archive", "delete", "conversation", "feedback", "evidence"}:
        return parts[0], parts[1]
    return None


def _agent_run_error(handler: JsonHandler, exc: agent_run_service.AgentRunServiceError, request_id: str) -> None:
    handler.write_json(exc.status, _error_payload(exc.code, exc.message, request_id))


def _can_operate_incident(actor: Actor) -> bool:
    return actor.has_role(ROLE_OPERATOR) or actor.has_role(ROLE_ADMIN)


def _require_incident_operator(handler: JsonHandler, actor: Actor, request_id: str) -> bool:
    if _can_operate_incident(actor):
        return True
    handler.write_json(HTTPStatus.FORBIDDEN, _error_payload("forbidden", "operator role is required", request_id))
    return False


def _agent_run_snapshot_for_auth(run_id: str, request_id: str) -> tuple[dict[str, Any] | None, Scope, agent_run_service.AgentRunServiceError | None]:
    try:
        snapshot = asyncio.run(agent_run_service.snapshot(run_id))
    except agent_run_service.AgentRunServiceError as exc:
        return None, Scope(), exc
    return snapshot, _agent_run_scope_from_run(snapshot["run"]), None


def _handle_agent_run_list(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, Scope(), request_id)
    if actor is None:
        return
    runs = asyncio.run(agent_run_service.list_runs())
    visible = [run for run in runs if actor.can(PERMISSION_VIEW_INCIDENT, _agent_run_scope_from_run(run))]
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="agent_run_list",
        result="success",
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=Scope(),
    )
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "agent_runs": visible})


def _handle_agent_run_create(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    scope = _agent_run_scope_from_payload(payload)
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    if not _require_incident_operator(handler, actor, request_id):
        return
    disabled = _disabled_runbook_for_payload(payload)
    if disabled is not None:
        _record_gateway_audit(
            actor,
            request_id=request_id,
            action="agent_run_create",
            result="runbook_disabled",
            cluster=disabled.get("scope", {}).get("cluster"),
            namespace=disabled.get("scope", {}).get("namespace"),
            incident_id=str(payload.get("incident_id") or "") or None,
            permission=PERMISSION_VIEW_INCIDENT,
            decision="deny",
            resource_scope=scope,
        )
        handler.write_json(HTTPStatus.CONFLICT, _error_payload("runbook_disabled", "runbook skeleton is disabled", request_id))
        return
    knowledge_context = asyncio.run(report_service.approved_kb_entries(_agent_run_scope_dict(payload)))
    payload = {**payload, "knowledge_context": knowledge_context}
    try:
        snapshot = asyncio.run(agent_run_service.create_run(payload, actor_id=actor.actor_id))
    except agent_run_service.AgentRunServiceError as exc:
        _agent_run_error(handler, exc, request_id)
        return
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="agent_run_create",
        result="success",
        cluster=snapshot["run"]["scope"].get("cluster"),
        namespace=snapshot["run"]["scope"].get("namespace"),
        incident_id=snapshot["run"].get("incident_id"),
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=scope,
    )
    if knowledge_context:
        _record_gateway_audit(
            actor,
            request_id=request_id,
            action="kb_agent_context_retrieve",
            result="success",
            cluster=snapshot["run"]["scope"].get("cluster"),
            namespace=snapshot["run"]["scope"].get("namespace"),
            incident_id=snapshot["run"].get("incident_id"),
            permission=PERMISSION_VIEW_INCIDENT,
            decision="allow",
            resource_scope=scope,
        )
    handler.write_json(HTTPStatus.CREATED, {"service": APP_NAME, "status": "ok", "request_id": request_id, "snapshot": snapshot})


def _handle_agent_run_snapshot(handler: JsonHandler, run_id: str) -> None:
    request_id = _request_id(handler)
    snapshot, scope, exc = _agent_run_snapshot_for_auth(run_id, request_id)
    if exc is not None:
        _agent_run_error(handler, exc, request_id)
        return
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="agent_run_snapshot",
        result="success",
        cluster=snapshot["run"]["scope"].get("cluster"),
        namespace=snapshot["run"]["scope"].get("namespace"),
        incident_id=snapshot["run"].get("incident_id"),
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=scope,
    )
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "snapshot": snapshot})


def _handle_agent_run_events(handler: JsonHandler, run_id: str, query: dict[str, list[str]]) -> None:
    request_id = _request_id(handler)
    snapshot, scope, exc = _agent_run_snapshot_for_auth(run_id, request_id)
    if exc is not None:
        _agent_run_error(handler, exc, request_id)
        return
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    after_id = _query_int(query, "after_id", default=_safe_int(handler.headers.get("Last-Event-ID"), default=0))
    events = asyncio.run(agent_run_service.events(run_id, after_id=after_id))
    if "text/event-stream" in str(handler.headers.get("Accept") or ""):
        _write_agent_run_sse(handler, events)
        return
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "events": events})


def _handle_agent_run_evidence(handler: JsonHandler, run_id: str) -> None:
    request_id = _request_id(handler)
    snapshot, scope, exc = _agent_run_snapshot_for_auth(run_id, request_id)
    if exc is not None:
        _agent_run_error(handler, exc, request_id)
        return
    actor = _authorize_resource(handler, PERMISSION_VIEW_EVIDENCE, scope, request_id)
    if actor is None:
        return
    events = snapshot.get("timeline") if isinstance(snapshot.get("timeline"), list) else []
    records = [
        {
            "source_type": event.get("event_type"),
            "source_ref": f"run-event-{event.get('id')}",
            "summary": event.get("message"),
            "payload": event.get("payload") if isinstance(event.get("payload"), dict) else {},
            "collected_at": event.get("created_at"),
        }
        for event in events
        if isinstance(event, dict) and "evidence" in str(event.get("event_type") or "")
    ]
    scope_dict = dict(snapshot["run"].get("scope") or {})
    nodes = evidence_service.nodes_from_records(records, scope=scope_dict, actor=actor)
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="agent_run_evidence_get",
        result="success",
        cluster=scope_dict.get("cluster"),
        namespace=scope_dict.get("namespace"),
        incident_id=snapshot["run"].get("incident_id"),
        permission=PERMISSION_VIEW_EVIDENCE,
        decision="allow",
        resource_scope=scope,
    )
    handler.write_json(
        HTTPStatus.OK,
        {
            "service": APP_NAME,
            "status": "ok",
            "request_id": request_id,
            "evidence": {"status": "ok", "scope": scope_dict, "nodes": nodes, "redaction": {"applied": True}},
        },
    )


def _handle_agent_run_stream(handler: JsonHandler, run_id: str) -> None:
    request_id = _request_id(handler)
    snapshot, scope, exc = _agent_run_snapshot_for_auth(run_id, request_id)
    if exc is not None:
        _agent_run_error(handler, exc, request_id)
        return
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    after_id = _safe_int(handler.headers.get("Last-Event-ID"), default=0)
    events = asyncio.run(agent_run_service.events(run_id, after_id=after_id))
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="agent_run_stream",
        result="success",
        cluster=snapshot["run"]["scope"].get("cluster"),
        namespace=snapshot["run"]["scope"].get("namespace"),
        incident_id=snapshot["run"].get("incident_id"),
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=scope,
    )
    _write_agent_run_sse(handler, events)


def _write_agent_run_sse(handler: JsonHandler, events: list[dict[str, Any]]) -> None:
    body = "".join(
        f"id: {event['id']}\ndata: {json.dumps(event, ensure_ascii=False, sort_keys=True)}\n\n"
        for event in events
    ).encode("utf-8")
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _handle_diagnosis_process_stream(handler: JsonHandler, incident_id: str) -> None:
    request_id = _request_id(handler)
    try:
        incident = asyncio.run(incident_store.get_incident(incident_id))
    except ValueError:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", "incident not found", request_id))
        return
    scope = _incident_resource_scope(incident)
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="diagnosis_process_stream",
        result="success",
        incident_id=incident_id,
        permission=_PERMISSION_WRITE_INCIDENT_REPORT,
        decision="allow",
        resource_scope=scope,
    )

    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()

    after_id = _safe_int(handler.headers.get("Last-Event-ID"), default=0)
    seen: set[str] = set()
    deadline = time.time() + 60
    while time.time() < deadline:
        status, payload = asyncio.run(read_diagnosis_process_view(incident_id))
        if status != HTTPStatus.OK:
            return
        process = payload.get("process") if isinstance(payload.get("process"), dict) else {}
        timeline = process.get("timeline") if isinstance(process.get("timeline"), list) else []
        wrote = False
        for index, item in enumerate(timeline, start=1):
            if not isinstance(item, dict):
                continue
            event_id = _sse_event_id(item.get("event_id"), index)
            if after_id and _safe_int(event_id, default=index) <= after_id:
                continue
            if event_id in seen:
                continue
            seen.add(event_id)
            body = f"id: {event_id}\ndata: {json.dumps(item, ensure_ascii=False, sort_keys=True)}\n\n".encode("utf-8")
            try:
                handler.wfile.write(body)
                handler.wfile.flush()
            except OSError:
                return
            wrote = True
        if _diagnosis_process_terminal(process) and not wrote:
            return
        if not wrote:
            try:
                handler.wfile.write(b": keepalive\n\n")
                handler.wfile.flush()
            except OSError:
                return
        time.sleep(2)


def _sse_event_id(value: Any, fallback: int) -> str:
    text = str(value or fallback).replace("\n", " ").replace("\r", " ").strip()
    return text or str(fallback)


def _diagnosis_process_terminal(process: dict[str, Any]) -> bool:
    diagnosis = process.get("diagnosis") if isinstance(process.get("diagnosis"), dict) else {}
    status = str(diagnosis.get("status") or "").strip()
    if status in {"succeeded", "partial", "needs_human", "failed"}:
        return True
    timeline = process.get("timeline") if isinstance(process.get("timeline"), list) else []
    return any(isinstance(item, dict) and item.get("type") == "investigate_end" for item in timeline)


def _handle_agent_run_message(handler: JsonHandler, run_id: str) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    snapshot, scope, exc = _agent_run_snapshot_for_auth(run_id, request_id)
    if exc is not None:
        _agent_run_error(handler, exc, request_id)
        return
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    if not _require_incident_operator(handler, actor, request_id):
        return
    try:
        user_event = asyncio.run(agent_run_service.append_message(run_id, payload, actor_id=actor.actor_id))
    except agent_run_service.AgentRunServiceError as service_exc:
        _record_agent_run_audit(actor, request_id=request_id, action="agent_run_message", result=service_exc.code, scope=scope, snapshot=snapshot)
        _agent_run_error(handler, service_exc, request_id)
        return

    response: dict[str, Any] = {"service": APP_NAME, "status": "ok", "request_id": request_id, "result": user_event}
    action_request = _chat_action_request(str(payload.get("message") or ""), snapshot=snapshot, user_event=user_event)
    if action_request is not None:
        agent_event, action_result = _handle_chat_action_request(run_id, action_request, actor=actor, request_id=request_id)
        response["agent_event"] = agent_event
        if action_result is not None:
            response["action_result"] = action_result

    _record_agent_run_audit(actor, request_id=request_id, action="agent_run_message", result="success", scope=scope, snapshot=snapshot)
    handler.write_json(HTTPStatus.OK, response)


def _handle_agent_run_promote(handler: JsonHandler, run_id: str) -> None:
    _handle_agent_run_mutation(handler, run_id, "agent_run_promote", agent_run_service.promote, actor_arg=True)


def _handle_agent_run_conversation(handler: JsonHandler, run_id: str) -> None:
    _handle_agent_run_mutation(handler, run_id, "agent_run_conversation_update", agent_run_service.update_conversation, actor_arg=False)


def _handle_agent_run_archive(handler: JsonHandler, run_id: str) -> None:
    _handle_agent_run_empty_mutation(handler, run_id, "agent_run_archive", agent_run_service.archive_conversation)


def _handle_agent_run_delete(handler: JsonHandler, run_id: str) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    snapshot, scope, exc = _agent_run_snapshot_for_auth(run_id, request_id)
    if exc is not None:
        _agent_run_error(handler, exc, request_id)
        return
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    if not _require_incident_operator(handler, actor, request_id):
        return
    try:
        result = asyncio.run(agent_run_service.delete_conversation(run_id, payload, actor_id=actor.actor_id))
    except agent_run_service.AgentRunServiceError as service_exc:
        _record_agent_run_audit(actor, request_id=request_id, action="agent_run_delete", result=service_exc.code, scope=scope, snapshot=snapshot)
        _agent_run_error(handler, service_exc, request_id)
        return
    _record_agent_run_audit(actor, request_id=request_id, action="agent_run_delete", result="success", scope=scope, snapshot=snapshot)
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "snapshot": result})


def _handle_audit_chain_list(handler: JsonHandler, query: dict[str, list[str]]) -> None:
    request_id = _request_id(handler)
    actor = _authorize(handler, PERMISSION_QUERY_AUDIT, Scope(), request_id)
    if actor is None:
        return
    chains = asyncio.run(audit_chain_service.list_chains(actor, limit=_query_limit(query)))
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="audit_chain_query",
        result="success",
        permission=PERMISSION_QUERY_AUDIT,
        decision="allow",
        resource_scope=Scope(),
    )
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "chains": chains})


def _handle_audit_chain_detail(handler: JsonHandler, chain_id: str) -> None:
    request_id = _request_id(handler)
    actor = _authorize(handler, PERMISSION_QUERY_AUDIT, Scope(), request_id)
    if actor is None:
        return
    try:
        chain = asyncio.run(audit_chain_service.get_chain(actor, chain_id))
    except audit_chain_service.AuditChainError as exc:
        handler.write_json(exc.status, _error_payload(exc.code, exc.message, request_id))
        return
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="audit_chain_get",
        result="success",
        incident_id=chain.get("incident_id"),
        permission=PERMISSION_QUERY_AUDIT,
        decision="allow",
        resource_scope=Scope(),
        approval_id=chain.get("approval_id"),
        action_proposal_id=chain.get("action_proposal_id"),
    )
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "chain": chain})


def _handle_audit_raw(handler: JsonHandler, query: dict[str, list[str]]) -> None:
    request_id = _request_id(handler)
    cluster = _first_query_value(query, "cluster")
    namespace = _first_query_value(query, "namespace")
    scope = resource_scope(cluster=cluster, namespace=namespace)
    actor = _authorize(handler, PERMISSION_QUERY_AUDIT, scope, request_id)
    if actor is None:
        return
    rows = asyncio.run(audit_chain_service.raw_logs(actor, cluster=cluster, namespace=namespace, limit=_query_limit(query)))
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="audit_raw_query",
        result="success",
        cluster=cluster,
        namespace=namespace,
        permission=PERMISSION_QUERY_AUDIT,
        decision="allow",
        resource_scope=scope,
    )
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "rows": rows})


def _handle_audit_tombstones(handler: JsonHandler, query: dict[str, list[str]]) -> None:
    request_id = _request_id(handler)
    actor = _authorize(handler, PERMISSION_QUERY_AUDIT, Scope(), request_id)
    if actor is None:
        return
    tombstones = asyncio.run(audit_chain_service.tombstones(actor, limit=_query_limit(query)))
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="audit_tombstone_query",
        result="success",
        permission=PERMISSION_QUERY_AUDIT,
        decision="allow",
        resource_scope=Scope(),
    )
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "tombstones": tombstones},
    )


def _handle_console_notifications(handler: JsonHandler, query: dict[str, list[str]]) -> None:
    request_id = _request_id(handler)
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, Scope(), request_id)
    if actor is None:
        return
    rows = notification_center.list_deliveries(
        status=_first_query_value(query, "status"),
        notification_type=_first_query_value(query, "notification_type", "type"),
        limit=_query_limit(query),
    )
    visible = [_notification_projection(row) for row in rows if _can_view_notification(actor, row)]
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="notification_query",
        result="success",
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=Scope(),
    )
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "notifications": visible})


def _handle_console_notifications_stream(handler: JsonHandler, query: dict[str, list[str]]) -> None:
    request_id = _request_id(handler)
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, Scope(), request_id)
    if actor is None:
        return
    rows = notification_center.list_deliveries(limit=_query_limit(query))
    visible = [_notification_projection(row) for row in rows if _can_view_notification(actor, row)]
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="notification_stream",
        result="success",
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=Scope(),
    )
    body = "".join(
        f"id: {item['id']}\ndata: {json.dumps(item, ensure_ascii=False, sort_keys=True)}\n\n"
        for item in visible
    ).encode("utf-8")
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _handle_console_notification_retry(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    delivery_id = str(payload.get("delivery_id") or "").strip()
    if not delivery_id:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", "delivery_id is required", request_id))
        return
    row = next((item for item in notification_center.list_deliveries(limit=500) if str(item.get("id")) == delivery_id), None)
    if row is None:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", "notification delivery not found", request_id))
        return
    scope = _notification_scope(row)
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    if not _can_view_notification(actor, row):
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", "notification delivery not found", request_id))
        return
    try:
        result = notification_center.retry_delivery(delivery_id)
    except ValueError as exc:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", str(exc), request_id))
        return
    delivery = result.get("delivery") if isinstance(result.get("delivery"), dict) else {}
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="notification_retry",
        result=str(delivery.get("delivery_status") or "unknown"),
        incident_id=delivery.get("incident_id"),
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=scope,
        approval_id=delivery.get("approval_id"),
    )
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "result": {**result, "delivery": _notification_projection(delivery)}},
    )


def _can_view_notification(actor: Actor, row: dict[str, Any]) -> bool:
    scope = _notification_scope(row)
    if scope == Scope() and not actor.has_role("admin"):
        return False
    return actor.can(PERMISSION_VIEW_INCIDENT, scope)


def _notification_scope(row: dict[str, Any]) -> Scope:
    incident_id = str(row.get("incident_id") or "").strip()
    if incident_id:
        try:
            return _incident_resource_scope(asyncio.run(incident_store.get_incident(incident_id)))
        except ValueError:
            pass
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    return resource_scope(
        cluster=_first_text(context.get("cluster"), context.get("cluster_id")),
        service=_first_text(row.get("service_id"), context.get("service"), context.get("service_id"), context.get("service_name")),
        team=_first_text(row.get("team_id"), context.get("team"), context.get("team_id"), context.get("owner_team")),
        namespace=_first_text(context.get("namespace")),
    )


def _notification_projection(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    return {
        "id": row.get("id"),
        "notification_id": row.get("notification_id"),
        "notification_type": row.get("notification_type"),
        "incident_id": row.get("incident_id"),
        "approval_id": row.get("approval_id"),
        "service_id": row.get("service_id"),
        "team_id": row.get("team_id"),
        "delivery_status": row.get("delivery_status"),
        "delivery_attempts": row.get("delivery_attempts"),
        "max_attempts": row.get("max_attempts"),
        "dedupe_key": row.get("dedupe_key"),
        "next_retry_at": row.get("next_retry_at"),
        "target_message_id": row.get("target_message_id"),
        "last_delivery_error": _safe_delivery_error(row.get("last_delivery_error")),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "sent_at": row.get("sent_at"),
        "summary": payload.get("summary") or context.get("summary"),
        "risk_level": payload.get("risk_level") or context.get("risk_level"),
        "status": payload.get("status") or context.get("status"),
    }


def _safe_delivery_error(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if any(marker in lowered for marker in ("token", "secret", "password", "authorization", "api-key")):
        return "[redacted]"
    return text[:240]


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _handle_agent_run_mutation(handler: JsonHandler, run_id: str, action: str, fn: Any, *, actor_arg: bool) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    snapshot, scope, exc = _agent_run_snapshot_for_auth(run_id, request_id)
    if exc is not None:
        _agent_run_error(handler, exc, request_id)
        return
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    if not _require_incident_operator(handler, actor, request_id):
        return
    try:
        result = asyncio.run(fn(run_id, payload, actor_id=actor.actor_id)) if actor_arg else asyncio.run(fn(run_id, payload))
    except agent_run_service.AgentRunServiceError as service_exc:
        _record_agent_run_audit(actor, request_id=request_id, action=action, result=service_exc.code, scope=scope, snapshot=snapshot)
        _agent_run_error(handler, service_exc, request_id)
        return
    _record_agent_run_audit(actor, request_id=request_id, action=action, result="success", scope=scope, snapshot=snapshot)
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "result": result})


def _handle_agent_run_empty_mutation(handler: JsonHandler, run_id: str, action: str, fn: Any) -> None:
    request_id = _request_id(handler)
    snapshot, scope, exc = _agent_run_snapshot_for_auth(run_id, request_id)
    if exc is not None:
        _agent_run_error(handler, exc, request_id)
        return
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    if not _require_incident_operator(handler, actor, request_id):
        return
    try:
        result = asyncio.run(fn(run_id))
    except agent_run_service.AgentRunServiceError as service_exc:
        _record_agent_run_audit(actor, request_id=request_id, action=action, result=service_exc.code, scope=scope, snapshot=snapshot)
        _agent_run_error(handler, service_exc, request_id)
        return
    _record_agent_run_audit(actor, request_id=request_id, action=action, result="success", scope=scope, snapshot=snapshot)
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "snapshot": result})


def _record_agent_run_audit(
    actor: Actor,
    *,
    request_id: str,
    action: str,
    result: str,
    scope: Scope,
    snapshot: dict[str, Any],
) -> None:
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action=action,
        result=result,
        cluster=snapshot["run"]["scope"].get("cluster"),
        namespace=snapshot["run"]["scope"].get("namespace"),
        incident_id=snapshot["run"].get("incident_id"),
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow" if result == "success" else "deny",
        resource_scope=scope,
    )


def _handle_chat_action_request(
    run_id: str,
    action_request: dict[str, Any],
    *,
    actor: Actor,
    request_id: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if action_request["status"] == "clarification_required":
        event = asyncio.run(
            agent_run_service.append_event(
                run_id,
                "agent_clarification_requested",
                action_request["message"],
                {
                    "missing_fields": action_request.get("missing_fields") or [],
                    "ambiguous_fields": action_request.get("ambiguous_fields") or [],
                    "inferred_target": action_request.get("inferred_target") or {},
                },
                actor_id=_CHAT_AGENT_ID,
            )
        )
        return event, {"clarification_required": True, **action_request}

    status, action_result = _propose_action_payload(action_request["payload"], actor=actor, request_id=request_id)
    event_type = "agent_action_proposed" if status < 400 else "agent_clarification_requested"
    message = "Gateway created an action request" if status < 400 else str(action_result.get("error", {}).get("message") or "Action request needs clarification")
    event = asyncio.run(
        agent_run_service.append_event(
            run_id,
            event_type,
            message,
            {
                "http_status": int(status),
                "inferred_target": action_request.get("inferred_target") or {},
                "action_id": (action_result.get("action") or {}).get("action_id"),
                "action_hash": (action_result.get("action") or {}).get("action_hash"),
                "approval_id": (action_result.get("approval_request") or {}).get("approval_id"),
                "policy": action_result.get("policy"),
                "execution": action_result.get("execution"),
                "error": action_result.get("error"),
            },
            actor_id=_CHAT_AGENT_ID,
        )
    )
    return event, action_result


def _chat_action_request(message: str, *, snapshot: dict[str, Any], user_event: dict[str, Any]) -> dict[str, Any] | None:
    text = message.strip()
    if not text or text.startswith("/btw"):
        return None
    lowered = text.lower()
    run = snapshot["run"]
    scope = run.get("scope") if isinstance(run.get("scope"), dict) else {}
    action_type = _chat_action_type(text)
    if action_type is None:
        if not _looks_like_action_request(text):
            return None
        target, inferred, missing, ambiguous = _chat_action_target(text, action_type="restart_deployment", scope=scope)
        return {
            "status": "clarification_required",
            "message": _clarification_message(["action_type", *missing], ambiguous),
            "missing_fields": ["action_type", *missing],
            "ambiguous_fields": ambiguous,
            "inferred_target": {**inferred, **{key: value for key, value in target.items() if value}},
        }
    target, inferred, missing, ambiguous = _chat_action_target(text, action_type=action_type, scope=scope)
    if missing or ambiguous:
        return {
            "status": "clarification_required",
            "message": _clarification_message(missing, ambiguous),
            "missing_fields": missing,
            "ambiguous_fields": ambiguous,
            "inferred_target": inferred,
        }
    payload = {
        "action_type": action_type,
        **target,
        "incident_id": run.get("incident_id") or "manual",
        "session_id": run["run_id"],
        "run_id": run["run_id"],
        "reason": text,
        "agent_id": _CHAT_AGENT_ID,
        "idempotency_key": f"chat:{run['run_id']}:{user_event['id']}",
        "action_proposal_id": f"chat-{run['run_id']}-{user_event['id']}",
    }
    return {"status": "ready", "payload": payload, "inferred_target": inferred, "message": lowered}


def _chat_action_type(text: str) -> str | None:
    lowered = text.lower()
    if "重启" in text or "restart" in lowered or "rollout restart" in lowered:
        return "restart_deployment"
    if "回滚" in text or "rollback" in lowered or "rollout undo" in lowered:
        return "rollback_deployment"
    if "scale" in lowered or "扩容" in text or "缩容" in text:
        return "scale_deployment"
    return None


def _looks_like_action_request(text: str) -> bool:
    lowered = text.lower()
    return "action" in lowered or "operate" in lowered or "执行" in text or "操作" in text


def _chat_action_target(text: str, *, action_type: str, scope: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[str], list[str]]:
    names = r"[A-Za-z0-9_.-]+"
    cluster = namespace = service = deployment = team = ""
    ambiguous: list[str] = []

    scoped = re.search(rf"\b({names})/({names})\b", text)
    if scoped:
        cluster, namespace = scoped.group(1), scoped.group(2)
    named = re.findall(rf"\b({names})\s*(?:服务|service|deployment|部署)", text, flags=re.IGNORECASE)
    if len(set(named)) > 1:
        ambiguous.append("target")
    elif named:
        service = named[0]
        deployment = named[0]
    if not service:
        command_target = re.search(rf"(?:重启|回滚|扩容|缩容|restart|rollback|scale)\s+(?:service\s+|deployment\s+)?({names})\b", text, flags=re.IGNORECASE)
        if command_target and "/" not in command_target.group(1):
            service = command_target.group(1)
            deployment = command_target.group(1)
    team_match = re.search(rf"(?:team|团队)\s*[:=]?\s*({names})\b", text, flags=re.IGNORECASE)
    if team_match:
        team = team_match.group(1)

    inferred: dict[str, Any] = {}
    values = {"cluster": cluster, "namespace": namespace, "service": service, "team": team, "deployment": deployment}
    for key in ("cluster", "namespace", "service", "team"):
        if not values[key] and scope.get(key):
            values[key] = str(scope[key])
            inferred[key] = values[key]
    if not values["deployment"] and values["service"]:
        values["deployment"] = values["service"]
        inferred["deployment"] = values["deployment"]
    if action_type == "scale_deployment":
        replicas = re.search(r"(?:replicas|副本)\s*[:=]?\s*(\d+)", text, flags=re.IGNORECASE)
        if replicas:
            values["replicas"] = replicas.group(1)

    required = ["cluster", "namespace", "service", "team", "deployment"]
    if action_type == "scale_deployment":
        required.append("replicas")
    missing = [key for key in required if not str(values.get(key) or "").strip()]
    return values, inferred, missing, ambiguous


def _clarification_message(missing: list[str], ambiguous: list[str]) -> str:
    fields = ", ".join([*missing, *ambiguous])
    return f"Please clarify the action target before Gateway creates approval or a policy grant: {fields}"


def _query_int(query: dict[str, list[str]], key: str, *, default: int) -> int:
    values = query.get(key) or []
    return _safe_int(values[0] if values else None, default=default)


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _handle_evidence_query(handler: JsonHandler, *, agent: bool) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    if not isinstance(payload, dict):
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", "request body must be a JSON object", request_id))
        return
    scope = _evidence_scope_from_payload(payload)
    actor = _authorize_resource(handler, PERMISSION_VIEW_EVIDENCE, scope, request_id)
    if actor is None:
        return
    try:
        evidence = evidence_service.query_evidence(payload, actor=actor, request_id=request_id, agent=agent)
    except evidence_service.EvidenceServiceError as exc:
        _record_gateway_audit(
            actor,
            request_id=request_id,
            action="evidence_agent_query" if agent else "evidence_query",
            result=exc.code,
            cluster=scope.clusters[0] if scope.clusters else None,
            namespace=scope.namespaces[0] if scope.namespaces else None,
            permission=PERMISSION_VIEW_EVIDENCE,
            decision="deny",
            resource_scope=scope,
        )
        handler.write_json(exc.status, _error_payload(exc.code, exc.message, request_id))
        return
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="evidence_agent_query" if agent else "evidence_query",
        result=str(evidence.get("status") or "success"),
        cluster=str(evidence.get("scope", {}).get("cluster") or "") or None,
        namespace=str(evidence.get("scope", {}).get("namespace") or "") or None,
        permission=PERMISSION_VIEW_EVIDENCE,
        decision="allow",
        resource_scope=scope,
    )
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "evidence": evidence},
    )


def _handle_user_list(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    actor = _authorize(handler, PERMISSION_VIEW_USERS, Scope(), request_id)
    if actor is None:
        return
    store = _identity_store()
    try:
        users = _with_recent_user_audit(store.list_user_records())
    finally:
        store.close()
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="user_list",
        result="success",
        permission=PERMISSION_VIEW_USERS,
        decision="allow",
        resource_scope=Scope(),
    )
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "users": users},
    )


def _handle_user_create(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    actor = _authorize(handler, PERMISSION_MANAGE_USERS, Scope(), request_id)
    if actor is None:
        return
    username = str(payload.get("username") or "").strip()
    store = _identity_store()
    try:
        user = store.create_local_user(payload)
    except IdentityError as exc:
        _record_user_management_audit(
            actor,
            request_id=request_id,
            username=username or "-",
            action="user_create",
            result=exc.code,
            decision="deny",
        )
        handler.write_json(_identity_error_status(exc), _error_payload(exc.code, exc.message, request_id))
        return
    finally:
        store.close()
    _record_user_management_audit(
        actor,
        request_id=request_id,
        username=user["username"],
        action="user_create",
        result="success",
        decision="allow",
    )
    _send_console_next_notification(
        "users_permission_changed",
        summary=f"user {user['username']} created",
        actor=actor,
        dedupe_key=f"users_permission_changed:create:{user['username']}:{request_id}",
        context={"username": user["username"], "action": "user_create", "user_scope": user.get("scope") or {}},
    )
    handler.write_json(
        HTTPStatus.CREATED,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "user": user},
    )


def _handle_user_update(handler: JsonHandler, username: str) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    actor = _authorize(handler, PERMISSION_MANAGE_USERS, Scope(), request_id)
    if actor is None:
        return
    store = _identity_store()
    try:
        user = store.update_user(username, payload, current_actor=actor)
    except IdentityError as exc:
        _record_user_management_audit(
            actor,
            request_id=request_id,
            username=username,
            action="user_update",
            result=exc.code,
            decision="deny",
        )
        handler.write_json(_identity_error_status(exc), _error_payload(exc.code, exc.message, request_id))
        return
    finally:
        store.close()
    _record_user_management_audit(
        actor,
        request_id=request_id,
        username=username,
        action="user_update",
        result="success",
        decision="allow",
    )
    _send_console_next_notification(
        "users_permission_changed",
        summary=f"user {username} updated",
        actor=actor,
        dedupe_key=f"users_permission_changed:update:{username}:{request_id}",
        context={"username": username, "action": "user_update", "user_scope": user.get("scope") or {}},
    )
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "user": user},
    )


def _handle_user_disable(handler: JsonHandler, username: str) -> None:
    request_id = _request_id(handler)
    actor = _authorize(handler, PERMISSION_MANAGE_USERS, Scope(), request_id)
    if actor is None:
        return
    store = _identity_store()
    try:
        user = store.disable_user(username, current_actor=actor)
    except IdentityError as exc:
        _record_user_management_audit(
            actor,
            request_id=request_id,
            username=username,
            action="user_disable",
            result=exc.code,
            decision="deny",
        )
        handler.write_json(_identity_error_status(exc), _error_payload(exc.code, exc.message, request_id))
        return
    finally:
        store.close()
    _record_user_management_audit(
        actor,
        request_id=request_id,
        username=username,
        action="user_disable",
        result="success",
        decision="allow",
    )
    _send_console_next_notification(
        "users_permission_changed",
        summary=f"user {username} disabled",
        actor=actor,
        dedupe_key=f"users_permission_changed:disable:{username}:{request_id}",
        context={"username": username, "action": "user_disable", "user_scope": user.get("scope") or {}},
    )
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "user": user},
    )


def _handle_user_reset_password(handler: JsonHandler, username: str) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    actor = _authorize(handler, PERMISSION_MANAGE_USERS, Scope(), request_id)
    if actor is None:
        return
    store = _identity_store()
    try:
        user = store.reset_local_password(username, str(payload.get("password") or ""))
    except IdentityError as exc:
        _record_user_management_audit(
            actor,
            request_id=request_id,
            username=username,
            action="user_reset_password",
            result=exc.code,
            decision="deny",
        )
        handler.write_json(_identity_error_status(exc), _error_payload(exc.code, exc.message, request_id))
        return
    finally:
        store.close()
    _record_user_management_audit(
        actor,
        request_id=request_id,
        username=username,
        action="user_reset_password",
        result="success",
        decision="allow",
    )
    _send_console_next_notification(
        "users_permission_changed",
        summary=f"user {username} password reset",
        actor=actor,
        dedupe_key=f"users_permission_changed:reset-password:{username}:{request_id}",
        context={"username": username, "action": "user_reset_password", "user_scope": user.get("scope") or {}},
    )
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "user": user},
    )


def _settings_error(handler: JsonHandler, exc: settings_service.SettingsServiceError, request_id: str) -> None:
    handler.write_json(exc.status, _error_payload(exc.code, exc.message, request_id))


def _record_settings_audit(actor: Actor, *, request_id: str, action: str, result: str) -> None:
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action=action,
        result=result,
        cluster="settings",
        permission=PERMISSION_MANAGE_SETTINGS,
        decision="allow",
        resource_scope=Scope(),
    )


def _require_settings_admin(handler: JsonHandler, actor: Actor, request_id: str) -> bool:
    if actor.has_role(ROLE_ADMIN):
        return True
    _record_settings_audit(actor, request_id=request_id, action="settings_admin_required", result="forbidden")
    handler.write_json(HTTPStatus.FORBIDDEN, _error_payload("forbidden", "settings and policy writes require admin", request_id))
    return False


def _handle_settings_get(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    actor = _authorize(handler, PERMISSION_VIEW_SETTINGS, Scope(), request_id)
    if actor is None:
        return
    try:
        current = settings_service.current()
    except settings_service.SettingsServiceError as exc:
        _settings_error(handler, exc, request_id)
        return
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "settings_version": current},
    )


def _handle_settings_preview(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    actor = _authorize(handler, PERMISSION_MANAGE_SETTINGS, Scope(), request_id)
    if actor is None:
        return
    if not _require_settings_admin(handler, actor, request_id):
        return
    try:
        preview = settings_service.preview(payload)
    except settings_service.SettingsServiceError as exc:
        _settings_error(handler, exc, request_id)
        return
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "preview": preview},
    )


def _handle_settings_save(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    actor = _authorize(handler, PERMISSION_MANAGE_SETTINGS, Scope(), request_id)
    if actor is None:
        return
    if not _require_settings_admin(handler, actor, request_id):
        return
    try:
        version = settings_service.save(payload, actor_id=actor.actor_id)
    except settings_service.SettingsServiceError as exc:
        _record_settings_audit(actor, request_id=request_id, action="settings_save", result=exc.code)
        _settings_error(handler, exc, request_id)
        return
    _record_settings_audit(actor, request_id=request_id, action="settings_save", result="success")
    _send_console_next_notification(
        "settings_permission_changed",
        summary=f"settings version {version['version_number']} saved",
        actor=actor,
        dedupe_key=f"settings_permission_changed:save:{version['version_id']}",
        context={"action": "settings_save", "settings_version": version["version_number"]},
    )
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "settings_version": version},
    )


def _handle_settings_rollback(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    actor = _authorize(handler, PERMISSION_MANAGE_SETTINGS, Scope(), request_id)
    if actor is None:
        return
    if not _require_settings_admin(handler, actor, request_id):
        return
    try:
        version = settings_service.rollback(actor_id=actor.actor_id)
    except settings_service.SettingsServiceError as exc:
        _record_settings_audit(actor, request_id=request_id, action="settings_rollback", result=exc.code)
        _settings_error(handler, exc, request_id)
        return
    _record_settings_audit(actor, request_id=request_id, action="settings_rollback", result="success")
    _send_console_next_notification(
        "settings_permission_changed",
        summary=f"settings version {version['version_number']} rolled back",
        actor=actor,
        dedupe_key=f"settings_permission_changed:rollback:{version['version_id']}",
        context={"action": "settings_rollback", "settings_version": version["version_number"]},
    )
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "settings_version": version},
    )


def _cluster_error(handler: JsonHandler, exc: cluster_registry.ClusterServiceError, request_id: str) -> None:
    handler.write_json(exc.status, _error_payload(exc.code, exc.message, request_id))


def _record_cluster_audit(actor: Actor, *, request_id: str, action: str, result: str, cluster_id: str | None) -> None:
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action=action,
        result=result,
        cluster=cluster_id or "clusters",
        namespace="config",
        permission=PERMISSION_MANAGE_SETTINGS,
        decision="allow" if result == "success" else "deny",
        resource_scope=Scope(),
    )


def _handle_cluster_list(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    actor = _authorize(handler, PERMISSION_VIEW_SETTINGS, Scope(), request_id)
    if actor is None:
        return
    clusters = cluster_registry.list_clusters()
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "clusters": clusters})


def _handle_cluster_save(handler: JsonHandler, cluster_id: str | None = None) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    actor = _authorize(handler, PERMISSION_MANAGE_SETTINGS, Scope(), request_id)
    if actor is None:
        return
    if not actor.has_role("admin"):
        handler.write_json(HTTPStatus.FORBIDDEN, _error_payload("forbidden", "cluster writes require admin", request_id))
        return
    try:
        cluster = cluster_registry.upsert_config(payload, actor_id=actor.actor_id, cluster_id=cluster_id)
    except cluster_registry.ClusterServiceError as exc:
        _record_cluster_audit(actor, request_id=request_id, action="cluster_config_save", result=exc.code, cluster_id=cluster_id)
        _cluster_error(handler, exc, request_id)
        return
    _record_cluster_audit(actor, request_id=request_id, action="cluster_config_save", result="success", cluster_id=cluster["cluster_id"])
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "cluster": cluster})


def _handle_cluster_runtime(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
        cluster = cluster_registry.report_runtime(payload)
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    except cluster_registry.ClusterServiceError as exc:
        _cluster_error(handler, exc, request_id)
        return
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "cluster": cluster})


def _handle_policies_get(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    actor = _authorize(handler, PERMISSION_VIEW_POLICY, Scope(), request_id)
    if actor is None:
        return
    try:
        policy = settings_service.policy_state()
    except settings_service.SettingsServiceError as exc:
        _settings_error(handler, exc, request_id)
        return
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "policy_state": policy},
    )


def _runbook_resource_scope(runbook: dict[str, Any]) -> Scope:
    raw = runbook.get("scope") if isinstance(runbook.get("scope"), dict) else {}
    return resource_scope(
        cluster=_scope_value(raw.get("cluster")),
        namespace=_scope_value(raw.get("namespace")),
        service=_scope_value(raw.get("service")),
        team=_scope_value(raw.get("team")),
    )


def _disabled_runbook_for_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    runbook_id = str(payload.get("runbook_skeleton") or payload.get("skeleton") or "service_health").strip()
    for runbook in runbook_service.list_runbooks():
        if runbook["id"] == runbook_id and not runbook.get("enabled"):
            return runbook
    return None


def _handle_runbook_list(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    actor = _authorize(handler, PERMISSION_VIEW_RUNBOOKS, Scope(), request_id)
    if actor is None:
        return
    runs = asyncio.run(agent_run_service.list_runs())
    runbooks = [
        runbook
        for runbook in runbook_service.list_runbooks(runs=runs)
        if actor.can(PERMISSION_VIEW_RUNBOOKS, _runbook_resource_scope(runbook))
    ]
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="runbook_list",
        result="success",
        cluster="runbooks",
        permission=PERMISSION_VIEW_RUNBOOKS,
        decision="allow",
        resource_scope=Scope(),
    )
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "runbooks": runbooks},
    )


def _handle_runbook_toggle(handler: JsonHandler, runbook_id: str) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    if not isinstance(payload.get("enabled"), bool):
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", "enabled must be a boolean", request_id))
        return
    runs = asyncio.run(agent_run_service.list_runs())
    existing = next((item for item in runbook_service.list_runbooks(runs=runs) if item["id"] == runbook_id), None)
    if existing is None:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", "runbook skeleton not found", request_id))
        return
    scope = _runbook_resource_scope(existing)
    actor = _authorize(handler, PERMISSION_MANAGE_RUNBOOKS, scope, request_id)
    if actor is None:
        return
    if not actor.has_role(ROLE_ADMIN):
        _record_gateway_audit(
            actor,
            request_id=request_id,
            action="runbook_toggle",
            result="forbidden",
            cluster="runbooks",
            permission=PERMISSION_MANAGE_RUNBOOKS,
            decision="deny",
            resource_scope=scope,
        )
        handler.write_json(HTTPStatus.FORBIDDEN, _error_payload("forbidden", "runbook writes require admin", request_id))
        return
    runbook = runbook_service.set_enabled(runbook_id, payload["enabled"], actor_id=actor.actor_id, runs=runs)
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="runbook_toggle",
        result="success",
        cluster="runbooks",
        namespace=str(runbook["scope"].get("namespace") or ""),
        permission=PERMISSION_MANAGE_RUNBOOKS,
        decision="allow",
        resource_scope=scope,
    )
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "runbook": runbook},
    )


def _handle_policy_test(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    actor = _authorize(handler, PERMISSION_VIEW_POLICY, Scope(), request_id)
    if actor is None:
        return
    try:
        result = settings_service.test_policy(payload, actor_id=actor.actor_id)
    except settings_service.SettingsServiceError as exc:
        _settings_error(handler, exc, request_id)
        return
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="policy_test",
        result=result["result"]["decision"],
        cluster=result["result"]["cluster"],
        namespace=result["result"].get("namespace"),
        permission=PERMISSION_VIEW_POLICY,
        decision="allow",
        resource_scope=Scope(),
    )
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, **result},
    )


def _approval_detail_id(route_path: str) -> str | None:
    prefix = "/api/approval-requests/"
    if not route_path.startswith(prefix):
        return None
    suffix = route_path[len(prefix):].strip("/")
    if not suffix or "/" in suffix:
        return None
    return suffix


def _action_detail_id(route_path: str) -> str | None:
    prefix = "/api/actions/"
    if not route_path.startswith(prefix):
        return None
    suffix = route_path[len(prefix):].strip("/")
    if not suffix or "/" in suffix or suffix == "propose":
        return None
    return suffix


def _incident_workbench_id(route_path: str) -> str | None:
    prefix = "/api/incidents/"
    if not route_path.startswith(prefix):
        return None
    parts = [unquote(part) for part in route_path[len(prefix):].split("/") if part]
    if len(parts) == 2 and parts[1] == "workbench":
        return parts[0]
    return None


def _incident_evidence_id(route_path: str) -> str | None:
    prefix = "/api/incidents/"
    if not route_path.startswith(prefix):
        return None
    parts = [unquote(part) for part in route_path[len(prefix):].split("/") if part]
    if len(parts) == 2 and parts[1] == "evidence":
        return parts[0]
    return None


def _incident_report_id(route_path: str) -> str | None:
    prefix = "/api/incidents/"
    if not route_path.startswith(prefix):
        return None
    parts = [unquote(part) for part in route_path[len(prefix):].split("/") if part]
    if len(parts) == 2 and parts[1] == "report":
        return parts[0]
    return None


def _incident_report_action(route_path: str) -> tuple[str, str] | None:
    prefix = "/api/incidents/"
    if not route_path.startswith(prefix):
        return None
    parts = [unquote(part) for part in route_path[len(prefix):].split("/") if part]
    if len(parts) == 3 and parts[1] == "report" and parts[2] in {"draft", "publish", "kb-candidates"}:
        return parts[0], parts[2]
    return None


def _incident_kb_candidate_approve(route_path: str) -> tuple[str, str] | None:
    prefix = "/api/incidents/"
    if not route_path.startswith(prefix):
        return None
    parts = [unquote(part) for part in route_path[len(prefix):].split("/") if part]
    if len(parts) == 5 and parts[1] == "report" and parts[2] == "kb-candidates" and parts[4] == "approve":
        return parts[0], parts[3]
    return None


def _incident_control_id(route_path: str) -> str | None:
    prefix = "/api/incidents/"
    if not route_path.startswith(prefix):
        return None
    parts = [unquote(part) for part in route_path[len(prefix):].split("/") if part]
    if len(parts) == 2 and parts[1] == "controls":
        return parts[0]
    return None


def _approval_execution_detail_id(route_path: str) -> str | None:
    prefix = "/api/approval-requests/"
    if not route_path.startswith(prefix):
        return None
    parts = [part for part in route_path[len(prefix):].split("/") if part]
    if len(parts) == 2 and parts[1] == "execution":
        return parts[0]
    return None


def _handle_incident_workbench(handler: JsonHandler, incident_id: str) -> None:
    request_id = _request_id(handler)
    try:
        incident = asyncio.run(incident_store.get_incident(incident_id))
    except ValueError:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", "incident not found", request_id))
        return
    scope = _incident_resource_scope(incident)
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    workbench = _incident_workbench_snapshot(incident, actor=actor, request_id=request_id)
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="incident_workbench_get",
        result="success",
        cluster=incident.get("cluster"),
        namespace=incident.get("namespace"),
        incident_id=incident_id,
        permission=_PERMISSION_WRITE_INCIDENT_REPORT,
        decision="allow",
        resource_scope=scope,
    )
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "workbench": workbench})


def _handle_incident_evidence(handler: JsonHandler, incident_id: str) -> None:
    request_id = _request_id(handler)
    try:
        incident = asyncio.run(incident_store.get_incident(incident_id))
    except ValueError:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", "incident not found", request_id))
        return
    scope = _incident_resource_scope(incident)
    actor = _authorize_resource(handler, PERMISSION_VIEW_EVIDENCE, scope, request_id)
    if actor is None:
        return
    scope_dict = _incident_scope_dict(incident)
    records = _incident_evidence_records(incident_id, incident)
    nodes = evidence_service.nodes_from_records(records, scope=scope_dict, actor=actor)
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="incident_evidence_get",
        result="success",
        cluster=incident.get("cluster"),
        namespace=incident.get("namespace"),
        incident_id=incident_id,
        permission=PERMISSION_VIEW_EVIDENCE,
        decision="allow",
        resource_scope=scope,
    )
    handler.write_json(
        HTTPStatus.OK,
        {
            "service": APP_NAME,
            "status": "ok",
            "request_id": request_id,
            "evidence": {"status": "ok", "scope": scope_dict, "nodes": nodes, "redaction": {"applied": True}},
        },
    )


def _read_scoped_diagnosis_process(incident_id: str, incident: dict[str, Any], actor: Actor) -> tuple[HTTPStatus, dict[str, Any]]:
    status, payload = asyncio.run(read_diagnosis_process_view(incident_id))
    if status != HTTPStatus.OK:
        return status, payload
    process = payload.get("process") if isinstance(payload.get("process"), dict) else {}
    process_evidence = process.get("evidence") if isinstance(process.get("evidence"), list) else []
    if not process_evidence:
        return status, payload
    diagnosis = process.get("diagnosis") if isinstance(process.get("diagnosis"), dict) else {}
    inherited_refs = {
        str(item.get("result_ref") or item.get("evidence_id") or "")
        for item in process_evidence
        if diagnosis and isinstance(item, dict) and not str(item.get("evidence_id") or "").startswith("missing-")
    }
    records = _incident_evidence_records(incident_id, incident, inherit_scope_refs=inherited_refs)
    nodes = evidence_service.nodes_from_records(records, scope=_incident_scope_dict(incident), actor=actor)
    allowed = {str(ref.get("ref_id")) for node in nodes for ref in node.get("refs", []) if isinstance(ref, dict)}
    process["evidence"] = [
        item
        for item in process_evidence
        if str(item.get("evidence_id") or item.get("result_ref") or "") in allowed
        or str(item.get("evidence_id") or "").startswith("missing-")
    ]
    return status, payload


def _incident_evidence_records(
    incident_id: str,
    incident: dict[str, Any],
    *,
    inherit_scope_refs: set[str] | None = None,
) -> list[dict[str, Any]]:
    scope = _incident_scope_dict(incident)
    records = asyncio.run(incident_store.list_evidence(incident_id))
    for record in records:
        source_ref = str(record.get("source_ref") or record.get("id") or "")
        if inherit_scope_refs is not None and source_ref not in inherit_scope_refs:
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if inherit_scope_refs is not None:
            payload.setdefault("scope", scope)
        record["payload"] = payload
    return records


def _handle_incident_report_get(handler: JsonHandler, incident_id: str, query: dict[str, list[str]]) -> None:
    request_id = _request_id(handler)
    try:
        incident = asyncio.run(incident_store.get_incident(incident_id))
    except ValueError:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", "incident not found", request_id))
        return
    scope = _incident_resource_scope(incident)
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    snapshot = asyncio.run(report_service.report_snapshot(incident_id))
    latest = snapshot.get("latest_report") if isinstance(snapshot.get("latest_report"), dict) else None
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="incident_report_get",
        result="success",
        cluster=incident.get("cluster"),
        namespace=incident.get("namespace"),
        incident_id=incident_id,
        permission=_PERMISSION_WRITE_INCIDENT_REPORT,
        decision="allow",
        resource_scope=scope,
    )
    if _first_query_value(query, "format") == "html":
        _write_html(handler, str((latest or {}).get("html") or "<article><h1>unknown</h1></article>"))
        return
    if _first_query_value(query, "format") in {"markdown", "md"}:
        _write_markdown(handler, report_service.markdown_export(latest))
        return
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "report": snapshot})


def _handle_incident_report_draft(handler: JsonHandler, incident_id: str) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
        incident = asyncio.run(incident_store.get_incident(incident_id))
    except ValueError as exc:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", str(exc), request_id))
        return
    except TypeError as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    scope = _incident_resource_scope(incident)
    actor = _authorize_incident_report_write(handler, scope, request_id)
    if actor is None:
        return
    try:
        report = asyncio.run(report_service.create_draft(incident_id, payload, actor_id=actor.actor_id))
    except report_service.ReportServiceError as exc:
        handler.write_json(exc.status, _error_payload(exc.code, exc.message, request_id))
        return
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="incident_report_draft",
        result="success",
        cluster=incident.get("cluster"),
        namespace=incident.get("namespace"),
        incident_id=incident_id,
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=scope,
    )
    handler.write_json(HTTPStatus.CREATED, {"service": APP_NAME, "status": "ok", "request_id": request_id, "report_version": report})


def _handle_incident_report_publish(handler: JsonHandler, incident_id: str) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
        incident = asyncio.run(incident_store.get_incident(incident_id))
    except ValueError as exc:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", str(exc), request_id))
        return
    except TypeError as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    scope = _incident_resource_scope(incident)
    actor = _authorize_incident_report_write(handler, scope, request_id)
    if actor is None:
        return
    try:
        report = asyncio.run(report_service.publish(incident_id, payload, actor_id=actor.actor_id))
    except report_service.ReportServiceError as exc:
        handler.write_json(exc.status, _error_payload(exc.code, exc.message, request_id))
        return
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="incident_report_publish",
        result="success",
        cluster=incident.get("cluster"),
        namespace=incident.get("namespace"),
        incident_id=incident_id,
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=scope,
    )
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "report_version": report})


def _handle_incident_report_kb_candidates(handler: JsonHandler, incident_id: str) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
        incident = asyncio.run(incident_store.get_incident(incident_id))
    except ValueError as exc:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", str(exc), request_id))
        return
    except TypeError as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    scope = _incident_resource_scope(incident)
    actor = _authorize_incident_report_write(handler, scope, request_id)
    if actor is None:
        return
    try:
        candidates = asyncio.run(report_service.generate_kb_candidates(incident_id, payload, actor_id=actor.actor_id))
    except report_service.ReportServiceError as exc:
        handler.write_json(exc.status, _error_payload(exc.code, exc.message, request_id))
        return
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="incident_report_kb_candidates",
        result="success",
        cluster=incident.get("cluster"),
        namespace=incident.get("namespace"),
        incident_id=incident_id,
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=scope,
    )
    handler.write_json(HTTPStatus.CREATED, {"service": APP_NAME, "status": "ok", "request_id": request_id, "kb_candidates": candidates})


def _handle_kb_candidate_approve(handler: JsonHandler, incident_id: str, candidate_id: str) -> None:
    request_id = _request_id(handler)
    try:
        incident = asyncio.run(incident_store.get_incident(incident_id))
    except ValueError as exc:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", str(exc), request_id))
        return
    incident_scope = _incident_resource_scope(incident)
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, incident_scope, request_id)
    if actor is None:
        return
    candidate = asyncio.run(report_service.get_kb_candidate(candidate_id))
    if candidate is None or str(candidate.get("incident_id") or "") != incident_id:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", "knowledge candidate not found", request_id))
        return
    candidate_scope = _kb_candidate_scope(candidate)
    admin_override = actor.has_role(ROLE_ADMIN)
    if not admin_override and not _can_approve_kb_candidate(actor, candidate, candidate_scope):
        _record_gateway_authz_audit(
            actor=actor,
            request_id=request_id,
            permission=_PERMISSION_APPROVE_KB_CANDIDATE,
            resource_scope=candidate_scope,
            decision="deny",
            result="forbidden",
        )
        handler.write_json(HTTPStatus.FORBIDDEN, _error_payload("forbidden", f"permission denied: {_PERMISSION_APPROVE_KB_CANDIDATE}", request_id))
        return
    try:
        approved = asyncio.run(
            report_service.approve_kb_candidate(
                candidate_id,
                actor_id=actor.actor_id,
                admin_override=admin_override,
            )
        )
    except report_service.ReportServiceError as exc:
        handler.write_json(exc.status, _error_payload(exc.code, exc.message, request_id))
        return
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="kb_candidate_approve",
        result="admin_override" if admin_override else "success",
        cluster=incident.get("cluster"),
        namespace=incident.get("namespace"),
        incident_id=incident_id,
        permission=_PERMISSION_APPROVE_KB_CANDIDATE,
        decision="allow",
        resource_scope=candidate_scope,
    )
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "kb_candidate": approved})


def _handle_feedback_create(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    incident_id = str(payload.get("incident_id") or "").strip()
    run_id = str(payload.get("run_id") or "").strip()
    try:
        if incident_id:
            incident = asyncio.run(incident_store.get_incident(incident_id))
            scope = _incident_resource_scope(incident)
        elif run_id:
            snapshot, scope, exc = _agent_run_snapshot_for_auth(run_id, request_id)
            if exc is not None:
                _agent_run_error(handler, exc, request_id)
                return
            incident = {}
        else:
            handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", "incident_id or run_id is required", request_id))
            return
    except ValueError as exc:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", str(exc), request_id))
        return
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    try:
        feedback = asyncio.run(report_service.add_feedback(payload, actor_id=actor.actor_id))
    except report_service.ReportServiceError as exc:
        handler.write_json(exc.status, _error_payload(exc.code, exc.message, request_id))
        return
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="human_feedback_create",
        result="success",
        cluster=incident.get("cluster") if incident else None,
        namespace=incident.get("namespace") if incident else None,
        incident_id=incident_id or None,
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=scope,
    )
    handler.write_json(HTTPStatus.CREATED, {"service": APP_NAME, "status": "ok", "request_id": request_id, "feedback": feedback})


def _handle_run_feedback(handler: JsonHandler, run_id: str) -> None:
    request_id = _request_id(handler)
    snapshot, scope, exc = _agent_run_snapshot_for_auth(run_id, request_id)
    if exc is not None:
        _agent_run_error(handler, exc, request_id)
        return
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    feedback = asyncio.run(report_service.list_run_feedback(run_id))
    _record_agent_run_audit(actor, request_id=request_id, action="agent_run_feedback_get", result="success", scope=scope, snapshot=snapshot)
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "feedback": feedback})


def _panel(name: str, fn: Any) -> dict[str, Any]:
    try:
        return {"name": name, "status": "ok", "data": fn()}
    except Exception as exc:
        return {"name": name, "status": "failed", "error": str(exc)}


def _incident_workbench_snapshot(incident: dict[str, Any], *, actor: Actor, request_id: str) -> dict[str, Any]:
    incident_id = str(incident["id"])
    approvals = approval_service.list_requests(incident_id=incident_id, limit=50)
    runs = asyncio.run(agent_run_service.list_incident_runs(incident_id))
    current_run = _current_incident_run(runs)
    executions = [
        approval_execution_service.get_execution(approval["approval_id"])
        for approval in approvals
        if approval_execution_service.get_execution(approval["approval_id"]) is not None
    ]
    historical_runs = [
        {
            "run_id": run["run_id"],
            "title": run.get("title"),
            "status": run.get("status"),
            "route": f"/agent-runs/{quote(str(run['run_id']))}",
            "created_at": run.get("created_at"),
        }
        for run in runs
        if current_run is None or run["run_id"] != current_run["run_id"]
    ]
    return {
        "incident": _overview_incident_row(incident),
        "current_run": current_run,
        "historical_runs": historical_runs,
        "panels": {
            "timeline": _panel("timeline", lambda: asyncio.run(incident_store.get_timeline(incident_id))),
            "evidence": _panel("evidence", lambda: evidence_service.nodes_from_records(asyncio.run(incident_store.list_evidence(incident_id)), scope=_incident_scope_dict(incident), actor=actor)),
            "diagnosis": _panel("diagnosis", lambda: _read_scoped_diagnosis_process(incident_id, incident, actor)[1].get("process")),
            "runs": {"name": "runs", "status": "ok", "data": runs},
            "approvals": {"name": "approvals", "status": "ok", "data": approvals},
            "executions": {"name": "executions", "status": "ok", "data": executions},
            "kb_candidates": _panel("kb_candidates", lambda: asyncio.run(report_service.report_snapshot(incident_id)).get("kb_candidates") or []),
        },
        "responsibility": {
            "owner": incident.get("operator") or incident.get("team") or incident.get("owner_team"),
            "approval_count": len(approvals),
            "execution_count": len(executions),
            "request_id": request_id,
        },
        "permissions": {
            "can_chat": _can_operate_incident(actor),
            "can_start_run": _can_operate_incident(actor),
            "can_control": _can_operate_incident(actor),
            "can_request_action": _can_operate_incident(actor),
        },
    }


def _current_incident_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    active = [run for run in runs if run.get("conversation_status") == agent_run_service.ACTIVE_CONVERSATION]
    return active[0] if active else (runs[0] if runs else None)


def _incident_scope_dict(incident: dict[str, Any]) -> dict[str, str]:
    return {
        "cluster": str(incident.get("cluster") or ""),
        "namespace": str(incident.get("namespace") or ""),
        "service": str(incident.get("service") or incident.get("service_id") or ""),
        "team": str(incident.get("team") or incident.get("owner_team") or ""),
        "environment": str(incident.get("environment") or "prod"),
    }


def _agent_run_scope_dict(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get("scope") if isinstance(payload.get("scope"), dict) else payload
    return {
        "cluster": str(raw.get("cluster") or raw.get("cluster_id") or ""),
        "namespace": str(raw.get("namespace") or ""),
        "service": str(raw.get("service") or raw.get("service_id") or ""),
        "team": str(raw.get("team") or raw.get("team_id") or ""),
        "environment": str(raw.get("environment") or "prod"),
    }


def _kb_candidate_scope(candidate: dict[str, Any]) -> Scope:
    raw = candidate.get("scope") if isinstance(candidate.get("scope"), dict) else {}
    return resource_scope(
        cluster=_scope_value(raw.get("cluster")),
        service=_scope_value(raw.get("service")),
        team=_scope_value(raw.get("team")),
        namespace=_scope_value(raw.get("namespace")),
    )


def _can_approve_kb_candidate(actor: Actor, candidate: dict[str, Any], scope: Scope) -> bool:
    if not (actor.has_role(ROLE_OPERATOR) or actor.has_role(ROLE_APPROVER)):
        return False
    if not scope.is_complete() or not actor.scope.matches(scope):
        return False
    raw = candidate.get("scope") if isinstance(candidate.get("scope"), dict) else {}
    owner = str(candidate.get("owner") or raw.get("team") or "").strip()
    return owner in {actor.actor_id, actor.username, actor.department, *actor.groups}


def _handle_incident_control(handler: JsonHandler, incident_id: str) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    try:
        incident = asyncio.run(incident_store.get_incident(incident_id))
    except ValueError as exc:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", str(exc), request_id))
        return
    scope = _incident_resource_scope(incident)
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    if not _require_incident_operator(handler, actor, request_id):
        return
    action = str(payload.get("action") or "").strip()
    try:
        result = _apply_incident_control(incident, action, payload, actor=actor, request_id=request_id)
    except ValueError as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action=f"incident_control_{action}",
        result="success",
        cluster=incident.get("cluster"),
        namespace=incident.get("namespace"),
        incident_id=incident_id,
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=scope,
    )
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "result": result})


def _apply_incident_control(
    incident: dict[str, Any],
    action: str,
    payload: dict[str, Any],
    *,
    actor: Actor,
    request_id: str,
) -> dict[str, Any]:
    incident_id = str(incident["id"])
    note = str(payload.get("note") or payload.get("reason") or action).strip()
    if action == "resolve":
        asyncio.run(incident_store.update_status(incident_id, "resolved", resolved_at=_now()))
        asyncio.run(incident_store.add_event(incident_id, "resolved", "console_control", "resolve incident", note, {"actor": actor.actor_id, "request_id": request_id}))
    elif action == "reopen":
        asyncio.run(incident_store.reopen_incident(incident_id, note or "reopened from Console"))
    elif action == "manual_takeover":
        asyncio.run(incident_store.update_operator(incident_id, actor.username))
        asyncio.run(incident_store.add_event(incident_id, "investigate_progress", "console_control", "manual takeover", note, {"actor": actor.actor_id, "request_id": request_id}))
    elif action == "block_approvals":
        runs = _active_incident_mainline_runs(incident_id)
        asyncio.run(incident_store.add_event(incident_id, "approval_skipped", "console_control", "block new approvals", note, {"actor": actor.actor_id, "request_id": request_id, "run_id": runs[0]["run_id"] if runs else None}))
    elif action == "pause_run":
        run = _require_current_incident_run(incident_id)
        asyncio.run(agent_run_service.set_run_status(str(run["run_id"]), "paused", actor_id=actor.actor_id, reason=note))
        asyncio.run(incident_store.add_event(incident_id, "investigate_progress", "console_control", action, note, {"actor": actor.actor_id, "request_id": request_id, "run_id": run["run_id"], "approved_execution_unchanged": True}))
    elif action == "terminate_run":
        run = _require_current_incident_run(incident_id)
        asyncio.run(agent_run_service.set_run_status(str(run["run_id"]), "terminated", actor_id=actor.actor_id, reason=note))
        asyncio.run(agent_run_service.archive_conversation(str(run["run_id"])))
        asyncio.run(incident_store.add_event(incident_id, "investigate_progress", "console_control", action, note, {"actor": actor.actor_id, "request_id": request_id, "run_id": run["run_id"]}))
    elif action == "human_note":
        asyncio.run(incident_store.add_event(incident_id, "investigate_progress", "console_control", action, note, {"actor": actor.actor_id, "request_id": request_id}))
    elif action == "restart_run":
        return _restart_incident_run(incident, payload, actor=actor)
    else:
        raise ValueError("unsupported incident control action")
    updated = asyncio.run(incident_store.get_incident(incident_id))
    return {"incident": _overview_incident_row(updated), "action": action}


def _restart_incident_run(incident: dict[str, Any], payload: dict[str, Any], *, actor: Actor) -> dict[str, Any]:
    incident_id = str(incident["id"])
    mode = str(payload.get("mode") or "continue_current")
    runs = _active_incident_mainline_runs(incident_id)
    if runs and mode == "continue_current":
        return {"action": "restart_run", "mode": mode, "current_run": runs[0], "choices": ["continue_current", "start_new", "terminate_old"]}
    if runs and mode in {"start_new", "terminate_old"}:
        for run in runs:
            asyncio.run(agent_run_service.set_run_status(str(run["run_id"]), "terminated", actor_id=actor.actor_id, reason="replaced by new incident run"))
            asyncio.run(agent_run_service.archive_conversation(str(run["run_id"])))
    elif runs:
        raise ValueError("active run exists")
    snapshot = asyncio.run(
        agent_run_service.create_run(
            {
                "title": f"{incident.get('alert_name') or 'Incident'} investigation",
                "message": str(payload.get("message") or "Restart incident investigation"),
                "incident_id": incident_id,
                "scope": {
                    "cluster": incident.get("cluster"),
                    "namespace": incident.get("namespace"),
                    "service": incident.get("service") or incident.get("service_id"),
                    "team": incident.get("team") or incident.get("owner_team"),
                },
                "tags": [incident.get("alert_name"), incident.get("service"), incident.get("team")],
            },
            actor_id=actor.actor_id,
        )
    )
    asyncio.run(incident_store.add_event(incident_id, "investigate_start", "console_control", "restart run", snapshot["run"]["run_id"], {"actor": actor.actor_id}))
    return {"action": "restart_run", "mode": mode, "snapshot": snapshot}


def _active_incident_mainline_runs(incident_id: str) -> list[dict[str, Any]]:
    return [
        run
        for run in asyncio.run(agent_run_service.list_incident_runs(incident_id))
        if run.get("conversation_status") == agent_run_service.ACTIVE_CONVERSATION
    ]


def _require_current_incident_run(incident_id: str) -> dict[str, Any]:
    runs = _active_incident_mainline_runs(incident_id)
    if not runs:
        raise ValueError("no active incident run")
    return runs[0]


def _now() -> float:
    import time

    return time.time()


def _action_scope(action: dict[str, Any]) -> Scope:
    target = action.get("target") if isinstance(action.get("target"), dict) else {}
    return resource_scope(
        cluster=str(target.get("cluster") or "").strip() or None,
        service=str(target.get("service") or "").strip() or None,
        team=str(target.get("team") or "").strip() or None,
        namespace=str(target.get("namespace") or "").strip() or None,
    )


def _action_error(handler: JsonHandler, exc: action_control_service.ActionControlError, request_id: str) -> None:
    payload = _error_payload(exc.code, exc.message, request_id)
    if exc.action is not None:
        payload["action"] = exc.action
    handler.write_json(exc.status, payload)


def _handle_action_detail(handler: JsonHandler, action_id: str) -> None:
    request_id = _request_id(handler)
    action = action_control_service.get_action(action_id)
    if action is None:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", "action not found", request_id))
        return
    scope = _action_scope(action)
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    approval = approval_service.get_request(str(action.get("approval_id") or "")) if action.get("approval_id") else None
    execution = approval_execution_service.get_execution(str(action.get("approval_id") or "")) if action.get("approval_id") else None
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="action_get",
        result="success",
        cluster=action["target"].get("cluster"),
        namespace=action["target"].get("namespace"),
        incident_id=action.get("incident_id"),
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=scope,
        approval_id=action.get("approval_id"),
        action_proposal_id=action.get("action_proposal_id"),
    )
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "action": action, "approval_request": approval, "execution": execution},
    )


def _handle_action_propose(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
        normalized = action_control_service.normalize_action_payload(payload)
    except action_control_service.ActionControlError as exc:
        _action_error(handler, exc, request_id)
        return
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    scope = resource_scope(
        cluster=normalized["target"]["cluster"],
        service=normalized["target"]["service"],
        team=normalized["target"]["team"],
        namespace=normalized["target"]["namespace"],
    )
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
    if actor is None:
        return
    status, response = _propose_action_payload(payload, actor=actor, request_id=request_id, normalized=normalized, scope=scope)
    handler.write_json(status, response)


def _propose_action_payload(
    payload: dict[str, Any],
    *,
    actor: Actor,
    request_id: str,
    normalized: dict[str, Any] | None = None,
    scope: Scope | None = None,
) -> tuple[HTTPStatus, dict[str, Any]]:
    try:
        normalized = normalized or action_control_service.normalize_action_payload(payload)
    except action_control_service.ActionControlError as exc:
        return exc.status, _error_payload(exc.code, exc.message, request_id)
    scope = scope or resource_scope(cluster=normalized["target"]["cluster"], service=normalized["target"]["service"], team=normalized["target"]["team"], namespace=normalized["target"]["namespace"])
    if not actor.can(PERMISSION_VIEW_INCIDENT, scope):
        return HTTPStatus.FORBIDDEN, _error_payload("forbidden", f"permission denied: {PERMISSION_VIEW_INCIDENT}", request_id)
    if not _can_operate_incident(actor):
        return HTTPStatus.FORBIDDEN, _error_payload("forbidden", "operator role is required", request_id)
    binding_denial = resource_catalog_http.proposal_denial(ResourceCatalog(_SESSIONS.database), normalized["target"], request_id, _error_payload)
    if binding_denial:
        return binding_denial
    try:
        disabled_reason = cluster_registry.mutation_disabled_reason(normalized["target"]["cluster"])
    except cluster_registry.ClusterServiceError as exc:
        if exc.code != "not_found":
            return exc.status, _error_payload(exc.code, exc.message, request_id)
        disabled_reason = None
    if disabled_reason:
        _record_gateway_audit(
            actor,
            request_id=request_id,
            action="action_propose",
            result=disabled_reason,
            cluster=normalized["target"]["cluster"],
            namespace=normalized["target"]["namespace"],
            incident_id=normalized.get("incident_id"),
            permission=PERMISSION_VIEW_INCIDENT,
            decision="deny",
            resource_scope=scope,
        )
        return HTTPStatus.CONFLICT, _error_payload(disabled_reason, "cluster connector is offline; mutation is disabled", request_id)
    policy_result = settings_service.test_policy(
        {
            "action_type": normalized["action_type"],
            "cluster": normalized["target"]["cluster"],
            "namespace": normalized["target"]["namespace"],
            "service": normalized["target"]["service"],
            "team": normalized["target"]["team"],
            "risk_level": normalized["risk_level"],
        },
        actor_id=actor.actor_id,
    )
    policy = policy_result["result"]
    if normalized.get("incident_id") and _incident_approvals_blocked(str(normalized["incident_id"])):
        _record_gateway_audit(
            actor,
            request_id=request_id,
            action="action_propose",
            result="approvals_blocked",
            cluster=normalized["target"]["cluster"],
            namespace=normalized["target"]["namespace"],
            incident_id=normalized.get("incident_id"),
            permission=PERMISSION_VIEW_INCIDENT,
            decision="deny",
            resource_scope=scope,
        )
        return HTTPStatus.CONFLICT, _error_payload("approvals_blocked", "new approvals are blocked for this incident run", request_id)
    try:
        action, idempotent = action_control_service.create_proposal(
            payload,
            actor_id=actor.actor_id,
            request_id=request_id,
            policy=policy,
            policy_hit=policy_result.get("policy_hit"),
        )
    except action_control_service.ActionControlError as exc:
        _record_gateway_audit(
            actor,
            request_id=request_id,
            action="action_propose",
            result=exc.code,
            cluster=normalized["target"]["cluster"],
            namespace=normalized["target"]["namespace"],
            incident_id=normalized.get("incident_id"),
            permission=PERMISSION_VIEW_INCIDENT,
            decision="deny",
            resource_scope=scope,
        )
        return exc.status, _error_payload(exc.code, exc.message, request_id)

    approval: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    if not idempotent and policy["decision"] == "approval_required":
        _record_run_action_event(
            action,
            "risk_classified",
            "Gateway classified action risk",
            {"policy": policy, "action_hash": action["action_hash"], "target": action["target"]},
        )
        approval, action = _create_action_approval(action, actor=actor, request_id=request_id, payload=payload)
        _record_run_action_event(
            action,
            "approval_requested",
            "Gateway created approval request",
            {"approval_id": approval["approval_id"], "action_hash": action["action_hash"], "policy": policy},
        )
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action="action_propose",
        result="idempotent" if idempotent else policy["decision"],
        cluster=action["target"].get("cluster"),
        namespace=action["target"].get("namespace"),
        incident_id=action.get("incident_id"),
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=scope,
        approval_id=action.get("approval_id"),
        action_proposal_id=action.get("action_proposal_id"),
    )
    response = {
        "service": APP_NAME,
        "status": "ok",
        "request_id": request_id,
        "idempotent": idempotent,
        "action": action,
        "policy": policy,
        "approval_request": approval,
        "execution": execution,
    }
    return HTTPStatus.OK if idempotent else HTTPStatus.CREATED, response


def _incident_approvals_blocked(incident_id: str) -> bool:
    try:
        timeline = asyncio.run(incident_store.get_timeline(incident_id))
    except Exception:
        return False
    return any(event.get("event_type") == "approval_skipped" and event.get("input_summary") == "block new approvals" for event in timeline)


def _create_action_approval(action: dict[str, Any], *, actor: Actor, request_id: str, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    approval_payload = action_control_service.approval_payload(
        action,
        assigned_approvers=[str(item).strip() for item in payload.get("assigned_approvers", []) if str(item).strip()]
        if isinstance(payload.get("assigned_approvers"), list)
        else [],
        expires_at=_optional_float_payload(payload.get("expires_at")),
    )
    approval, _ = approval_service.create_request(approval_payload, actor_id=actor.actor_id, request_id=request_id)
    action = action_control_service.attach_approval(action["action_id"], approval["approval_id"])
    return approval, action


def _optional_float_payload(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _approval_action(route_path: str) -> tuple[str, str] | None:
    prefix = "/api/approval-requests/"
    if not route_path.startswith(prefix):
        return None
    parts = [part for part in route_path[len(prefix):].split("/") if part]
    if len(parts) != 2 or parts[1] not in {"approve", "reject", "cancel", "expire"}:
        return None
    return parts[0], parts[1]


def _approval_execute_id(route_path: str) -> str | None:
    prefix = "/api/approval-requests/"
    if not route_path.startswith(prefix):
        return None
    parts = [part for part in route_path[len(prefix):].split("/") if part]
    if len(parts) == 2 and parts[1] == "execute":
        return parts[0]
    return None


def _handle_approval_create(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return

    try:
        normalized = approval_service.normalize_create_payload(payload)
    except approval_service.ApprovalServiceError as exc:
        handler.write_json(exc.status, _error_payload(exc.code, exc.message, request_id))
        return

    actor = _authorize_resource(handler, PERMISSION_VIEW_INCIDENT, _resource_scope_from_approval_payload(normalized), request_id)
    if actor is None:
        return
    try:
        approval, idempotent = approval_service.create_request(payload, actor_id=actor.actor_id, request_id=request_id)
    except approval_service.ApprovalServiceError as exc:
        handler.write_json(exc.status, _approval_error_payload(exc, request_id))
        return

    scope = _approval_resource_scope(approval)
    if not actor.can(PERMISSION_VIEW_INCIDENT, scope):
        _record_approval_audit(
            actor,
            request_id=request_id,
            action="approval_create",
            result="forbidden",
            permission=PERMISSION_VIEW_INCIDENT,
            decision="deny",
            resource_scope=scope,
        )
        handler.write_json(
            HTTPStatus.FORBIDDEN,
            _error_payload("forbidden", f"permission denied: {PERMISSION_VIEW_INCIDENT}", request_id),
        )
        return
    if not idempotent:
        notification_type = "blocked:no_approver" if not approval.get("assigned_approvers") else "approval_pending"
        notification_result = _send_approval_notification(notification_type, approval, dedupe_suffix=notification_type)
        approval = approval_service.mark_notification_result(approval["approval_id"], notification_result) or approval
    _record_approval_audit(
        actor,
        request_id=request_id,
        action="approval_create",
        result="idempotent" if idempotent else "success",
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=scope,
        approval=approval,
    )
    if not idempotent:
        _record_approval_timeline(
            approval,
            "approval_requested",
            "approval requested",
            approval["action_summary"],
            request_id=request_id,
        )
        _update_incident_status_best_effort(approval, "pending_approval")
    handler.write_json(
        HTTPStatus.OK if idempotent else HTTPStatus.CREATED,
        {
            "service": APP_NAME,
            "status": "ok",
            "request_id": request_id,
            "idempotent": idempotent,
            "approval_request": approval,
        },
    )


def _handle_approval_decision(handler: JsonHandler, approval_id: str, action: str) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    approval = approval_service.get_request(approval_id)
    if approval is None:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", "approval request not found", request_id))
        return
    scope = _approval_resource_scope(approval)
    session, auth_mode = _request_session(handler)
    if session is None:
        _record_gateway_authz_audit(
            actor=None,
            request_id=request_id,
            permission=PERMISSION_APPROVE_ACTION,
            resource_scope=scope,
            decision="deny",
            result="unauthorized",
        )
        _record_approval_authorization_deny_audit(
            None,
            request_id=request_id,
            result="unauthorized",
            permission=PERMISSION_APPROVE_ACTION,
            resource_scope=scope,
            approval=approval,
        )
        handler.write_json(HTTPStatus.UNAUTHORIZED, _error_payload("unauthorized", "missing or invalid bearer token", request_id))
        return
    if auth_mode == "cookie" and not _csrf_valid(handler, session.token):
        _record_gateway_authz_audit(
            actor=session.actor,
            request_id=request_id,
            permission=PERMISSION_APPROVE_ACTION,
            resource_scope=scope,
            decision="deny",
            result="csrf_required",
        )
        handler.write_json(HTTPStatus.FORBIDDEN, _error_payload("csrf_required", "missing or invalid CSRF token", request_id))
        return
    actor = session.actor
    if not actor.has_role("admin") and not scope.is_complete():
        _record_gateway_authz_audit(
            actor=actor,
            request_id=request_id,
            permission=PERMISSION_APPROVE_ACTION,
            resource_scope=scope,
            decision="deny",
            result="missing_scope",
        )
        _record_approval_authorization_deny_audit(
            actor,
            request_id=request_id,
            result="missing_scope",
            permission=PERMISSION_APPROVE_ACTION,
            resource_scope=scope,
            approval=approval,
        )
        handler.write_json(HTTPStatus.FORBIDDEN, _error_payload("forbidden", f"permission denied: {PERMISSION_APPROVE_ACTION}", request_id))
        return
    if not actor.can(PERMISSION_APPROVE_ACTION, scope):
        _record_gateway_authz_audit(
            actor=actor,
            request_id=request_id,
            permission=PERMISSION_APPROVE_ACTION,
            resource_scope=scope,
            decision="deny",
            result="forbidden",
        )
        _record_approval_authorization_deny_audit(
            actor,
            request_id=request_id,
            result="forbidden",
            permission=PERMISSION_APPROVE_ACTION,
            resource_scope=scope,
            approval=approval,
        )
        handler.write_json(HTTPStatus.FORBIDDEN, _error_payload("forbidden", f"permission denied: {PERMISSION_APPROVE_ACTION}", request_id))
        return

    decision = {"approve": approval_service.APPROVED, "reject": approval_service.REJECTED, "cancel": approval_service.CANCELLED, "expire": approval_service.EXPIRED}[action]
    action_record = action_control_service.get_by_proposal_id(str(approval.get("action_proposal_id") or "")) if decision == approval_service.APPROVED else None
    if resource_catalog_http.deny_approval(handler, ResourceCatalog(_SESSIONS.database), action_record, request_id, _error_payload):
        return
    reason = payload.get("reason")
    try:
        updated, idempotent = approval_service.decide(
            approval_id,
            decision=decision,
            actor_id=actor.actor_id,
            reason=str(reason).strip() if reason is not None else None,
            request_id=request_id,
        )
    except approval_service.ApprovalServiceError as exc:
        result = exc.approval or approval
        _record_approval_audit(
            actor,
            request_id=request_id,
            action=f"approval_{action}",
            result=exc.code,
            permission=PERMISSION_APPROVE_ACTION,
            decision="deny",
            resource_scope=scope,
            approval=result,
        )
        handler.write_json(exc.status, _approval_error_payload(exc, request_id))
        return

    if not idempotent:
        _send_approval_notification(_approval_decision_notification_type(decision), updated, dedupe_suffix=decision)
        _record_approval_timeline(
            updated,
            _approval_decision_event_type(decision),
            f"approval {decision}",
            str(updated.get("decision_reason") or decision),
            request_id=request_id,
        )
    _record_approval_audit(
        actor,
        request_id=request_id,
        action=f"approval_{action}",
        result="idempotent" if idempotent else "success",
        permission=PERMISSION_APPROVE_ACTION,
        decision=decision,
        resource_scope=scope,
        approval=updated,
    )
    auto_execution: dict[str, Any] | None = None
    if decision == approval_service.APPROVED and not idempotent:
        if action_record is not None:
            grant, _ = action_control_service.create_grant(
                action_record,
                grant_type="human_approval",
                source_id=updated["approval_id"],
                actor_id=actor.actor_id,
            )
            _, execution_payload = _execute_approved_mutation(
                _GATEWAY_EXECUTOR_ACTOR,
                updated,
                action_control_service.execution_payload_for(action_record, grant_id=grant["grant_id"]),
                request_id,
                scope,
            )
            auto_execution = execution_payload.get("execution")
    handler.write_json(
        HTTPStatus.OK,
        {
            "service": APP_NAME,
            "status": "ok",
            "request_id": request_id,
            "idempotent": idempotent,
            "approval_request": updated,
            "execution": auto_execution,
        },
    )


def _handle_approval_execute(handler: JsonHandler, approval_id: str) -> None:
    request_id = _request_id(handler)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
        return
    approval = approval_service.get_request(approval_id)
    if approval is None:
        handler.write_json(HTTPStatus.NOT_FOUND, _error_payload("not_found", "approval request not found", request_id))
        return
    scope = _approval_resource_scope(approval)
    actor = _authorize(handler, PERMISSION_EXECUTE_MUTATION, scope, request_id)
    if actor is None:
        return

    status, response = _execute_approved_mutation(actor, approval, payload, request_id, scope)
    handler.write_json(status, response)


def _execute_approved_mutation(
    actor: Actor,
    approval: dict[str, Any],
    payload: dict[str, Any],
    request_id: str,
    scope: Scope,
) -> tuple[HTTPStatus, dict[str, Any]]:
    approval_id = str(approval["approval_id"])
    payload = dict(payload)
    payload.setdefault("grant_id", approval_id)
    try:
        execution, idempotent = approval_execution_service.create_or_replay(
            approval,
            payload,
            actor_id=actor.actor_id,
        )
    except approval_execution_service.ApprovalExecutionError as exc:
        _record_approval_execution_audit(
            actor,
            request_id=request_id,
            action="approval_execute",
            result=exc.code,
            decision="deny",
            resource_scope=scope,
            approval=approval,
        )
        return exc.status, _approval_execution_error_payload(exc, request_id)

    if idempotent:
        _record_approval_execution_audit(
            actor,
            request_id=request_id,
            action="approval_execute",
            result="idempotent",
            decision="allow",
            resource_scope=scope,
            approval=approval,
        )
        return HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "idempotent": True, "execution": execution}

    _record_run_approval_event(
        approval,
        "execution_started",
        "Gateway started automatic execution",
        {"execution_id": execution["execution_id"], "approval_id": approval_id, "executor": actor.actor_id},
    )

    try:
        if payload.get("action_hash"):
            action_control_service.consume_grant(
                str(payload.get("grant_id") or ""),
                action_hash=str(payload["action_hash"]),
                execution_id=str(execution["execution_id"]),
            )
    except action_control_service.ActionControlError as exc:
        _record_approval_execution_audit(
            actor,
            request_id=request_id,
            action="approval_execute_grant",
            result=exc.code,
            decision="deny",
            resource_scope=scope,
            approval=approval,
            execution=execution,
        )
        return exc.status, _error_payload(exc.code, exc.message, request_id)

    lock_key = str(payload.get("lock_key") or "")
    if lock_key:
        try:
            action_control_service.claim_lock(
                lock_key,
                action_id=str(payload.get("action_id") or approval.get("action_proposal_id") or approval_id),
                execution_id=str(execution["execution_id"]),
                owner=actor.actor_id,
            )
        except action_control_service.ActionControlError as exc:
            execution = approval_execution_service.update_execution(
                approval_id,
                "failed",
                error_code=exc.code,
                error_message=exc.message,
            )
            _record_approval_execution_audit(
                actor,
                request_id=request_id,
                action="approval_execute_lock",
                result=exc.code,
                decision="deny",
                resource_scope=scope,
                approval=approval,
                execution=execution,
            )
            _send_execution_notification(approval, execution, dedupe_suffix="lock-failed")
            return exc.status, _execution_response(request_id, execution, ok=False)

    def _release_lock() -> None:
        if lock_key:
            action_control_service.release_lock(lock_key, str(execution["execution_id"]))

    claimed, execution = approval_execution_service.claim(approval_id)
    if not claimed:
        _release_lock()
        status = HTTPStatus.OK if execution["status"] in approval_execution_service.TERMINAL_STATUSES else HTTPStatus.CONFLICT
        return status, {
            "service": APP_NAME,
            "status": "ok" if status == HTTPStatus.OK else "failed",
            "request_id": request_id,
            "idempotent": False,
            "execution": execution,
        }

    _record_approval_timeline(
        approval,
        "remediate_progress",
        "approved mutation preflight started",
        approval["action_summary"],
        request_id=request_id,
        execution_id=execution["execution_id"],
    )
    preflight_result = _dispatch_execution_payload(execution["preflight"], mutation=False)
    if preflight_result.get("status") != "succeeded":
        execution = approval_execution_service.update_execution(
            approval_id,
            "preflight_failed",
            preflight_result=preflight_result,
            error_code=str(preflight_result.get("error_code") or "preflight_failed"),
            error_message=str(preflight_result.get("error_message") or preflight_result.get("stderr") or "preflight failed"),
        )
        _record_approval_execution_audit(
            actor,
            request_id=request_id,
            action="approval_execute_preflight",
            result="preflight_failed",
            decision="allow",
            resource_scope=scope,
            approval=approval,
            execution=execution,
        )
        _record_approval_timeline(
            approval,
            "remediate_progress",
            "approved mutation preflight failed",
            execution["error_message"] or "preflight failed",
            request_id=request_id,
            execution_id=execution["execution_id"],
        )
        _record_run_approval_event(
            approval,
            "preflight_finished",
            "Gateway preflight failed",
            {"execution_id": execution["execution_id"], "result": preflight_result, "status": execution["status"]},
        )
        _send_execution_notification(approval, execution, dedupe_suffix="preflight-failed")
        _release_lock()
        return HTTPStatus.CONFLICT, _execution_response(request_id, execution, ok=False)

    approval_execution_service.update_execution(approval_id, "executing", preflight_result=preflight_result)
    _update_incident_status_best_effort(approval, "executing")
    _record_approval_execution_audit(
        actor,
        request_id=request_id,
        action="approval_execute_preflight",
        result="succeeded",
        decision="allow",
        resource_scope=scope,
        approval=approval,
        execution=execution,
    )
    _record_run_approval_event(
        approval,
        "preflight_finished",
        "Gateway preflight passed",
        {"execution_id": execution["execution_id"], "result": preflight_result, "status": "succeeded"},
    )
    mutation_result = _dispatch_execution_payload(execution["action"], mutation=True)
    if mutation_result.get("status") != "succeeded":
        execution = approval_execution_service.update_execution(
            approval_id,
            "failed",
            preflight_result=preflight_result,
            execution_result=mutation_result,
            error_code=str(mutation_result.get("error_code") or "execution_failed"),
            error_message=str(mutation_result.get("error_message") or mutation_result.get("stderr") or "execution failed"),
        )
        _record_approval_execution_audit(
            actor,
            request_id=request_id,
            action="approval_execute_mutation",
            result="failed",
            decision="allow",
            resource_scope=scope,
            approval=approval,
            execution=execution,
        )
        _record_approval_timeline(
            approval,
            "remediate_progress",
            "approved mutation execution failed",
            execution["error_message"] or "execution failed",
            request_id=request_id,
            execution_id=execution["execution_id"],
        )
        _record_run_approval_event(
            approval,
            "mutation_finished",
            "Gateway mutation failed",
            {"execution_id": execution["execution_id"], "result": mutation_result, "status": execution["status"]},
        )
        _send_execution_notification(approval, execution, dedupe_suffix="failed")
        _release_lock()
        return HTTPStatus.BAD_GATEWAY, _execution_response(request_id, execution, ok=False)

    approval_execution_service.update_execution(approval_id, "post_checking", execution_result=mutation_result)
    _record_approval_execution_audit(
        actor,
        request_id=request_id,
        action="approval_execute_mutation",
        result="succeeded",
        decision="allow",
        resource_scope=scope,
        approval=approval,
        execution=execution,
    )
    _record_approval_timeline(
        approval,
        "remediate_executed",
        "approved mutation executed",
        approval["action_summary"],
        request_id=request_id,
        execution_id=execution["execution_id"],
    )
    _record_run_approval_event(
        approval,
        "mutation_finished",
        "Gateway mutation finished",
        {"execution_id": execution["execution_id"], "result": mutation_result, "status": "succeeded"},
    )
    post_check_result = _dispatch_execution_payload(execution["post_check"], mutation=False)
    if post_check_result.get("status") != "succeeded":
        execution = approval_execution_service.update_execution(
            approval_id,
            "rollback_required",
            preflight_result=preflight_result,
            execution_result=mutation_result,
            post_check_result=post_check_result,
            error_code=str(post_check_result.get("error_code") or "post_check_failed"),
            error_message=str(post_check_result.get("error_message") or post_check_result.get("stderr") or "post-check failed"),
        )
        _record_approval_execution_audit(
            actor,
            request_id=request_id,
            action="approval_execute_post_check",
            result="rollback_required",
            decision="allow",
            resource_scope=scope,
            approval=approval,
            execution=execution,
        )
        _mark_rollback_required(approval, execution, request_id=request_id)
        _send_execution_notification(approval, execution, dedupe_suffix="rollback-required")
        _record_run_approval_event(
            approval,
            "post_check_finished",
            "Gateway post-check failed",
            {"execution_id": execution["execution_id"], "result": post_check_result, "status": execution["status"]},
        )
        _release_lock()
        return HTTPStatus.CONFLICT, _execution_response(request_id, execution, ok=False)

    execution = approval_execution_service.update_execution(
        approval_id,
        "succeeded",
        preflight_result=preflight_result,
        execution_result=mutation_result,
        post_check_result=post_check_result,
    )
    _record_approval_execution_audit(
        actor,
        request_id=request_id,
        action="approval_execute_post_check",
        result="succeeded",
        decision="allow",
        resource_scope=scope,
        approval=approval,
        execution=execution,
    )
    _record_approval_timeline(
        approval,
        "remediate_verified",
        "approved mutation post-check passed",
        approval["action_summary"],
        request_id=request_id,
        execution_id=execution["execution_id"],
    )
    _record_run_approval_event(
        approval,
        "post_check_finished",
        "Gateway post-check passed",
        {"execution_id": execution["execution_id"], "result": post_check_result, "status": execution["status"]},
    )
    _send_execution_notification(approval, execution, dedupe_suffix="succeeded")
    _release_lock()
    return HTTPStatus.OK, _execution_response(request_id, execution, ok=True)


def _resource_scope_from_approval_payload(payload: dict[str, Any]) -> Scope:
    raw = payload.get("resource_scope") if isinstance(payload.get("resource_scope"), dict) else {}
    return _approval_scope_from_raw(raw)


def _send_approval_notification(notification_type: str, approval: dict[str, Any], *, dedupe_suffix: str) -> dict[str, Any]:
    payload = {
        "notification_type": notification_type,
        "notification_id": f"{notification_type}-{approval['approval_id']}",
        "incident_id": approval["incident_id"],
        "approval_id": approval["approval_id"],
        "summary": approval["action_summary"],
        "risk_level": approval["risk_level"],
        "dedupe_key": f"{notification_type}:{approval['approval_id']}:{dedupe_suffix}",
        "context": {
            "incident_id": approval["incident_id"],
            "session_id": approval["session_id"],
            "action_proposal_id": approval["action_proposal_id"],
            "approval_id": approval["approval_id"],
            "risk_level": approval["risk_level"],
            "status": approval["status"],
            "resource_scope": approval.get("resource_scope") or {},
            **(approval.get("resource_scope") or {}),
        },
    }
    try:
        return notification_center.send_notification(payload)
    except Exception as exc:
        return {
            "ok": False,
            "delivery": {
                "delivery_status": "failed",
                "last_delivery_error": str(exc),
            },
        }


def _send_execution_notification(approval: dict[str, Any], execution: dict[str, Any], *, dedupe_suffix: str) -> dict[str, Any]:
    return _send_approval_notification(
        _execution_notification_type(str(execution.get("status") or "")),
        {
            **approval,
            "status": execution["status"],
            "action_summary": execution.get("error_message") or approval["action_summary"],
        },
        dedupe_suffix=dedupe_suffix,
    )


def _send_console_next_notification(
    notification_type: str,
    *,
    summary: str,
    actor: Actor,
    dedupe_key: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "notification_type": notification_type,
        "notification_id": f"{notification_type}-{uuid.uuid4().hex}",
        "summary": summary,
        "dedupe_key": dedupe_key,
        "context": {
            "actor": actor.actor_id,
            "status": "changed",
            **(context or {}),
        },
    }
    try:
        return notification_center.send_notification(payload)
    except Exception as exc:
        return {"ok": False, "delivery": {"delivery_status": "failed", "last_delivery_error": str(exc)}}


def _approval_decision_notification_type(decision: str) -> str:
    if decision == approval_service.APPROVED:
        return "approval_approved"
    if decision == approval_service.REJECTED:
        return "approval_rejected"
    return "approval_result"


def _execution_notification_type(status: str) -> str:
    if status == "succeeded":
        return "execution_succeeded"
    if status == "rollback_required":
        return "execution_rollback_required"
    return "execution_failed"


def _dispatch_execution_payload(payload: dict[str, Any], *, mutation: bool) -> dict[str, Any]:
    try:
        envelope = build_mutation_envelope(payload) if mutation else build_read_envelope(payload)
    except (TypeError, ValueError) as exc:
        return {"status": "command_rejected", "error_code": "invalid_request", "error_message": str(exc)}
    connector_url = os.getenv("AIOPS_CONNECTOR_URL", "")
    if not connector_url:
        return {"status": "failed", "error_code": "connector_offline", "error_message": "AIOPS_CONNECTOR_URL is not set"}
    route = next((item for item in _ROUTES.values() if item.cluster_id == envelope.cluster_id), None)
    if route is None:
        return {"status": "failed", "error_code": "connector_offline", "error_message": "no connector route for cluster"}
    return dispatch_read_envelope(envelope, route=route, connector_url=connector_url).to_dict()


def _execution_response(request_id: str, execution: dict[str, Any], *, ok: bool) -> dict[str, Any]:
    payload = {
        "service": APP_NAME,
        "status": "ok" if ok else "failed",
        "request_id": request_id,
        "idempotent": False,
        "execution": execution,
    }
    if not ok:
        payload["error"] = {
            "code": execution.get("error_code") or execution.get("status") or "execution_failed",
            "message": execution.get("error_message") or "execution failed",
        }
    return payload


def _approval_decision_event_type(decision: str) -> str:
    if decision == approval_service.APPROVED:
        return "approval_approved"
    if decision == approval_service.EXPIRED:
        return "approval_expired"
    return "approval_denied"


def _record_approval_timeline(
    approval: dict[str, Any],
    event_type: str,
    input_summary: str,
    output_summary: str,
    *,
    request_id: str,
    execution_id: str | None = None,
) -> None:
    try:
        asyncio.run(
            incident_store.add_event(
                approval["incident_id"],
                event_type,
                "gateway",
                input_summary,
                output_summary,
                {
                    "request_id": request_id,
                    "approval_id": approval.get("approval_id"),
                    "action_proposal_id": approval.get("action_proposal_id"),
                    "execution_id": execution_id,
                },
            )
        )
    except (KeyError, ValueError):
        return


def _record_run_action_event(action: dict[str, Any] | None, event_type: str, message: str, payload: dict[str, Any]) -> None:
    if not action:
        return
    run_id = str(action.get("run_id") or "").strip()
    if not run_id:
        return
    try:
        asyncio.run(agent_run_service.append_event(run_id, event_type, message, payload, actor_id="gateway"))
    except Exception:
        return


def _record_run_approval_event(approval: dict[str, Any], event_type: str, message: str, payload: dict[str, Any]) -> None:
    action = action_control_service.get_by_proposal_id(str(approval.get("action_proposal_id") or ""))
    _record_run_action_event(action, event_type, message, payload)


def _mark_rollback_required(approval: dict[str, Any], execution: dict[str, Any], *, request_id: str) -> None:
    try:
        asyncio.run(
            incident_store.mark_rollback_required(
                approval["incident_id"],
                reason_code=str(execution.get("error_code") or "post_check_failed"),
                summary=str(execution.get("error_message") or "post-check failed; rollback required"),
                metadata={
                    "request_id": request_id,
                    "approval_id": approval.get("approval_id"),
                    "action_proposal_id": approval.get("action_proposal_id"),
                    "execution_id": execution.get("execution_id"),
                    "rollback_plan": approval.get("rollback_plan"),
                },
                tool_name="gateway",
            )
        )
    except (KeyError, ValueError):
        return


def _update_incident_status_best_effort(approval: dict[str, Any], status: str) -> None:
    try:
        incident = asyncio.run(incident_store.get_incident(approval["incident_id"]))
        current = str(incident.get("status") or "")
        path = _incident_status_path(current, status)
        for next_status in path:
            asyncio.run(incident_store.update_status(approval["incident_id"], next_status))
    except (KeyError, ValueError):
        return


def _incident_status_path(current: str, target: str) -> list[str]:
    if current == target:
        return []
    paths = {
        ("new", "pending_approval"): ["triaging", "investigating", "pending_approval"],
        ("triaging", "pending_approval"): ["investigating", "pending_approval"],
        ("investigating", "pending_approval"): ["pending_approval"],
        ("new", "executing"): ["triaging", "investigating", "pending_approval", "executing"],
        ("triaging", "executing"): ["investigating", "pending_approval", "executing"],
        ("investigating", "executing"): ["pending_approval", "executing"],
        ("pending_approval", "executing"): ["executing"],
    }
    return paths.get((current, target), [target])


def _record_approval_audit(
    actor: Actor,
    *,
    request_id: str,
    action: str,
    result: str,
    permission: str,
    decision: str,
    resource_scope: Scope,
    approval: dict[str, Any] | None = None,
) -> None:
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action=action,
        result=result,
        incident_id=approval.get("incident_id") if approval else None,
        permission=permission,
        decision=decision,
        resource_scope=resource_scope,
        approval_id=approval.get("approval_id") if approval else None,
        action_proposal_id=approval.get("action_proposal_id") if approval else None,
    )


def _record_approval_execution_audit(
    actor: Actor,
    *,
    request_id: str,
    action: str,
    result: str,
    decision: str,
    resource_scope: Scope,
    approval: dict[str, Any],
    execution: dict[str, Any] | None = None,
) -> None:
    _record_gateway_audit(
        actor,
        request_id=request_id,
        action=action,
        result=result,
        cluster=execution.get("cluster_id") if execution else None,
        namespace=execution.get("namespace") if execution else None,
        incident_id=approval.get("incident_id"),
        permission=PERMISSION_EXECUTE_MUTATION,
        decision=decision,
        resource_scope=resource_scope,
        approval_id=approval.get("approval_id"),
        action_proposal_id=approval.get("action_proposal_id"),
    )


def _actor_from_request(handler: JsonHandler) -> Actor | None:
    token = _extract_bearer_token(handler.headers.get("Authorization"))
    session = _SESSIONS.get(token or "")
    return session.actor if session is not None else None


def _record_approval_authorization_deny_audit(
    actor: Actor | None,
    *,
    request_id: str,
    result: str,
    permission: str,
    resource_scope: Scope,
    approval: dict[str, Any],
) -> None:
    asyncio.run(
        audit_log.record_audit(
            who=actor.username if actor else "anonymous",
            what="approval_authorize",
            trigger="gateway",
            tool_level="control-plane",
            tool_name="gateway",
            result=result,
            incident_id=approval.get("incident_id"),
            actor=actor.actor_id if actor else None,
            role=_audit_role(actor) if actor else None,
            scope=actor.scope.to_dict() if actor else None,
            request_id=request_id,
            permission=permission,
            decision="deny",
            resource_scope=resource_scope.to_dict(),
            approval_id=approval.get("approval_id"),
            action_proposal_id=approval.get("action_proposal_id"),
        )
    )


def _approval_error_payload(exc: approval_service.ApprovalServiceError, request_id: str) -> dict[str, Any]:
    payload = _error_payload(exc.code, exc.message, request_id)
    if exc.approval is not None:
        payload["approval_request"] = exc.approval
    return payload


def _approval_execution_error_payload(
    exc: approval_execution_service.ApprovalExecutionError,
    request_id: str,
) -> dict[str, Any]:
    payload = _error_payload(exc.code, exc.message, request_id)
    if exc.execution is not None:
        payload["execution"] = exc.execution
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIOps K8s Gateway service")
    parser.add_argument("--host", default=os.getenv("AIOPS_GATEWAY_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AIOPS_GATEWAY_PORT", "8080")))
    return parser


def main() -> None:
    """Start the Gateway HTTP service."""
    args = _build_parser().parse_args()
    start_incident_reconciler(_incident_service(), connector_commands=ConnectorCommands(_SESSIONS.database))
    start_diagnosis_delivery(DiagnosisDelivery(_SESSIONS.database))
    serve(GatewayHandler, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
