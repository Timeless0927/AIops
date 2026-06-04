"""Container smoke checks for V1 facade imports and offline query paths."""

from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
from typing import Any


_REQUIRED_IMPORTS = (
    "toolsets.query_guard",
    "toolsets.loki_query",
    "toolsets.audit_log",
)


class _FakeLokiResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "status": "success",
            "data": {
                "result": [
                    {
                        "stream": {"app": "api"},
                        "values": [["1780531200000000000", "ok"]],
                    }
                ]
            },
        }


class _FakeAsyncClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.requests: list[tuple[str, dict[str, Any] | None]] = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeLokiResponse:
        if not url.endswith("/loki/api/v1/query_range"):
            raise AssertionError(f"unexpected Loki URL: {url}")
        self.requests.append((url, params))
        return _FakeLokiResponse()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _assert_toolsets_origin() -> None:
    package = importlib.import_module("toolsets")
    origin = Path(str(package.__file__)).resolve()
    expected = _repo_root() / "toolsets" / "__init__.py"
    if origin != expected:
        raise RuntimeError(f"toolsets resolved to {origin}, expected {expected}")


def _import_required_facades() -> None:
    os.environ.setdefault("AIOPS_DATA_DIR", "/tmp/aiops-image-smoke")
    _assert_toolsets_origin()
    for module_name in _REQUIRED_IMPORTS:
        importlib.import_module(module_name)


async def _assert_loki_success_path() -> None:
    loki_query = importlib.import_module("toolsets.loki_query")
    original_client = loki_query.httpx.AsyncClient
    os.environ["LOKI_URL"] = "http://loki.example"
    loki_query.httpx.AsyncClient = _FakeAsyncClient
    try:
        result = await loki_query.loki_query(
            '{app="api"}',
            start="2026-06-04T00:00:00Z",
            end="2026-06-04T00:01:00Z",
            limit=1,
        )
    finally:
        loki_query.httpx.AsyncClient = original_client
        os.environ.pop("LOKI_URL", None)

    if result.get("allowed") is not True or result.get("error"):
        raise RuntimeError(f"fake Loki success path failed: {result}")
    if result.get("results") != [{"stream": {"app": "api"}, "values": [["1780531200000000000", "ok"]]}]:
        raise RuntimeError(f"fake Loki result contract changed: {result}")


async def _assert_loki_backend_unavailable_path() -> None:
    loki_query = importlib.import_module("toolsets.loki_query")
    os.environ.pop("LOKI_URL", None)
    os.environ["HERMES_CONFIG"] = "/tmp/aiops-image-smoke/missing-config.yaml"
    os.environ["HERMES_HOME"] = "/tmp/aiops-image-smoke/missing-home"

    result = await loki_query.loki_query(
        '{app="api"}',
        start="2026-06-04T00:00:00Z",
        end="2026-06-04T00:01:00Z",
        limit=1,
    )
    if result.get("allowed") is not True or not result.get("error") or result.get("results") != []:
        raise RuntimeError(f"Loki backend-unavailable path failed: {result}")


async def _assert_contract_negative_path() -> None:
    query_guard = importlib.import_module("toolsets.query_guard")
    result = await query_guard.validate_loki_query('{job=~".+"}', None, None, None)
    if result.get("allowed") is not False or "全量匹配" not in result.get("message", ""):
        raise RuntimeError(f"Loki contract negative path failed: {result}")


async def _run() -> None:
    _import_required_facades()
    await _assert_loki_success_path()
    await _assert_loki_backend_unavailable_path()
    await _assert_contract_negative_path()


def main() -> None:
    asyncio.run(_run())
    print("AIOps image smoke passed: imports, fake Loki success, backend unavailable, contract negative")


if __name__ == "__main__":
    main()
