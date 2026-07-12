"""Internal HTTP adapter for Notification Destination and Route administration."""

from __future__ import annotations

from http import HTTPStatus
from urllib.parse import unquote, urlparse

from .configuration import NotificationConfiguration, NotificationConfigurationError
from .templates import VARIABLES


def dispatch(handler, configuration: NotificationConfiguration, authorize) -> bool:
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
            handler.write_json(HTTPStatus.OK, {"silences": configuration.noise.list_silences()})
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
            handler.write_json(HTTPStatus.CREATED, {"silence": configuration.noise.create_silence(handler.read_json_body())})
            return True
        destination_id = _member(path, "/admin/notification-destinations/")
        if handler.command == "POST" and destination_id and destination_id.endswith("/test"):
            handler.write_json(HTTPStatus.OK, {"destination": configuration.test_destination(destination_id[:-5])})
            return True
        if handler.command == "PATCH" and destination_id and destination_id.endswith("/noise-control"):
            noise_control = configuration.noise.update_destination(destination_id[:-14], handler.read_json_body())
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
    except (NotificationConfigurationError, TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, {"status": "rejected", "error": str(exc)})
        return True
    handler.write_not_found()
    return True


def _member(path: str, prefix: str) -> str | None:
    if not path.startswith(prefix):
        return None
    value = unquote(path[len(prefix) :]).strip("/")
    return value or None
