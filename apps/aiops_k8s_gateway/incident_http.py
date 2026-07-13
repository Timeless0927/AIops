"""Authenticated HTTP adapter for V1 Incident reads."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Callable
from urllib.parse import unquote

from .incident import IncidentService


def dispatch(
    handler: Any,
    route_path: str,
    sessions: Any,
    incidents: IncidentService,
    changes: Any,
    phase_approvals: Any,
    request_session: Callable[[Any], tuple[Any, str | None]],
    request_id_for: Callable[[Any], str],
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> bool:
    if route_path == "/api/v1/incidents":
        incident_id = None
    elif route_path.startswith("/api/v1/incidents/") and route_path.endswith("/workbench"):
        incident_id = unquote(route_path[len("/api/v1/incidents/") : -len("/workbench")]).strip("/")
        if not incident_id or "/" in incident_id:
            return False
    else:
        return False

    request_id = request_id_for(handler)
    session, _ = request_session(handler)
    if session is None:
        handler.write_json(HTTPStatus.UNAUTHORIZED, error_payload("unauthorized", "authentication required", request_id))
        return True
    actor = sessions.actor_view(session.actor)
    capabilities = list(actor["capabilities"])
    if "view_incident" not in capabilities:
        handler.write_json(HTTPStatus.FORBIDDEN, error_payload("forbidden", "access denied", request_id))
        return True
    team_ids = None if actor["is_platform_administrator"] else incidents.team_ids_for_actor(session.actor.actor_id)
    if incident_id is None:
        handler.write_json(
            HTTPStatus.OK,
            {"request_id": request_id, "incidents": incidents.list_incidents(team_ids=team_ids)},
        )
        return True
    snapshot = incidents.workbench(incident_id, team_ids=team_ids, actor_capabilities=capabilities)
    if snapshot is None:
        handler.write_json(HTTPStatus.NOT_FOUND, error_payload("not_found", "incident not found", request_id))
        return True
    projected = snapshot
    projected["change_requests"] = changes.list_for_incident_for_actor(
        incident_id, actor_id=session.actor.actor_id,
        phase_access=phase_approvals.access_for_projection,
    )
    handler.write_json(HTTPStatus.OK, {"request_id": request_id, **projected})
    return True
