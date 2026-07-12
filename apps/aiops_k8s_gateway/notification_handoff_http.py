"""Authenticated HTTP Adapter for Notification Engine handoff."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from urllib import error, request

from apps.internal_auth import internal_auth_headers


JSON = dict[str, object]


def send_notification_request(payload: JSON) -> tuple[int, JSON]:
    base_url = os.getenv("AIOPS_NOTIFICATION_ENGINE_URL", "").strip()
    if not base_url:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"status": "notification_engine_unconfigured"}
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    correlation_id = str(payload.get("event_id") or "notification")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Request-ID": f"notification-{correlation_id}",
        "X-Correlation-ID": correlation_id,
        **internal_auth_headers(),
    }
    req = request.Request(
        f"{base_url.rstrip('/')}/notification-requests", data=body, headers=headers, method="POST"
    )
    try:
        with request.urlopen(req, timeout=2.0) as response:
            result = json.loads(response.read().decode() or "{}")
            return response.status, result if isinstance(result, dict) else {"status": "invalid_response"}
    except error.HTTPError as exc:
        result = json.loads(exc.read().decode() or "{}")
        return exc.code, result if isinstance(result, dict) else {"status": "invalid_response"}
