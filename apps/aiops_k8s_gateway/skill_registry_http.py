"""Public Gateway adapter for versioned Skill administration."""

from __future__ import annotations

from dataclasses import dataclass
import json
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

from .mcp_registry import MCPRegistry
from .skill_registry import SkillRegistry, SkillRegistryError


@dataclass(frozen=True)
class SkillRegistryHTTPAdapter:
    registry: SkillRegistry
    mcp_registry: MCPRegistry
    authorize_admin: Any
    require_fresh: Any
    request_id_for: Any
    error_payload: Any

    def dispatch(self, handler: Any, route_path: str) -> bool:
        path = route_path
        registry = self.registry
        mcp_registry = self.mcp_registry
        authorize_admin = self.authorize_admin
        require_fresh = self.require_fresh
        request_id_fn = self.request_id_for
        error_payload = self.error_payload
        route = _route(path)
        if route is None:
            return False
        kind, skill_id = route
        request_id = request_id_fn(handler)
        action = {
            "collection": "skill_create",
            "detail": "skill_view",
            "versions": "skill_version_create",
            "enable": "skill_enable",
            "disable": "skill_disable",
        }[kind]
        session = authorize_admin(
            handler,
            request_id,
            audit_target=("skill", skill_id, action),
        )
        if session is None:
            return True
        try:
            integrations = mcp_registry.list()
            if handler.command == "GET":
                if kind == "collection":
                    handler.write_json(
                        HTTPStatus.OK,
                        {"request_id": request_id, "skills": registry.list(integrations)},
                    )
                elif kind == "detail":
                    handler.write_json(
                        HTTPStatus.OK,
                        {"request_id": request_id, "skill": registry.get(skill_id or "", integrations)},
                    )
                else:
                    return False
                return True
            payload = handler.read_json_body()
            if not isinstance(payload, dict):
                raise SkillRegistryError("invalid_request", "Skill request must be an object")
            reason = payload.pop("reason", "")
            if not isinstance(reason, str) or not reason.strip():
                raise SkillRegistryError("reason_required", "reason is required")
            if not require_fresh(
                handler,
                session,
                request_id,
                audit_target=("skill", skill_id, action),
                reason=reason.strip(),
            ):
                return True
            if kind == "collection" and handler.command == "POST":
                _fields(payload, {"name", "instruction", "workflow", "applicable_scope", "required_mcp"})
                changed = registry.create(
                    **payload,
                    actor_id=session.actor.actor_id,
                    reason=reason,
                    request_id=request_id,
                )
                status = HTTPStatus.CREATED
            elif kind == "versions" and handler.command == "POST":
                _fields(payload, {"instruction", "workflow", "applicable_scope", "required_mcp"})
                changed = registry.create_version(
                    skill_id or "",
                    **payload,
                    actor_id=session.actor.actor_id,
                    reason=reason,
                    request_id=request_id,
                )
                status = HTTPStatus.CREATED
            elif kind == "enable" and handler.command == "POST":
                _fields(payload, {"version", "expected_active_version"})
                changed = registry.set_enabled(
                    skill_id or "",
                    version=payload["version"],
                    expected_active_version=payload["expected_active_version"],
                    mcp_integrations=integrations,
                    actor_id=session.actor.actor_id,
                    reason=reason,
                    request_id=request_id,
                )
                status = HTTPStatus.OK
            elif kind == "disable" and handler.command == "POST":
                _fields(payload, {"expected_active_version"})
                changed = registry.set_enabled(
                    skill_id or "",
                    version=None,
                    expected_active_version=payload["expected_active_version"],
                    mcp_integrations=integrations,
                    actor_id=session.actor.actor_id,
                    reason=reason,
                    request_id=request_id,
                )
                status = HTTPStatus.OK
            else:
                return False
            result = registry.get(str(changed["id"]), mcp_registry.list())
        except (TypeError, ValueError, json.JSONDecodeError, SkillRegistryError) as exc:
            error = exc if isinstance(exc, SkillRegistryError) else SkillRegistryError("invalid_request", str(exc))
            _write_error(handler, error, request_id, error_payload)
            return True
        handler.write_json(status, {"request_id": request_id, "skill": result})
        return True


def _route(path: str) -> tuple[str, str | None] | None:
    prefix = "/api/v1/admin/skills"
    if path == prefix:
        return "collection", None
    if not path.startswith(prefix + "/"):
        return None
    parts = [unquote(part) for part in path[len(prefix) + 1:].split("/") if part]
    if len(parts) == 1:
        return "detail", parts[0]
    if len(parts) == 2 and parts[1] in {"versions", "enable", "disable"}:
        return parts[1], parts[0]
    return None


def _fields(payload: dict[str, object], expected: set[str]) -> None:
    if set(payload) != expected:
        raise SkillRegistryError("invalid_request", "Skill request fields are invalid")


def _write_error(handler, exc: SkillRegistryError, request_id: str, error_payload) -> None:
    status = (
        HTTPStatus.NOT_FOUND if exc.code in {"skill_not_found", "version_not_found"}
        else HTTPStatus.CONFLICT if exc.code in {
            "skill_conflict", "version_conflict", "skill_dependency_unavailable",
        }
        else HTTPStatus.BAD_REQUEST
    )
    handler.write_json(status, error_payload(exc.code, str(exc), request_id))
