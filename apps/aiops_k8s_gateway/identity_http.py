"""HTTP adapter for Gateway authentication and identity administration."""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
from http import HTTPStatus
from http.cookies import SimpleCookie
from typing import Any, Callable
from urllib.parse import unquote

from aiops.domain.identity import Actor, AuthSession, IdentityError, ROLE_ADMIN

from . import APP_NAME
from .gateway_audit import GatewayAudit
from .gateway_sessions import GatewaySessions
from .identity_administration import IdentityAdministration


_SESSION_COOKIE_NAME = "aiops_session"
_CSRF_HEADER_NAME = "X-CSRF-Token"
_CSRF_MESSAGE = b"aiops-console-csrf"
_IDENTITY_COLLECTIONS = {"users", "teams", "team-memberships", "role-bindings", "audit"}


class IdentityHTTPAdapter:
    def __init__(
        self,
        sessions: GatewaySessions,
        identities: IdentityAdministration,
        audit: GatewayAudit,
        login: Callable[[str, str], Actor],
        request_id_for: Callable[[Any], str],
        error_payload: Callable[[str, str, str], dict[str, object]],
    ) -> None:
        self._sessions = sessions
        self._identities = identities
        self._audit = audit
        self._login = login
        self._request_id_for = request_id_for
        self._error_payload = error_payload

    def dispatch(self, handler: Any, route_path: str) -> bool:
        method = handler.command
        admin_route = _admin_route(route_path)
        if method == "GET" and route_path == "/api/v1/actor":
            self._actor(handler)
        elif method == "GET" and route_path == "/auth/csrf":
            self._csrf(handler)
        elif method == "GET" and admin_route and admin_route[1] is None:
            self._admin_get(handler, admin_route[0])
        elif method == "POST" and route_path == "/auth/login":
            self._handle_login(handler)
        elif method == "POST" and route_path == "/auth/reauth":
            self._reauth(handler)
        elif method == "POST" and route_path == "/auth/logout":
            self._logout(handler)
        elif method == "POST" and admin_route and admin_route[1] is None and admin_route[0] != "audit":
            self._admin_mutation(handler, admin_route[0], None)
        elif method == "PATCH" and admin_route and admin_route[1] is not None and admin_route[0] != "audit":
            self._admin_mutation(handler, admin_route[0], admin_route[1])
        else:
            return False
        return True

    def request_session(self, handler: Any) -> tuple[AuthSession | None, str | None]:
        bearer = _extract_bearer_token(handler.headers.get("Authorization"))
        session = self._sessions.lookup(bearer or "")
        if session is not None:
            return session, "bearer"
        session = self._sessions.lookup(_extract_session_cookie(handler.headers.get("Cookie")) or "")
        return (session, "cookie") if session is not None else (None, None)

    def csrf_valid(self, handler: Any, session_token: str) -> bool:
        supplied = handler.headers.get(_CSRF_HEADER_NAME, "").strip()
        return bool(supplied) and hmac.compare_digest(supplied, _csrf_token(session_token))

    def authorize_admin(
        self,
        handler: Any,
        request_id: str,
        *,
        audit_target: tuple[str, str | None, str] | None = None,
    ) -> AuthSession | None:
        session, auth_mode = self.request_session(handler)
        if session is None:
            self._audit.denial(None, audit_target, "unauthorized", request_id)
            handler.write_json(HTTPStatus.UNAUTHORIZED, self._error_payload("unauthorized", "authentication required", request_id))
            return None
        if auth_mode == "cookie" and handler.command not in {"GET", "HEAD", "OPTIONS"} and not self.csrf_valid(handler, session.token):
            self._audit.denial(session.actor.actor_id, audit_target, "csrf_required", request_id)
            handler.write_json(HTTPStatus.FORBIDDEN, self._error_payload("csrf_required", "missing or invalid CSRF token", request_id))
            return None
        if not self._sessions.actor_view(session.actor)["is_platform_administrator"]:
            self._audit.denial(session.actor.actor_id, audit_target, "forbidden", request_id)
            handler.write_json(HTTPStatus.FORBIDDEN, self._error_payload("forbidden", "Platform Administrator access required", request_id))
            return None
        return session

    def require_fresh_auth(
        self,
        handler: Any,
        session: AuthSession,
        request_id: str,
        *,
        audit_target: tuple[str, str | None, str],
        reason: str,
    ) -> bool:
        if self._sessions.is_fresh(session.token):
            return True
        self._audit.denial(session.actor.actor_id, audit_target, "fresh_auth_required", request_id, reason)
        handler.write_json(
            HTTPStatus.FORBIDDEN,
            self._error_payload("fresh_auth_required", "re-authentication is required", request_id),
        )
        return False

    def _actor(self, handler: Any) -> None:
        request_id = self._request_id_for(handler)
        session, _ = self.request_session(handler)
        if session is None:
            handler.write_json(HTTPStatus.UNAUTHORIZED, self._error_payload("unauthorized", "authentication required", request_id))
        else:
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, "actor": self._sessions.actor_view(session.actor)})

    def _csrf(self, handler: Any) -> None:
        request_id = self._request_id_for(handler)
        session, _ = self.request_session(handler)
        if session is None:
            handler.write_json(HTTPStatus.UNAUTHORIZED, self._error_payload("unauthorized", "missing or invalid session", request_id))
        else:
            handler.write_json(
                HTTPStatus.OK,
                {"service": APP_NAME, "status": "ok", "request_id": request_id, "csrf_token": _csrf_token(session.token)},
            )

    def _admin_get(self, handler: Any, collection: str) -> None:
        request_id = self._request_id_for(handler)
        if self.authorize_admin(handler, request_id) is None:
            return
        if collection == "audit":
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, "audit": self._audit.recent()})
        else:
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, **self._identities.state()})

    def _admin_mutation(self, handler: Any, collection: str, target_id: str | None) -> None:
        request_id = self._request_id_for(handler)
        action = f"{collection}_{'update' if target_id else 'create'}"
        audit_target = (collection, target_id, action)
        session = self.authorize_admin(handler, request_id, audit_target=audit_target)
        if session is None:
            return
        try:
            payload = handler.read_json_body()
        except (TypeError, ValueError) as exc:
            self._audit.denial(session.actor.actor_id, audit_target, "invalid_request", request_id)
            handler.write_json(HTTPStatus.BAD_REQUEST, self._error_payload("invalid_request", str(exc), request_id))
            return
        raw_reason = payload.pop("reason", "")
        reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
        if not reason:
            self._audit.denial(session.actor.actor_id, audit_target, "reason_required", request_id, "missing")
            handler.write_json(HTTPStatus.BAD_REQUEST, self._error_payload("reason_required", "reason is required", request_id))
            return
        if not self.require_fresh_auth(handler, session, request_id, audit_target=audit_target, reason=reason):
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
            self._audit.denial(session.actor.actor_id, audit_target, "invalid_request", request_id, reason)
            handler.write_json(HTTPStatus.BAD_REQUEST, self._error_payload("invalid_request", "invalid administration fields", request_id))
            return
        try:
            response_key, after = self._identities.mutate(
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
            self._audit.denial(session.actor.actor_id, audit_target, exc.code, request_id, reason)
            handler.write_json(status, self._error_payload(exc.code, exc.message, request_id))
            return
        except sqlite3.Error:
            handler.write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                self._error_payload("administration_write_failed", "administration change was not committed", request_id),
            )
            return
        handler.write_json(HTTPStatus.OK if target_id else HTTPStatus.CREATED, {"request_id": request_id, response_key: after})

    def _handle_login(self, handler: Any) -> None:
        request_id = self._request_id_for(handler)
        try:
            payload = handler.read_json_body()
            actor = self._login(str(payload.get("username") or ""), str(payload.get("password") or ""))
            if (
                payload.get("session_mode") == "cookie"
                and actor.auth_source == "local"
                and actor.has_role(ROLE_ADMIN)
                and os.getenv("AIOPS_BOOTSTRAP_ADMIN_PASSWORD")
                and actor.username == (os.getenv("AIOPS_BOOTSTRAP_ADMIN_USERNAME", "admin").strip() or "admin")
            ):
                self._identities.ensure_platform_administrator(actor.actor_id)
            session = self._sessions.issue(actor)
        except IdentityError as exc:
            status = HTTPStatus.SERVICE_UNAVAILABLE if exc.code == "ldap_unavailable" else HTTPStatus.UNAUTHORIZED
            handler.write_json(status, self._error_payload(exc.code, exc.message, request_id))
            return
        except (TypeError, ValueError) as exc:
            handler.write_json(HTTPStatus.BAD_REQUEST, self._error_payload("invalid_request", str(exc), request_id))
            return
        response: dict[str, object] = {
            "service": APP_NAME,
            "status": "ok",
            "request_id": request_id,
            "expires_at": session.expires_at,
            "actor": self._sessions.actor_view(actor),
        }
        if payload.get("session_mode") != "cookie":
            response["token"] = session.token
        handler.write_json(
            HTTPStatus.OK,
            response,
            headers={"Set-Cookie": _session_cookie_header(handler, session.token, self._sessions.ttl_seconds)},
        )

    def _reauth(self, handler: Any) -> None:
        request_id = self._request_id_for(handler)
        session, auth_mode = self.request_session(handler)
        if session is None:
            handler.write_json(HTTPStatus.UNAUTHORIZED, self._error_payload("unauthorized", "authentication required", request_id))
            return
        if auth_mode == "cookie" and not self.csrf_valid(handler, session.token):
            handler.write_json(HTTPStatus.FORBIDDEN, self._error_payload("csrf_required", "missing or invalid CSRF token", request_id))
            return
        try:
            actor = self._login(session.actor.username, str(handler.read_json_body().get("password") or ""))
        except IdentityError as exc:
            handler.write_json(HTTPStatus.UNAUTHORIZED, self._error_payload(exc.code, exc.message, request_id))
            return
        if actor.actor_id != session.actor.actor_id:
            handler.write_json(HTTPStatus.UNAUTHORIZED, self._error_payload("invalid_credentials", "identity mismatch", request_id))
            return
        self._sessions.mark_fresh(session.token)
        handler.write_json(HTTPStatus.OK, {"status": "ok", "request_id": request_id})

    def _logout(self, handler: Any) -> None:
        request_id = self._request_id_for(handler)
        session, auth_mode = self.request_session(handler)
        if session is not None:
            if auth_mode == "cookie" and not self.csrf_valid(handler, session.token):
                handler.write_json(HTTPStatus.FORBIDDEN, self._error_payload("csrf_required", "missing or invalid CSRF token", request_id))
                return
            self._sessions.revoke(session.token)
        handler.write_json(
            HTTPStatus.OK,
            {"service": APP_NAME, "status": "ok", "request_id": request_id},
            headers={"Set-Cookie": _clear_session_cookie_header(handler)},
        )


def _admin_route(path: str) -> tuple[str, str | None] | None:
    prefix = "/api/v1/admin/"
    if not path.startswith(prefix):
        return None
    parts = path[len(prefix) :].strip("/").split("/")
    if not parts or len(parts) > 2 or parts[0] not in _IDENTITY_COLLECTIONS:
        return None
    return parts[0], unquote(parts[1]) if len(parts) == 2 else None


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


def _csrf_token(session_token: str) -> str:
    return hmac.new(session_token.encode(), _CSRF_MESSAGE, hashlib.sha256).hexdigest()


def _secure_session_cookie(handler: Any) -> bool:
    configured = os.getenv("AIOPS_SECURE_SESSION_COOKIE", "").lower() in {"1", "true", "yes"}
    return configured or handler.headers.get("X-Forwarded-Proto", "").lower() == "https"


def _session_cookie_header(handler: Any, token: str, max_age: int) -> str:
    secure = "; Secure" if _secure_session_cookie(handler) else ""
    return f"{_SESSION_COOKIE_NAME}={token}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax{secure}"


def _clear_session_cookie_header(handler: Any) -> str:
    secure = "; Secure" if _secure_session_cookie(handler) else ""
    return f"{_SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax{secure}"
