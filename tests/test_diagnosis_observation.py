"""Public contract tests for bounded Diagnosis Observation feedback."""

from __future__ import annotations

import json
from typing import Any

from diagnosis_service.diagnosis_provider import ProviderResult, ToolCall
from toolsets.diagnosis_session import run_diagnosis_session


def _incident() -> dict[str, str]:
    return {
        "incident_id": "observation-contract-1",
        "session_id": "session-observation-contract-1",
        "alert_name": "HighErrorRate",
        "summary": "checkout error rate is elevated",
        "cluster": "prod-a",
        "namespace": "payments",
        "service": "checkout-api",
    }


def _final() -> ProviderResult:
    return ProviderResult(
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "root_cause_candidates": [
                        {"cause": "upstream timeout", "confidence": 0.8, "evidence_refs": ["ev-metrics"]}
                    ],
                    "recommended_actions": [],
                    "confidence": {"score": 0.8, "level": "medium"},
                }
            ),
        },
        [],
        "stop",
        {},
    )


async def test_next_model_turn_receives_bounded_redacted_observation() -> None:
    calls: list[list[dict[str, Any]]] = []

    async def metrics(_args: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "summary": "metrics contain a rising error rate",
            "data": {
                "series": list(range(200)),
                "labels": {"service": "checkout-api", "authorization": "Bearer raw-token"},
                "password": "must-not-persist",
                "representative": "x" * 6000,
            },
            "evidence_refs": [{"ref_id": "ev-metrics"}],
        }

    class Provider:
        turn = 0

        async def chat_with_tools(self, messages: list[dict[str, Any]], _tools: list[dict[str, Any]]) -> ProviderResult:
            calls.append(messages)
            self.turn += 1
            if self.turn == 1:
                return ProviderResult(
                    {"role": "assistant", "content": None},
                    [ToolCall("call-metrics", "query_metrics", {"query": "rate(query)"})],
                    "tool_calls",
                    {},
                )
            observation = json.loads(messages[-1]["content"])
            assert observation["purpose"]
            assert observation["authorized_scope"]["namespace"] == "payments"
            assert observation["time_range"]
            assert observation["status"] == "succeeded"
            assert observation["key_facts"]
            assert observation["representative_samples"]
            assert observation["evidence_ref"] == "ev-metrics"
            assert observation["redaction"]["applied"] is True
            assert observation["truncation"]["truncated"] is True
            encoded = messages[-1]["content"].encode("utf-8")
            assert len(encoded) <= 4096
            assert "must-not-persist" not in messages[-1]["content"]
            assert "Bearer raw-token" not in messages[-1]["content"]
            assert "series" not in observation
            return _final()

    session = await run_diagnosis_session(
        _incident(),
        metrics_adapter=metrics,
        provider=Provider(),
    )

    assert session["steps"][0]["key_facts"]
    assert session["tool_activity"][0]["evidence_ref"] is not None
    serialized_session = json.dumps(session, ensure_ascii=False)
    assert "must-not-persist" not in serialized_session
    assert "Bearer raw-token" not in serialized_session


async def test_failed_source_keeps_successful_facts_and_projects_partial() -> None:
    async def metrics(_args: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "summary": "metrics show 5xx increase",
            "data": {"error_rate": 0.42},
            "evidence_refs": [{"ref_id": "ev-metrics"}],
        }

    async def logs(_args: dict[str, Any]) -> dict[str, Any]:
        raise TimeoutError("loki unavailable")

    class Provider:
        async def chat_with_tools(self, messages: list[dict[str, Any]], _tools: list[dict[str, Any]]) -> ProviderResult:
            baseline = [json.loads(item["content"].split("：", 1)[-1]) for item in messages if item["role"] == "user" and "基线查询" in item["content"]]
            assert any(item["status"] == "succeeded" and item["key_facts"] for item in baseline)
            failed = next(item for item in baseline if item["status"] == "failed")
            assert failed["evidence_ref"] is None
            assert failed["missing_reason"]
            return _final()

    session = await run_diagnosis_session(
        _incident(),
        metrics_adapter=metrics,
        logs_adapter=logs,
        provider=Provider(),
    )

    assert session["status"] == "partial"
    assert any(step["evidence_ref"] == "ev-metrics" for step in session["steps"])
    assert any(activity["status"] == "failed" for activity in session["tool_activity"])


async def test_empty_and_skipped_sources_are_structured_observations() -> None:
    async def empty_metrics(_args: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "succeeded",
            "summary": "metrics query returned no samples",
            "data": {"series": []},
            "evidence_refs": [{"ref_id": "ev-empty"}],
        }

    class Provider:
        async def chat_with_tools(self, messages: list[dict[str, Any]], _tools: list[dict[str, Any]]) -> ProviderResult:
            baseline = [
                json.loads(item["content"].split("：", 1)[-1])
                for item in messages
                if item["role"] == "user" and "基线查询" in item["content"]
            ]
            assert {item["status"] for item in baseline} == {"partial", "skipped"}
            assert all(item["purpose"] and "truncation" in item and "redaction" in item for item in baseline)
            return _final()

    session = await run_diagnosis_session(_incident(), metrics_adapter=empty_metrics, provider=Provider())

    assert session["status"] == "partial"
    assert {item["status"] for item in session["tool_activity"]} >= {"partial", "skipped"}
