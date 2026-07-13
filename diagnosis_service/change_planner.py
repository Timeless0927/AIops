"""Model-only planning for Gateway-owned Change Requests."""

from __future__ import annotations

import json
from typing import Any

from aiops.contracts import (
    ChangePlanningContractError,
    validate_change_planning_result,
    validate_controlled_restart_plan,
)


_SYSTEM_PROMPT = """You plan exact Kubernetes changes from sanitized AIOps facts.
Return JSON only. If target, desired state, scope, or post-check is ambiguous, return exactly
{"status":"needs_input","question":"one blocking question"} and no plan.
Otherwise return {"status":"validating","plan":{"summary":"...","changes":[{"target":{"api_version":"...","kind":"...","namespace":null,"name":"..."},"operation":"create|patch|delete","payload":{},"post_checks":[{"type":"exists"}],"rollback":{"status":"available"}}]}}. Use an opaque {{secure-input:...}} placeholder exactly as supplied; never infer its value. When reliable rollback is impossible, set rollback to {"status":"unavailable","concrete_loss":"specific permanent effect"}.
For create, payload is one complete JSON object whose identity exactly matches target. For patch, payload is an RFC 6902 array using only add/remove/replace; do not add precondition tests because Connector freezes them from live state. For delete, payload is {"propagation_policy":"Foreground|Background|Orphan"}. Use structured Kubernetes post-checks only: exists, absent, json_pointer, condition, observed_generation, workload_rollout, job_terminal, or crd_established. Ask one blocking question at a time. Do not call tools, emit YAML, shell, free-form kubectl, credentials, reasoning traces, UID/resourceVersion guesses, or execution authority.
For a controlled Deployment restart, use only an RFC 6902 add at /spec/template/metadata/annotations/aiops.dev~1restart-request-id with change_request_id as its string value. Verify both that exact annotation with a json_pointer post-check and the Deployment with workload_rollout. Never emit a typed restart action or rollout command."""


class ChangePlannerError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def plan_change_request(payload: dict[str, object], provider: Any) -> dict[str, object]:
    if provider is None:
        raise ChangePlannerError("provider_unavailable", "Model Provider is not configured")
    _validate_payload(payload)
    turn = await provider.chat_with_tools(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
        ],
        [],
    )
    if turn.tool_calls:
        raise ChangePlannerError("invalid_plan", "Change planning model must not call tools")
    content = turn.message.get("content")
    if not isinstance(content, str):
        raise ChangePlannerError("invalid_plan", "Change planning model must return JSON content")
    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ChangePlannerError("invalid_plan", "Change planning model returned invalid JSON") from exc
    try:
        validated = validate_change_planning_result(result)
        facts = payload["facts"]
        assert isinstance(facts, dict)
        return validate_controlled_restart_plan(
            str(payload["change_request_id"]),
            facts.get("change_intent"),
            validated,
        )
    except ChangePlanningContractError as exc:
        raise ChangePlannerError("invalid_plan", f"Change planning model returned invalid data: {exc}") from exc


def _validate_payload(payload: dict[str, object]) -> None:
    required = {"change_request_id", "incident_id", "desired_outcome", "context", "facts", "inputs"}
    if set(payload) != required:
        raise ChangePlannerError("invalid_request", "Change planning request fields are invalid")
    if not all(isinstance(payload[field], str) for field in ("change_request_id", "incident_id", "desired_outcome", "context")):
        raise ChangePlannerError("invalid_request", "Change planning request text fields are invalid")
    if not isinstance(payload["facts"], dict) or not isinstance(payload["inputs"], list):
        raise ChangePlannerError("invalid_request", "Change planning facts and inputs are invalid")
