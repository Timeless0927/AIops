"""Recommendation normalization and rendering for Incident diagnosis."""

from __future__ import annotations

from typing import Any


MUTATION_KEYWORDS = {
    "apply",
    "delete",
    "exec",
    "patch",
    "restart",
    "rollback",
    "scale",
    "write",
}


def normalize_recommendations(
    actions: list[dict[str, Any]],
    level: str,
) -> list[dict[str, Any]]:
    if not actions:
        actions = [{"summary": "Collect missing read-only evidence before remediation.", "action_type": "read"}]

    normalized = []
    for action in actions:
        summary = str(action.get("summary") or action.get("description") or "")
        action_type = str(action.get("action_type") or action.get("type") or "read").lower()
        mutates = bool(action.get("mutates", False)) or action_type in {"mutation", "k8s_write", "write"}
        mutates = mutates or any(keyword in summary.lower() for keyword in MUTATION_KEYWORDS)
        normalized.append(
            {
                "summary": summary,
                "action_type": action_type,
                "approval_required": bool(action.get("approval_required", False)) or mutates,
                "execute_automatically": False,
                "allowed_with_confidence": level != "low" or not mutates,
            }
        )
    return normalized


def output_instruction() -> str:
    return (
        '"recommended_actions":[{"summary":"<text>",'
        '"action_type":"read|k8s_write|mutation",'
        '"approval_required":<bool>}]'
    )


def render_recommendations(actions: list[dict[str, Any]]) -> list[str]:
    return [
        f"- [{'approval required' if action['approval_required'] else 'read-only'}] {action['summary']}"
        for action in actions
    ]
