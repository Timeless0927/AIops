"""Authenticated HTTP adapters for Change Request planning."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from typing import Any, Callable
from urllib import error, request
from urllib.parse import unquote

from apps.internal_auth import internal_auth_headers

from .change_requests import ChangeRequestError, ChangeRequests
from .incident import IncidentService
from .kubernetes_change_authorities import (
    KubernetesChangeAuthorities,
    KubernetesChangeAuthorityError,
)


def dispatch(
    handler: Any,
    route_path: str,
    sessions: Any,
    incidents: IncidentService,
    changes: ChangeRequests,
    authorities: KubernetesChangeAuthorities,
    phase_approvals: Any,
    request_session: Callable[[Any], tuple[Any, str | None]],
    csrf_valid: Callable[[Any, str], bool],
    request_id_for: Callable[[Any], str],
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> bool:
    create_incident_id = _create_route(route_path)
    input_request_id = _input_route(route_path)
    retry_request_id = _retry_route(route_path)
    detail_request_id = _detail_route(route_path)
    if create_incident_id is None and input_request_id is None and retry_request_id is None and detail_request_id is None:
        return False
    request_id = request_id_for(handler)
    session, auth_mode = request_session(handler)
    if session is None:
        handler.write_json(HTTPStatus.UNAUTHORIZED, error_payload("unauthorized", "authentication required", request_id))
        return True
    actor = sessions.actor_view(session.actor)
    capabilities = list(actor["capabilities"])
    team_ids = None if actor["is_platform_administrator"] else incidents.team_ids_for_actor(session.actor.actor_id)
    project = lambda item: changes.project_for_actor(  # noqa: E731
        item, actor_id=session.actor.actor_id,
        phase_access=phase_approvals.access_for_projection,
    )

    if handler.command == "GET" and detail_request_id is not None:
        try:
            item = changes.get(detail_request_id)
        except ChangeRequestError:
            handler.write_json(HTTPStatus.NOT_FOUND, error_payload("not_found", "Change Request not found", request_id))
            return True
        if incidents.workbench(str(item["incident_id"]), team_ids=team_ids, actor_capabilities=capabilities) is None:
            handler.write_json(HTTPStatus.NOT_FOUND, error_payload("not_found", "Change Request not found", request_id))
            return True
        handler.write_json(
            HTTPStatus.OK, {"request_id": request_id, "change_request": project(item)},
        )
        return True
    if handler.command != "POST" or (create_incident_id is None and input_request_id is None and retry_request_id is None):
        return False
    if "manage_investigation" not in capabilities:
        handler.write_json(HTTPStatus.FORBIDDEN, error_payload("forbidden", "access denied", request_id))
        return True
    if auth_mode == "cookie" and not csrf_valid(handler, session.token):
        handler.write_json(HTTPStatus.FORBIDDEN, error_payload("csrf_required", "missing or invalid CSRF token", request_id))
        return True
    try:
        payload = handler.read_json_body()
        planner = lambda planning_payload: send_plan_request(planning_payload, request_id)
        if create_incident_id is not None:
            if set(payload) != {"desired_outcome", "context", "idempotency_key"}:
                raise ChangeRequestError("invalid_request", "invalid Change Request fields")
            facts = incidents.planning_facts(create_incident_id, team_ids=team_ids)
            if facts is None:
                raise ChangeRequestError("not_found", "Incident not found")
            authorities.authorize_proposal(facts, actor_id=session.actor.actor_id)
            created, item = changes.submit(
                incident_id=create_incident_id,
                facts=facts,
                actor_id=session.actor.actor_id,
                desired_outcome=payload["desired_outcome"],
                context=payload["context"],
                idempotency_key=payload["idempotency_key"],
                request_id=request_id,
                planner=planner,
                plan_authorizer=lambda plan: authorities.authorize_draft_plan(
                    facts, actor_id=session.actor.actor_id, plan=plan,
                ),
            )
            handler.write_json(
                HTTPStatus.CREATED if created else HTTPStatus.OK,
                {"request_id": request_id, "change_request": project(item)},
            )
            return True
        if retry_request_id is not None:
            if set(payload) != {"idempotency_key"}:
                raise ChangeRequestError("invalid_request", "invalid Change Request retry fields")
            current = changes.get(retry_request_id)
            facts = incidents.planning_facts(str(current["incident_id"]), team_ids=team_ids)
            if facts is None:
                raise ChangeRequestError("not_found", "Change Request not found")
            authorities.authorize_proposal(facts, actor_id=session.actor.actor_id)
            item = changes.retry(
                retry_request_id,
                facts=facts,
                actor_id=session.actor.actor_id,
                idempotency_key=payload["idempotency_key"],
                request_id=request_id,
                planner=planner,
                plan_authorizer=lambda plan: authorities.authorize_draft_plan(
                    facts, actor_id=session.actor.actor_id, plan=plan,
                ),
            )
            handler.write_json(
                HTTPStatus.OK, {"request_id": request_id, "change_request": project(item)},
            )
            return True
        if set(payload) != {"content", "idempotency_key"}:
            raise ChangeRequestError("invalid_request", "invalid Change Request input fields")
        current = changes.get(str(input_request_id))
        facts = incidents.planning_facts(str(current["incident_id"]), team_ids=team_ids)
        if facts is None:
            raise ChangeRequestError("not_found", "Change Request not found")
        authorities.authorize_proposal(facts, actor_id=session.actor.actor_id)
        item = changes.add_input(
            str(input_request_id),
            facts=facts,
            actor_id=session.actor.actor_id,
            content=payload["content"],
            idempotency_key=payload["idempotency_key"],
            request_id=request_id,
            planner=planner,
            plan_authorizer=lambda plan: authorities.authorize_draft_plan(
                facts, actor_id=session.actor.actor_id, plan=plan,
            ),
        )
        handler.write_json(
            HTTPStatus.OK, {"request_id": request_id, "change_request": project(item)},
        )
    except (ChangeRequestError, KubernetesChangeAuthorityError) as exc:
        status = {
            "not_found": HTTPStatus.NOT_FOUND,
            "input_not_expected": HTTPStatus.CONFLICT,
            "planning_conflict": HTTPStatus.CONFLICT,
            "planning_not_retryable": HTTPStatus.CONFLICT,
            "idempotency_conflict": HTTPStatus.CONFLICT,
            "planner_unavailable": HTTPStatus.SERVICE_UNAVAILABLE,
            "cluster_not_ready": HTTPStatus.CONFLICT,
            "proposal_forbidden": HTTPStatus.FORBIDDEN,
        }.get(exc.code, HTTPStatus.BAD_REQUEST)
        handler.write_json(status, error_payload(exc.code, exc.message, request_id))
    except (TypeError, ValueError) as exc:
        handler.write_json(HTTPStatus.BAD_REQUEST, error_payload("invalid_request", str(exc), request_id))
    return True


def send_plan_request(payload: dict[str, object], request_id: str) -> dict[str, object]:
    base_url = os.getenv("AIOPS_DIAGNOSIS_URL", "").strip()
    if not base_url:
        raise ChangeRequestError("planner_unavailable", "Diagnosis planning service is not configured")
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Request-ID": request_id,
        "X-Correlation-ID": str(payload["change_request_id"]),
        **internal_auth_headers(),
    }
    outgoing = request.Request(f"{base_url.rstrip('/')}/change-plans", data=body, headers=headers, method="POST")
    try:
        with request.urlopen(outgoing, timeout=10) as response:
            result = json.loads(response.read().decode() or "{}")
    except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ChangeRequestError("planner_unavailable", "Diagnosis planning request failed") from exc
    if not isinstance(result, dict):
        raise ChangeRequestError("invalid_plan", "Diagnosis planning response must be an object")
    if result.get("service") != "diagnosis":
        raise ChangeRequestError("invalid_plan", "Diagnosis planning response has an invalid service identity")
    return {key: value for key, value in result.items() if key != "service"}


def _create_route(path: str) -> str | None:
    prefix = "/api/v1/incidents/"
    suffix = "/change-requests"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    incident_id = unquote(path[len(prefix) : -len(suffix)]).strip("/")
    return incident_id if incident_id and "/" not in incident_id else None


def _input_route(path: str) -> str | None:
    prefix = "/api/v1/change-requests/"
    suffix = "/input"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    request_id = unquote(path[len(prefix) : -len(suffix)]).strip("/")
    return request_id if request_id and "/" not in request_id else None


def _retry_route(path: str) -> str | None:
    prefix = "/api/v1/change-requests/"
    suffix = "/retry"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    request_id = unquote(path[len(prefix) : -len(suffix)]).strip("/")
    return request_id if request_id and "/" not in request_id else None


def _detail_route(path: str) -> str | None:
    prefix = "/api/v1/change-requests/"
    if not path.startswith(prefix) or path.endswith(("/input", "/retry")):
        return None
    request_id = unquote(path[len(prefix) :]).strip("/")
    return request_id if request_id and "/" not in request_id else None
