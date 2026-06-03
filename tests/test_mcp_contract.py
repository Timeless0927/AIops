"""测试 V1 MCP contract skeleton 与 Prometheus facade。"""

from __future__ import annotations

import builtins
from datetime import datetime, timezone
from pathlib import Path

import toolsets
from toolsets import mcp_contract


def _metric_args(**overrides):
    args = {
        "request_id": "req-metric-001",
        "correlation_id": "incident-001",
        "actor": {"actor_type": "human", "actor_id": "u-1"},
        "agent_id": "agent-center-001",
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
        "reason": "排查 payment-api 5xx 升高",
    }
    args.update(overrides)
    return args


def test_v1_tool_schemas_include_required_tools() -> None:
    """MCP skeleton 应包含 AIO-48/AIO-50 固化的 V1 工具。"""
    assert set(mcp_contract.MCP_TOOL_SCHEMAS) == {
        "query_metrics",
        "query_logs",
        "get_service_topology",
        "run_k8s_read",
        "submit_k8s_change",
        "get_k8s_execution_status",
    }
    assert mcp_contract.MCP_TOOL_SCHEMAS["run_k8s_read"]["required"] == [
        "cluster_id",
        "namespace",
        "argv",
        "reason",
    ]
    assert "trace_deferred" in mcp_contract.MCP_ERROR_CODES


def test_toolsets_resolves_to_repo_package() -> None:
    """镜像内应优先解析 /app/toolsets package，而不是 site-packages/toolsets.py。"""
    assert toolsets.__file__ is not None
    assert Path(toolsets.__file__).name == "__init__.py"


async def test_query_metrics_returns_success_envelope() -> None:
    """Prometheus facade 应返回统一 envelope、evidence ref 和审计字段。"""

    async def fake_runner(query, start, end):
        assert "http_requests_total" in query
        assert start == "2026-06-03T07:30:00Z"
        assert end == "2026-06-03T08:00:00Z"
        return {
            "allowed": True,
            "query": query,
            "start": start,
            "end": end,
            "results": [{"metric": {"service": "payment-api"}, "values": [["1", "0.03"]]}],
        }

    result = await mcp_contract.query_metrics(_metric_args(), runner=fake_runner)

    assert result["request_id"] == "req-metric-001"
    assert result["tool_name"] == "query_metrics"
    assert result["status"] == "succeeded"
    assert result["data"]["query_mode"] == "typed"
    assert result["data"]["series"][0]["metric"]["service"] == "payment-api"
    assert result["data"]["query_digest"].startswith("sha256:")
    assert "promql" not in result["data"]
    assert result["data"]["time_range"] == {
        "start": "2026-06-03T07:30:00Z",
        "end": "2026-06-03T08:00:00Z",
    }
    assert result["evidence_refs"][0]["source"] == "prometheus"
    assert result["evidence_refs"][0]["time_range"] == (
        "2026-06-03T07:30:00Z/2026-06-03T08:00:00Z"
    )
    assert result["audit"]["decision"] == "allowed"
    assert result["audit"]["actor"]["actor_id"] == "u-1"
    assert result["audit"]["request_id"] == "req-metric-001"
    assert result["audit"]["correlation_id"] == "incident-001"
    assert result["audit"]["tool_name"] == "query_metrics"
    assert result["audit"]["query_digest"] == result["data"]["query_digest"]
    assert result["audit"]["returned_bytes"] == result["limits"]["returned_bytes"]
    assert result["audit"]["truncated"] is False


async def test_query_metrics_maps_guard_rejection_to_standard_error() -> None:
    """底层 guard 拒绝时应映射为 query_rejected。"""

    async def fake_runner(query, start, end):
        return {
            "allowed": False,
            "query": query,
            "start": start,
            "end": end,
            "message": "开始时间必须早于结束时间",
        }

    result = await mcp_contract.query_metrics(
        _metric_args(promql="up", metric=None),
        runner=fake_runner,
    )

    assert result["status"] == "failed"
    assert result["audit"]["decision"] == "rejected"
    assert result["errors"][0]["code"] == "query_rejected"
    assert result["audit"]["query_digest"] == result["data"]["query_digest"]


async def test_query_metrics_maps_backend_error_to_backend_unavailable() -> None:
    """Prometheus 不可用时应保留 evidence ref 并返回 backend_unavailable。"""

    async def fake_runner(query, start, end):
        return {
            "allowed": True,
            "query": query,
            "start": start,
            "end": end,
            "error": "未配置 PROMETHEUS_URL，无法执行查询",
            "results": [],
        }

    result = await mcp_contract.query_metrics(_metric_args(promql="up", metric=None), runner=fake_runner)

    assert result["status"] == "failed"
    assert result["audit"]["decision"] == "partial"
    assert result["errors"][0]["code"] == "backend_unavailable"
    assert result["evidence_refs"][0]["query_digest"] == result["data"]["query_digest"]
    assert result["audit"]["query_digest"] == result["data"]["query_digest"]


async def test_query_metrics_maps_timeout_error() -> None:
    """Prometheus 超时应映射为 timeout。"""

    async def fake_runner(query, start, end):
        return {
            "allowed": True,
            "query": query,
            "start": start,
            "end": end,
            "error": "Prometheus 查询超时（30s）",
            "results": [],
        }

    result = await mcp_contract.query_metrics(_metric_args(promql="up", metric=None), runner=fake_runner)

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "timeout"
    assert result["audit"]["error_code"] == "timeout"


async def test_query_metrics_rejects_missing_required_fields() -> None:
    """缺少 contract 必填字段时应返回 invalid_request。"""
    result = await mcp_contract.query_metrics({"promql": "up"})

    assert result["status"] == "failed"
    assert result["audit"]["decision"] == "rejected"
    assert result["audit"]["actor"] == {"actor_type": "agent", "actor_id": "unknown"}
    assert result["audit"]["agent_id"] == "unknown"
    assert result["audit"]["request_id"].startswith("req-")
    assert result["audit"]["tool_name"] == "query_metrics"
    assert result["audit"]["returned_bytes"] == 0
    assert result["audit"]["truncated"] is False
    assert result["audit"]["query_digest"] is None
    assert result["errors"][0]["code"] == "invalid_request"
    assert result["errors"][0]["details"]["missing_fields"] == [
        "cluster_id",
        "time_range",
        "reason",
    ]


async def test_query_metrics_rejects_metric_and_promql_together() -> None:
    """metric/promql 必须 exactly-one。"""
    result = await mcp_contract.query_metrics(_metric_args(promql="up"))

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "invalid_request"
    assert "必须且只能提供一个" in result["summary"]


async def test_query_metrics_rejects_missing_metric_and_promql() -> None:
    """metric/promql 不能同时缺失。"""
    result = await mcp_contract.query_metrics(_metric_args(metric=None, promql=None))

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "invalid_request"
    assert "必须且只能提供一个" in result["summary"]


async def test_query_metrics_rejects_custom_metric_without_promql() -> None:
    """custom metric 必须通过 promql 表达。"""
    result = await mcp_contract.query_metrics(_metric_args(metric="custom"))

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "invalid_request"
    assert "custom metric" in result["summary"]


async def test_query_metrics_rejects_invalid_typed_request() -> None:
    """typed metric 缺少必要上下文时应在 facade 层拒绝。"""
    result = await mcp_contract.query_metrics(_metric_args(namespace="", service=""))

    assert result["status"] == "failed"
    assert result["audit"]["decision"] == "rejected"
    assert result["errors"][0]["code"] == "invalid_request"


async def test_query_metrics_rejects_unknown_metric() -> None:
    """未知 metric 不应穿透到后端查询。"""
    result = await mcp_contract.query_metrics(_metric_args(metric="unknown"))

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "invalid_request"
    assert "不支持的 metric" in result["summary"]


async def test_query_metrics_converts_relative_time_range_to_utc_bounds() -> None:
    """relative last_30m 应转换为明确 UTC start/end。"""

    async def fake_runner(query, start, end):
        return {
            "allowed": True,
            "query": query,
            "start": start,
            "end": end,
            "results": [],
        }

    result = await mcp_contract.query_metrics(
        _metric_args(time_range={"type": "relative", "value": "last_30m"}),
        runner=fake_runner,
        now=datetime(2026, 6, 3, 8, 0, 0, tzinfo=timezone.utc),
    )

    assert result["status"] == "succeeded"
    assert result["data"]["time_range"] == {
        "start": "2026-06-03T07:30:00Z",
        "end": "2026-06-03T08:00:00Z",
    }
    assert result["evidence_refs"][0]["time_range"] == (
        "2026-06-03T07:30:00Z/2026-06-03T08:00:00Z"
    )


async def test_query_metrics_rejects_missing_time_range() -> None:
    """time_range 缺失必须在 facade 层拒绝。"""
    result = await mcp_contract.query_metrics(_metric_args(time_range=None))

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "invalid_request"
    assert "time_range" in result["summary"]


async def test_query_metrics_rejects_relative_range_over_6h() -> None:
    """relative 查询窗口超过 6h 应返回 query_cost_exceeded。"""
    result = await mcp_contract.query_metrics(
        _metric_args(time_range={"type": "relative", "value": "last_7h"}),
        now=datetime(2026, 6, 3, 8, 0, 0, tzinfo=timezone.utc),
    )

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "query_cost_exceeded"
    assert result["errors"][0]["details"]["max_seconds"] == 21600


async def test_query_metrics_rejects_absolute_range_over_6h() -> None:
    """absolute 查询窗口超过 6h 应返回 query_cost_exceeded。"""
    result = await mcp_contract.query_metrics(
        _metric_args(
            time_range={
                "type": "absolute",
                "start": "2026-06-03T00:00:00Z",
                "end": "2026-06-03T08:00:00Z",
            }
        )
    )

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "query_cost_exceeded"


async def test_query_metrics_rejects_reversed_absolute_range() -> None:
    """反向 absolute 时间窗应返回 query_rejected。"""
    result = await mcp_contract.query_metrics(
        _metric_args(
            time_range={
                "type": "absolute",
                "start": "2026-06-03T08:00:00Z",
                "end": "2026-06-03T07:30:00Z",
            }
        )
    )

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "query_rejected"


async def test_query_metrics_rejects_invalid_relative_range() -> None:
    """不支持的 relative value 应返回 invalid_request。"""
    result = await mcp_contract.query_metrics(
        _metric_args(time_range={"type": "relative", "value": "30m"})
    )

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "invalid_request"


async def test_query_metrics_default_runner_dependency_error_returns_envelope(monkeypatch) -> None:
    """默认 runner 缺 Prometheus 依赖时不能抛异常，应返回 V1 envelope。"""
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in {"toolsets.prometheus_query", "prometheus_query"}:
            raise ImportError("No module named 'prometheus_api_client'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    result = await mcp_contract.query_metrics(_metric_args(promql="up", metric=None))

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "backend_unavailable"
    assert "prometheus_api_client" in result["errors"][0]["message"]
    assert result["audit"]["query_digest"] == result["data"]["query_digest"]
