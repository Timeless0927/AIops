"""Structured contract shared by Change Plan producer and consumer."""

from __future__ import annotations

from .kubernetes_change import KubernetesChangeContractError, validate_draft_kubernetes_change


class ChangePlanningContractError(ValueError):
    pass


def validate_change_planning_result(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ChangePlanningContractError("planning result must be an object")
    status = raw.get("status")
    if status == "needs_input":
        if set(raw) != {"status", "question"}:
            raise ChangePlanningContractError("needs_input result fields are invalid")
        return {"status": status, "question": _text(raw.get("question"), "question", 2000)}
    if status != "validating" or set(raw) != {"status", "plan"}:
        raise ChangePlanningContractError("planning result must be needs_input or validating")
    plan = raw.get("plan")
    if not isinstance(plan, dict) or set(plan) != {"summary", "changes"}:
        raise ChangePlanningContractError("validating result requires a structured plan")
    changes = plan.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 100:
        raise ChangePlanningContractError("plan changes must contain between 1 and 100 items")
    return {
        "status": status,
        "plan": {
            "summary": _text(plan.get("summary"), "plan summary", 2000),
            "changes": [_change(item) for item in changes],
        },
    }


def _change(raw: object) -> dict[str, object]:
    try:
        change = validate_draft_kubernetes_change(raw)
    except KubernetesChangeContractError as exc:
        raise ChangePlanningContractError(f"draft Kubernetes Change is invalid: {exc}") from exc
    if change["operation"] == "delete" and "rollback" not in change:
        raise ChangePlanningContractError(
            "draft Kubernetes Change is invalid: irreversible delete requires concrete rollback loss",
        )
    return change


def _text(value: object, field: str, limit: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > limit:
        raise ChangePlanningContractError(f"{field} is invalid")
    return normalized
