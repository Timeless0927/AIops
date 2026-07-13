"""Shared canonical restart contract tests."""

from __future__ import annotations

import pytest

from aiops.contracts import (
    CONTROLLED_RESTART_ANNOTATION_PATH,
    ChangePlanningContractError,
    validate_change_planning_result,
    validate_controlled_restart_plan,
)
from apps.aiops_k8s_gateway.change_planning_boundary import validate_gateway_plan
from apps.aiops_k8s_gateway.change_requests import ChangeRequestError


def _restart_result(*, value: str = "change-1") -> dict[str, object]:
    return validate_change_planning_result({
        "status": "validating",
        "plan": {
            "summary": "Restart through a controlled rollout",
            "changes": [{
                "target": {
                    "api_version": "apps/v1", "kind": "Deployment",
                    "namespace": "payments", "name": "checkout-api",
                },
                "operation": "patch",
                "payload": [{
                    "op": "add", "path": CONTROLLED_RESTART_ANNOTATION_PATH,
                    "value": value,
                }],
                "post_checks": [
                    {
                        "type": "json_pointer",
                        "path": CONTROLLED_RESTART_ANNOTATION_PATH,
                        "operator": "eq", "value": value,
                    },
                    {"type": "workload_rollout"},
                ],
            }],
        },
    })


def test_restart_value_is_bound_to_change_request_identity() -> None:
    with pytest.raises(ChangePlanningContractError, match="canonical annotation"):
        validate_controlled_restart_plan(
            "change-1", "controlled_restart", _restart_result(value="another-change"),
        )


def test_non_restart_generic_change_is_unchanged() -> None:
    result = _restart_result(value="another-change")
    assert validate_controlled_restart_plan(
        "change-1", "generic", result,
    ) is result


def test_free_text_neither_false_positives_nor_bypasses_structured_intent() -> None:
    result = _restart_result(value="another-change")
    payload = {
        "change_request_id": "change-1",
        "desired_outcome": "stop restart loops by raising memory",
        "facts": {"change_intent": "generic"},
    }
    assert validate_gateway_plan(payload, result) == result

    payload["desired_outcome"] = "roll out new pods"
    payload["facts"] = {"change_intent": "controlled_restart"}
    with pytest.raises(ChangeRequestError, match="canonical annotation") as caught:
        validate_gateway_plan(payload, result)
    assert caught.value.code == "invalid_plan"
