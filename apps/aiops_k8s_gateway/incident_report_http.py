"""Authenticated HTTP adapter for V1 Incident Reports."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus
from typing import Any, Callable
from urllib.parse import unquote

from .incident_reports import IncidentReportError, IncidentReports


def dispatch(
    handler: Any,
    path: str,
    sessions: Any,
    incidents: Any,
    request_session: Callable[[Any], tuple[Any, str | None]],
    csrf_valid: Callable[[Any, str], bool],
    request_id_for: Callable[[Any], str],
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> bool:
    library = path == "/api/v1/reports"
    parsed = None if library else _route(path)
    if not library and parsed is None:
        return False
    incident_id, action = ("", None) if parsed is None else parsed
    if (library and handler.command != "GET") or (
        not library and action is None and handler.command not in {"GET", "PATCH"}
    ) or (
        action == "publish" and handler.command != "POST"
    ):
        return False
    request_id = request_id_for(handler)
    session, auth_mode = request_session(handler)
    if session is None:
        handler.write_json(HTTPStatus.UNAUTHORIZED, error_payload("unauthorized", "authentication required", request_id))
        return True
    actor = sessions.actor_view(session.actor)
    if "view_incident" not in actor["capabilities"]:
        handler.write_json(HTTPStatus.FORBIDDEN, error_payload("forbidden", "access denied", request_id))
        return True
    if handler.command != "GET" and auth_mode == "cookie" and not csrf_valid(handler, session.token):
        handler.write_json(HTTPStatus.FORBIDDEN, error_payload("csrf_required", "missing or invalid CSRF token", request_id))
        return True
    team_ids = None if actor["is_platform_administrator"] else incidents.team_ids_for_actor(session.actor.actor_id)
    reports = IncidentReports(sessions.database)
    try:
        if library:
            handler.write_json(HTTPStatus.OK, {
                "request_id": request_id,
                "reports": reports.list_for_actor(team_ids=team_ids),
            })
        elif handler.command == "GET":
            report = reports.get(incident_id, team_ids=team_ids, actor_id=session.actor.actor_id)
            if report is None:
                raise IncidentReportError("not_found", "Incident not found")
            can_edit = report["draft"] is not None and report["draft"]["status"] == "draft"  # type: ignore[index]
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, "can_edit": can_edit, **report})
        elif handler.command == "PATCH":
            draft = reports.update(
                incident_id, handler.read_json_body(), team_ids=team_ids, actor_id=session.actor.actor_id
            )
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, "draft": draft})
        else:
            payload = handler.read_json_body()
            if payload:
                raise IncidentReportError("invalid_request", "publish request body must be empty")
            publication = reports.publish(incident_id, team_ids=team_ids, actor_id=session.actor.actor_id)
            handler.write_json(HTTPStatus.CREATED, {"request_id": request_id, "publication": publication})
    except IncidentReportError as exc:
        status = {
            "not_found": HTTPStatus.NOT_FOUND,
            "report_not_ready": HTTPStatus.CONFLICT,
            "report_published": HTTPStatus.CONFLICT,
        }.get(exc.code, HTTPStatus.BAD_REQUEST)
        handler.write_json(status, error_payload(exc.code, exc.message, request_id))
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, error_payload("invalid_request", str(exc), request_id))
    except sqlite3.Error:
        handler.write_json(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            error_payload("report_write_failed", "Incident Report operation was not committed", request_id),
        )
    return True


def _route(path: str) -> tuple[str, str | None] | None:
    parts = [unquote(part) for part in path.strip("/").split("/")]
    if len(parts) == 5 and parts[:3] == ["api", "v1", "incidents"] and parts[4] == "report" and parts[3]:
        return parts[3], None
    if len(parts) == 6 and parts[:3] == ["api", "v1", "incidents"] and parts[4:] == ["report", "publish"] and parts[3]:
        return parts[3], "publish"
    return None
