"""Public Gateway HTTP/SSE adapter for private Chat Sessions."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from typing import Any, Callable
from urllib import error, request
from urllib.parse import parse_qs, unquote, urlparse

from apps.internal_auth import internal_auth_headers

from .chat_handoffs import ChatHandoffError, ChatHandoffs
from .chat_scope import ChatScopeError, freeze_chat_scope
from .chat_sessions import ChatError, ChatSessions


def dispatch(
    handler: Any,
    route_path: str,
    chats: ChatSessions,
    handoffs: ChatHandoffs,
    mcp_registry: Any,
    sessions: Any,
    catalog: Any,
    incidents: Any,
    connector_status: Callable[[], list[dict[str, object]]],
    request_session: Callable[[Any], tuple[Any, str | None]],
    csrf_valid: Callable[[Any, str], bool],
    request_id_for: Callable[[Any], str],
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> bool:
    route = _route(route_path)
    if route is None:
        return False
    request_id = request_id_for(handler)
    session, auth_mode = request_session(handler)
    if session is None:
        handler.write_json(HTTPStatus.UNAUTHORIZED, error_payload("unauthorized", "authentication required", request_id))
        return True
    if handler.command == "POST" and auth_mode == "cookie" and not csrf_valid(handler, session.token):
        handler.write_json(HTTPStatus.FORBIDDEN, error_payload("csrf_required", "missing or invalid CSRF token", request_id))
        return True
    owner_id = session.actor.actor_id
    kind, session_id, message_id = route

    def respond(chat_request: dict[str, object]) -> dict[str, object]:
        payload = dict(chat_request)
        if payload.get("scope") is not None:
            payload["capabilities"] = mcp_registry.authorized_snapshot(
                payload["scope"], actor_id=owner_id,
                request_id=str(payload.get("request_id") or request_id),
            )
        return send_governed_chat(payload)

    def freeze_for_actor(selection: object, *, report_unbound: bool = False) -> dict[str, object]:
        actor = sessions.actor_view(session.actor)
        if "view_incident" not in actor["capabilities"]:
            raise ChatScopeError("chat_scope_not_found", "Chat resource scope not found")
        team_ids = None if actor["is_platform_administrator"] else incidents.team_ids_for_actor(owner_id)
        workspace = catalog.list_for_actor(team_ids=team_ids, connector_status=connector_status())
        if report_unbound and isinstance(selection, dict):
            target_id = selection.get("deployment_target_id")
            for resource in workspace.get("resources", []):
                if isinstance(resource, dict) and resource.get("id") == target_id and resource.get("binding_state") != "bound":
                    raise ChatHandoffError("resource_not_bound", "Deployment Target is not bound to a Service")
        incident = None
        if isinstance(selection, dict) and isinstance(selection.get("incident_id"), str):
            incident = incidents.workbench(
                selection["incident_id"], team_ids=team_ids,
                actor_capabilities=list(actor["capabilities"]),
            )
        return freeze_chat_scope(selection, workspace=workspace, incident=incident)

    try:
        if handler.command == "GET" and kind == "collection":
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, "chat_sessions": chats.list(owner_id)})
        elif handler.command == "GET" and kind == "session":
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, "chat_session": chats.get(owner_id, session_id or "")})
        elif handler.command == "GET" and kind in {"events", "stream"}:
            after, limit = _pagination(handler)
            if kind == "stream":
                _stream(handler, chats, owner_id, session_id or "", after)
            else:
                handler.write_json(
                    HTTPStatus.OK,
                    {"request_id": request_id, **chats.list_events(owner_id, session_id or "", after=after, limit=limit)},
                )
        elif handler.command == "POST" and kind == "collection":
            payload = handler.read_json_body()
            _only(payload, {"idempotency_key"})
            chat_session = chats.create(owner_id, idempotency_key=_text(payload, "idempotency_key", 200))
            handler.write_json(HTTPStatus.CREATED, {"request_id": request_id, "chat_session": chat_session})
        elif handler.command == "POST" and kind == "messages":
            payload = handler.read_json_body()
            _fields(payload, {"content", "idempotency_key"}, {"scope"})
            frozen_scope = freeze_for_actor(payload["scope"]) if "scope" in payload else None
            chat_session = chats.send(
                owner_id,
                session_id or "",
                content=_text(payload, "content", 8_000),
                idempotency_key=_text(payload, "idempotency_key", 200),
                scope=frozen_scope,
                respond=respond,
            )
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, "chat_session": chat_session})
        elif handler.command == "POST" and kind == "retry":
            payload = handler.read_json_body()
            _only(payload, set())
            chat_session = chats.retry(
                owner_id, session_id or "", message_id or "", respond=respond,
                refreeze_scope=freeze_for_actor,
            )
            handler.write_json(HTTPStatus.OK, {"request_id": request_id, "chat_session": chat_session})
        elif handler.command == "POST" and kind == "handoffs":
            payload = handler.read_json_body()
            _only(payload, {"message_ids", "idempotency_key", "target"})
            actor = sessions.actor_view(session.actor)
            if "manage_investigation" not in actor["capabilities"]:
                raise ChatHandoffError("forbidden", "manage_investigation capability is required")
            team_ids = None if actor["is_platform_administrator"] else incidents.team_ids_for_actor(owner_id)
            target = payload.get("target")
            if not isinstance(target, dict):
                raise ChatHandoffError("invalid_handoff", "Handoff target is required")
            if target.get("type") == "existing_incident" and set(target) == {"type", "incident_id"}:
                handoff = handoffs.execute(
                    actor_id=owner_id, session_id=session_id or "", message_ids=payload["message_ids"],
                    idempotency_key=_text(payload, "idempotency_key", 200), team_ids=team_ids,
                    target_incident_id=target.get("incident_id"),
                )
            elif target.get("type") == "user_created_incident" and set(target) == {"type", "problem_summary", "scope"}:
                handoff = handoffs.execute(
                    actor_id=owner_id, session_id=session_id or "", message_ids=payload["message_ids"],
                    idempotency_key=_text(payload, "idempotency_key", 200), team_ids=team_ids,
                    problem_summary=target.get("problem_summary"),
                    frozen_scope=freeze_for_actor(target.get("scope"), report_unbound=True),
                )
            else:
                raise ChatHandoffError("invalid_handoff", "Handoff target is invalid")
            handler.write_json(
                HTTPStatus.OK if handoff["idempotent"] else HTTPStatus.CREATED,
                {"request_id": request_id, "handoff": handoff},
            )
        else:
            return False
    except (TypeError, ValueError, json.JSONDecodeError, ChatError, ChatScopeError, ChatHandoffError) as exc:
        code = str(getattr(exc, "code", "invalid_request"))
        status = {
            "chat_not_found": HTTPStatus.NOT_FOUND,
            "chat_scope_not_found": HTTPStatus.NOT_FOUND,
            "chat_scope_changed": HTTPStatus.CONFLICT,
            "chat_message_not_found": HTTPStatus.NOT_FOUND,
            "handoff_target_not_found": HTTPStatus.NOT_FOUND,
            "idempotency_conflict": HTTPStatus.CONFLICT,
            "investigation_terminal": HTTPStatus.CONFLICT,
            "resource_not_bound": HTTPStatus.CONFLICT,
            "forbidden": HTTPStatus.FORBIDDEN,
            "handoff_too_large": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            "message_not_retryable": HTTPStatus.CONFLICT,
            "message_conflict": HTTPStatus.CONFLICT,
        }.get(code, HTTPStatus.BAD_REQUEST)
        handler.write_json(status, error_payload(code, str(exc), request_id))
    return True


def send_governed_chat(chat_request: dict[str, object]) -> dict[str, object]:
    """Call the internal Diagnosis Chat endpoint without exposing it publicly."""
    base_url = os.getenv("AIOPS_DIAGNOSIS_URL", "").strip()
    if not base_url:
        raise ChatError("model_unavailable", "Diagnosis is not configured")
    payload = dict(chat_request)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json", **internal_auth_headers()}
    outbound = request.Request(f"{base_url.rstrip('/')}/chat", data=body, headers=headers, method="POST")
    try:
        with request.urlopen(outbound, timeout=15) as response:
            payload = json.loads(response.read().decode() or "{}")
    except (OSError, TimeoutError, error.URLError, ValueError, json.JSONDecodeError) as exc:
        raise ChatError("model_unavailable", "Chat model is unavailable") from exc
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise ChatError("invalid_model_response", "Chat model returned an invalid result")
    return result


def _stream(handler: Any, chats: ChatSessions, owner_id: str, session_id: str, after: int) -> None:
    page = chats.list_events(owner_id, session_id, after=after, limit=200)
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    cursor = after
    while True:
        for event in page["events"]:  # type: ignore[union-attr]
            cursor = int(event["id"])
            data = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handler.wfile.write(f"id: {cursor}\nevent: chat\ndata: {data}\n\n".encode())
        if not page["has_more"]:
            break
        page = chats.list_events(owner_id, session_id, after=cursor, limit=200)
    handler.wfile.write(b": reconnect\n\n")
    handler.wfile.flush()


def _route(path: str) -> tuple[str, str | None, str | None] | None:
    prefix = "/api/v1/chat/sessions"
    if path == prefix:
        return "collection", None, None
    if not path.startswith(prefix + "/"):
        return None
    parts = [unquote(part) for part in path[len(prefix) + 1 :].split("/") if part]
    if len(parts) == 1:
        return "session", parts[0], None
    if len(parts) == 2 and parts[1] in {"messages", "events", "handoffs"}:
        return parts[1], parts[0], None
    if len(parts) == 3 and parts[1:] == ["events", "stream"]:
        return "stream", parts[0], None
    if len(parts) == 4 and parts[1] == "messages" and parts[3] == "retry":
        return "retry", parts[0], parts[2]
    return None


def _pagination(handler: Any) -> tuple[int, int]:
    query = parse_qs(urlparse(handler.path).query)
    after = max(_integer(query.get("after", ["0"])[0], "after", 0), _integer(handler.headers.get("Last-Event-ID", "0"), "Last-Event-ID", 0))
    return after, _integer(query.get("limit", ["100"])[0], "limit", 1, 200)


def _integer(value: object, field: str, minimum: int, maximum: int | None = None) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ChatError("invalid_pagination", f"{field} must be an integer") from exc
    if parsed < minimum or (maximum is not None and parsed > maximum):
        raise ChatError("invalid_pagination", f"{field} is outside the supported range")
    return parsed


def _text(payload: dict[str, object], field: str, maximum: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ChatError("invalid_request", f"{field} is required")
    return value.strip()


def _only(payload: dict[str, object], fields: set[str]) -> None:
    if set(payload) != fields:
        raise ChatError("invalid_request", "Chat request fields are invalid")


def _fields(payload: dict[str, object], required: set[str], optional: set[str]) -> None:
    if not required <= set(payload) <= required | optional:
        raise ChatError("invalid_request", "Chat request fields are invalid")
