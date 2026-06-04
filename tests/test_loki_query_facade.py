"""测试 AIO-48 Loki query_logs facade。"""

from __future__ import annotations

import importlib

import pytest


def _args(**overrides: object) -> dict[str, object]:
    args: dict[str, object] = {
        "request_id": "req-log-001",
        "correlation_id": "inc-payment-001",
        "actor": {"actor_type": "agent", "actor_id": "agent-001"},
        "agent_id": "agent-center-001",
        "brain_provider": "codex",
        "cluster_id": "qa-shanghai",
        "namespace": "payment",
        "service": "payment-api",
        "workload": {"kind": "Deployment", "name": "payment-api"},
        "time_range": {"type": "relative", "value": "last_30m"},
        "reason": "排查 payment-api 错误日志",
    }
    args.update(overrides)
    return args


def _loki_result(lines: list[str]) -> dict[str, object]:
    values = []
    for index, line in enumerate(lines):
        values.append([str(1_801_638_000_000_000_000 + index * 1_000_000_000), line])
    return {
        "results": [
            {
                "stream": {"namespace": "payment", "service": "payment-api", "pod": "payment-api-abc", "container": "app"},
                "values": values,
            }
        ]
    }


def test_standard_import_does_not_require_httpx_or_hermes_registry() -> None:
    """镜像 smoke 需要标准导入不被可选依赖阻塞。"""
    module = importlib.import_module("toolsets.loki_query")

    assert hasattr(module, "query_logs")
    assert module.QUERY_LOGS_SCHEMA["name"] == "query_logs"


@pytest.mark.asyncio
async def test_query_logs_summary_samples_success_with_fake_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """summary_samples 成功路径应返回摘要、样本、聚合模式和 evidence refs。"""
    module = importlib.import_module("toolsets.loki_query")
    calls: list[dict[str, object]] = []

    async def fake_runner(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return _loki_result(
            [
                "ERROR upstream timeout calling order-api request_id=req-123",
                "ERROR upstream timeout calling order-api request_id=req-456",
            ]
        )

    monkeypatch.setattr(module, "_record_query_logs_audit", lambda envelope: None)

    result = await module.query_logs(_args(keywords=["error", "timeout"], sample_size=2), runner=fake_runner)

    assert result["status"] == "succeeded"
    assert result["tool_name"] == "query_logs"
    assert result["errors"] == []
    assert result["data"]["total_matched"] == 2
    assert result["data"]["returned_lines"] == 2
    assert len(result["data"]["samples"]) == 2
    assert len(result["data"]["grouped_patterns"]) == 1
    assert result["evidence_refs"][0]["query_digest"].startswith("sha256:")
    assert result["audit"]["decision"] == "allowed"
    assert result["audit"]["actor_id"] == "agent-001"
    assert result["audit"]["resource_kind"] == "Deployment"
    assert result["audit"]["reason"] == "排查 payment-api 错误日志"
    assert calls[0]["limit"] == 201


@pytest.mark.asyncio
async def test_query_logs_rejects_missing_required_contract_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """cluster_id、time_range、reason 缺失时应返回 V1 invalid_request envelope。"""
    module = importlib.import_module("toolsets.loki_query")
    monkeypatch.setattr(module, "_record_query_logs_audit", lambda envelope: None)

    result = await module.query_logs({"time_range": {"type": "relative", "value": "last_30m"}, "reason": "排查"})

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "invalid_request"
    assert "cluster_id" in result["errors"][0]["details"]["missing_fields"]
    assert result["audit"]["decision"] == "rejected"


@pytest.mark.asyncio
async def test_query_logs_rejects_prod_raw_page_broad_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """prod raw_page 宽查询必须被拒绝，避免日志水管化。"""
    module = importlib.import_module("toolsets.loki_query")
    monkeypatch.setattr(module, "_record_query_logs_audit", lambda envelope: None)

    result = await module.query_logs(
        _args(cluster_id="prod-shanghai", namespace="", service="", response_mode="raw_page")
    )

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "query_cost_exceeded"
    assert result["audit"]["decision"] == "rejected"


@pytest.mark.asyncio
async def test_query_logs_backend_unavailable_is_controlled_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """后端不可用不能抛未捕获异常，应返回 backend_unavailable envelope。"""
    module = importlib.import_module("toolsets.loki_query")
    monkeypatch.setattr(module, "_record_query_logs_audit", lambda envelope: None)

    async def fake_runner(**kwargs: object) -> dict[str, object]:
        del kwargs
        raise module.LokiBackendUnavailable("LOKI_URL is not configured")

    result = await module.query_logs(_args(), runner=fake_runner)

    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "backend_unavailable"
    assert result["data"] == {}
    assert result["audit"]["decision"] == "rejected"


@pytest.mark.asyncio
async def test_query_logs_truncates_and_uses_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """raw_page 超过 max_lines 时应截断并返回可继续分页的 cursor。"""
    module = importlib.import_module("toolsets.loki_query")
    monkeypatch.setattr(module, "_record_query_logs_audit", lambda envelope: None)

    async def fake_runner(**kwargs: object) -> dict[str, object]:
        del kwargs
        return _loki_result(["line-0", "line-1", "line-2", "line-3"])

    first = await module.query_logs(_args(response_mode="raw_page", max_lines=2), runner=fake_runner)
    second = await module.query_logs(
        _args(response_mode="raw_page", max_lines=2, cursor=first["next_cursor"]),
        runner=fake_runner,
    )

    assert first["status"] == "partial"
    assert first["truncated"] is True
    assert first["errors"][0]["code"] == "output_truncated"
    assert len(first["data"]["raw_lines"]) == 2
    assert first["next_cursor"]
    assert second["data"]["raw_lines"][0]["line"] == "line-2"


@pytest.mark.asyncio
async def test_query_logs_ref_only_returns_reference_without_backend_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """ref_only 应只返回证据句柄，不拉取原始日志。"""
    module = importlib.import_module("toolsets.loki_query")
    monkeypatch.setattr(module, "_record_query_logs_audit", lambda envelope: None)

    async def fake_runner(**kwargs: object) -> dict[str, object]:
        raise AssertionError("ref_only must not query Loki")

    result = await module.query_logs(_args(response_mode="ref_only", cursor="opaque"), runner=fake_runner)

    assert result["status"] == "succeeded"
    assert result["data"]["returned_lines"] == 0
    assert result["data"]["samples"] == []
    assert result["evidence_refs"][0]["cursor"] == "opaque"
    assert result["next_cursor"] == "opaque"


@pytest.mark.asyncio
async def test_query_logs_summary_only_returns_patterns_without_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    """summary_only 只返回聚合摘要和 evidence ref，不回传样本行。"""
    module = importlib.import_module("toolsets.loki_query")
    monkeypatch.setattr(module, "_record_query_logs_audit", lambda envelope: None)

    async def fake_runner(**kwargs: object) -> dict[str, object]:
        del kwargs
        return _loki_result(["ERROR timeout request_id=req-1", "ERROR timeout request_id=req-2"])

    result = await module.query_logs(_args(response_mode="summary_only"), runner=fake_runner)

    assert result["status"] == "succeeded"
    assert result["data"]["returned_lines"] == 0
    assert result["data"]["samples"] == []
    assert result["data"]["grouped_patterns"]
