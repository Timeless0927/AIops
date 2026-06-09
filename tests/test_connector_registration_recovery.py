"""Connector registration recovery tests."""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from typing import Any

from apps.aiops_k8s_gateway import main as gateway_main
from apps.cluster_connector import main as connector_main
from apps.cluster_connector.stream_client import ConnectorRegistration


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


def test_sync_gateway_registration_restores_gateway_connectors_endpoint() -> None:
    gateway_main._ROUTES.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway_main.GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        assert connector_main._sync_gateway_registration(gateway_url, _registration()) is True
        assert gateway_main._ROUTES["connector-local"].cluster_id == "cluster-local"

        gateway_main._ROUTES.clear()
        assert connector_main._gateway_has_registration(gateway_url, "connector-local") is False
        assert connector_main._sync_gateway_registration(gateway_url, _registration()) is True
        assert gateway_main._ROUTES["connector-local"].connector_id == "connector-local"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_main._ROUTES.clear()


def test_sync_gateway_registration_keeps_existing_route(monkeypatch) -> None:
    requests: list[tuple[str, str]] = []

    def fake_urlopen(request, timeout: int = 0):  # noqa: ANN001
        requests.append((request.get_method(), request.full_url))
        return _Response(
            payload={
                "connectors": [
                    {
                        "connector_id": "connector-local",
                        "cluster_id": "cluster-local",
                        "session_id": "session-existing",
                    }
                ]
            }
        )

    monkeypatch.setattr(connector_main.urllib.request, "urlopen", fake_urlopen)

    assert connector_main._sync_gateway_registration("http://gateway:8080", _registration()) is True
    assert requests == [("GET", "http://gateway:8080/connectors")]


def test_sync_gateway_registration_recovers_after_gateway_restart(monkeypatch) -> None:
    requests: list[tuple[str, str]] = []

    def fake_urlopen(request, timeout: int = 0):  # noqa: ANN001
        requests.append((request.get_method(), request.full_url))
        if request.get_method() == "GET":
            return _Response(payload={"connectors": []})
        return _Response(status=201, payload={"status": "registered"})

    monkeypatch.setattr(connector_main.urllib.request, "urlopen", fake_urlopen)

    assert connector_main._sync_gateway_registration("http://gateway:8080", _registration()) is True
    assert requests == [
        ("GET", "http://gateway:8080/connectors"),
        ("POST", "http://gateway:8080/connectors/register"),
    ]


def test_sync_gateway_registration_reports_unregistered_when_gateway_unavailable(monkeypatch) -> None:
    def fake_urlopen(request, timeout: int = 0):  # noqa: ANN001, ARG001
        raise OSError("gateway unavailable")

    monkeypatch.setattr(connector_main.urllib.request, "urlopen", fake_urlopen)

    assert connector_main._sync_gateway_registration("http://gateway:8080", _registration()) is False
