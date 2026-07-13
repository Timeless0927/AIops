"""HTTP adapter for one approved generic Kubernetes Change execution."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Callable
from urllib.parse import unquote

from .change_requests import ChangeRequestError, ChangeRequests
from .kubernetes_change_executions import (
    KubernetesChangeExecutionError,
    KubernetesChangeExecutions,
)


def dispatch(
    handler: Any,
    path: str,
    sessions: Any,
    changes: ChangeRequests,
    phase_approvals: Any,
    executions: KubernetesChangeExecutions,
    request_session: Callable[[Any], tuple[Any, str | None]],
    csrf_valid: Callable[[Any, str], bool],
    request_id_for: Callable[[Any], str],
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> bool:
    route = _route(path)
    if route is None:
        return False
    change_request_id, starting = route
    if (starting and handler.command != "POST") or (not starting and handler.command != "GET"):
        return False
    request_id = request_id_for(handler)
    session, auth_mode = request_session(handler)
    if session is None:
        handler.write_json(
            HTTPStatus.UNAUTHORIZED,
            error_payload("unauthorized", "authentication required", request_id),
        )
        return True
    try:
        change = changes.get(change_request_id)
        visible, _ = phase_approvals.access_for_projection(
            change_request_id, session.actor.actor_id, str(change["status"]),
        )
        if not visible:
            raise KubernetesChangeExecutionError("not_found", "Phase execution not found")
        if not starting:
            execution = executions.for_phase(str(change["active_phase"]["id"]))  # type: ignore[index]
            handler.write_json(
                HTTPStatus.OK,
                {"request_id": request_id, "phase_execution": execution},
            )
            return True
        if auth_mode == "cookie" and not csrf_valid(handler, session.token):
            handler.write_json(
                HTTPStatus.FORBIDDEN,
                error_payload("csrf_required", "missing or invalid CSRF token", request_id),
            )
            return True
        if not sessions.is_fresh(session.token):
            handler.write_json(
                HTTPStatus.FORBIDDEN,
                error_payload("fresh_auth_required", "re-authentication is required", request_id),
            )
            return True
        payload = handler.read_json_body()
        if set(payload) != {
            "phase_id", "reason", "idempotency_key", "execution_timeout_seconds",
        }:
            raise KubernetesChangeExecutionError("invalid_request", "Exact execution start fields are required")
        execution = executions.start(
            change_request_id,
            phase_id=payload["phase_id"], actor_id=session.actor.actor_id,
            reason=payload["reason"], idempotency_key=payload["idempotency_key"],
            request_id=request_id,
            execution_timeout_seconds=payload["execution_timeout_seconds"],
        )
        handler.write_json(
            HTTPStatus.OK if execution["idempotent"] else HTTPStatus.CREATED,
            {"request_id": request_id, "phase_execution": execution},
        )
    except (ChangeRequestError, KubernetesChangeExecutionError) as exc:
        code = exc.code
        message = exc.message
        status = {
            "not_found": HTTPStatus.NOT_FOUND,
            "phase_stale": HTTPStatus.CONFLICT,
            "phase_expired": HTTPStatus.CONFLICT,
            "execution_exists": HTTPStatus.CONFLICT,
            "cluster_not_ready": HTTPStatus.CONFLICT,
            "idempotency_conflict": HTTPStatus.CONFLICT,
            "approval_actor_mismatch": HTTPStatus.FORBIDDEN,
        }.get(code, HTTPStatus.BAD_REQUEST)
        handler.write_json(status, error_payload(code, message, request_id))
    except (TypeError, ValueError) as exc:
        handler.write_json(
            HTTPStatus.BAD_REQUEST,
            error_payload("invalid_request", str(exc), request_id),
        )
    return True


def _route(path: str) -> tuple[str, bool] | None:
    prefix = "/api/v1/change-requests/"
    start_suffix = "/phase-execution/start"
    read_suffix = "/phase-execution"
    if not path.startswith(prefix):
        return None
    if path.endswith(start_suffix):
        suffix, starting = start_suffix, True
    elif path.endswith(read_suffix):
        suffix, starting = read_suffix, False
    else:
        return None
    change_request_id = unquote(path[len(prefix):-len(suffix)]).strip("/")
    return (change_request_id, starting) if change_request_id and "/" not in change_request_id else None
