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


@pytest.mark.asyncio
async def test_model_output_is_fully_validated_before_crossing_diagnosis_boundary() -> None:
    provider = ScriptedProvider(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "status": "validating",
                                    "plan": {"summary": "unsafe", "changes": [{"target": "not-an-object"}]},
                                    "reasoning": "must not cross the boundary",
                                }
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        ]
    )

    with pytest.raises(ChangePlannerError, match="invalid") as caught:
        await plan_change_request(_payload(), provider)

    assert caught.value.code == "invalid_plan"


@pytest.mark.asyncio
async def test_model_returns_typed_change_without_live_precondition_guesses() -> None:
    plan = {
        "status": "validating",
        "plan": {
            "summary": "扩容 checkout-api",
            "changes": [{
                "target": {
                    "api_version": "apps/v1", "kind": "Deployment", "namespace": "payments", "name": "checkout-api",
                },
                "operation": "patch",
                "payload": [{"op": "replace", "path": "/spec/replicas", "value": 5}],
                "post_checks": [
                    {"type": "json_pointer", "path": "/spec/replicas", "operator": "eq", "value": 5},
                ],
            }],
        },
    }
    provider = ScriptedProvider([{
        "choices": [{"message": {"role": "assistant", "content": json.dumps(plan)}, "finish_reason": "stop"}],
    }])

    result = await plan_change_request(_payload(), provider)

    assert result == plan
    system_prompt = provider.messages_history[0][0]["content"]
    assert "RFC 6902" in system_prompt
    assert "UID/resourceVersion guesses" in system_prompt


@pytest.mark.asyncio
async def test_model_plans_controlled_restart_as_canonical_annotation_patch() -> None:
    payload = _payload()
    payload["desired_outcome"] = "Restart checkout-api through a controlled rollout"
    payload["facts"]["change_intent"] = "controlled_restart"  # type: ignore[index]
    annotation_path = "/spec/template/metadata/annotations/aiops.dev~1restart-request-id"
    plan = {
        "status": "validating",
        "plan": {
            "summary": "Restart checkout-api through a pod-template annotation rollout",
            "changes": [{
                "target": {
                    "api_version": "apps/v1", "kind": "Deployment",
                    "namespace": "payments", "name": "checkout-api",
                },
                "operation": "patch",
                "payload": [{"op": "add", "path": annotation_path, "value": "change-1"}],
                "post_checks": [
                    {
                        "type": "json_pointer", "path": annotation_path,
                        "operator": "eq", "value": "change-1",
                    },
                    {"type": "workload_rollout"},
                ],
            }],
        },
    }
    provider = ScriptedProvider([{
        "choices": [{"message": {"role": "assistant", "content": json.dumps(plan)}, "finish_reason": "stop"}],
    }])

    assert await plan_change_request(payload, provider) == plan
    prompt = provider.messages_history[0][0]["content"]
    assert annotation_path in prompt
    assert "Never emit a typed restart action" in prompt


@pytest.mark.asyncio
async def test_model_cannot_return_a_noncanonical_restart_patch() -> None:
    payload = _payload()
    payload["desired_outcome"] = "Roll out new checkout-api pods"
    payload["facts"]["change_intent"] = "controlled_restart"  # type: ignore[index]
    provider = ScriptedProvider([{
        "choices": [{
            "message": {
                "role": "assistant",
                "content": json.dumps({
                    "status": "validating",
                    "plan": {
                        "summary": "Scale to force a restart",
                        "changes": [{
                            "target": {
                                "api_version": "apps/v1", "kind": "Deployment",
                                "namespace": "payments", "name": "checkout-api",
                            },
                            "operation": "patch",
                            "payload": [{
                                "op": "replace", "path": "/spec/replicas", "value": 0,
                            }],
                            "post_checks": [{
                                "type": "json_pointer", "path": "/spec/replicas",
                                "operator": "eq", "value": 0,
                            }],
                        }],
                    },
                }),
            },
            "finish_reason": "stop",
        }],
    }])

    with pytest.raises(ChangePlannerError, match="canonical annotation") as caught:
        await plan_change_request(payload, provider)

    assert caught.value.code == "invalid_plan"
