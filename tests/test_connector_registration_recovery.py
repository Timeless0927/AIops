"""Connector outbound registration and heartbeat tests."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest

from apps.cluster_connector import gateway_client
from apps.cluster_connector import main as connector_main
from apps.cluster_connector.stream_client import ConnectorRegistration
from runtime import service_image_smoke


class _Response:
    def __init__(self, *, status: int = 200, payload: dict[str, Any] | None = None) -> None:
        self.status = status
        self._payload = payload or {}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _registration() -> ConnectorRegistration:
    return ConnectorRegistration(
        connector_id="connector-local",
        cluster_id="cluster-local",
        namespace_scope=("default",),
        capabilities=("health", "validate"),
    )


def test_sync_gateway_registration_authenticates_and_heartbeats(monkeypatch) -> None:
    requests: list[tuple[str, str, str, dict]] = []

    def fake_urlopen(request, timeout: int = 0):  # noqa: ANN001, ARG001
        requests.append(
            (
                request.get_method(),
                request.full_url,
                request.headers["Authorization"],
                json.loads(request.data),
            )
        )
        return _Response(status=201 if request.full_url.endswith("/register") else 200)

    monkeypatch.setattr(gateway_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        gateway_client,
        "discover_candidates",
        lambda registration: [
            {
                "namespace": "default",
                "workload_kind": "Deployment",
                "workload_name": "checkout-api",
                "service_name": "checkout",
                "service_hint": "checkout",
                "team_hint": "payments",
            }
        ],
    )

    assert gateway_client.sync_gateway_registration(
        "https://gateway.example.com", _registration(), "connector-secret"
    ) is True
    assert requests == [
        (
            "POST",
                "https://gateway.example.com/api/v1/connectors/register",
            "Bearer connector-secret",
            {
                "connector_id": "connector-local",
                "cluster_id": "cluster-local",
                "namespace_scope": ["default"],
                "capabilities": ["health", "validate"],
            },
        ),
        (
            "POST",
                "https://gateway.example.com/api/v1/connectors/heartbeat",
            "Bearer connector-secret",
            {
                "connector_id": "connector-local",
                "cluster_id": "cluster-local",
                "status": "online",
            },
        ),
        (
            "POST",
                "https://gateway.example.com/api/v1/connectors/discovery-candidates",
            "Bearer connector-secret",
            {
                "connector_id": "connector-local",
                "cluster_id": "cluster-local",
                "candidates": [
                    {
                        "namespace": "default",
                        "workload_kind": "Deployment",
                        "workload_name": "checkout-api",
                        "service_name": "checkout",
                        "service_hint": "checkout",
                        "team_hint": "payments",
                    }
                ],
            },
        ),
    ]


def test_sync_gateway_registration_fails_closed_without_credential(monkeypatch) -> None:
    called = False

    def fake_urlopen(request, timeout: int = 0):  # noqa: ANN001, ARG001
        nonlocal called
        called = True
        return _Response()

    monkeypatch.setattr(gateway_client.urllib.request, "urlopen", fake_urlopen)

    assert gateway_client.sync_gateway_registration("http://gateway:8080", _registration(), "") is False
    assert gateway_client.sync_gateway_registration("http://gateway:8080", _registration(), "credential") is False
    assert called is False


def test_sync_gateway_registration_reports_unregistered_when_gateway_unavailable(monkeypatch) -> None:
    def fake_urlopen(request, timeout: int = 0):  # noqa: ANN001, ARG001
        raise OSError("gateway unavailable")

    monkeypatch.setattr(gateway_client.urllib.request, "urlopen", fake_urlopen)

    assert gateway_client.sync_gateway_registration(
        "https://gateway.example.com", _registration(), "connector-secret"
    ) is False


def test_registration_loop_sends_periodic_heartbeat(monkeypatch) -> None:
    stop = threading.Event()
    calls = 0

    def fake_sync(*_args, **_kwargs) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            stop.set()
        return True

    monkeypatch.setattr(connector_main, "sync_gateway_registration", fake_sync)

    connector_main._registration_loop("http://gateway:8080", _registration(), "credential", 0, stop)

    assert calls == 2


def test_connector_image_smoke_runs_public_entrypoint() -> None:
    service_image_smoke.assert_connector_entrypoint()


def test_discovery_is_batched_for_large_clusters(monkeypatch) -> None:
    requests: list[dict] = []

    def fake_urlopen(request, timeout: int = 0):  # noqa: ANN001, ARG001
        requests.append(json.loads(request.data))
        return _Response()

    monkeypatch.setattr(gateway_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        gateway_client,
        "discover_candidates",
        lambda registration: [
            {
                "namespace": "default",
                "workload_kind": "Deployment",
                "workload_name": f"workload-{index}",
                "service_name": None,
                "service_hint": None,
                "team_hint": None,
            }
            for index in range(1001)
        ],
    )

    assert gateway_client.sync_gateway_registration(
        "https://gateway.example.com", _registration(), "connector-secret"
    ) is True
    discovery_batches = [request["candidates"] for request in requests if "candidates" in request]
    assert [len(batch) for batch in discovery_batches] == [1000, 1]


def test_connector_http_surface_does_not_execute_commands() -> None:
    connector_main.ConnectorHandler.registration = _registration()
    server = ThreadingHTTPServer(("127.0.0.1", 0), connector_main.ConnectorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/commands/execute",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("Connector command execution must not be exposed over HTTP")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_connector_readiness_requires_command_polling_not_optional_enrollment(monkeypatch) -> None:
    connector_main.ConnectorHandler.registration = _registration()
    connector_main.ConnectorHandler.gateway_url = "http://aiops-gateway:8080"
    connector_main.ConnectorHandler.registered_with_gateway = False
    connector_main.ConnectorHandler.command_thread = SimpleNamespace(is_alive=lambda: True)
    monkeypatch.setattr(
        connector_main,
        "sync_gateway_registration",
        lambda *_args, **_kwargs: pytest.fail("readiness must not perform registration I/O"),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), connector_main.ConnectorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_address[1]}/readyz", timeout=3
        ) as response:
            assert response.status == 200
            assert json.load(response) == {
                "service": "cluster-connector",
                "status": "ok",
                "registered_with_gateway": False,
                "command_polling": True,
            }
        connector_main.ConnectorHandler.command_thread = SimpleNamespace(is_alive=lambda: False)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/readyz", timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
            assert json.load(exc) == {
                "service": "cluster-connector",
                "status": "not_ready",
                "registered_with_gateway": False,
                "command_polling": False,
            }
        else:
            raise AssertionError("Connector without command polling must not be Ready")
    finally:
        connector_main.ConnectorHandler.command_thread = None
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
