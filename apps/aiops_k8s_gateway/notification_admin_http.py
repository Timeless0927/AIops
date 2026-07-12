"""Gateway authorization and proxy adapter for Notification Engine administration."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from urllib import error, request
from urllib.parse import unquote

from apps.internal_auth import internal_auth_headers


PREFIX = "/api/v1/admin/notification-"


def dispatch(handler, path, sessions, authorize, require_fresh, request_id_fn, error_payload) -> bool:
    if not path.startswith(PREFIX):
        return False
    request_id = request_id_fn(handler)
    target = _target(path)
    session = authorize(handler, request_id, audit_target=target if handler.command != "GET" else None)
    if session is None:
        return True
    payload = None
    reason = ""
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
    if mutating:
        if not reason:
            handler.write_json(HTTPStatus.BAD_REQUEST, error_payload("reason_required", "reason is required", request_id))
            return True
        if not require_fresh(handler, session, request_id, audit_target=target, reason=reason):
            return True
    status, result = _send(handler.command, path.removeprefix("/api/v1"), payload, request_id)
    if mutating:
        sessions.record_admin_audit(
            actor_id=session.actor.actor_id,
            target_type=target[0],
            target_id=target[1],
            action=target[2],
            reason=reason,
            before=None,
            after=result if status < 400 else None,
            result="success" if status < 400 else "rejected",
            request_id=request_id,
        )
    if status >= 400:
        handler.write_json(status, error_payload("notification_configuration_rejected", str(result.get("error") or "Notification Engine request failed"), request_id))
    else:
        handler.write_json(status, {"request_id": request_id, **result})
    return True


def _send(method: str, path: str, payload: dict[str, object] | None, request_id: str) -> tuple[int, dict[str, object]]:
    base_url = os.getenv("AIOPS_NOTIFICATION_ENGINE_URL", "").strip()
    if not base_url:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Notification Engine is unavailable"}
    body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {"Accept": "application/json", "X-Request-ID": request_id, **internal_auth_headers()}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = request.Request(f"{base_url.rstrip('/')}{path}", data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode() or "{}")
            return response.status, result if isinstance(result, dict) else {"error": "invalid Notification Engine response"}
    except error.HTTPError as exc:
        result = json.loads(exc.read().decode() or "{}")
        return exc.code, result if isinstance(result, dict) else {"error": "invalid Notification Engine response"}
    except (OSError, ValueError):
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Notification Engine is unavailable"}


def _target(path: str) -> tuple[str, str | None, str]:
    suffix = path.removeprefix("/api/v1/admin/").strip("/")
    parts = suffix.split("/")
    resource = parts[0]
    target_id = unquote(parts[1]) if len(parts) > 1 and parts[1] not in {"simulate"} else None
    action = parts[-1] if parts[-1] in {"test", "preview", "simulate", "redeliver"} else "update" if target_id else "create"
    return resource, target_id, f"{resource}_{action}"
