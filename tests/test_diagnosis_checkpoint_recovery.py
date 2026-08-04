"""Diagnosis loop checkpoint and recovery contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from diagnosis_service.diagnosis_provider import ProviderResult, ToolCall
from diagnosis_service.jobs import DiagnosisJobs
from diagnosis_service.runtime import DiagnosisRuntime


class Clock:
    now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _request() -> dict[str, object]:
    return {
        "request_id": "diagnosis-checkpoint-1",
        "session_id": "diagnosis-checkpoint-1",
        "incident_id": "incident-checkpoint-1",
        "investigation_id": "investigation-checkpoint-1",
        "source": "gateway",
        "alert": {
            "alertname": "HighErrorRate",
            "cluster": "prod-a",
            "namespace": "payments",
            "service": "checkout-api",
            "status": "firing",
        },
    }


def _final_provider(evidence_ref: str, *, expect_reconciliation: bool = False):
    class Provider:
        calls = 0

        async def chat_with_tools(
            self,
            messages: list[dict[str, Any]],
            _tools: list[dict[str, Any]],
        ) -> ProviderResult:
            self.calls += 1
            if expect_reconciliation:
                assert "reconciliation" in json.dumps(messages, ensure_ascii=False)
            return ProviderResult(
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "root_cause_candidates": [
                                {
                                    "cause": "checkout-api 错误率上升",
                                    "confidence": 0.7,
                                    "evidence_refs": [evidence_ref],
                                }
                            ],
                            "recommended_actions": [],
                            "confidence": {"score": 0.7, "level": "medium"},
                        },
                        ensure_ascii=False,
                    ),
                },
                [],
                "stop",
                {},
            )

    return Provider()


def _runtime(
    jobs: DiagnosisJobs,
    provider: Any,
    *,
    metrics_adapter: Any,
    logs_adapter: Any,
) -> DiagnosisRuntime:
    runtime = DiagnosisRuntime(
        jobs,
        lambda: None,  # provider resolution is replaced by the frozen fake below
        metrics_adapter=metrics_adapter,
        logs_adapter=logs_adapter,
        k8s_read_adapter=None,
        topology_adapter=None,
    )
    runtime.resolve_provider = lambda _revision=None: provider  # type: ignore[method-assign]
    return runtime


def test_reopened_job_reuses_completed_observation_and_surfaces_inflight_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WallClock(datetime):
        now_value = datetime(2026, 1, 1, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return cls.now_value

    monkeypatch.setattr("toolsets.incident_diagnosis.datetime", WallClock)
    clock = Clock()
    db_path = tmp_path / "diagnosis.db"
    jobs = DiagnosisJobs(
        db_path,
        clock=clock,
        execution_lease_seconds=1,
        max_execution_attempts=2,
    )
    jobs.accept(_request(), provider_revision="model-provider:revision-1")
    calls = {"metrics": 0, "logs": 0}

    async def metrics(_args: dict[str, Any]) -> dict[str, Any]:
        calls["metrics"] += 1
        return {
            "status": "succeeded",
            "summary": "metrics show elevated 5xx",
            "data": {"error_rate": 0.42},
            "evidence_refs": [{"ref_id": "evidence:metrics:1"}],
        }

    async def interrupted_logs(_args: dict[str, Any]) -> dict[str, Any]:
        calls["logs"] += 1
        raise SystemExit("diagnosis process stopped during the tool call")

    first_runtime = _runtime(
        jobs,
        _final_provider("evidence:metrics:1"),
        metrics_adapter=metrics,
        logs_adapter=interrupted_logs,
    )
    with pytest.raises(SystemExit):
        jobs.run_execution_once(first_runtime.execute_job)

    checkpoint = jobs.get("diagnosis-checkpoint-1")["loop_checkpoint"]  # type: ignore[index]
    assert checkpoint["completion_state"] == "tool_in_flight"  # type: ignore[index]
    assert checkpoint["model_turn"] == 0  # type: ignore[index]
    assert checkpoint["remaining_budget"] == {"model_turns": 6, "tool_calls": 22}  # type: ignore[index]
    assert checkpoint["evidence_references"] == ["evidence:metrics:1"]  # type: ignore[index]

    clock.now += 2
    WallClock.now_value += timedelta(minutes=30)
    reopened = DiagnosisJobs(
        db_path,
        clock=clock,
        execution_lease_seconds=1,
        max_execution_attempts=2,
    )

    async def must_not_repeat(_args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("a checkpointed tool call must not execute again")

    provider = _final_provider("evidence:metrics:1", expect_reconciliation=True)
    resumed_runtime = _runtime(
        reopened,
        provider,
        metrics_adapter=must_not_repeat,
        logs_adapter=must_not_repeat,
    )

    assert reopened.run_execution_once(resumed_runtime.execute_job) is True
    assert calls == {"metrics": 1, "logs": 1}
    assert provider.calls == 1
    result = reopened.export("diagnosis-checkpoint-1")
    assert result is not None
    assert result["status"] in {"partial", "needs_human"}
    unknown = next(
        item
        for item in result["tool_activity"]
        if item["tool"] == "query_logs"  # type: ignore[index]
    )
    assert unknown["status"] == "failed"
    assert "reconciliation required" in unknown["summary"]


def test_completed_loop_checkpoint_finishes_job_without_repeating_model_or_tools(
    tmp_path: Path,
) -> None:
    clock = Clock()
    db_path = tmp_path / "diagnosis.db"
    jobs = DiagnosisJobs(
        db_path,
        clock=clock,
        execution_lease_seconds=1,
        max_execution_attempts=2,
    )
    jobs.accept(_request(), provider_revision="model-provider:revision-1")
    calls = {"tools": 0}

    async def evidence(_args: dict[str, Any]) -> dict[str, Any]:
        calls["tools"] += 1
        return {
            "status": "succeeded",
            "summary": "evidence collected",
            "data": {"error_rate": 0.42},
            "evidence_refs": [{"ref_id": "evidence:metrics:1"}],
        }

    provider = _final_provider("evidence:metrics:1")
    runtime = _runtime(
        jobs,
        provider,
        metrics_adapter=evidence,
        logs_adapter=evidence,
    )

    def stop_after_safe_completion(payload: dict[str, object]) -> dict[str, object]:
        runtime.execute_job(payload)
        raise SystemExit("process stopped before Diagnosis Job terminal write")

    with pytest.raises(SystemExit):
        jobs.run_execution_once(stop_after_safe_completion)
    assert jobs.get("diagnosis-checkpoint-1")["loop_checkpoint"]["completion_state"] == "completed"  # type: ignore[index]

    clock.now += 2
    reopened = DiagnosisJobs(
        db_path,
        clock=clock,
        execution_lease_seconds=1,
        max_execution_attempts=2,
    )
    resumed_runtime = _runtime(
        reopened,
        _final_provider("evidence:metrics:1"),
        metrics_adapter=lambda _args: pytest.fail("tool repeated"),
        logs_adapter=lambda _args: pytest.fail("tool repeated"),
    )

    assert reopened.run_execution_once(resumed_runtime.execute_job) is True
    assert reopened.get("diagnosis-checkpoint-1")["status"] == "completed"  # type: ignore[index]
    assert calls["tools"] == 2
    assert provider.calls == 1


def test_recovery_replays_checkpointed_model_proposal_without_recalling_model(
    tmp_path: Path,
) -> None:
    clock = Clock()
    db_path = tmp_path / "diagnosis.db"
    jobs = DiagnosisJobs(
        db_path,
        clock=clock,
        execution_lease_seconds=1,
        max_execution_attempts=2,
    )
    jobs.accept(_request(), provider_revision="model-provider:revision-1")
    calls = {"metrics": 0, "logs": 0}

    async def metrics(args: dict[str, Any]) -> dict[str, Any]:
        calls["metrics"] += 1
        if str(args.get("query") or "").startswith("ALERTS{"):
            return {
                "status": "succeeded",
                "summary": "firing alert is present",
                "data": {"alert_count": 1},
                "evidence_refs": [{"ref_id": "evidence:metrics:baseline"}],
            }
        raise SystemExit("process stopped during proposed tool call")

    async def logs(_args: dict[str, Any]) -> dict[str, Any]:
        calls["logs"] += 1
        return {
            "status": "succeeded",
            "summary": "logs checked",
            "data": {"timeout_count": 0},
            "evidence_refs": [{"ref_id": "evidence:logs:baseline"}],
        }

    class ProposingProvider:
        calls = 0

        async def chat_with_tools(self, _messages: list[dict[str, Any]], _tools: list[dict[str, Any]]) -> ProviderResult:
            self.calls += 1
            return ProviderResult(
                {"role": "assistant", "content": None, "tool_calls": []},
                [ToolCall("call-detail", "query_metrics", {"query": "sum(rate(errors[5m]))"})],
                "tool_calls",
                {},
            )

    proposing = ProposingProvider()
    runtime = _runtime(
        jobs,
        proposing,
        metrics_adapter=metrics,
        logs_adapter=logs,
    )
    with pytest.raises(SystemExit):
        jobs.run_execution_once(runtime.execute_job)

    checkpoint = jobs.get("diagnosis-checkpoint-1")["loop_checkpoint"]  # type: ignore[index]
    assert checkpoint["model_turn"] == 1  # type: ignore[index]
    assert checkpoint["proposed_tool_calls"][-1]["name"] == "query_metrics"  # type: ignore[index]

    clock.now += 2
    reopened = DiagnosisJobs(
        db_path,
        clock=clock,
        execution_lease_seconds=1,
        max_execution_attempts=2,
    )
    final = _final_provider("evidence:metrics:baseline", expect_reconciliation=True)
    resumed = _runtime(
        reopened,
        final,
        metrics_adapter=lambda _args: pytest.fail("metrics repeated"),
        logs_adapter=lambda _args: pytest.fail("logs repeated"),
    )

    assert reopened.run_execution_once(resumed.execute_job) is True
    assert proposing.calls == 1
    assert final.calls == 1
    assert calls == {"metrics": 2, "logs": 1}
