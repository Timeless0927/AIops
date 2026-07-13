"""Gateway authorization and proxy adapter for Model Provider administration."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from urllib import error, request

from apps.internal_auth import internal_auth_headers


def dispatch(
    handler,
    path: str,
    sessions,
    authorize_admin,
    require_fresh,
    request_session,
    request_id_fn,
    error_payload,
) -> bool:
    public = path == "/api/v1/model-provider/status"
    admin = path in {"/api/v1/admin/model-provider", "/api/v1/admin/model-provider/test"}
    if not public and not admin:
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
        status, result = _send("GET", "/model-provider/status", None, request_id)
        return _write_result(handler, status, result, request_id, error_payload)

    action = "model_provider_test" if path.endswith("/test") else (
        "model_provider_delete" if handler.command == "DELETE" else "model_provider_update"
    )
    audit_target = ("model_provider", None, action)
    session = authorize_admin(handler, request_id, audit_target=audit_target)
    if session is None:
        return True
    if handler.command == "GET":
        status, result = _send("GET", "/admin/model-provider", None, request_id)
        return _write_result(handler, status, result, request_id, error_payload)
    try:
        payload = handler.read_json_body()
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, error_payload("invalid_request", str(exc), request_id))
        return True
    reason = str(payload.pop("reason", "")).strip()
    if not reason:
        handler.write_json(
            HTTPStatus.BAD_REQUEST,
            error_payload("reason_required", "reason is required", request_id),
        )
        return True
    allowed = (
        {"expected_revision"}
        if handler.command == "DELETE" or path.endswith("/test")
        else {"endpoint", "endpoint_scope", "model", "timeout_seconds", "api_key", "expected_revision"}
    )
    if set(payload) != allowed:
        handler.write_json(
            HTTPStatus.BAD_REQUEST,
            error_payload("invalid_request", "Model Provider request fields are invalid", request_id),
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
    payload["actor_id"] = session.actor.actor_id
    target = "/admin/model-provider/test" if path.endswith("/test") else "/admin/model-provider"
    if path.endswith("/test"):
        payload["operation_id"] = f"model-verification:{request_id}"
    else:
        payload["operation_id"] = f"model-provider-{action}:{request_id}"
    status, result = _send(handler.command, target, payload, request_id)
    sessions.record_admin_audit(
        actor_id=session.actor.actor_id,
        target_type="model_provider",
        target_id=None,
        action=action,
        reason=reason,
        before=None,
        after=result if status < 400 else None,
        result="success" if status < 400 else "rejected",
        request_id=request_id,
    )
    return _write_result(handler, status, result, request_id, error_payload)


def _send(
    method: str,
    path: str,
    payload: dict[str, object] | None,
    request_id: str,
) -> tuple[int, dict[str, object]]:
    base_url = os.getenv("AIOPS_DIAGNOSIS_URL", "").strip()
    if not base_url:
        return HTTPStatus.SERVICE_UNAVAILABLE, {
            "error": {"code": "owner_unavailable", "message": "Diagnosis is unavailable"}
        }
    body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {
        "Accept": "application/json",
        "X-Request-ID": request_id,
        "X-Correlation-ID": request_id,
        **internal_auth_headers(),
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    outbound = request.Request(f"{base_url.rstrip('/')}{path}", data=body, headers=headers, method=method)
    try:
        with request.urlopen(outbound, timeout=3) as response:
            result = json.loads(response.read().decode() or "{}")
            return response.status, result if isinstance(result, dict) else {}
    except error.HTTPError as exc:
        result = json.loads(exc.read().decode() or "{}")
        return exc.code, result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return HTTPStatus.SERVICE_UNAVAILABLE, {
            "error": {"code": "owner_unavailable", "message": "Diagnosis is unavailable"}
        }


def _write_result(handler, status, result, request_id, error_payload) -> bool:
    if status >= 400:
        error_value = result.get("error")
        bounded = error_value if isinstance(error_value, dict) else {}
        code = str(bounded.get("code") or "owner_unavailable")
        message = str(bounded.get("message") or "Model Provider request failed")
        handler.write_json(status, error_payload(code, message, request_id))
    else:
        handler.write_json(status, {"request_id": request_id, **result})
    return True
