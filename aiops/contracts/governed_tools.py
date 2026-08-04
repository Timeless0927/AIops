"""Static read-only tool contract used before the dynamic MCP Registry exists."""

from __future__ import annotations


_VERSIONS = {
    "query_metrics": "prometheus-query-v1",
    "query_logs": "loki-query-v1",
    "run_k8s_read": "gateway-k8s-read-v1",
    "get_service_topology": "topology-query-v1",
}


def default_capability_snapshot() -> dict[str, dict[str, object]]:
    return {
        name: {
            "name": name,
            "version": version,
            "enabled": True,
            "read_only": True,
            "mutation": False,
        }
        for name, version in _VERSIONS.items()
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
    return None
