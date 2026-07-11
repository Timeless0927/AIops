"""Connector outbound registration and heartbeat tests."""

from __future__ import annotations

import json
from typing import Any

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

    monkeypatch.setattr(connector_main.urllib.request, "urlopen", fake_urlopen)

    assert connector_main._sync_gateway_registration(
        "http://gateway:8080", _registration(), "connector-secret"
    ) is True
    assert requests == [
        (
            "POST",
            "http://gateway:8080/api/v1/connectors/register",
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
            "http://gateway:8080/api/v1/connectors/heartbeat",
            "Bearer connector-secret",
            {
                "connector_id": "connector-local",
                "cluster_id": "cluster-local",
                "status": "online",
            },
        ),
    ]


def test_sync_gateway_registration_fails_closed_without_credential(monkeypatch) -> None:
    called = False

    def fake_urlopen(request, timeout: int = 0):  # noqa: ANN001, ARG001
        nonlocal called
        called = True
        return _Response()

    monkeypatch.setattr(connector_main.urllib.request, "urlopen", fake_urlopen)

    assert connector_main._sync_gateway_registration("http://gateway:8080", _registration(), "") is False
    assert called is False


def test_sync_gateway_registration_reports_unregistered_when_gateway_unavailable(monkeypatch) -> None:
    def fake_urlopen(request, timeout: int = 0):  # noqa: ANN001, ARG001
        raise OSError("gateway unavailable")

    monkeypatch.setattr(connector_main.urllib.request, "urlopen", fake_urlopen)

    assert connector_main._sync_gateway_registration(
        "http://gateway:8080", _registration(), "connector-secret"
    ) is False
