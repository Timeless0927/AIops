"""Structured contract shared by Change Plan producer and consumer."""

from __future__ import annotations


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
    if not isinstance(raw, dict) or set(raw) != {"target", "desired_state", "post_check"}:
        raise ChangePlanningContractError("draft change fields are invalid")
    target = raw.get("target")
    if not isinstance(target, dict) or set(target) != {"api_version", "kind", "namespace", "name"}:
        raise ChangePlanningContractError("draft target fields are invalid")
    namespace = target.get("namespace")
    if namespace is not None and not isinstance(namespace, str):
        raise ChangePlanningContractError("target namespace must be a string or null")
    return {
        "target": {
            "api_version": _text(target.get("api_version"), "target api_version", 200),
            "kind": _text(target.get("kind"), "target kind", 200),
            "namespace": _optional_text(namespace, "target namespace", 253) or None,
            "name": _text(target.get("name"), "target name", 253),
        },
        "desired_state": _text(raw.get("desired_state"), "desired state", 4000),
        "post_check": _text(raw.get("post_check"), "post-check", 2000),
    }


def _text(value: object, field: str, limit: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > limit:
        raise ChangePlanningContractError(f"{field} is invalid")
    return normalized


def _optional_text(value: object, field: str, limit: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if len(normalized) > limit:
        raise ChangePlanningContractError(f"{field} is invalid")
    return normalized
