"""Authenticated HTTP adapter for the cross-Incident Change Center."""

from __future__ import annotations

from functools import partial
from http import HTTPStatus
from typing import Any, Callable
from urllib.parse import unquote

from .change_center import ChangeCenter, ChangeCenterError
from .incident import IncidentService


def dispatch(
    handler: Any,
    route_path: str,
    sessions: Any,
    incidents: IncidentService,
    center: ChangeCenter,
    phase_approvals: Any,
    request_session: Callable[[Any], tuple[Any, str | None]],
    request_id_for: Callable[[Any], str],
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> bool:
    detail_id = _detail_route(route_path)
    if route_path != "/api/v1/changes" and detail_id is None:
        return False
    if handler.command != "GET":
        return False
    request_id = request_id_for(handler)
    session, _auth_mode = request_session(handler)
    if session is None:
        handler.write_json(
            HTTPStatus.UNAUTHORIZED,
            error_payload("unauthorized", "authentication required", request_id),
        )
        return True
    actor = sessions.actor_view(session.actor)
    capabilities = list(actor["capabilities"])
    if "view_incident" not in capabilities:
        handler.write_json(
            HTTPStatus.FORBIDDEN,
            error_payload("forbidden", "access denied", request_id),
        )
        return True
    team_ids = (
        None if actor["is_platform_administrator"]
        else incidents.team_ids_for_actor(session.actor.actor_id)
    )
    visible_incidents = incidents.list_incidents(team_ids=team_ids)
    arguments = {
        "actor_id": session.actor.actor_id,
        "can_manage": "manage_investigation" in capabilities,
        "phase_access": partial(
            phase_approvals.access_for_projection, request_id=request_id,
        ),
    }
    try:
        if detail_id is not None:
            detail = center.detail_for_actor(
                detail_id,
                visible_incidents,
                incident_snapshot=lambda incident_id: incidents.workbench(
                    incident_id,
                    team_ids=team_ids,
                    actor_capabilities=capabilities,
                ),
                **arguments,
            )
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, **detail})
        else:
            listing = center.list_for_actor(visible_incidents, **arguments)
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, **listing})
    except ChangeCenterError as exc:
        handler.write_json(
            HTTPStatus.NOT_FOUND,
            error_payload(exc.code, exc.message, request_id),
        )
    return True


def _detail_route(path: str) -> str | None:
    prefix = "/api/v1/changes/"
    if not path.startswith(prefix):
        return None
    change_request_id = unquote(path[len(prefix):]).strip("/")
    return change_request_id if change_request_id and "/" not in change_request_id else None
