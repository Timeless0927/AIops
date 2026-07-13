"""Authenticated HTTP adapter for actor-scoped Secure Input."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Callable
from urllib.parse import unquote

from .secure_inputs import SecureInputError, SecureInputs


def dispatch(
    handler: Any,
    path: str,
    sessions: Any,
    inputs: SecureInputs,
    request_session: Callable[[Any], tuple[Any, str | None]],
    csrf_valid: Callable[[Any, str], bool],
    request_id_for: Callable[[Any], str],
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> bool:
    input_id = _route(path)
    if input_id is False or (input_id is None and handler.command != "POST") or (
        isinstance(input_id, str) and handler.command != "GET"
    ):
        return False
    request_id = request_id_for(handler)
    session, auth_mode = request_session(handler)
    if session is None:
        handler.write_json(
            HTTPStatus.UNAUTHORIZED,
            error_payload("unauthorized", "authentication required", request_id),
        )
        return True
    capabilities = sessions.actor_view(session.actor)["capabilities"]
    if "manage_investigation" not in capabilities:
        handler.write_json(
            HTTPStatus.FORBIDDEN, error_payload("forbidden", "access denied", request_id),
        )
        return True
    try:
        if isinstance(input_id, str):
            secure_input = inputs.get(input_id, actor_id=session.actor.actor_id)
            handler.write_json(
                HTTPStatus.OK,
                {"request_id": request_id, "secure_input": secure_input},
            )
            return True
        if auth_mode == "cookie" and not csrf_valid(handler, session.token):
            handler.write_json(
                HTTPStatus.FORBIDDEN,
                error_payload("csrf_required", "missing or invalid CSRF token", request_id),
            )
            return True
        payload = handler.read_json_body()
        user_fields = {"key_name", "value", "idempotency_key"}
        generated_fields = {"key_name", "generated_bytes", "idempotency_key"}
        if set(payload) not in (user_fields, generated_fields):
            raise SecureInputError("invalid_request", "Exact Secure Input fields are required")
        secure_input = inputs.create(
            actor_id=session.actor.actor_id,
            key_name=payload["key_name"],
            value=payload.get("value"),
            generated_bytes=payload.get("generated_bytes"),
            idempotency_key=payload["idempotency_key"],
            request_id=request_id,
        )
        handler.write_json(
            HTTPStatus.OK if secure_input["idempotent"] else HTTPStatus.CREATED,
            {"request_id": request_id, "secure_input": secure_input},
        )
    except SecureInputError as exc:
        status = {
            "not_found": HTTPStatus.NOT_FOUND,
            "idempotency_conflict": HTTPStatus.CONFLICT,
            "secure_input_unavailable": HTTPStatus.SERVICE_UNAVAILABLE,
        }.get(exc.code, HTTPStatus.BAD_REQUEST)
        handler.write_json(status, error_payload(exc.code, exc.message, request_id))
    except (TypeError, ValueError) as exc:
        handler.write_json(
            HTTPStatus.BAD_REQUEST, error_payload("invalid_request", str(exc), request_id),
        )
    return True


def _route(path: str) -> str | None | bool:
    collection = "/api/v1/secure-inputs"
    if path == collection:
        return None
    prefix = f"{collection}/"
    if not path.startswith(prefix):
        return False
    input_id = unquote(path[len(prefix):]).strip("/")
    return input_id if input_id and "/" not in input_id else False
