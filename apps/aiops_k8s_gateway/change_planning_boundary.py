"""Gateway trust boundary for Diagnosis-produced Change Plans."""

from __future__ import annotations

from aiops.contracts import (
    ChangePlanningContractError,
    validate_change_planning_result,
    validate_controlled_restart_plan,
)

from .change_requests import ChangeRequestError


def validate_gateway_plan(
    planning_payload: dict[str, object],
    response: object,
) -> dict[str, object]:
    """Validate the producer response against Gateway-owned structured intent."""

    facts = planning_payload.get("facts")
    change_intent = facts.get("change_intent") if isinstance(facts, dict) else None
    try:
        result = validate_change_planning_result(response)
        return validate_controlled_restart_plan(
            str(planning_payload.get("change_request_id") or ""),
            change_intent,
            result,
        )
    except ChangePlanningContractError as exc:
        raise ChangeRequestError(
            "invalid_plan", f"Diagnosis planning response is invalid: {exc}",
        ) from exc
