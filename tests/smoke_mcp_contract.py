"""镜像级 MCP contract smoke 测试。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _metric_args(**overrides):
    args = {
        "request_id": "req-smoke-001",
        "correlation_id": "incident-smoke-001",
        "actor": {"actor_type": "agent", "actor_id": "smoke"},
        "agent_id": "agent-center-smoke",
        "brain_provider": "codex",
        "cluster_id": "qa-shanghai",
        "namespace": "payment",
        "service": "payment-api",
        "time_range": {
            "type": "absolute",
            "start": "2026-06-03T07:30:00Z",
            "end": "2026-06-03T08:00:00Z",
        },
        "metric": "error_rate",
        "reason": "smoke test query_metrics envelope",
    }
    args.update(overrides)
    return args


async def _main() -> None:
    import toolsets
    from toolsets import mcp_contract

    os.environ.pop("PROMETHEUS_URL", None)
    os.environ.pop("HERMES_CONFIG", None)
    os.environ.pop("HERMES_HOME", None)

    toolsets_path = Path(toolsets.__file__ or "")
    assert toolsets_path.name == "__init__.py", toolsets_path
    assert "site-packages" not in str(toolsets_path), toolsets_path

    async def fake_runner(query, start, end):
        assert start == "2026-06-03T07:30:00Z"
        assert end == "2026-06-03T08:00:00Z"
        return {
            "allowed": True,
            "query": query,
            "start": start,
            "end": end,
            "results": [{"metric": {"service": "payment-api"}, "values": [["1", "0.03"]]}],
        }

    success = await mcp_contract.query_metrics(_metric_args(), runner=fake_runner)
    assert success["status"] == "succeeded", success
    assert success["tool_name"] == "query_metrics"
    assert success["evidence_refs"][0]["source"] == "prometheus"
    assert success["audit"]["request_id"] == "req-smoke-001"
    assert success["audit"]["correlation_id"] == "incident-smoke-001"
    assert success["audit"]["tool_name"] == "query_metrics"
    assert success["audit"]["query_digest"] == success["data"]["query_digest"]
    assert success["audit"]["returned_bytes"] == success["limits"]["returned_bytes"]
    assert success["audit"]["truncated"] is False
    assert "promql" not in success["data"]

    rejected = await mcp_contract.query_metrics(_metric_args(promql="up"))
    assert rejected["status"] == "failed", rejected
    assert rejected["errors"][0]["code"] == "invalid_request", rejected

    backend_unavailable = await mcp_contract.query_metrics(_metric_args(promql="up", metric=None))
    assert backend_unavailable["status"] == "failed", backend_unavailable
    assert backend_unavailable["errors"][0]["code"] == "backend_unavailable", backend_unavailable
    assert backend_unavailable["audit"]["query_digest"] == backend_unavailable["data"]["query_digest"]


if __name__ == "__main__":
    asyncio.run(_main())
