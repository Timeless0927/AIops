"""HTTP adapter for V1 Approval Authority and approve-and-execute."""

from __future__ import annotations

import sqlite3
import string
from http import HTTPStatus
from typing import Any, Callable
from urllib.parse import unquote

from .approval import ApprovalError, Approvals


def dispatch(
    handler: Any,
    path: str,
    sessions: Any,
    approvals: Approvals,
    authorize_admin: Callable[..., Any],
    require_fresh_auth: Callable[..., bool],
    request_session: Callable[[Any], tuple[Any, str | None]],
    csrf_valid: Callable[[Any, str], bool],
    request_id_for: Callable[[Any], str],
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> bool:
    if path == "/api/v1/admin/approval-authorities" and handler.command == "GET":
        request_id = request_id_for(handler)
        if authorize_admin(handler, request_id) is not None:
            handler.write_json(
                HTTPStatus.OK, {"request_id": request_id, "approval_authorities": approvals.list_authorities()}
            )
        return True
    if path == "/api/v1/admin/approval-authorities" and handler.command == "POST":
        _admin_write(
            handler, None, sessions, approvals, authorize_admin, require_fresh_auth,
            request_id_for(handler), error_payload,
        )
        return True
    prefix = "/api/v1/admin/approval-authorities/"
    if path.startswith(prefix) and handler.command == "PATCH":
        authority_id = unquote(path[len(prefix):]).strip("/")
        if authority_id and "/" not in authority_id:
            _admin_write(
                handler, authority_id, sessions, approvals, authorize_admin, require_fresh_auth,
                request_id_for(handler), error_payload,
            )
            return True
    parsed = _execution_route(path)
    if parsed is None or handler.command != "POST":
        return False
    incident_id, action_id = parsed
    request_id = request_id_for(handler)
    session, auth_mode = request_session(handler)
    if session is None:
        handler.write_json(HTTPStatus.UNAUTHORIZED, error_payload("unauthorized", "authentication required", request_id))
        return True
    if auth_mode == "cookie" and not csrf_valid(handler, session.token):
        handler.write_json(HTTPStatus.FORBIDDEN, error_payload("csrf_required", "missing or invalid CSRF token", request_id))
        return True
    try:
        payload = handler.read_json_body()
        if set(payload) != {"action_version", "action_hash", "idempotency_key"}:
            raise ApprovalError("invalid_request", "exact frozen action fields are required")
        if not isinstance(payload["action_version"], int) or isinstance(payload["action_version"], bool) or payload["action_version"] < 1:
            raise ApprovalError("invalid_request", "action_version must be a positive integer")
        if not isinstance(payload["action_hash"], str) or len(payload["action_hash"]) != 64 or any(character not in string.hexdigits for character in payload["action_hash"]):
            raise ApprovalError("invalid_request", "action_hash must be a SHA-256 digest")
        if not isinstance(payload["idempotency_key"], str) or not payload["idempotency_key"].strip() or len(payload["idempotency_key"].strip()) > 200:
            raise ApprovalError("invalid_request", "idempotency_key is required")
        execution = approvals.approve_and_execute(
            incident_id=incident_id, action_id=action_id, action_version=payload["action_version"],
            action_hash=payload["action_hash"], actor_id=session.actor.actor_id,
            idempotency_key=payload["idempotency_key"].strip(), request_id=request_id,
        )
        handler.write_json(HTTPStatus.OK if execution["idempotent"] else HTTPStatus.CREATED, {
            "request_id": request_id, "execution": execution,
        })
    except (ApprovalError, TypeError, ValueError) as exc:
        code = exc.code if isinstance(exc, ApprovalError) else "invalid_request"
        message = exc.message if isinstance(exc, ApprovalError) else str(exc)
        status = {
            "forbidden": HTTPStatus.FORBIDDEN,
            "action_stale": HTTPStatus.CONFLICT,
            "mutation_disabled": HTTPStatus.CONFLICT,
            "connector_unavailable": HTTPStatus.CONFLICT,
            "idempotency_conflict": HTTPStatus.CONFLICT,
        }.get(code, HTTPStatus.BAD_REQUEST)
        handler.write_json(status, error_payload(code, message, request_id))
    except sqlite3.Error:
        handler.write_json(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            error_payload("approval_write_failed", "Approval was not committed", request_id),
        )
    return True


def _admin_write(
    handler: Any,
    authority_id: str | None,
    sessions: Any,
    approvals: Approvals,
    authorize_admin: Callable[..., Any],
    require_fresh_auth: Callable[..., bool],
    request_id: str,
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> None:
    action = f"approval-authorities_{'update' if authority_id else 'create'}"
    target = ("approval-authorities", authority_id, action)
    session = authorize_admin(handler, request_id, audit_target=target)
    if session is None:
        return
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        _admin_denial(sessions, session.actor.actor_id, target, "invalid_request", request_id, "unavailable_before_validation")
        handler.write_json(HTTPStatus.BAD_REQUEST, error_payload("invalid_request", str(exc), request_id))
        return
    reason = payload.pop("reason", "")
    if not isinstance(reason, str) or not reason.strip():
        _admin_denial(sessions, session.actor.actor_id, target, "reason_required", request_id, "missing")
        handler.write_json(HTTPStatus.BAD_REQUEST, error_payload("reason_required", "reason is required", request_id))
        return
    if not require_fresh_auth(handler, session, request_id, audit_target=target, reason=reason.strip()):
        return
    try:
        if authority_id is None:
            if set(payload) != {"user_id", "environment", "scope_type", "scope_id"}:
                raise ApprovalError("invalid_request", "invalid Approval Authority fields")
            authority = approvals.create_authority(
                user_id=payload["user_id"], environment=payload["environment"],
                scope_type=payload["scope_type"], scope_id=payload["scope_id"],
                actor_id=session.actor.actor_id, reason=reason.strip(), request_id=request_id,
            )
            status = HTTPStatus.CREATED
        else:
            if set(payload) != {"active"} or not isinstance(payload["active"], bool):
                raise ApprovalError("invalid_request", "active must be boolean")
            authority = approvals.update_authority(
                authority_id, active=payload["active"], actor_id=session.actor.actor_id,
                reason=reason.strip(), request_id=request_id,
            )
            status = HTTPStatus.OK
        handler.write_json(status, {"request_id": request_id, "approval_authority": authority})
    except ApprovalError as exc:
        sessions.record_admin_audit(
            actor_id=session.actor.actor_id, target_type="approval-authorities", target_id=authority_id,
            action=action, reason=reason.strip(), before=None, after=None, result=exc.code, request_id=request_id,
        )
        handler.write_json(
            HTTPStatus.NOT_FOUND if exc.code.endswith("not_found") else HTTPStatus.CONFLICT
            if exc.code == "approval_authority_exists" else HTTPStatus.BAD_REQUEST,
            error_payload(exc.code, exc.message, request_id),
        )


def _execution_route(path: str) -> tuple[str, str] | None:
    parts = [unquote(part) for part in path.strip("/").split("/")]
    if len(parts) == 7 and parts[:3] == ["api", "v1", "incidents"] and parts[4] == "actions" and parts[6:] == ["approve-and-execute"]:
        return parts[3], parts[5]
    return None


def _admin_denial(
    sessions: Any,
    actor_id: str,
    target: tuple[str, str | None, str],
    result: str,
    request_id: str,
    reason: str,
) -> None:
    target_type, target_id, action = target
    sessions.record_admin_audit(
        actor_id=actor_id, target_type=target_type, target_id=target_id, action=action,
        reason=reason, before=None, after=None, result=result, request_id=request_id,
    )
