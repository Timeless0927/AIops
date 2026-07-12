"""V1 HTTP adapter for Investigation Events, Human Input and controls."""

from __future__ import annotations

import json
import threading
import time
from http import HTTPStatus
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from .incident import IncidentError, IncidentService
from .investigation_events import InvestigationEventError, InvestigationEvents


_SSE_LOCK = threading.Lock()
_SSE_CONNECTIONS = 0


def dispatch_get(
    handler: Any,
    route_path: str,
    sessions: Any,
    incidents: IncidentService,
    events: InvestigationEvents,
    request_session: Callable[[Any], tuple[Any, str | None]],
    request_id_for: Callable[[Any], str],
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> bool:
    parsed = _event_route(route_path)
    if parsed is None:
        return False
    investigation_id, stream = parsed
    request_id = request_id_for(handler)
    access = _access(handler, investigation_id, sessions, incidents, events, request_session, request_id, error_payload)
    if access is None:
        return True
    try:
        query = parse_qs(urlparse(handler.path).query)
        query_after = _integer(query.get("after", ["0"])[0], "after", minimum=0)
        header_after = _integer(handler.headers.get("Last-Event-ID", "0"), "Last-Event-ID", minimum=0)
        after = max(query_after, header_after)
        limit = _integer(query.get("limit", ["100"])[0], "limit", minimum=1, maximum=200)
        if stream:
            _stream(
                handler,
                events,
                investigation_id,
                after,
                lambda: _allowed(handler, investigation_id, sessions, incidents, events, request_session),
            )
        else:
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, **events.list(investigation_id, after=after, limit=limit)})
    except InvestigationEventError as exc:
        _write_error(handler, exc, request_id, error_payload)
    return True


def dispatch_post(
    handler: Any,
    route_path: str,
    sessions: Any,
    incidents: IncidentService,
    events: InvestigationEvents,
    request_session: Callable[[Any], tuple[Any, str | None]],
    csrf_valid: Callable[[Any, str], bool],
    request_id_for: Callable[[Any], str],
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> bool:
    investigation_action = _investigation_action(route_path)
    reinvestigate_id = _reinvestigate_route(route_path)
    if investigation_action is None and reinvestigate_id is None:
        return False
    request_id = request_id_for(handler)
    if investigation_action is not None:
        investigation_id, action = investigation_action
        access = _access(handler, investigation_id, sessions, incidents, events, request_session, request_id, error_payload)
    else:
        investigation_id, action = "", "reinvestigate"
        access = _incident_access(handler, reinvestigate_id or "", sessions, incidents, request_session, request_id, error_payload)
    if access is None:
        return True
    session, actor = access
    if "manage_investigation" not in actor["capabilities"]:
        handler.write_json(HTTPStatus.FORBIDDEN, error_payload("forbidden", "access denied", request_id))
        return True
    if not csrf_valid(handler, session.token):
        handler.write_json(HTTPStatus.FORBIDDEN, error_payload("csrf_required", "missing or invalid CSRF token", request_id))
        return True
    try:
        payload = handler.read_json_body()
        idempotency_key = _text(payload, "idempotency_key", maximum=200)
        if action == "human-input":
            _only_fields(payload, {"kind", "content", "idempotency_key", "target_event_id"})
            event = events.submit_human_input(
                investigation_id,
                kind=_text(payload, "kind", maximum=20),
                content=_text(payload, "content", maximum=4000),
                actor_id=session.actor.actor_id,
                idempotency_key=idempotency_key,
                target_event_id=_optional_integer(payload.get("target_event_id"), "target_event_id"),
            )
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, "event": event})
        elif action == "controls":
            _only_fields(payload, {"action", "idempotency_key"})
            event = events.control(
                investigation_id,
                action=_text(payload, "action", maximum=20),
                actor_id=session.actor.actor_id,
                idempotency_key=idempotency_key,
            )
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, "event": event})
        else:
            _only_fields(payload, {"idempotency_key"})
            investigation = incidents.reinvestigate(reinvestigate_id or "", idempotency_key=idempotency_key)
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, "investigation": investigation})
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, error_payload("invalid_request", str(exc), request_id))
    except InvestigationEventError as exc:
        _write_error(handler, exc, request_id, error_payload)
    except IncidentError as exc:
        status = HTTPStatus.NOT_FOUND if exc.code == "incident_not_found" else HTTPStatus.CONFLICT
        handler.write_json(status, error_payload(exc.code, exc.message, request_id))
    return True


def _access(
    handler: Any,
    investigation_id: str,
    sessions: Any,
    incidents: IncidentService,
    events: InvestigationEvents,
    request_session: Callable[[Any], tuple[Any, str | None]],
    request_id: str,
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> tuple[Any, dict[str, object]] | None:
    session, _ = request_session(handler)
    if session is None:
        handler.write_json(HTTPStatus.UNAUTHORIZED, error_payload("unauthorized", "authentication required", request_id))
        return None
    actor = sessions.actor_view(session.actor)
    if "view_incident" not in actor["capabilities"]:
        handler.write_json(HTTPStatus.FORBIDDEN, error_payload("forbidden", "access denied", request_id))
        return None
    incident_id = events.incident_id(investigation_id)
    if incident_id is None:
        handler.write_json(HTTPStatus.NOT_FOUND, error_payload("not_found", "Investigation not found", request_id))
        return None
    team_ids = None if actor["is_platform_administrator"] else incidents.team_ids_for_actor(session.actor.actor_id)
    if incidents.workbench(incident_id, team_ids=team_ids, actor_capabilities=list(actor["capabilities"])) is None:
        handler.write_json(HTTPStatus.NOT_FOUND, error_payload("not_found", "Incident not found", request_id))
        return None
    return session, actor


def _incident_access(
    handler: Any,
    incident_id: str,
    sessions: Any,
    incidents: IncidentService,
    request_session: Callable[[Any], tuple[Any, str | None]],
    request_id: str,
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> tuple[Any, dict[str, object]] | None:
    session, _ = request_session(handler)
    if session is None:
        handler.write_json(HTTPStatus.UNAUTHORIZED, error_payload("unauthorized", "authentication required", request_id))
        return None
    actor = sessions.actor_view(session.actor)
    if "view_incident" not in actor["capabilities"]:
        handler.write_json(HTTPStatus.FORBIDDEN, error_payload("forbidden", "access denied", request_id))
        return None
    team_ids = None if actor["is_platform_administrator"] else incidents.team_ids_for_actor(session.actor.actor_id)
    if incidents.workbench(incident_id, team_ids=team_ids, actor_capabilities=list(actor["capabilities"])) is None:
        handler.write_json(HTTPStatus.NOT_FOUND, error_payload("not_found", "Incident not found", request_id))
        return None
    return session, actor


def _allowed(
    handler: Any,
    investigation_id: str,
    sessions: Any,
    incidents: IncidentService,
    events: InvestigationEvents,
    request_session: Callable[[Any], tuple[Any, str | None]],
) -> bool:
    session, _ = request_session(handler)
    incident_id = events.incident_id(investigation_id)
    if session is None or incident_id is None:
        return False
    actor = sessions.actor_view(session.actor)
    if "view_incident" not in actor["capabilities"]:
        return False
    team_ids = None if actor["is_platform_administrator"] else incidents.team_ids_for_actor(session.actor.actor_id)
    return incidents.workbench(incident_id, team_ids=team_ids, actor_capabilities=list(actor["capabilities"])) is not None


def _stream(
    handler: Any,
    events: InvestigationEvents,
    investigation_id: str,
    after: int,
    allowed: Callable[[], bool],
) -> None:
    global _SSE_CONNECTIONS
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    cursor = after
    deadline = time.monotonic() + 15
    next_auth_check = time.monotonic() + 1
    with _SSE_LOCK:
        _SSE_CONNECTIONS += 1
    try:
        while time.monotonic() < deadline:
            if time.monotonic() >= next_auth_check:
                if not allowed():
                    handler.wfile.write(b"event: permission_denied\ndata: {}\n\n")
                    handler.wfile.flush()
                    return
                next_auth_check = time.monotonic() + 1
            page, status = events.poll(investigation_id, after=cursor, limit=200)
            for event in page["events"]:  # type: ignore[union-attr]
                cursor = int(event["id"])
                data = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handler.wfile.write(f"id: {cursor}\nevent: investigation\ndata: {data}\n\n".encode())
            if page["events"]:
                handler.wfile.flush()
            if status in {"completed", "failed", "terminated"} and not page["has_more"]:
                handler.wfile.write(f"event: terminal\ndata: {json.dumps({'status': status})}\n\n".encode())
                handler.wfile.flush()
                return
            time.sleep(0.25)
        handler.wfile.write(b": reconnect\n\n")
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        return
    finally:
        with _SSE_LOCK:
            _SSE_CONNECTIONS -= 1


def sse_connections() -> int:
    with _SSE_LOCK:
        return _SSE_CONNECTIONS


def _event_route(path: str) -> tuple[str, bool] | None:
    prefix = "/api/v1/investigations/"
    if not path.startswith(prefix):
        return None
    parts = [unquote(part) for part in path[len(prefix) :].split("/") if part]
    if len(parts) == 2 and parts[1] == "events":
        return parts[0], False
    if len(parts) == 3 and parts[1:] == ["events", "stream"]:
        return parts[0], True
    return None


def _investigation_action(path: str) -> tuple[str, str] | None:
    prefix = "/api/v1/investigations/"
    if not path.startswith(prefix):
        return None
    parts = [unquote(part) for part in path[len(prefix) :].split("/") if part]
    return (parts[0], parts[1]) if len(parts) == 2 and parts[1] in {"human-input", "controls"} else None


def _reinvestigate_route(path: str) -> str | None:
    prefix = "/api/v1/incidents/"
    if not path.startswith(prefix):
        return None
    parts = [unquote(part) for part in path[len(prefix) :].split("/") if part]
    return parts[0] if len(parts) == 2 and parts[1] == "reinvestigate" else None


def _text(payload: dict[str, object], field: str, *, maximum: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"{field} is required")
    return value.strip()


def _only_fields(payload: dict[str, object], allowed: set[str]) -> None:
    extras = set(payload) - allowed
    if extras:
        raise ValueError(f"unsupported fields: {', '.join(sorted(extras))}")


def _integer(value: object, field: str, *, minimum: int, maximum: int | None = None) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise InvestigationEventError("invalid_pagination", f"{field} must be an integer") from exc
    if parsed < minimum or (maximum is not None and parsed > maximum):
        raise InvestigationEventError("invalid_pagination", f"{field} is outside the supported range")
    return parsed


def _optional_integer(value: object, field: str) -> int | None:
    return None if value is None else _integer(value, field, minimum=1)


def _write_error(
    handler: Any,
    exc: InvestigationEventError,
    request_id: str,
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> None:
    status = {
        "investigation_not_found": HTTPStatus.NOT_FOUND,
        "idempotency_conflict": HTTPStatus.CONFLICT,
        "investigation_terminal": HTTPStatus.CONFLICT,
        "invalid_transition": HTTPStatus.CONFLICT,
    }.get(exc.code, HTTPStatus.BAD_REQUEST)
    handler.write_json(status, error_payload(exc.code, exc.message, request_id))
