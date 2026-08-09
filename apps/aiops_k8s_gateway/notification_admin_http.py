"""Gateway authorization and proxy adapter for Notification Engine administration."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from http import HTTPStatus
from typing import Any
from urllib import error, request
from urllib.parse import unquote

from apps.internal_auth import internal_auth_headers
from apps.service_http import read_bounded_json


PREFIX = "/api/v1/admin/notification-"
PUBLIC_STATUS = "/api/v1/notification/status"


def read_status(request_id: str) -> dict[str, object]:
    status, result, outcome_known = _send("GET", "/notification/status", None, request_id)
    notification = result.get("notification")
    if status >= 400 or not outcome_known or not isinstance(notification, dict):
        raise OSError("Notification status owner is unavailable")
    return notification


@dataclass(frozen=True)
class NotificationAdminHTTPAdapter:
    unresolved_admin_request: Any
    record_admin_audit: Any
    authorize: Any
    require_fresh: Any
    request_session: Any
    request_id_for: Any
    error_payload: Any
    setup_decisions: Any = None

    def dispatch(self, handler: Any, route_path: str) -> bool:
        path = route_path
        unresolved_admin_request = self.unresolved_admin_request
        record_admin_audit = self.record_admin_audit
        authorize = self.authorize
        require_fresh = self.require_fresh
        request_session = self.request_session
        request_id_fn = self.request_id_for
        error_payload = self.error_payload
        setup_decisions = self.setup_decisions
        public = path == PUBLIC_STATUS
        if not public and not path.startswith(PREFIX):
            return False
        request_id = request_id_fn(handler)
        if public:
            session, _ = request_session(handler)
            if session is None:
                handler.write_json(
                    HTTPStatus.UNAUTHORIZED,
                    error_payload("unauthorized", "authentication required", request_id),
                )
                return True
            status, result, outcome_known = _send("GET", "/notification/status", None, request_id)
            return _write_result(handler, status, result, request_id, error_payload, outcome_known)
        target = _target(path)
        session = authorize(handler, request_id, audit_target=target if handler.command != "GET" else None)
        if session is None:
            return True
        payload = None
        reason = ""
        destination_action = None
        mutating = handler.command in {"POST", "PATCH"} and not path.endswith("/simulate")
        if handler.command in {"POST", "PATCH"}:
            try:
                payload = handler.read_json_body()
            except (TypeError, ValueError) as exc:
                handler.write_json(HTTPStatus.BAD_REQUEST, error_payload("invalid_request", str(exc), request_id))
                return True
            reason = str(payload.pop("reason", "")).strip()
            if path == "/api/v1/admin/notification-silences" and reason:
                payload["reason"] = reason
            destination_action = _destination_action(path)
            if path == "/api/v1/admin/notification-destinations" and handler.command == "POST":
                payload["operation_id"] = f"notification-destination-create:{request_id}"
            elif destination_action in {"test", "select_pilot_route"}:
                if set(payload) != {"expected_revision"} or not isinstance(payload["expected_revision"], str):
                    handler.write_json(
                        HTTPStatus.BAD_REQUEST,
                        error_payload("invalid_request", "Notification Destination request fields are invalid", request_id),
                    )
                    return True
                prefix = "notification-delivery" if destination_action == "test" else "notification-pilot-route"
                payload["operation_id"] = f"{prefix}:{request_id}"
            elif destination_action == "update":
                allowed = {"name", "config", "enabled", "expected_revision"}
                if set(payload) - allowed or "expected_revision" not in payload or len(payload) < 2:
                    handler.write_json(
                        HTTPStatus.BAD_REQUEST,
                        error_payload("invalid_request", "Notification Destination update fields are invalid", request_id),
                    )
                    return True
                payload["operation_id"] = f"notification-destination-update:{request_id}"
        if mutating:
            if not reason:
                handler.write_json(HTTPStatus.BAD_REQUEST, error_payload("reason_required", "reason is required", request_id))
                return True
            if not require_fresh(handler, session, request_id, audit_target=target, reason=reason):
                return True
        configuration_mutation = (
            path == "/api/v1/admin/notification-destinations" and handler.command == "POST"
        ) or destination_action == "update"
        credential_mutation = configuration_mutation and (
            destination_action != "update" or payload is not None and "config" in payload
        )
        if credential_mutation:
            unresolved = unresolved_admin_request(*target)
            if unresolved is not None and unresolved != request_id:
                handler.write_json(
                    HTTPStatus.CONFLICT,
                    error_payload(
                        "outcome_reconciliation_required",
                        "reconcile the previous Notification credential mutation before retrying",
                        unresolved,
                    ),
                )
                return True
        status, result, outcome_known = _send(
            handler.command, path.removeprefix("/api/v1"), payload, request_id,
        )
        if mutating:
            record_admin_audit(
                actor_id=session.actor.actor_id,
                target_type=target[0],
                target_id=target[1],
                action=target[2],
                reason=reason,
                before=None,
                after=result if status < 400 else None,
                result="success" if status < 400 else "rejected" if outcome_known else "outcome_unknown",
                request_id=request_id,
            )
        if status < 400 and configuration_mutation and setup_decisions is not None:
            setup_decisions.activate_after_configuration(
                actor_id=session.actor.actor_id,
                reason=reason,
                request_id=request_id,
            )
        return _write_result(handler, status, result, request_id, error_payload, outcome_known)


def _send(
    method: str,
    path: str,
    payload: dict[str, object] | None,
    request_id: str,
) -> tuple[int, dict[str, object], bool]:
    base_url = os.getenv("AIOPS_NOTIFICATION_ENGINE_URL", "").strip()
    if not base_url:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Notification Engine is unavailable"}, True
    body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {
        "Accept": "application/json",
        "X-Request-ID": request_id,
        "X-Correlation-ID": request_id,
        **internal_auth_headers(),
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = request.Request(f"{base_url.rstrip('/')}{path}", data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=3) as response:
            result = read_bounded_json(response)
            if result is None:
                return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Notification Engine outcome is unknown"}, False
            return response.status, result, True
    except error.HTTPError as exc:
        result = read_bounded_json(exc)
        return exc.code, result or {"error": "Notification Engine rejected the request"}, True
    except OSError:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Notification Engine outcome is unknown"}, False
def _target(path: str) -> tuple[str, str | None, str]:
    suffix = path.removeprefix("/api/v1/admin/").strip("/")
    parts = suffix.split("/")
    resource = parts[0]
    target_id = unquote(parts[1]) if len(parts) > 1 and parts[1] not in {"simulate"} else None
    action = parts[-1].replace("-", "_") if parts[-1] in {
        "test", "preview", "simulate", "redeliver", "select-pilot-route",
    } else "update" if target_id else "create"
    return resource, target_id, f"{resource}_{action}"


def _destination_action(path: str) -> str | None:
    prefix = "/api/v1/admin/notification-destinations/"
    if not path.startswith(prefix):
        return None
    if path.endswith("/noise-control"):
        return None
    if path.endswith("/test"):
        return "test"
    if path.endswith("/select-pilot-route"):
        return "select_pilot_route"
    return "update" if "/" not in path[len(prefix):].strip("/") else None


def _write_result(handler, status, result, request_id, error_payload, outcome_known: bool = True) -> bool:
    if status >= 400:
        message = str(result.get("error") or "Notification Engine request failed")
        code = "outcome_unknown" if not outcome_known else "revision_conflict" if "revision" in message and (
            "conflict" in message or "has changed" in message
        ) else "notification_configuration_rejected"
        handler.write_json(status, error_payload(code, message, request_id))
    else:
        handler.write_json(status, {"request_id": request_id, **result})
    return True
