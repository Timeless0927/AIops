"""Internal HTTP adapter for Change Request planning."""

from __future__ import annotations

import asyncio
import json
from http import HTTPStatus
from typing import Any

from apps.internal_auth import enforce_internal_auth

from .change_planner import ChangePlannerError, plan_change_request
from .diagnosis_provider import ProviderUnavailable


def handle(handler: Any, provider: Any, service_name: str) -> None:
    if enforce_internal_auth(
        handler,
        service_name=service_name,
        allowed_service_account="aiops-gateway",
    ) is None:
        return
    try:
        result = asyncio.run(plan_change_request(handler.read_json_body(), provider))
    except ProviderUnavailable as exc:
        handler.write_json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"service": service_name, "status": "rejected", "error": {"code": exc.code, "message": exc.message}},
        )
        return
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        code = exc.code if isinstance(exc, ChangePlannerError) else "invalid_request"
        message = exc.message if isinstance(exc, ChangePlannerError) else str(exc)
        status = HTTPStatus.SERVICE_UNAVAILABLE if code == "provider_unavailable" else HTTPStatus.BAD_REQUEST
        handler.write_json(
            status,
            {"service": service_name, "status": "rejected", "error": {"code": code, "message": message}},
        )
        return
    handler.write_json(HTTPStatus.OK, {"service": service_name, **result})
