"""Smokeable entry point for the AIOps K8s Gateway process."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from dataclasses import asdict
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse

from aiops.domain.identity import (
    Actor,
    IdentityConfig,
    IdentityError,
    IdentityProvider,
    PERMISSION_APPROVE_ACTION,
    PERMISSION_K8S_READ,
    PERMISSION_QUERY_AUDIT,
    PERMISSION_SYNC_LDAP,
    PERMISSION_VIEW_INCIDENT,
    Scope,
    SessionTokenStore,
    resource_scope,
    role_permission_matrix,
)
from apps.service_http import JsonHandler, connectivity_payload, serve
from toolsets import audit_log, incident_store

from . import APP_NAME
from . import approval_service
from . import notification_center
from .alertmanager_webhook import handle_http_request
from .command_service import build_read_envelope, dispatch_read_envelope
from .connector_router import ConnectorRoute
from .diagnosis_writeback import apply_diagnosis_writeback, authorize_writeback_request, read_incident_view
from .case_profile_service import apply_case_profile, read_case_profile


_ROUTES: dict[str, ConnectorRoute] = {}
_SESSIONS = SessionTokenStore()
_MISSING_SCOPE_VALUE = "__missing_scope__"


def _identity_provider() -> IdentityProvider:
    return IdentityProvider(IdentityConfig.load())


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


def _resource_scope_from_payload(payload: dict[str, Any]) -> Scope:
    return resource_scope(
        service=str(payload.get("service") or "").strip() or None,
        team=str(payload.get("team") or "").strip() or None,
        namespace=str(payload.get("namespace") or "").strip() or None,
    )


def _approval_resource_scope(approval: dict[str, Any]) -> Scope:
    raw = approval.get("resource_scope") if isinstance(approval.get("resource_scope"), dict) else {}
    return resource_scope(
        service=str(raw.get("service_id") or raw.get("service") or "").strip() or None,
        team=str(raw.get("team_id") or raw.get("team") or "").strip() or None,
        namespace=str(raw.get("namespace") or "").strip() or None,
    )


def _required_scope_value(value: Any) -> str:
    text = str(value or "").strip()
    return text or _MISSING_SCOPE_VALUE


def _incident_resource_scope(incident: dict[str, Any]) -> Scope:
    return resource_scope(
        service=_required_scope_value(incident.get("service")),
        team=_required_scope_value(incident.get("team")),
        namespace=_required_scope_value(incident.get("namespace")),
    )


def _authorize(handler: JsonHandler, permission: str, scope: Scope, request_id: str) -> Actor | None:
    token = _extract_bearer_token(handler.headers.get("Authorization"))
    session = _SESSIONS.get(token or "")
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

        if route_path == "/auth/me":
            request_id = _request_id(self)
            token = _extract_bearer_token(self.headers.get("Authorization"))
            session = _SESSIONS.get(token or "")
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

        if route_path in {"/notifications/retry", "/api/notifications/retry"}:
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


def _parse_incident_view_route(path: str) -> str | None:
    parts = [part for part in urlparse(path).path.split("/") if part]
    if len(parts) == 2 and parts[0] == "incidents" and parts[1].strip():
        return parts[1].strip()
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


def _approval_detail_id(route_path: str) -> str | None:
    prefix = "/api/approval-requests/"
    if not route_path.startswith(prefix):
        return None
    suffix = route_path[len(prefix):].strip("/")
    if not suffix or "/" in suffix:
        return None
    return suffix


def _approval_action(route_path: str) -> tuple[str, str] | None:
    prefix = "/api/approval-requests/"
    if not route_path.startswith(prefix):
        return None
    parts = [part for part in route_path[len(prefix):].split("/") if part]
    if len(parts) != 2 or parts[1] not in {"approve", "reject", "cancel", "expire"}:
        return None
    return parts[0], parts[1]


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
    actor = _authorize(handler, PERMISSION_APPROVE_ACTION, scope, request_id)
    if actor is None:
        _record_approval_authorization_deny_audit(
            _actor_from_request(handler),
            request_id=request_id,
            result="forbidden" if _actor_from_request(handler) is not None else "unauthorized",
            permission=PERMISSION_APPROVE_ACTION,
            resource_scope=scope,
            approval=approval,
        )
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
    handler.write_json(
        HTTPStatus.OK,
        {
            "service": APP_NAME,
            "status": "ok",
            "request_id": request_id,
            "idempotent": idempotent,
            "approval_request": updated,
        },
    )


def _resource_scope_from_approval_payload(payload: dict[str, Any]) -> Scope:
    raw = payload.get("resource_scope") if isinstance(payload.get("resource_scope"), dict) else {}
    return resource_scope(
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
