"""Internal HTTP adapter for Diagnosis-owned Model Provider state."""

from __future__ import annotations

from http import HTTPStatus

from diagnosis_service.model_provider import ModelProviderConfiguration, ModelProviderError


def dispatch(handler, path: str, owner: ModelProviderConfiguration, authorize_gateway) -> bool:
    known = {"/model-provider/status", "/admin/model-provider", "/admin/model-provider/test"}
    if path not in known:
        return False
    if authorize_gateway(handler) is None:
        return True
    try:
        if path == "/model-provider/status" and handler.command == "GET":
            handler.write_json(HTTPStatus.OK, {"model": owner.public_status()})
            return True
        if path == "/admin/model-provider" and handler.command == "GET":
            handler.write_json(HTTPStatus.OK, {"model_provider": owner.detail()})
            return True
        payload = handler.read_json_body()
        allowed = (
            {"actor_id", "operation_id", "expected_revision"}
            if handler.command == "DELETE" or path.endswith("/test")
            else {
                "actor_id", "operation_id", "expected_revision", "endpoint",
                "endpoint_scope", "model", "timeout_seconds", "api_key",
            }
        )
        if set(payload) != allowed:
            raise ModelProviderError("invalid_request", "Model Provider request fields are invalid")
        actor_id = str(payload.pop("actor_id", "")).strip()
        if not actor_id:
            raise ModelProviderError("invalid_request", "actor_id is required")
        if path == "/admin/model-provider" and handler.command == "PUT":
            expected_revision = payload.pop("expected_revision", None)
            operation_id = str(payload.pop("operation_id", "")).strip() or None
            detail = owner.save(
                payload,
                actor_id=actor_id,
                expected_revision=str(expected_revision) if expected_revision is not None else None,
                operation_id=operation_id,
            )
            handler.write_json(HTTPStatus.OK, {"model_provider": detail})
            return True
        if path == "/admin/model-provider" and handler.command == "DELETE":
            expected_revision = str(payload.get("expected_revision") or "")
            operation_id = str(payload.get("operation_id") or "").strip() or None
            detail = owner.delete(expected_revision=expected_revision, operation_id=operation_id)
            handler.write_json(HTTPStatus.OK, {"model_provider": detail})
            return True
        if path == "/admin/model-provider/test" and handler.command == "POST":
            operation = owner.start_verification(
                expected_revision=str(payload.get("expected_revision") or ""),
                actor_id=actor_id,
                operation_id=str(payload.get("operation_id") or ""),
            )
            handler.write_json(HTTPStatus.ACCEPTED, {"verification": operation})
            return True
    except (TypeError, ValueError, ModelProviderError) as exc:
        code = str(getattr(exc, "code", "invalid_request"))
        status = HTTPStatus.CONFLICT if code in {"revision_conflict", "operation_conflict"} else HTTPStatus.BAD_REQUEST
        handler.write_json(
            status,
            {"status": "rejected", "error": {"code": code, "message": str(exc)}},
        )
        return True
    return False
