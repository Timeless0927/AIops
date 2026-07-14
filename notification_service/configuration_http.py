"""Internal HTTP adapter for Notification Destination and Route administration."""

from __future__ import annotations

from http import HTTPStatus
from urllib.parse import unquote, urlparse

from .configuration import NotificationConfiguration, NotificationConfigurationError
from .noise_controls import NotificationNoiseControls
from .requests import NotificationRequestError, NotificationStore
from .templates import VARIABLES


def dispatch(
    handler,
    configuration: NotificationConfiguration,
    noise: NotificationNoiseControls,
    store: NotificationStore,
    authorize,
) -> bool:
    path = urlparse(handler.path).path
    if not path.startswith("/admin/notification-"):
        return False
    if authorize(handler) is None:
        return True
    try:
        if handler.command == "GET" and path == "/admin/notification-destinations":
            handler.write_json(HTTPStatus.OK, {"destinations": configuration.list_destinations()})
            return True
        if handler.command == "GET" and path == "/admin/notification-routes":
            handler.write_json(HTTPStatus.OK, {"routes": configuration.list_routes()})
            return True
        if handler.command == "GET" and path == "/admin/notification-templates":
            handler.write_json(HTTPStatus.OK, {"templates": configuration.list_templates(), "variables": list(VARIABLES)})
            return True
        if handler.command == "GET" and path == "/admin/notification-silences":
            handler.write_json(HTTPStatus.OK, {"silences": noise.list_silences()})
            return True
        if handler.command == "POST" and path == "/admin/notification-destinations":
            handler.write_json(HTTPStatus.CREATED, {"destination": configuration.create_destination(handler.read_json_body())})
            return True
        if handler.command == "POST" and path == "/admin/notification-routes":
            handler.write_json(HTTPStatus.CREATED, {"route": configuration.create_route(handler.read_json_body())})
            return True
        if handler.command == "POST" and path == "/admin/notification-routes/simulate":
            handler.write_json(HTTPStatus.OK, {"simulation": configuration.simulate(handler.read_json_body())})
            return True
        if handler.command == "POST" and path == "/admin/notification-templates":
            payload = handler.read_json_body()
            source_id = str(payload.pop("source_template_id", ""))
            handler.write_json(HTTPStatus.CREATED, {"template": configuration.copy_template(source_id, payload)})
            return True
        if handler.command == "POST" and path == "/admin/notification-silences":
            handler.write_json(HTTPStatus.CREATED, {"silence": noise.create_silence(handler.read_json_body())})
            return True
        destination_id = _member(path, "/admin/notification-destinations/")
        if handler.command == "POST" and destination_id and destination_id.endswith("/test"):
            payload = handler.read_json_body()
            _only_fields(payload, {"expected_revision", "operation_id"})
            verification = store.accept_test(
                destination_id[:-5],
                expected_revision=str(payload.get("expected_revision") or ""),
                operation_id=str(payload.get("operation_id") or ""),
            )
            handler.write_json(HTTPStatus.ACCEPTED, {"verification": verification})
            return True
        if handler.command == "POST" and destination_id and destination_id.endswith("/select-pilot-route"):
            payload = handler.read_json_body()
            _only_fields(payload, {"expected_revision", "operation_id"})
            destination = configuration.select_pilot_route(
                destination_id[:-19],
                expected_revision=str(payload.get("expected_revision") or ""),
                operation_id=str(payload.get("operation_id") or ""),
            )
            handler.write_json(HTTPStatus.OK, {"destination": destination})
            return True
        if handler.command == "PATCH" and destination_id and destination_id.endswith("/noise-control"):
            noise_control = noise.update_destination(destination_id[:-14], handler.read_json_body())
            handler.write_json(HTTPStatus.OK, {"noise_control": noise_control})
            return True
        if handler.command == "PATCH" and destination_id:
            handler.write_json(HTTPStatus.OK, {"destination": configuration.update_destination(destination_id, handler.read_json_body())})
            return True
        route_id = _member(path, "/admin/notification-routes/")
        if handler.command == "PATCH" and route_id:
            handler.write_json(HTTPStatus.OK, {"route": configuration.update_route(route_id, handler.read_json_body())})
            return True
        template_id = _member(path, "/admin/notification-templates/")
        if handler.command == "POST" and template_id and template_id.endswith("/preview"):
            payload = handler.read_json_body()
            preview = configuration.preview_template(template_id[:-8], payload.get("request", {}))
            handler.write_json(HTTPStatus.OK, {"template": configuration.templates.get(template_id[:-8]), "preview": preview})
            return True
        if handler.command == "POST" and template_id and template_id.endswith("/test"):
            payload = handler.read_json_body()
            result = configuration.test_template(template_id[:-5], str(payload.get("destination_id", "")), payload.get("request", {}))
            handler.write_json(HTTPStatus.OK, result)
            return True
        if handler.command == "PATCH" and template_id:
            handler.write_json(HTTPStatus.OK, {"template": configuration.update_template(template_id, handler.read_json_body())})
            return True
    except (NotificationConfigurationError, NotificationRequestError, TypeError, ValueError) as exc:
        status = HTTPStatus.CONFLICT if "conflict" in str(exc) or "revision has changed" in str(exc) else HTTPStatus.BAD_REQUEST
        handler.write_json(status, {"status": "rejected", "error": str(exc)})
        return True
    handler.write_not_found()
    return True


def _member(path: str, prefix: str) -> str | None:
    if not path.startswith(prefix):
        return None
    value = unquote(path[len(prefix) :]).strip("/")
    return value or None


def _only_fields(payload: dict[str, object], allowed: set[str]) -> None:
    if set(payload) != allowed or not all(isinstance(payload[field], str) and payload[field] for field in allowed):
        raise NotificationConfigurationError("Notification Destination request fields are invalid")
