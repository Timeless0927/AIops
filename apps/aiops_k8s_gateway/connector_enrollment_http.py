"""HTTP adapter for safe Connector Enrollment status."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any


def dispatch_get(
    handler: Any,
    path: str,
    enrollments: Any,
    request_session: Any,
    request_id_for: Any,
    error_payload: Any,
) -> bool:
    if path != "/api/v1/connectors/status":
        return False
    request_id = request_id_for(handler)
    session, _ = request_session(handler)
    if session is None:
        handler.write_json(
            HTTPStatus.UNAUTHORIZED,
            error_payload("unauthorized", "authentication required", request_id),
        )
    else:
        handler.write_json(
            HTTPStatus.OK,
            {"request_id": request_id, "connectors": enrollments.public_status()},
        )
    return True
