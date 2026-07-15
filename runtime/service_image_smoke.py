"""Service-target import smoke for split Docker images."""

from __future__ import annotations

import importlib
import inspect
import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer
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
    command_thread = None
    entered = threading.Event()
    release = threading.Event()
    real_thread = threading.Thread
    signature = inspect.signature(command_worker.run_command_cycle)

    class Journal:
        def cleanup_expired(self) -> None:
            pass

    class Thread:
        def __init__(
            self, group=None, target=None, name=None, args=(), kwargs=None, *, daemon=None
        ) -> None:
            nonlocal command_stop, command_thread
            self.thread = real_thread(
                group=group,
                target=target,
                name=name,
                args=args,
                kwargs=kwargs or {},
                daemon=daemon,
            )
            self.name = self.thread.name
            if self.name == "connector-command-poll":
                command_stop = args[-1]
                command_thread = self

        def start(self) -> None:
            self.thread.start()

        def is_alive(self) -> bool:
            return self.thread.is_alive()

        def join(self, timeout: float | None = None) -> None:
            self.thread.join(timeout)

    def run_cycle(*args, **kwargs) -> bool:
        nonlocal calls
        signature.bind(*args, **kwargs)
        calls += 1
        entered.set()
        release.wait(3)
        return True

    def probe_readiness(handler, **_kwargs) -> None:
        if not entered.wait(3):
            raise RuntimeError("Connector command polling did not start")
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server_thread = real_thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/readyz", timeout=3
            ) as response:
                payload = json.load(response)
                if response.status != 200 or payload.get("command_polling") is not True:
                    raise RuntimeError("Connector command polling was not Ready")
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=3)
            if command_stop is not None:
                command_stop.set()
            release.set()
            if command_thread is not None:
                command_thread.join(timeout=3)

    parser = SimpleNamespace(parse_args=lambda: SimpleNamespace(host="0.0.0.0", port=8081))
    with (
        patch.object(connector_main, "_build_parser", return_value=parser),
        patch.object(connector_main, "sync_gateway_registration", return_value=True),
        patch.object(connector_main, "_registration_loop", return_value=None),
        patch.object(connector_main, "ConnectorCommandJournal", side_effect=lambda _path: Journal()),
        patch.object(connector_main, "run_command_cycle", side_effect=run_cycle),
        patch.object(connector_main.threading, "Thread", Thread),
        patch.object(connector_main, "serve", side_effect=probe_readiness),
    ):
        connector_main.main()

    if calls != 1:
        raise RuntimeError("Connector command polling cycle did not run exactly once")


def main() -> None:
    service = os.getenv("SERVICE_NAME", "").strip()
    assert_service_image(service)
    print(f"AIOps service image smoke passed: {service}")


if __name__ == "__main__":
    main()
