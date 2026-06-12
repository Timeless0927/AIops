"""Smokeable entry point for the AIOps K8s Gateway process."""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from dataclasses import asdict
from http import HTTPStatus
from typing import Any

from aiops.domain.identity import (
    Actor,
    IdentityConfig,
    IdentityError,
    IdentityProvider,
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
from .alertmanager_webhook import handle_http_request
from .command_service import build_read_envelope, dispatch_read_envelope
from .connector_router import ConnectorRoute


_ROUTES: dict[str, ConnectorRoute] = {}
_SESSIONS = SessionTokenStore()


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
        if self.path == "/healthz":
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "connector_url": os.getenv("AIOPS_CONNECTOR_URL", ""),
                },
            )
            return

        if self.path == "/readyz":
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "registered_connectors": len(_ROUTES),
                },
            )
            return

        if self.path == "/connectivity/connector":
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

        if self.path == "/connectors":
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": APP_NAME,
                    "status": "ok",
                    "connectors": [asdict(route) for route in _ROUTES.values()],
                },
            )
            return

        if self.path == "/auth/me":
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

        self.write_not_found()

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/auth/login":
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

        if self.path == "/auth/sync":
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

        if self.path == "/incidents/query":
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
                if actor.can(
                    PERMISSION_VIEW_INCIDENT,
                    resource_scope(
                        service=str(incident.get("service") or "").strip() or None,
                        team=str(incident.get("team") or "").strip() or None,
                        namespace=str(incident.get("namespace") or "").strip() or None,
                    ),
                )
            ]
            _record_gateway_audit(actor, request_id=request_id, action="incident_query", result="success")
            self.write_json(
                HTTPStatus.OK,
                {"service": APP_NAME, "status": "ok", "request_id": request_id, "incidents": filtered},
            )
            return

        if self.path == "/audit/query":
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

        if self.path == "/k8s/read":
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

        if self.path == "/webhooks/alertmanager":
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length > 0 else b""
            status, payload = handle_http_request(body, dict(self.headers))
            self.write_json(status, payload)
            return

        if self.path != "/connectors/register":
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
