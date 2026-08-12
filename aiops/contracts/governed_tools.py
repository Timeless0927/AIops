"""AIOps-approved read-only tool capability contract."""

from __future__ import annotations


_REGISTRY_CAPABILITIES = {
    "query_metrics": ("prometheus-query-v1", "/query_metrics"),
    "query_logs": ("loki-query-v1", "/query_logs"),
    "get_service_topology": ("topology-query-v1", "/get_service_topology"),
}
_NATIVE_CAPABILITIES = {
    "run_k8s_read": {"version": "gateway-k8s-read-v1", "owner": "gateway"},
}


def default_capability_snapshot() -> dict[str, dict[str, object]]:
    snapshot = {
        name: {
            "name": name,
            "version": version,
            "enabled": True,
            "read_only": True,
            "mutation": False,
        }
        for name, (version, _path) in _REGISTRY_CAPABILITIES.items()
    }
    snapshot.update(native_capability_snapshot())
    return snapshot


def native_capability_snapshot() -> dict[str, dict[str, object]]:
    return {
        name: {
            "name": name,
            "version": capability["version"],
            "enabled": True,
            "read_only": True,
            "mutation": False,
            "owner": capability["owner"],
        }
        for name, capability in _NATIVE_CAPABILITIES.items()
    }


def approved_capability(name: str) -> dict[str, object] | None:
    configured = _REGISTRY_CAPABILITIES.get(name)
    if configured is None:
        return None
    version, path = configured
    return {
        "name": name, "version": version, "read_only": True,
        "mutation": False, "path": path,
    }


def capability_denial(tool: str, snapshot: object) -> str | None:
    expected = default_capability_snapshot().get(tool)
    if expected is None:
        return "unknown or mutation-like tool is not allowed"
    actual = snapshot.get(tool) if isinstance(snapshot, dict) else None
    if not isinstance(actual, dict):
        return "tool capability is not enabled"
    if actual.get("name") != tool or actual.get("version") != expected["version"]:
        return "tool capability snapshot changed"
    if actual.get("enabled") is not True:
        return "tool capability is disabled"
    if actual.get("read_only") is not True or actual.get("mutation") is not False:
        return "tool capability is not verified read-only"
    owner = _NATIVE_CAPABILITIES.get(tool, {}).get("owner")
    if owner is not None and actual.get("owner") != owner:
        return "tool capability owner changed"
    return None


def capability_binding(tool: str, snapshot: object) -> tuple[dict[str, str] | None, str | None]:
    denied = capability_denial(tool, snapshot)
    actual = snapshot.get(tool) if isinstance(snapshot, dict) else None
    if denied is not None:
        return None, denied
    assert isinstance(actual, dict)
    if tool in _NATIVE_CAPABILITIES and actual.get("owner") == _NATIVE_CAPABILITIES[tool]["owner"]:
        return None, None
    integration_id = actual.get("integration_id")
    revision = actual.get("integration_revision")
    if not isinstance(integration_id, str) or not integration_id or not isinstance(revision, str) or not revision:
        return None, "tool capability registry binding is missing"
    return {"integration_id": integration_id, "integration_revision": revision}, None
