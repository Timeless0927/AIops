"""Notification Delivery payload to configured Apprise transport Adapter."""

from __future__ import annotations

import uuid

from .configuration import (
    NotificationConfiguration,
    NotificationConfigurationError,
    NotificationDestinationRevisionChanged,
)


JSON = dict[str, object]


def send_delivery(configuration: NotificationConfiguration, payload: JSON) -> JSON:
    if payload["destination"] == "builtin-fake":
        return {"ok": True, "message_id": f"fake-{uuid.uuid4().hex}"}
    try:
        result = configuration.delivery_result(
            str(payload["destination"]),
            str(payload.get("subject") or payload["title"]),
            str(payload.get("html") or payload["body"]),
            body_format="html" if payload.get("html") else "text",
            expected_revision=str(payload["destination_revision"]) if payload.get("destination_revision") else None,
            allow_disabled=bool(payload.get("is_test")),
        )
    except NotificationDestinationRevisionChanged:
        return {
            "ok": False,
            "paused": True,
            "retryable": True,
            "reason_code": "configuration_changed",
            "error": "destination revision has changed",
        }
    except NotificationConfigurationError as exc:
        return {"ok": False, "retryable": False, "reason_code": "provider_rejected", "error": str(exc)}
    except Exception as exc:
        reason_code = "timeout" if isinstance(exc, TimeoutError) else "provider_unavailable"
        return {"ok": False, "retryable": True, "reason_code": reason_code, "error": f"{type(exc).__name__}: Apprise transport failed"}
    if not result.get("ok"):
        return result
    return {"ok": True, "message_id": f"apprise-{uuid.uuid4().hex}"}
