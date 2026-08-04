"""Freeze actor-visible Resource Catalog state for one environment Chat request."""

from __future__ import annotations

import hashlib
import json
from typing import Any


JSON = dict[str, object]
_FIELDS = {"cluster_id", "namespace", "service_id", "deployment_target_id", "incident_id"}
_MAX_RESOURCES = 200


class ChatScopeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def freeze_chat_scope(
    selection: object,
    *,
    workspace: JSON,
    incident: JSON | None = None,
) -> JSON:
    """Return an immutable, bounded subset of an already authorized projection."""
    selected = _selection(selection)
    services = {
        str(item.get("id")): str(item.get("name") or item.get("id"))
        for item in workspace.get("services", [])
        if isinstance(item, dict) and item.get("id")
    }
    if "incident_id" in selected:
        resources = [_incident_resource(selected, incident)]
    else:
        resources = [
            projected
            for item in workspace.get("resources", [])
            if isinstance(item, dict)
            and item.get("binding_state") == "bound"
            and (projected := _resource(item, services)) is not None
            and _matches(selected, projected)
        ]
    if not resources:
        raise ChatScopeError("chat_scope_not_found", "Chat resource scope not found")
    if len(resources) > _MAX_RESOURCES:
        # ponytail: one bounded snapshot; require a narrower selector if real catalogs exceed it.
        raise ChatScopeError("chat_scope_too_broad", "Chat resource scope must be narrowed")
    resources.sort(key=lambda item: str(item["deployment_target_id"]))
    frozen: JSON = {
        "selection": selected,
        "resources": resources,
        "time_range": {"type": "relative", "value": "30m"},
    }
    frozen["revision"] = hashlib.sha256(
        json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return frozen


def _selection(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value or set(value) - _FIELDS:
        raise ChatScopeError("invalid_scope", "Chat resource scope is invalid")
    selected = {
        str(key): str(item).strip()
        for key, item in value.items()
        if isinstance(item, str) and item.strip() and len(item.strip()) <= 200
    }
    if set(selected) != set(value):
        raise ChatScopeError("invalid_scope", "Chat resource scope is invalid")
    return selected


def _resource(item: dict[str, Any], services: dict[str, str]) -> JSON | None:
    required = ("id", "cluster_id", "namespace", "kind", "name", "service_id")
    if any(not isinstance(item.get(field), str) or not str(item[field]).strip() for field in required):
        return None
    service_id = str(item["service_id"])
    return {
        "deployment_target_id": str(item["id"]),
        "cluster_id": str(item["cluster_id"]),
        "namespace": str(item["namespace"]),
        "service_id": service_id,
        "service_name": services.get(service_id, service_id),
        "workload_kind": str(item["kind"]),
        "workload_name": str(item["name"]),
    }


def _matches(selected: dict[str, str], resource: JSON) -> bool:
    return all(
        field == "incident_id" or resource.get(field) == expected
        for field, expected in selected.items()
    )


def _incident_resource(selected: dict[str, str], incident: JSON | None) -> JSON:
    incident_data = incident.get("incident") if isinstance(incident, dict) else None
    context = incident.get("resource_context") if isinstance(incident, dict) else None
    if (
        not isinstance(incident_data, dict)
        or incident_data.get("id") != selected["incident_id"]
        or not isinstance(context, dict)
        or not context.get("resource_binding_id")
    ):
        raise ChatScopeError("chat_scope_not_found", "Chat resource scope not found")
    values = {
        "deployment_target_id": context.get("deployment_target_id"),
        "cluster_id": context.get("cluster_id"),
        "namespace": context.get("namespace"),
        "service_id": context.get("service_id"),
        "service_name": context.get("service_name") or context.get("service_id"),
        "workload_kind": context.get("workload_kind"),
        "workload_name": context.get("workload_name"),
    }
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise ChatScopeError("chat_scope_not_found", "Chat resource scope not found")
    resource: JSON = {key: str(value) for key, value in values.items()}
    if not _matches(selected, resource):
        raise ChatScopeError("chat_scope_not_found", "Chat resource scope not found")
    return resource
