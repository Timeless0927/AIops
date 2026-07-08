"""Smokeable entry point for the AIOps K8s Gateway process."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
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
    PERMISSION_QUERY_AUDIT,
    PERMISSION_SYNC_LDAP,
    PERMISSION_VIEW_EVIDENCE,
    PERMISSION_VIEW_INCIDENT,
    PERMISSION_MANAGE_USERS,
    PERMISSION_VIEW_USERS,
    PERMISSION_VIEW_POLICY,
    PERMISSION_VIEW_SETTINGS,
    ROLE_OPERATOR,
    Scope,
    SessionTokenStore,
    SQLiteIdentityStore,
    resource_scope,
    role_permission_matrix,
)
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
from . import settings_service
from .alertmanager_webhook import handle_http_request
from .command_service import build_mutation_envelope, build_read_envelope, dispatch_read_envelope
from .connector_router import ConnectorRoute
from .diagnosis_writeback import (
    apply_diagnosis_writeback,
    authorize_writeback_request,
    read_diagnosis_process_view,
    read_incident_view,
)
from .case_profile_service import apply_case_profile, read_case_profile


_ROUTES: dict[str, ConnectorRoute] = {}
_SESSIONS = SessionTokenStore()
_MISSING_SCOPE_VALUE = "__missing_scope__"
_GATEWAY_SERVICE_TOKEN_ENV = "AIOPS_GATEWAY_SERVICE_TOKEN"
_SESSION_COOKIE_NAME = "aiops_session"
_CSRF_HEADER_NAME = "X-CSRF-Token"
_CSRF_MESSAGE = b"aiops-console-csrf"
_APP_ROUTE_PREFIXES = (
    "/incidents",
    "/agent-runs",
    "/approvals",
    "/audit",
    "/policies",
    "/users",
    "/settings",
    "/search",
    "/notifications",
)
_APP_ROUTE_EXACT = {"/", "/login"}
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


def _identity_provider() -> IdentityProvider:
    return IdentityProvider(IdentityConfig.load())


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
        cluster=_required_scope_value(raw.get("cluster") or raw.get("cluster_id")),
        service=_required_scope_value(raw.get("service") or raw.get("service_id")),
        team=_required_scope_value(raw.get("team") or raw.get("team_id")),
        namespace=_required_scope_value(raw.get("namespace")),
    )


def _agent_run_scope_from_payload(payload: dict[str, Any]) -> Scope:
    raw = payload.get("scope") if isinstance(payload.get("scope"), dict) else payload
    return resource_scope(
        cluster=_required_scope_value(raw.get("cluster") or raw.get("cluster_id")),
        service=_required_scope_value(raw.get("service") or raw.get("service_id")),
        team=_required_scope_value(raw.get("team") or raw.get("team_id")),
        namespace=_required_scope_value(raw.get("namespace")),
    )


def _agent_run_scope_from_run(run: dict[str, Any]) -> Scope:
    raw = run.get("scope") if isinstance(run.get("scope"), dict) else {}
    return resource_scope(
        cluster=_required_scope_value(raw.get("cluster")),
        service=_required_scope_value(raw.get("service")),
        team=_required_scope_value(raw.get("team")),
        namespace=_required_scope_value(raw.get("namespace")),
    )


def _approval_resource_scope(approval: dict[str, Any]) -> Scope:
    raw = approval.get("resource_scope") if isinstance(approval.get("resource_scope"), dict) else {}
    return resource_scope(
        cluster=str(raw.get("cluster_id") or raw.get("cluster") or "").strip() or None,
        service=str(raw.get("service_id") or raw.get("service") or "").strip() or None,
        team=str(raw.get("team_id") or raw.get("team") or "").strip() or None,
        namespace=str(raw.get("namespace") or "").strip() or None,
    )


def _required_scope_value(value: Any) -> str:
    text = str(value or "").strip()
    return text or _MISSING_SCOPE_VALUE


def _incident_resource_scope(incident: dict[str, Any]) -> Scope:
    return resource_scope(
        cluster=_required_scope_value(incident.get("cluster")),
        service=_required_scope_value(incident.get("service")),
        team=_required_scope_value(incident.get("team")),
        namespace=_required_scope_value(incident.get("namespace")),
    )


def _authorize(handler: JsonHandler, permission: str, scope: Scope, request_id: str) -> Actor | None:
    token = _extract_bearer_token(handler.headers.get("Authorization"))
    service_actor = _service_actor_for_token(token, permission, scope)
    if service_actor is not None:
        return service_actor
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
    if not actor.can(permission, scope):
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


def _service_actor_for_token(token: str | None, permission: str, scope: Scope) -> Actor | None:
    if permission != PERMISSION_K8S_READ or not token:
        return None
    configured = os.getenv(_GATEWAY_SERVICE_TOKEN_ENV, "").strip()
    if not configured or not hmac.compare_digest(token, configured):
        return None
    if not _DIAGNOSIS_SERVICE_ACTOR.can(permission, scope):
        return None
    return _DIAGNOSIS_SERVICE_ACTOR


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


class GatewayHandler(JsonHandler):
    """Minimal Gateway HTTP surface used by image and compose smoke tests."""

    def do_GET(self) -> None:  # noqa: N802
        if self.is_metrics_request():
            self.write_metrics(APP_NAME)
            return
        parsed = urlparse(self.path)
        route_path = parsed.path
        query = parse_qs(parsed.query)

        if _serve_console_asset(self, route_path):
            return

        incident_id = _parse_incident_view_route(route_path)

        if incident_id is not None:
            denied = authorize_writeback_request(
                method="GET",
                path=self.path,
                body=b"",
                headers=dict(self.headers),
            )
            if denied is not None:
                status, payload = denied
                self.write_json(status, {"service": APP_NAME, **payload})
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

        if route_path == "/api/search":
            _handle_global_search(self, query)
            return

        if route_path == "/api/users":
            _handle_user_list(self)
            return

        if route_path == "/api/settings":
            _handle_settings_get(self)
            return

        if route_path == "/api/policies":
            _handle_policies_get(self)
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

        agent_run = _agent_run_route(route_path)
        if agent_run is not None:
            run_id, action = agent_run
            if action == "snapshot":
                _handle_agent_run_snapshot(self, run_id)
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
            actor = _authorize(self, PERMISSION_VIEW_INCIDENT, scope, request_id)
            if actor is None:
                return
            status, payload = asyncio.run(read_diagnosis_process_view(process_incident_id))
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
            actor = _authorize(self, PERMISSION_VIEW_INCIDENT, scope, request_id)
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
            actor = _authorize(self, PERMISSION_VIEW_INCIDENT, scope, request_id)
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
        parsed = urlparse(self.path)
        route_path = parsed.path

        if route_path == "/diagnosis/writeback":
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length > 0 else b""
            denied = authorize_writeback_request(
                method="POST",
                path=self.path,
                body=body,
                headers=dict(self.headers),
            )
            if denied is not None:
                status, payload = denied
                self.write_json(status, {"service": APP_NAME, **payload})
                return
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
                if not isinstance(payload, dict):
                    raise ValueError("request body must be a JSON object")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self.write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"service": APP_NAME, "status": "invalid", "error": str(exc)},
                )
                return
            status, result = asyncio.run(apply_diagnosis_writeback(payload))
            self.write_json(status, {"service": APP_NAME, **result})
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

        if route_path == "/api/policies/test":
            _handle_policy_test(self)
            return

        if route_path in {"/api/evidence/query", "/api/evidence/agent-query"}:
            _handle_evidence_query(self, agent=route_path.endswith("/agent-query"))
            return

        if route_path == "/api/agent-runs":
            _handle_agent_run_create(self)
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
                session = _SESSIONS.issue(actor)
            except IdentityError as exc:
                status = HTTPStatus.SERVICE_UNAVAILABLE if exc.code == "ldap_unavailable" else HTTPStatus.UNAUTHORIZED
                self.write_json(status, _error_payload(exc.code, exc.message, request_id))
                return
            except (TypeError, ValueError) as exc:
                self.write_json(HTTPStatus.BAD_REQUEST, _error_payload("invalid_request", str(exc), request_id))
                return

            _record_gateway_audit(actor, request_id=request_id, action="ldap_login", result="success")
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "request_id": request_id,
                    "token": session.token,
                    "expires_at": session.expires_at,
                    "actor": actor.to_dict(),
                    "role_permission_matrix": role_permission_matrix(),
                },
                headers={"Set-Cookie": _session_cookie_header(self, session.token, _SESSIONS.ttl_seconds)},
            )
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
            try:
                payload = self.read_json_body()
                actor = _authorize(self, PERMISSION_K8S_READ, _resource_scope_from_payload(payload), request_id)
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
            status, payload = handle_http_request(body, dict(self.headers))
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

        if route_path != "/connectors/register":
            self.write_not_found()
            return

        try:
            payload = self.read_json_body()
            connector_id = str(payload["connector_id"])
            cluster_id = str(payload["cluster_id"])
        except (KeyError, ValueError, TypeError) as exc:
            self.write_json(
                HTTPStatus.BAD_REQUEST,
                {"service": APP_NAME, "status": "invalid", "error": str(exc)},
            )
            return

        route = ConnectorRoute(
            cluster_id=cluster_id,
            connector_id=connector_id,
            session_id=f"session-{uuid.uuid4().hex}",
        )
        _ROUTES[connector_id] = route
        self.write_json(
            HTTPStatus.CREATED,
            {
                "service": APP_NAME,
                "status": "registered",
                "route": asdict(route),
            },
        )

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route_path = parsed.path
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

    def remember(scope: dict[str, Any]) -> None:
        for resource_kind, key in (("cluster", "cluster"), ("namespace", "namespace"), ("service", "service"), ("team", "team")):
            value = str(scope.get(key) or scope.get(f"{key}_id") or "").strip()
            if not value or value == _MISSING_SCOPE_VALUE:
                continue
            resources[(resource_kind, value)] = {
                "type": resource_kind,
                "id": value,
                "title": value,
                "subtitle": resource_kind,
                "route": f"/search?type={quote(resource_kind)}&q={quote(value)}",
                "status": "visible",
                "scope": {key: value},
            }

    if actor.can(PERMISSION_VIEW_INCIDENT, Scope()):
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

    if actor.can(PERMISSION_APPROVE_ACTION, Scope()):
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

    if actor.can(PERMISSION_QUERY_AUDIT, Scope()):
        for chain in asyncio.run(audit_chain_service.list_chains(actor, limit=500)):
            raw_scope = chain.get("scope") if isinstance(chain.get("scope"), dict) else {}
            remember(raw_scope)
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
            for key in ("clusters", "namespaces", "services", "teams"):
                for value in scope.get(key, []) or []:
                    remember({key.removesuffix("s"): value})
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

    for result in resources.values():
        add(result)
    return results


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
    if len(parts) == 2 and parts[1] in {"events", "stream", "messages", "promote", "archive", "delete", "conversation", "feedback"}:
        return parts[0], parts[1]
    return None


def _agent_run_error(handler: JsonHandler, exc: agent_run_service.AgentRunServiceError, request_id: str) -> None:
    handler.write_json(exc.status, _error_payload(exc.code, exc.message, request_id))


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
    after_id = _query_int(query, "after_id", default=0)
    events = asyncio.run(agent_run_service.events(run_id, after_id=after_id))
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "events": events})


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
        permission=PERMISSION_VIEW_INCIDENT,
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
    _handle_agent_run_mutation(handler, run_id, "agent_run_message", agent_run_service.append_message, actor_arg=True)


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
    actor = _authorize(handler, PERMISSION_VIEW_EVIDENCE, scope, request_id)
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
    try:
        version = settings_service.save(payload, actor_id=actor.actor_id)
    except settings_service.SettingsServiceError as exc:
        _record_settings_audit(actor, request_id=request_id, action="settings_save", result=exc.code)
        _settings_error(handler, exc, request_id)
        return
    _record_settings_audit(actor, request_id=request_id, action="settings_save", result="success")
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "settings_version": version},
    )


def _handle_settings_rollback(handler: JsonHandler) -> None:
    request_id = _request_id(handler)
    actor = _authorize(handler, PERMISSION_MANAGE_SETTINGS, Scope(), request_id)
    if actor is None:
        return
    try:
        version = settings_service.rollback(actor_id=actor.actor_id)
    except settings_service.SettingsServiceError as exc:
        _record_settings_audit(actor, request_id=request_id, action="settings_rollback", result=exc.code)
        _settings_error(handler, exc, request_id)
        return
    _record_settings_audit(actor, request_id=request_id, action="settings_rollback", result="success")
    handler.write_json(
        HTTPStatus.OK,
        {"service": APP_NAME, "status": "ok", "request_id": request_id, "settings_version": version},
    )


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
    if len(parts) == 3 and parts[1] == "report" and parts[2] in {"draft", "publish"}:
        return parts[0], parts[2]
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
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=scope,
    )
    handler.write_json(HTTPStatus.OK, {"service": APP_NAME, "status": "ok", "request_id": request_id, "workbench": workbench})


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
        permission=PERMISSION_VIEW_INCIDENT,
        decision="allow",
        resource_scope=scope,
    )
    if _first_query_value(query, "format") == "html":
        _write_html(handler, str((latest or {}).get("html") or "<article><h1>unknown</h1></article>"))
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
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
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
    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, scope, request_id)
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
    runs = [run for run in asyncio.run(agent_run_service.list_runs()) if run.get("incident_id") == incident_id]
    executions = [
        approval_execution_service.get_execution(approval["approval_id"])
        for approval in approvals
        if approval_execution_service.get_execution(approval["approval_id"]) is not None
    ]
    return {
        "incident": _overview_incident_row(incident),
        "panels": {
            "timeline": _panel("timeline", lambda: asyncio.run(incident_store.get_timeline(incident_id))),
            "evidence": _panel("evidence", lambda: asyncio.run(incident_store.list_evidence(incident_id))),
            "diagnosis": _panel("diagnosis", lambda: asyncio.run(read_diagnosis_process_view(incident_id))[1].get("process")),
            "runs": {"name": "runs", "status": "ok", "data": runs},
            "approvals": {"name": "approvals", "status": "ok", "data": approvals},
            "executions": {"name": "executions", "status": "ok", "data": executions},
        },
        "responsibility": {
            "owner": incident.get("operator") or incident.get("team") or incident.get("owner_team"),
            "approval_count": len(approvals),
            "execution_count": len(executions),
            "request_id": request_id,
        },
        "permissions": {
            "can_control": actor.can(PERMISSION_VIEW_INCIDENT, _incident_resource_scope(incident)),
            "can_request_action": actor.can(PERMISSION_VIEW_INCIDENT, _incident_resource_scope(incident)),
        },
    }


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
        asyncio.run(incident_store.add_event(incident_id, "approval_skipped", "console_control", "block new approvals", note, {"actor": actor.actor_id, "request_id": request_id}))
    elif action in {"pause_run", "terminate_run", "human_note"}:
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
    runs = [run for run in asyncio.run(agent_run_service.list_runs()) if run.get("incident_id") == incident_id and run.get("status") == "running"]
    if runs and mode == "continue_current":
        return {"action": "restart_run", "mode": mode, "current_run": runs[0], "choices": ["continue_current", "start_new", "terminate_old"]}
    if runs and mode == "terminate_old":
        asyncio.run(agent_run_service.archive_conversation(str(runs[0]["run_id"])))
    elif runs and mode != "start_new":
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
        _action_error(handler, exc, request_id)
        return

    approval: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    if not idempotent and policy["decision"] == "approval_required":
        approval, action = _create_action_approval(action, actor=actor, request_id=request_id, payload=payload)
    elif not idempotent and policy["decision"] in {"policy_grant", "auto_execute"}:
        grant, _ = action_control_service.create_grant(
            action,
            grant_type="policy",
            source_id=str((policy_result.get("policy_hit") or {}).get("id") or policy["reason"]),
            actor_id=actor.actor_id,
        )
        synthetic = action_control_service.synthetic_approval_for_policy_grant(action, grant)
        _, execution_payload = _execute_approved_mutation(
            actor,
            synthetic,
            action_control_service.execution_payload_for(action, grant_id=grant["grant_id"]),
            request_id,
            scope,
        )
        execution = execution_payload.get("execution")

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
    handler.write_json(
        HTTPStatus.OK if idempotent else HTTPStatus.CREATED,
        {
            "service": APP_NAME,
            "status": "ok",
            "request_id": request_id,
            "idempotent": idempotent,
            "action": action,
            "policy": policy,
            "approval_request": approval,
            "execution": execution,
        },
    )


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

    actor = _authorize(handler, PERMISSION_VIEW_INCIDENT, _resource_scope_from_approval_payload(normalized), request_id)
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
        notification_result = _send_approval_notification("approval_required", approval, dedupe_suffix="required")
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

    decision = {
        "approve": approval_service.APPROVED,
        "reject": approval_service.REJECTED,
        "cancel": approval_service.CANCELLED,
        "expire": approval_service.EXPIRED,
    }[action]
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
        _send_approval_notification("approval_result", updated, dedupe_suffix=decision)
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
        action_record = action_control_service.get_by_proposal_id(str(updated.get("action_proposal_id") or ""))
        if action_record is not None:
            grant, _ = action_control_service.create_grant(
                action_record,
                grant_type="human_approval",
                source_id=updated["approval_id"],
                actor_id=actor.actor_id,
            )
            _, execution_payload = _execute_approved_mutation(
                actor,
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
    _send_execution_notification(approval, execution, dedupe_suffix="succeeded")
    _release_lock()
    return HTTPStatus.OK, _execution_response(request_id, execution, ok=True)


def _resource_scope_from_approval_payload(payload: dict[str, Any]) -> Scope:
    raw = payload.get("resource_scope") if isinstance(payload.get("resource_scope"), dict) else {}
    return resource_scope(
        cluster=str(raw.get("cluster_id") or raw.get("cluster") or "").strip() or None,
        service=str(raw.get("service_id") or raw.get("service") or "").strip() or None,
        team=str(raw.get("team_id") or raw.get("team") or "").strip() or None,
        namespace=str(raw.get("namespace") or "").strip() or None,
    )


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
        "execution_result",
        {
            **approval,
            "status": execution["status"],
            "action_summary": execution.get("error_message") or approval["action_summary"],
        },
        dedupe_suffix=dedupe_suffix,
    )


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
    serve(GatewayHandler, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
