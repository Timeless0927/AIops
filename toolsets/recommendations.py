"""Evidence-grounded Recommendation guidance for Incident diagnosis."""

from __future__ import annotations

from typing import Any


_EXECUTABLE_FIELDS = frozenset({
    "action_type",
    "approval_required",
    "execute_automatically",
    "mutates",
    "parameters",
    "rollback_plan",
    "type",
})


def normalize_recommendations(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not actions:
        return [{"summary": "Collect missing read-only evidence before proposing a Change Request."}]
    normalized = []
    for action in actions:
        if set(action) & _EXECUTABLE_FIELDS:
            raise ValueError("Recommended Actions are guidance and cannot contain executable fields")
        summary = _text(action.get("summary"), "summary")
        recommendation: dict[str, Any] = {"summary": summary}
        if "change_intent" in action:
            intent = _text(action["change_intent"], "change_intent")
            if intent not in {"generic", "controlled_restart"}:
                raise ValueError("Recommendation change_intent is unsupported")
            recommendation["change_intent"] = intent
        for field in ("id", "action_proposal_id"):
            if field in action:
                recommendation[field] = _text(action[field], field)
        for field in ("evidence_step_ids", "safeguards"):
            if field in action:
                recommendation[field] = _texts(action[field], field)
        normalized.append(recommendation)
    return normalized


def output_instruction() -> str:
    return (
        '"recommended_actions":[{"summary":"<desired operational outcome>",'
        '"change_intent":"generic|controlled_restart",'
        '"evidence_step_ids":["<evidence step id>"],'
        '"safeguards":["<planning safeguard>"]}]'
    )


def render_recommendations(actions: list[dict[str, Any]]) -> list[str]:
    return [f"- {action['summary']}" for action in actions]


def _text(value: object, field: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > 4000:
        raise ValueError(f"Recommendation {field} must be a non-empty string")
    return text


def _texts(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError(f"Recommendation {field} must be a bounded list")
    result = [_text(item, field) for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"Recommendation {field} must not contain duplicates")
    return result
