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
        "response_mode": "summary_samples",
        "max_lines": 2,
        "sample_size": 1,
    }
    args.update(overrides)
    for key, value in list(args.items()):
        if value is None:
            args.pop(key)
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

    assert result.status == "succeeded"
    assert result.tool_name == "query_logs"
    assert result.request_id == "req-1"
    assert result.correlation_id == "corr-1"
    assert result.data["total_matched"] == 2
    assert result.data["returned_lines"] == 2
    assert len(result.data["samples"]) == 1
    assert result.data["grouped_patterns"][0]["message_template"] == "error"
    assert result.data["grouped_patterns"][0]["count"] == 2
    assert result.data["grouped_patterns"][0]["fingerprint"]
    assert result.data["grouped_patterns"][0]["first_seen"]
    assert result.data["grouped_patterns"][0]["last_seen"]
    assert result.data["grouped_patterns"][0]["sample_ref"].startswith("ev_loki_")
    assert result.audit["query_digest"] == result.data["query_digest"]
    assert result.audit["returned_lines"] == 2
    assert result.audit["total_matched"] == 2
    assert result.audit["truncated"] is False
    assert result.evidence_refs[0].source == "loki"
    assert result.evidence_refs[0].cluster_id == "prod-a"
    assert result.evidence_refs[0].query_digest == result.data["query_digest"]
    assert runner.calls[0]["limit"] == 2


@pytest.mark.asyncio
async def test_query_logs_missing_required_fields_returns_invalid_request() -> None:
    result = await query_logs({"request_id": "req-2", "query": '{app="api"}'}, runner=FakeRunner())

    assert result.status == "failed"
    assert result.errors[0].code == ErrorCode.INVALID_REQUEST
    assert result.errors[0].details["missing"] == ["cluster_id", "time_range", "reason"]
    assert result.audit["status"] == "failed"
    assert result.audit["error_code"] == ErrorCode.INVALID_REQUEST.value


@pytest.mark.asyncio
async def test_query_logs_guard_rejects_forbidden_logql() -> None:
    result = await query_logs(_args(query='{job=~".+"}'), runner=FakeRunner())

    assert result.status == "failed"
    assert result.errors[0].code == ErrorCode.QUERY_REJECTED


@pytest.mark.asyncio
async def test_query_logs_rejects_prod_raw_page_wide_query() -> None:
    result = await query_logs(
        _args(environment="prod", response_mode="raw_page", query="{}", max_lines=10),
        runner=FakeRunner(),
    )

    assert result.status == "failed"
    assert result.errors[0].code == ErrorCode.QUERY_REJECTED
    assert result.errors[0].details["mode"] == "raw_page"


@pytest.mark.asyncio
async def test_query_logs_backend_unavailable_is_controlled_error() -> None:
    result = await query_logs(_args(), runner=FakeRunner(error=LokiBackendError("down")))

    assert result.status == "failed"
    assert result.errors[0].code == ErrorCode.BACKEND_UNAVAILABLE
    assert "down" in result.errors[0].message
    assert result.audit["error_code"] == ErrorCode.BACKEND_UNAVAILABLE.value


@pytest.mark.asyncio
async def test_query_logs_truncation_and_cursor_fetch_next_page() -> None:
    runner = FakeRunner(_results(3))
    first = await query_logs(_args(max_lines=2, sample_size=2), runner=runner)

    assert first.truncated is True
    assert first.next_cursor
    assert first.status == "partial"
    assert first.data["returned_lines"] == 2
    assert first.audit["truncated"] is True

    second = await query_logs(_args(max_lines=2, sample_size=2, cursor=first.next_cursor), runner=runner)

    assert second.status == "succeeded"
    assert second.truncated is False
    assert second.next_cursor is None
    assert second.data["returned_lines"] == 1
    assert second.data["samples"][0]["line"] == "error line 2"


@pytest.mark.asyncio
async def test_query_logs_ref_only_hides_samples_and_raw_lines() -> None:
    result = await query_logs(_args(response_mode="ref_only"), runner=FakeRunner(_results(1)))

    assert result.status == "succeeded"
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

    assert result.status == "failed"
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

    assert result.status == "partial"
    assert result.truncated is True
    assert result.audit["status"] == "partial"
    assert len(json.dumps(asdict(result), ensure_ascii=False, default=str).encode("utf-8")) <= result.data["limits"]["max_bytes"]


@pytest.mark.asyncio
async def test_query_logs_response_mode_overrides_legacy_flags() -> None:
    result = await query_logs(
        _args(response_mode="raw_page", summary_only=True, ref_only=True),
        runner=FakeRunner(_results(1)),
    )

    assert result.status == "succeeded"
    assert result.data["response_mode"] == "raw_page"
    assert result.data["raw_lines"][0]["line"] == "error line 0"
    assert "samples" not in result.data


@pytest.mark.asyncio
async def test_query_logs_response_mode_ref_only_and_summary_only() -> None:
    ref_only = await query_logs(_args(response_mode="ref_only"), runner=FakeRunner(_results(1)))
    summary_only = await query_logs(_args(response_mode="summary_only"), runner=FakeRunner(_results(1)))

    assert ref_only.status == "succeeded"
    assert "samples" not in ref_only.data
    assert "raw_lines" not in ref_only.data
    assert summary_only.status == "succeeded"
    assert summary_only.data["samples"] == []
    assert "raw_lines" not in summary_only.data


@pytest.mark.asyncio
async def test_query_logs_sample_size_default_and_cap() -> None:
    default_args = _args(max_lines=30)
    default_args.pop("sample_size")
    default_result = await query_logs(default_args, runner=FakeRunner(_results(30)))
    assert default_result.data["limits"]["sample_size"] == 20
    assert len(default_result.data["samples"]) == 20

    capped_result = await query_logs(_args(max_lines=100, sample_size=99), runner=FakeRunner(_results(80)))
    assert capped_result.data["limits"]["sample_size"] == 50
    assert len(capped_result.data["samples"]) == 50
