"""Focused Recommendation guidance contract tests."""

from __future__ import annotations

import pytest

from toolsets.recommendations import (
    normalize_recommendations,
    output_instruction,
    render_recommendations,
)


def test_executable_fields_are_rejected() -> None:
    with pytest.raises(ValueError, match="guidance"):
        normalize_recommendations([{
            "summary": "Patch deployment env after approval",
            "action_type": "mutation",
        }])


def test_text_never_gains_implicit_execution_authority() -> None:
    recommendations = normalize_recommendations([{
        "summary": "kubectl scale deployment checkout-api to 0 then 3",
    }])

    assert recommendations == [{
        "summary": "kubectl scale deployment checkout-api to 0 then 3",
    }]
    assert render_recommendations(recommendations) == [
        "- kubectl scale deployment checkout-api to 0 then 3",
    ]
    assert "action_type" not in output_instruction()
    assert "approval_required" not in output_instruction()


def test_structured_change_intent_is_bounded_guidance() -> None:
    assert normalize_recommendations([{
        "summary": "Restart through a controlled rollout",
        "change_intent": "controlled_restart",
    }])[0]["change_intent"] == "controlled_restart"
    with pytest.raises(ValueError, match="change_intent"):
        normalize_recommendations([{
            "summary": "Restart through a controlled rollout",
            "change_intent": "restart_now",
        }])
