"""Authenticated HTTP adapter for Diagnosis result writeback."""

from __future__ import annotations

import asyncio
import json
from http import HTTPStatus
from typing import Any

from apps.internal_auth import enforce_internal_auth

from . import APP_NAME
from .diagnosis_delivery import DiagnosisDelivery, DiagnosisDeliveryError
from .diagnosis_writeback import apply_diagnosis_writeback


def dispatch(handler: Any, route_path: str, delivery: DiagnosisDelivery) -> bool:
    if route_path != "/diagnosis/writeback":
        return False
    if enforce_internal_auth(
        handler,
        service_name=APP_NAME,
        allowed_service_account="aiops-diagnosis",
    ) is None:
        return True
    try:
        payload = handler.read_json_body()
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, {"service": APP_NAME, "status": "invalid", "error": str(exc)})
        return True

    if "request_id" not in payload and "investigation_id" not in payload:
        status, result = asyncio.run(apply_diagnosis_writeback(payload))
    else:
        try:
            result = delivery.accept_writeback(payload)
            status = HTTPStatus.OK
        except DiagnosisDeliveryError as exc:
            status = {
                "request_not_found": HTTPStatus.NOT_FOUND,
                "result_conflict": HTTPStatus.CONFLICT,
                "request_terminal": HTTPStatus.CONFLICT,
            }.get(exc.code, HTTPStatus.BAD_REQUEST)
            result = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
    handler.write_json(status, {"service": APP_NAME, **result})
    return True
