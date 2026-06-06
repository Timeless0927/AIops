"""Tests for the Prometheus query_metrics V1 facade."""

from __future__ import annotations

from typing import Any

import pytest

from aiops.contracts import ErrorCode
from apps.mcp_prometheus import facade
from toolsets.prometheus_query import PrometheusBackendError, PrometheusTimeoutError, query_metrics


class FakeRunner:
    def __init__(self, results: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.results = results or []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def query_range(self, query: str, start: str, end: str, step: str) -> list[dict[str, Any]]:
        self.calls.append({"query": query, "start": start, "end": end, "step": step})
        if self.error:
            raise self.error
        return self.results


def _args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "request_id": "req-1",
        "correlation_id": "corr-1",
        "cluster_id": "prod-a",
        "namespace": "default",
        "service": "payment-api",
        "query": 'sum(rate(http_requests_total{app="payment-api"}[5m]))',
        "start": "2026-06-04T00:00:00Z",
        "end": "2026-06-04T00:10:00Z",
        "step": "60s",
        "reason": "investigate elevated error rate",
        "max_series": 2,
    }
    args.update(overrides)
    for key, value in list(args.items()):
        if value is None:
            args.pop(key)
    return args


def _results(count: int) -> list[dict[str, Any]]:
    return [
        {
            "metric": {"app": "payment-api", "instance": f"pod-{index}"},
            "values": [[1780531200 + index, str(index)]],
        }
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_query_metrics_success_records_evidence() -> None:
    runner = FakeRunner(_results(2))

    result = await query_metrics(_args(), runner=runner)

    assert result.status == "succeeded"
    assert result.tool_name == "query_metrics"
    assert result.request_id == "req-1"
    assert result.correlation_id == "corr-1"
    assert result.data["series_count"] == 2
    assert result.data["returned_series"] == 2
    assert result.data["series"][0]["metric"]["app"] == "payment-api"
    assert result.data["series"][0]["sample_count"] == 1
    assert result.data["ref"].startswith("ev_prom_")
    assert result.audit["query_digest"] == result.data["query_digest"]
    assert result.audit["series_count"] == 2
    assert result.audit["returned_series"] == 2
    assert result.audit["truncated"] is False
    assert result.evidence_refs[0].source == "prometheus"
    assert result.evidence_refs[0].cluster_id == "prod-a"
    assert result.evidence_refs[0].namespace == "default"
    assert result.evidence_refs[0].query_digest == result.data["query_digest"]
    assert runner.calls[0]["step"] == "60s"


@pytest.mark.asyncio
async def test_query_metrics_missing_required_fields_returns_invalid_request() -> None:
    result = await query_metrics({"request_id": "req-2", "query": "up"}, runner=FakeRunner())

    assert result.status == "failed"
    assert result.errors[0].code == ErrorCode.INVALID_REQUEST
    assert result.errors[0].details["missing"] == ["cluster_id", "reason"]
    assert result.audit["status"] == "failed"
    assert result.audit["error_code"] == ErrorCode.INVALID_REQUEST.value


@pytest.mark.asyncio
async def test_query_metrics_guard_rejects_invalid_time_window() -> None:
    result = await query_metrics(
        _args(start="2026-06-04T00:10:00Z", end="2026-06-04T00:00:00Z"),
        runner=FakeRunner(),
    )

    assert result.status == "failed"
    assert result.errors[0].code == ErrorCode.QUERY_REJECTED
    assert result.summary == "query_metrics 查询被护栏拒绝"


@pytest.mark.asyncio
async def test_query_metrics_backend_unavailable_is_controlled_error() -> None:
    result = await query_metrics(_args(), runner=FakeRunner(error=PrometheusBackendError("down")))

    assert result.status == "failed"
    assert result.errors[0].code == ErrorCode.BACKEND_UNAVAILABLE
    assert "down" in result.errors[0].message
    assert result.audit["error_code"] == ErrorCode.BACKEND_UNAVAILABLE.value


@pytest.mark.asyncio
async def test_query_metrics_timeout_is_controlled_error() -> None:
    result = await query_metrics(_args(), runner=FakeRunner(error=PrometheusTimeoutError("slow")))

    assert result.status == "failed"
    assert result.errors[0].code == ErrorCode.TIMEOUT
    assert result.audit["error_code"] == ErrorCode.TIMEOUT.value


@pytest.mark.asyncio
async def test_query_metrics_truncates_to_max_series() -> None:
    result = await query_metrics(_args(max_series=1), runner=FakeRunner(_results(3)))

    assert result.status == "partial"
    assert result.truncated is True
    assert result.data["series_count"] == 3
    assert result.data["returned_series"] == 1
    assert result.audit["truncated"] is True


@pytest.mark.asyncio
async def test_mcp_prometheus_facade_routes_query_metrics() -> None:
    result = await facade.query_metrics(_args(), runner=FakeRunner(_results(1)))

    assert facade.tool_name() == "query_metrics"
    assert result.status == "succeeded"
    assert result.tool_name == "query_metrics"
