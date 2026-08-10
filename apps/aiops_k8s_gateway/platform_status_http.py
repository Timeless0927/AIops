"""Gateway HTTP Adapter for Platform Status."""

from __future__ import annotations

from dataclasses import dataclass
import json
from http import HTTPStatus
from typing import Any

from .platform_status import PlatformStatus, PlatformStatusError


PUBLIC_PATH = "/api/v1/platform/status"
STREAM_PATH = "/api/v1/platform/status/stream"
DECISION_PATH = "/api/v1/admin/platform/capabilities/notification/setup-decision"


@dataclass(frozen=True)
class PlatformStatusHTTPAdapter:
    service: PlatformStatus
    authorize_admin: Any
    require_fresh: Any
    request_session: Any
    request_id_for: Any
    error_payload: Any

    def dispatch(self, handler: Any, route_path: str) -> bool:
        path = route_path
        service = self.service
        authorize_admin = self.authorize_admin
        require_fresh = self.require_fresh
        request_session = self.request_session
        request_id_fn = self.request_id_for
        error_payload = self.error_payload
        if path not in {PUBLIC_PATH, STREAM_PATH, DECISION_PATH}:
            return False
        request_id = request_id_fn(handler)
        if path in {PUBLIC_PATH, STREAM_PATH}:
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
            snapshot = {"request_id": request_id, **service.snapshot(request_id)}
            if path == STREAM_PATH:
                payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handler.send_response(HTTPStatus.OK)
                handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
                handler.send_header("Cache-Control", "no-cache")
                handler.send_header("X-Accel-Buffering", "no")
                handler.end_headers()
                handler.wfile.write(f"event: platform_status\ndata: {payload}\n\n".encode())
                handler.wfile.flush()
                return True
            handler.write_json(HTTPStatus.OK, snapshot)
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
        reason = payload["reason"].strip()
        if not reason:
            handler.write_json(
                HTTPStatus.BAD_REQUEST,
                error_payload("reason_required", "reason is required", request_id),
            )
            return True
        if not require_fresh(
            handler,
            session,
            request_id,
            audit_target=audit_target,
            reason=reason,
        ):
            return True
        try:
            result = service.set_setup_decision(
                capability="notification",
                decision=payload["setup_decision"],
                expected_revision=payload["expected_revision"],
                actor_id=session.actor.actor_id,
                reason=reason,
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
