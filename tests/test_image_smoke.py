"""Image smoke entry tests."""

from __future__ import annotations

import pytest

from runtime import image_smoke


def test_toolsets_package_resolves_to_repo_package() -> None:
    """The local toolsets package must beat any installed toolsets.py module."""
    image_smoke._import_required_facades()


@pytest.mark.asyncio
async def test_image_smoke_fake_loki_paths() -> None:
    """Smoke runner covers offline success and error paths without live backends."""
    await image_smoke._assert_loki_success_path()
    await image_smoke._assert_loki_backend_unavailable_path()
    await image_smoke._assert_contract_negative_path()
