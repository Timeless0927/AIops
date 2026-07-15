"""Structured contract shared by Change Plan producer and consumer."""

from __future__ import annotations

import re

from .kubernetes_change import (
    CONTROLLED_RESTART_ANNOTATION_PATH,
    CONTROLLED_VERIFICATION_ANNOTATION_PATH,
    KubernetesChangeContractError,
    validate_draft_kubernetes_change,
)


_VERIFICATION_RUN_ID = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,62}[a-z0-9])?$")


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


def validate_controlled_restart_plan(
    change_request_id: str,
    change_intent: object,
    result: dict[str, object],
) -> dict[str, object]:
    """Require the one canonical Generic Change when User intent is a restart."""

    if change_intent != "controlled_restart":
        return result
    if result.get("status") == "needs_input":
        return result
    plan = result.get("plan")
    changes = plan.get("changes") if isinstance(plan, dict) else None
    if not isinstance(changes, list) or len(changes) != 1:
        raise ChangePlanningContractError(
            "controlled restart requires exactly one canonical Deployment change",
        )
    change = changes[0]
    target = change.get("target") if isinstance(change, dict) else None
    patch = change.get("payload") if isinstance(change, dict) else None
    annotation_path = CONTROLLED_RESTART_ANNOTATION_PATH
    annotation_value = change_request_id
    if (
        isinstance(patch, list)
        and len(patch) == 1
        and isinstance(patch[0], dict)
        and patch[0].get("path") == CONTROLLED_VERIFICATION_ANNOTATION_PATH
        and isinstance(patch[0].get("value"), str)
        and _VERIFICATION_RUN_ID.fullmatch(str(patch[0]["value"])) is not None
    ):
        annotation_path = CONTROLLED_VERIFICATION_ANNOTATION_PATH
        annotation_value = str(patch[0]["value"])
    expected_patch = [{"op": "add", "path": annotation_path, "value": annotation_value}]
    expected_annotation_check = {
        "type": "json_pointer",
        "path": annotation_path,
        "operator": "eq",
        "value": annotation_value,
    }
    checks = change.get("post_checks") if isinstance(change, dict) else None
    rollback = change.get("rollback") if isinstance(change, dict) else None
    if (
        not isinstance(target, dict)
        or target.get("api_version") != "apps/v1"
        or target.get("kind") != "Deployment"
        or change.get("operation") != "patch"
        or change.get("payload") != expected_patch
        or not isinstance(checks, list)
        or len(checks) != 2
        or expected_annotation_check not in checks
        or {"type": "workload_rollout"} not in checks
        or not isinstance(rollback, dict)
        or rollback.get("status") != "unavailable"
    ):
        raise ChangePlanningContractError(
            "controlled restart must use the canonical annotation patch and post-checks",
        )
    return result


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
