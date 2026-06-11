"""Image smoke entry tests."""

from __future__ import annotations

import pytest

from runtime import image_smoke
from runtime import service_image_smoke


def test_toolsets_package_resolves_to_repo_package() -> None:
    """The local toolsets package must beat any installed toolsets.py module."""
    image_smoke._import_required_facades()


@pytest.mark.asyncio
async def test_image_smoke_fake_loki_paths() -> None:
    """Smoke runner covers offline success and error paths without live backends."""
    await image_smoke._assert_loki_success_path()
    await image_smoke._assert_loki_backend_unavailable_path()
    await image_smoke._assert_query_logs_success_path()
    await image_smoke._assert_query_logs_backend_unavailable_path()
    await image_smoke._assert_contract_negative_path()


def test_service_image_smoke_import_sets() -> None:
    for service in ("gateway", "hermes", "connectors", "mcp-prometheus", "mcp-loki"):
        assert service in service_image_smoke._SERVICE_IMPORTS
    assert "apps.aiops_k8s_gateway.alertmanager_webhook" in service_image_smoke._SERVICE_IMPORTS["gateway"]
    assert "prometheus_api_client" in service_image_smoke._SERVICE_IMPORTS["mcp-prometheus"]
    assert "httpx" in service_image_smoke._SERVICE_IMPORTS["mcp-loki"]
