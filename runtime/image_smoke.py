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
    "apps.mcp_loki.facade",
)


class _FakeQueryLogsRunner:
    async def query_range(self, query: str, start: str, end: str, limit: int) -> list[dict[str, Any]]:
        return [
            {
                "stream": {"app": "api"},
                "values": [["1780531200000000000", "error: pod restarted"]],
            }
        ]


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
    original_runner = loki_query.HttpLokiRunner
    loki_query.HttpLokiRunner = lambda: _FakeQueryLogsRunner()
    try:
        result = await loki_query.loki_query(
            '{app="api"}',
            start="2026-06-04T00:00:00Z",
            end="2026-06-04T00:01:00Z",
            limit=1,
        )
    finally:
        loki_query.HttpLokiRunner = original_runner

    if result.get("allowed") is not True or result.get("error"):
        raise RuntimeError(f"fake Loki success path failed: {result}")
    if result.get("results") != [{"stream": {"app": "api"}, "values": [["1780531200000000000", "error: pod restarted"]]}]:
        raise RuntimeError(f"fake Loki result contract changed: {result}")


async def _assert_query_logs_success_path() -> None:
    facade = importlib.import_module("apps.mcp_loki.facade")
    result = await facade.query_logs(
        {
            "request_id": "smoke-1",
            "cluster_id": "prod-a",
            "namespace": "default",
            "query": '{app="api"}',
            "time_range": {
                "type": "absolute",
                "value": "2026-06-04T00:00:00Z/2026-06-04T00:01:00Z",
            },
            "reason": "image smoke",
            "mode": "summary_samples",
        },
        runner=_FakeQueryLogsRunner(),
    )
    if result.status != "succeeded" or result.tool_name != "query_logs":
        raise RuntimeError(f"query_logs success path failed: {result}")
    if not result.evidence_refs or not result.data.get("query_digest"):
        raise RuntimeError(f"query_logs evidence contract changed: {result}")
    if not result.audit or result.audit.get("status") != "succeeded":
        raise RuntimeError(f"query_logs audit contract changed: {result}")


async def _assert_query_logs_backend_unavailable_path() -> None:
    facade = importlib.import_module("apps.mcp_loki.facade")
    os.environ.pop("LOKI_URL", None)
    os.environ["HERMES_CONFIG"] = "/tmp/aiops-image-smoke/missing-config.yaml"
    os.environ["HERMES_HOME"] = "/tmp/aiops-image-smoke/missing-home"
    result = await facade.query_logs(
        {
            "request_id": "smoke-2",
            "cluster_id": "prod-a",
            "query": '{app="api"}',
            "time_range": {
                "type": "absolute",
                "value": "2026-06-04T00:00:00Z/2026-06-04T00:01:00Z",
            },
            "reason": "image smoke",
        }
    )
    if result.status != "failed" or not result.errors or result.errors[0].code.value != "backend_unavailable":
        raise RuntimeError(f"query_logs backend-unavailable path failed: {result}")


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

    facade = importlib.import_module("apps.mcp_loki.facade")
    rejected = await facade.query_logs(
        {
            "request_id": "smoke-3",
            "cluster_id": "prod-a",
            "query": '{job=~".+"}',
            "time_range": {
                "type": "absolute",
                "value": "2026-06-04T00:00:00Z/2026-06-04T00:01:00Z",
            },
            "reason": "image smoke",
        },
        runner=_FakeQueryLogsRunner(),
    )
    if rejected.status != "failed" or rejected.errors[0].code.value != "query_rejected":
        raise RuntimeError(f"query_logs security negative path failed: {rejected}")


async def _run() -> None:
    _import_required_facades()
    await _assert_loki_success_path()
    await _assert_loki_backend_unavailable_path()
    await _assert_query_logs_success_path()
    await _assert_query_logs_backend_unavailable_path()
    await _assert_contract_negative_path()


def main() -> None:
    asyncio.run(_run())
    print("AIOps image smoke passed: imports, query_logs success, backend unavailable, contract/security negative")


if __name__ == "__main__":
    main()
