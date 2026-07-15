"""Service-target import smoke for split Docker images."""

from __future__ import annotations

import importlib
import inspect
import os
import threading
from types import SimpleNamespace
from unittest.mock import patch


_SERVICE_IMPORTS = {
    "gateway": (
        "apps.aiops_k8s_gateway.main",
        "apps.aiops_k8s_gateway.alertmanager_webhook",
        "apps.service_http",
    ),
    "diagnosis": (
        "diagnosis_service.service_main",
        "runtime.service_mesh_smoke",
    ),
    "notification": (
        "notification_service.service_main",
        "notification_service.requests",
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
    "verification": ("verification_service.__main__",),
}


def assert_service_image(service: str) -> None:
    """Run the checks required by one split service image."""
    modules = _SERVICE_IMPORTS.get(service)
    if not modules:
        raise RuntimeError(f"unknown service image smoke target: {service}")
    for module_name in modules:
        importlib.import_module(module_name)
    if service == "connectors":
        assert_connector_entrypoint()


def assert_connector_entrypoint() -> None:
    """Start the public Connector entry point without external I/O."""
    from apps.cluster_connector import command_worker
    from apps.cluster_connector import main as connector_main

    calls = 0
    command_stop: threading.Event | None = None
    signature = inspect.signature(command_worker.run_command_cycle)

    class Journal:
        def cleanup_expired(self) -> None:
            pass

    class Thread:
        def __init__(self, *, target, args, daemon, name) -> None:
            nonlocal command_stop
            self.target = target
            self.args = args
            self.name = name
            self.started = False
            if name == "connector-command-poll":
                command_stop = args[-1]

        def start(self) -> None:
            self.started = True
            if self.name == "connector-command-poll":
                self.target(*self.args)

        def is_alive(self) -> bool:
            return self.started

    def run_cycle(*args, **kwargs) -> bool:
        nonlocal calls
        signature.bind(*args, **kwargs)
        calls += 1
        if command_stop is None:
            raise RuntimeError("Connector command stop event was not wired")
        command_stop.set()
        return True

    parser = SimpleNamespace(parse_args=lambda: SimpleNamespace(host="0.0.0.0", port=8081))
    with (
        patch.object(connector_main, "_build_parser", return_value=parser),
        patch.object(connector_main, "sync_gateway_registration", return_value=True),
        patch.object(connector_main, "ConnectorCommandJournal", side_effect=lambda _path: Journal()),
        patch.object(connector_main, "run_command_cycle", side_effect=run_cycle),
        patch.object(connector_main.threading, "Thread", Thread),
        patch.object(connector_main, "serve"),
    ):
        connector_main.main()

    thread = connector_main.ConnectorHandler.command_thread
    if calls != 1 or thread is None or not thread.is_alive():
        raise RuntimeError("Connector command polling did not start")


def main() -> None:
    service = os.getenv("SERVICE_NAME", "").strip()
    assert_service_image(service)
    print(f"AIOps service image smoke passed: {service}")


if __name__ == "__main__":
    main()
