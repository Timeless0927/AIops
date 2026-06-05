"""Tests for the Loki query_logs V1 facade."""

from __future__ import annotations

from dataclasses import asdict
import json
from typing import Any

import pytest

from aiops.contracts import ErrorCode
from toolsets.loki_query import LokiBackendError, query_logs


class FakeRunner:
    def __init__(self, results: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.results = results or []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def query_range(self, query: str, start: str, end: str, limit: int) -> list[dict[str, Any]]:
        self.calls.append({"query": query, "start": start, "end": end, "limit": limit})
        if self.error:
            raise self.error
        return self.results


def _args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "request_id": "req-1",
        "correlation_id": "corr-1",
        "cluster_id": "prod-a",
        "namespace": "default",
        "query": '{app="api"}',
        "time_range": {
            "type": "absolute",
            "value": "2026-06-04T00:00:00Z/2026-06-04T00:10:00Z",
        },
        "reason": "investigate incident",
        "mode": "summary_samples",
        "max_lines": 2,
        "sample_size": 1,
    }
    args.update(overrides)
    return args


def _results(count: int) -> list[dict[str, Any]]:
    return [
        {
            "stream": {"app": "api", "namespace": "default"},
            "values": [[str(1780531200000000000 + index), f"error line {index}"] for index in range(count)],
        }
    ]


@pytest.mark.asyncio
async def test_query_logs_summary_samples_success_records_evidence() -> None:
    runner = FakeRunner(_results(2))

    result = await query_logs(_args(), runner=runner)

    assert result.status == "success"
    assert result.tool_name == "query_logs"
    assert result.request_id == "req-1"
    assert result.correlation_id == "corr-1"
    assert result.data["line_count"] == 2
    assert len(result.data["samples"]) == 1
    assert result.data["grouped_patterns"] == [{"pattern": "error", "count": 2}]
    assert result.evidence_refs[0].source == "loki"
    assert result.evidence_refs[0].cluster_id == "prod-a"
    assert result.evidence_refs[0].query_digest == result.data["query_digest"]
    assert runner.calls[0]["limit"] == 2


@pytest.mark.asyncio
async def test_query_logs_missing_required_fields_returns_invalid_request() -> None:
    result = await query_logs({"request_id": "req-2", "query": '{app="api"}'}, runner=FakeRunner())

    assert result.status == "error"
    assert result.errors[0].code == ErrorCode.INVALID_REQUEST
    assert result.errors[0].details["missing"] == ["cluster_id", "time_range", "reason"]


@pytest.mark.asyncio
async def test_query_logs_guard_rejects_forbidden_logql() -> None:
    result = await query_logs(_args(query='{job=~".+"}'), runner=FakeRunner())

    assert result.status == "rejected"
    assert result.errors[0].code == ErrorCode.QUERY_REJECTED


@pytest.mark.asyncio
async def test_query_logs_rejects_prod_raw_page_wide_query() -> None:
    result = await query_logs(
        _args(environment="prod", mode="raw_page", query="{}", max_lines=10),
        runner=FakeRunner(),
    )

    assert result.status == "rejected"
    assert result.errors[0].code == ErrorCode.QUERY_REJECTED
    assert result.errors[0].details["mode"] == "raw_page"


@pytest.mark.asyncio
async def test_query_logs_backend_unavailable_is_controlled_error() -> None:
    result = await query_logs(_args(), runner=FakeRunner(error=LokiBackendError("down")))

    assert result.status == "error"
    assert result.errors[0].code == ErrorCode.BACKEND_UNAVAILABLE
    assert "down" in result.errors[0].message


@pytest.mark.asyncio
async def test_query_logs_truncation_and_cursor_fetch_next_page() -> None:
    runner = FakeRunner(_results(3))
    first = await query_logs(_args(max_lines=2, sample_size=2), runner=runner)

    assert first.truncated is True
    assert first.next_cursor
    assert first.data["line_count"] == 2

    second = await query_logs(_args(max_lines=2, sample_size=2, cursor=first.next_cursor), runner=runner)

    assert second.truncated is False
    assert second.next_cursor is None
    assert second.data["line_count"] == 1
    assert second.data["samples"][0]["line"] == "error line 2"


@pytest.mark.asyncio
async def test_query_logs_ref_only_hides_samples_and_raw_lines() -> None:
    result = await query_logs(_args(mode="ref_only"), runner=FakeRunner(_results(1)))

    assert result.status == "success"
    assert "samples" not in result.data
    assert "raw_lines" not in result.data
    assert result.data["ref"].startswith("ev_loki_")


@pytest.mark.asyncio
async def test_query_logs_query_cost_exceeded() -> None:
    result = await query_logs(
        _args(
            max_lines=1000,
            time_range={
                "type": "absolute",
                "value": "2026-06-04T00:00:00Z/2026-06-04T05:00:00Z",
            },
        ),
        runner=FakeRunner(),
    )

    assert result.status == "rejected"
    assert result.errors[0].code == ErrorCode.QUERY_COST_EXCEEDED


@pytest.mark.asyncio
async def test_query_logs_envelope_is_trimmed_to_limit() -> None:
    long_results = [
        {
            "stream": {"app": "api"},
            "values": [[str(1780531200000000000 + index), "x" * 2000] for index in range(20)],
        }
    ]

    result = await query_logs(_args(max_lines=20, sample_size=20), runner=FakeRunner(long_results))

    assert result.truncated is True
    assert len(json.dumps(asdict(result), ensure_ascii=False, default=str).encode("utf-8")) <= result.data["limits"]["max_bytes"]
