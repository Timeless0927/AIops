"""测试 V1 MCP contract skeleton 与 Prometheus facade。"""

from __future__ import annotations

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
    assert result["evidence_refs"][0]["source"] == "prometheus"
    assert result["evidence_refs"][0]["time_range"] == (
        "2026-06-03T07:30:00Z/2026-06-03T08:00:00Z"
    )
    assert result["audit"]["decision"] == "allowed"
    assert result["audit"]["actor"]["actor_id"] == "u-1"


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
