"""Gateway HTTP Adapter for Platform Status."""

from __future__ import annotations

from http import HTTPStatus

from . import model_provider_http, notification_admin_http
from .platform_status import PlatformSetupDecisions, PlatformStatus, PlatformStatusError
from .platform_status_observability import read_status as read_observability_status


PUBLIC_PATH = "/api/v1/platform/status"
DECISION_PATH = "/api/v1/admin/platform/capabilities/notification/setup-decision"


def dispatch(
    handler,
    path: str,
    sessions,
    connectors,
    authorize_admin,
    request_session,
    request_id_fn,
    error_payload,
) -> bool:
    if path not in {PUBLIC_PATH, DECISION_PATH}:
        return False
    request_id = request_id_fn(handler)
    service = PlatformStatus(
        PlatformSetupDecisions(sessions.database),
        model_status=model_provider_http.read_status,
        notification_status=notification_admin_http.read_status,
        connector_status=connectors.admin_state,
        observability_status=read_observability_status,
    )
    if path == PUBLIC_PATH:
        if handler.command != "GET":
            handler.write_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                error_payload("method_not_allowed", "method is not allowed", request_id),
            )
            return True
        session, _ = request_session(handler)
        if session is None:
            handler.write_json(
                HTTPStatus.UNAUTHORIZED,
                error_payload("unauthorized", "authentication required", request_id),
            )
            return True
        handler.write_json(HTTPStatus.OK, {"request_id": request_id, **service.snapshot(request_id)})
        return True

    audit_target = ("platform_capability", "notification", "setup_decision_update")
    session = authorize_admin(handler, request_id, audit_target=audit_target)
    if session is None:
        return True
    if handler.command != "PUT":
        handler.write_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            error_payload("method_not_allowed", "method is not allowed", request_id),
        )
        return True
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(
            HTTPStatus.BAD_REQUEST, error_payload("invalid_request", str(exc), request_id),
        )
        return True
    if set(payload) != {"setup_decision", "expected_revision", "reason"}:
        handler.write_json(
            HTTPStatus.BAD_REQUEST,
            error_payload("invalid_request", "setup decision fields are invalid", request_id),
        )
        return True
    if (
        not isinstance(payload["setup_decision"], str)
        or payload["setup_decision"] not in {"active", "skipped"}
        or not isinstance(payload["expected_revision"], (str, type(None)))
        or not isinstance(payload["reason"], str)
    ):
        handler.write_json(
            HTTPStatus.BAD_REQUEST,
            error_payload("invalid_request", "setup decision fields are invalid", request_id),
        )
        return True
    try:
        result = service.set_setup_decision(
            capability="notification",
            decision=payload["setup_decision"],
            expected_revision=payload["expected_revision"],
            actor_id=session.actor.actor_id,
            reason=payload["reason"],
            request_id=request_id,
        )
    except PlatformStatusError as exc:
        status = HTTPStatus.SERVICE_UNAVAILABLE if exc.code == "owner_unavailable" else (
            HTTPStatus.CONFLICT if exc.code in {
                "stale_configuration", "capability_already_ready", "idempotency_conflict",
            } else HTTPStatus.BAD_REQUEST
        )
        handler.write_json(status, error_payload(exc.code, exc.message, request_id))
        return True
    handler.write_json(HTTPStatus.OK, {"request_id": request_id, "setup_decision": result})
    return True
