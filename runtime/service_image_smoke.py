"""Service-target import smoke for split Docker images."""

from __future__ import annotations

import importlib
import os


_SERVICE_IMPORTS = {
    "gateway": (
        "apps.aiops_k8s_gateway.main",
        "apps.aiops_k8s_gateway.alertmanager_webhook",
        "apps.service_http",
    ),
    "hermes": (
        "hermes.service_main",
        "runtime.service_mesh_smoke",
    ),
    "connectors": (
        "apps.cluster_connector.main",
        "apps.cluster_connector.kubectl_executor",
    ),
    "mcp-prometheus": (
        "apps.mcp_prometheus.main",
        "apps.mcp_prometheus.facade",
        "toolsets.prometheus_query",
        "prometheus_api_client",
    ),
    "mcp-loki": (
        "apps.mcp_loki.main",
        "apps.mcp_loki.facade",
        "toolsets.loki_query",
        "httpx",
    ),
    "mcp-topology": (
        "apps.mcp_topology.main",
        "apps.mcp_topology.facade",
        "toolsets.topology_store",
    ),
}


def assert_service_imports(service: str) -> None:
    """Import the modules required by one split service image."""
    modules = _SERVICE_IMPORTS.get(service)
    if not modules:
        raise RuntimeError(f"unknown service image smoke target: {service}")
    for module_name in modules:
        importlib.import_module(module_name)


def main() -> None:
    service = os.getenv("SERVICE_NAME", "").strip()
    assert_service_imports(service)
    print(f"AIOps service image smoke passed: {service}")


if __name__ == "__main__":
    main()
