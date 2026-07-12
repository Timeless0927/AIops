"""HTTP entry point for the independent Notification Engine."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from http import HTTPStatus
from pathlib import Path

from apps.internal_auth import enforce_internal_auth
from apps.service_http import JsonHandler, serve

from . import configuration_http
from .configuration import NotificationConfiguration
from .noise_controls import NotificationNoiseControls
from .requests import NotificationRequestError, NotificationStore, start_delivery_worker


SERVICE_NAME = "notification-engine"
_STORE: NotificationStore | None = None
_CONFIGURATION: NotificationConfiguration | None = None
_NOISE: NotificationNoiseControls | None = None
_KEY_PATH = Path("/var/run/secrets/aiops-notification/key")


class NotificationServiceHandler(JsonHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.is_metrics_request():
            self.write_metrics(SERVICE_NAME)
            return
        if self.path in {"/healthz", "/readyz"}:
            self.write_json(HTTPStatus.OK, {"service": SERVICE_NAME, "status": "ok"})
            return
        if self.path == "/admin/notification-deliveries":
            if _authorize_gateway(self) is not None:
                self.write_json(HTTPStatus.OK, {"deliveries": _notification_store().list_delivery_results()})
            return
        if self.path.startswith("/admin/notification-") and configuration_http.dispatch(self, _notification_configuration(), _notification_noise(), _authorize_gateway):
            return
        self.write_not_found()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/admin/notification-") and configuration_http.dispatch(self, _notification_configuration(), _notification_noise(), _authorize_gateway):
            return
        if self.path != "/notification-requests":
            self.write_not_found()
            return
        if enforce_internal_auth(
            self,
            service_name=SERVICE_NAME,
            allowed_service_account="aiops-gateway",
        ) is None:
            return
        try:
            payload = self.read_json_body()
            result = _notification_store().accept(payload)
        except (ValueError, TypeError, json.JSONDecodeError, NotificationRequestError) as exc:
            status = HTTPStatus.CONFLICT if "conflict" in str(exc) else HTTPStatus.BAD_REQUEST
            self.write_json(status, {"status": "rejected", "error": str(exc)})
            return
        self.write_json(HTTPStatus.ACCEPTED, result)

    def do_PATCH(self) -> None:  # noqa: N802
        if configuration_http.dispatch(self, _notification_configuration(), _notification_noise(), _authorize_gateway):
            return
        self.write_not_found()


def _notification_store() -> NotificationStore:
    global _STORE
    path = Path(os.getenv("AIOPS_DATA_DIR", "/data/aiops")) / "notification.db"
    if _STORE is None or _STORE.db_path != path:
        _STORE = NotificationStore(
            path,
            console_base_url=os.getenv("AIOPS_CONSOLE_BASE_URL", "https://aiops.invalid"),
            router=_notification_configuration().route,
            noise_evaluator=_notification_noise().evaluate,
        )
    return _STORE


def _notification_configuration() -> NotificationConfiguration:
    global _CONFIGURATION
    path = Path(os.getenv("AIOPS_DATA_DIR", "/data/aiops")) / "notification.db"
    if _CONFIGURATION is None or _CONFIGURATION.db_path != path:
        _CONFIGURATION = NotificationConfiguration(path, _KEY_PATH, _notification_noise())
    return _CONFIGURATION


def _notification_noise() -> NotificationNoiseControls:
    global _NOISE
    path = Path(os.getenv("AIOPS_DATA_DIR", "/data/aiops")) / "notification.db"
    if _NOISE is None or _NOISE.db_path != path:
        _NOISE = NotificationNoiseControls(path)
    return _NOISE


def _authorize_gateway(handler) -> str | None:
    return enforce_internal_auth(handler, service_name=SERVICE_NAME, allowed_service_account="aiops-gateway")


def _provider_send(payload: dict[str, object]) -> dict[str, object]:
    if payload["destination"] == "builtin-fake":
        return {"ok": True, "message_id": f"fake-{uuid.uuid4().hex}"}
    sent = _notification_configuration().send(
        str(payload["destination"]),
        str(payload.get("subject") or payload["title"]),
        str(payload.get("html") or payload["body"]),
        body_format="html" if payload.get("html") else "text",
    )
    return {"ok": sent, "message_id": f"apprise-{uuid.uuid4().hex}" if sent else None}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIOps Notification Engine")
    parser.add_argument("--host", default=os.getenv("AIOPS_NOTIFICATION_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AIOPS_NOTIFICATION_PORT", "8086")))
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    start_delivery_worker(_notification_store(), sender=_provider_send)
    serve(NotificationServiceHandler, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
