"""Public Gateway adapter for MCP Integration administration."""

from __future__ import annotations

import json
from http import HTTPStatus
from urllib.parse import unquote

from apps.internal_auth import enforce_internal_auth

from .mcp_registry import MCPRegistry, MCPRegistryError


def dispatch(
    handler,
    path: str,
    registry: MCPRegistry,
    authorize_admin,
    require_fresh,
    request_id_fn,
    error_payload,
) -> bool:
    internal_tool = _internal_tool(path)
    if internal_tool is not None:
        return _dispatch_tool(handler, internal_tool, registry, request_id_fn, error_payload)
    route = _route(path)
    if route is None:
        return False
    kind, integration_id = route
    request_id = request_id_fn(handler)
    action = (
        "mcp_integration_verify" if kind == "verify"
        else "mcp_integration_create" if integration_id is None
        else "mcp_integration_update"
    )
    audit_target = ("mcp_integration", integration_id, action)
    session = authorize_admin(handler, request_id, audit_target=audit_target)
    if session is None:
        return True
    if handler.command == "GET":
        try:
            if integration_id is None:
                handler.write_json(
                    HTTPStatus.OK,
                    {"request_id": request_id, "mcp_integrations": registry.list()},
                )
            else:
                handler.write_json(
                    HTTPStatus.OK,
                    {"request_id": request_id, "mcp_integration": registry.get(integration_id)},
                )
        except MCPRegistryError as exc:
            _write_error(handler, exc, request_id, error_payload)
        return True
    try:
        payload = handler.read_json_body()
        if not isinstance(payload, dict):
            raise MCPRegistryError("invalid_request", "MCP Integration request must be an object")
        reason = payload.pop("reason", "")
        if not isinstance(reason, str) or not reason.strip():
            raise MCPRegistryError("reason_required", "reason is required")
        if not require_fresh(
            handler, session, request_id, audit_target=audit_target, reason=reason.strip(),
        ):
            return True
        if kind == "verify":
            if payload:
                raise MCPRegistryError("invalid_request", "MCP verification fields are invalid")
            result = registry.verify(
                integration_id or "", actor_id=session.actor.actor_id,
                reason=reason, request_id=request_id,
            )
            status = HTTPStatus.OK
        elif integration_id is None and handler.command == "POST":
            _fields(payload, {"name", "endpoint", "capabilities", "allowed_scope", "enabled"}, {"credential"})
            credential = payload.pop("credential", None)
            result = registry.create(
                **payload, credential=credential,
                actor_id=session.actor.actor_id, reason=reason, request_id=request_id,
            )
            status = HTTPStatus.CREATED
        elif integration_id is not None and handler.command == "PATCH":
            _fields(
                payload,
                {"name", "endpoint", "capabilities", "allowed_scope", "enabled", "expected_revision"},
                {"credential"},
            )
            credential = payload.pop("credential", None)
            result = registry.update(
                integration_id, **payload, credential=credential,
                actor_id=session.actor.actor_id, reason=reason, request_id=request_id,
            )
            status = HTTPStatus.OK
        else:
            return False
    except (TypeError, ValueError, json.JSONDecodeError, MCPRegistryError) as exc:
        error_value = exc if isinstance(exc, MCPRegistryError) else MCPRegistryError("invalid_request", str(exc))
        _write_error(handler, error_value, request_id, error_payload)
        return True
    handler.write_json(status, {"request_id": request_id, "mcp_integration": result})
    return True


def _dispatch_tool(handler, tool: str, registry: MCPRegistry, request_id_fn, error_payload) -> bool:
    request_id = request_id_fn(handler)
    actor_id = enforce_internal_auth(
        handler, service_name="gateway", allowed_service_account="aiops-diagnosis",
    )
    if actor_id is None:
        return True
    try:
        payload = handler.read_json_body()
        if handler.command != "POST" or set(payload) != {"integration_id", "integration_revision", "arguments"}:
            raise MCPRegistryError("invalid_request", "MCP tool request fields are invalid")
        arguments = payload["arguments"]
        if not isinstance(arguments, dict) or arguments.get("request_id") != request_id:
            raise MCPRegistryError("invalid_request", "MCP tool request_id does not match")
        result = registry.invoke(
            tool, arguments, integration_id=str(payload["integration_id"]),
            integration_revision=str(payload["integration_revision"]), actor_id=actor_id,
            request_id=request_id,
        )
    except (TypeError, ValueError, json.JSONDecodeError, MCPRegistryError) as exc:
        error_value = exc if isinstance(exc, MCPRegistryError) else MCPRegistryError("invalid_request", str(exc))
        _write_error(handler, error_value, request_id, error_payload)
        return True
    handler.write_json(HTTPStatus.OK, result)
    return True


def _route(path: str) -> tuple[str, str | None] | None:
    prefix = "/api/v1/admin/mcp-integrations"
    if path == prefix:
        return "collection", None
    if not path.startswith(prefix + "/"):
        return None
    parts = [unquote(part) for part in path[len(prefix) + 1:].split("/") if part]
    if len(parts) == 1:
        return "detail", parts[0]
    if len(parts) == 2 and parts[1] == "verify":
        return "verify", parts[0]
    return None


def _internal_tool(path: str) -> str | None:
    prefix = "/api/v1/internal/mcp-tools/"
    if not path.startswith(prefix):
        return None
    tool = unquote(path[len(prefix):])
    return tool if tool and "/" not in tool else None


def _fields(payload: dict[str, object], required: set[str], optional: set[str]) -> None:
    if not required <= set(payload) <= required | optional:
        raise MCPRegistryError("invalid_request", "MCP Integration request fields are invalid")


def _write_error(handler, exc: MCPRegistryError, request_id: str, error_payload) -> None:
    status = (
        HTTPStatus.NOT_FOUND if exc.code == "integration_not_found"
        else HTTPStatus.CONFLICT if exc.code in {"revision_conflict", "integration_conflict"}
        else HTTPStatus.FORBIDDEN if exc.code == "capability_denied"
        else HTTPStatus.SERVICE_UNAVAILABLE if exc.code in {"encryption_failed", "integration_unavailable"}
        else HTTPStatus.BAD_REQUEST
    )
    handler.write_json(status, error_payload(exc.code, str(exc), request_id))
