"""Diagnosis Change Plan model adapter tests."""

from __future__ import annotations

import json

import pytest

from diagnosis_service.change_planner import ChangePlannerError, plan_change_request
from diagnosis_service.diagnosis_provider import ScriptedProvider


def _payload() -> dict[str, object]:
    return {
        "change_request_id": "change-1",
        "incident_id": "incident-1",
        "desired_outcome": "恢复 checkout-api",
        "context": "最近发布可能引入回归",
        "facts": {
            "incident": {"id": "incident-1", "severity": "critical"},
            "resource": {
                "cluster_id": "cluster-prod",
                "namespace": "payments",
                "workload_kind": "Deployment",
                "workload_name": "checkout-api",
            },
            "evidence_steps": [],
        },
        "inputs": [],
    }


@pytest.mark.asyncio
async def test_model_returns_one_blocking_question_without_plan() -> None:
    provider = ScriptedProvider(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"status": "needs_input", "question": "恢复到哪个稳定版本？"}),
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        ]
    )

    result = await plan_change_request(_payload(), provider)

    assert result == {"status": "needs_input", "question": "恢复到哪个稳定版本？"}
    messages = provider.messages_history[0]
    assert messages[0]["role"] == "system"
    assert "one blocking question" in messages[0]["content"]
    assert json.loads(messages[1]["content"])["facts"]["resource"]["workload_name"] == "checkout-api"
    assert "api_key" not in messages[1]["content"]


@pytest.mark.asyncio
async def test_model_must_return_json_without_tool_calls() -> None:
    provider = ScriptedProvider(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "not json",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "kubectl", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        ]
    )

    with pytest.raises(ChangePlannerError, match="must not call tools") as caught:
        await plan_change_request(_payload(), provider)

    assert caught.value.code == "invalid_plan"
