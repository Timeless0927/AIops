"""Model-only planning for Gateway-owned Change Requests."""

from __future__ import annotations

import json
from typing import Any


_SYSTEM_PROMPT = """You plan Kubernetes changes from sanitized AIOps facts.
Return JSON only. If target, desired state, scope, or post-check is ambiguous, return exactly
{"status":"needs_input","question":"one blocking question"} and no plan.
Otherwise return {"status":"validating","plan":{"summary":"...","changes":[{"target":{"api_version":"...","kind":"...","namespace":null,"name":"..."},"desired_state":"...","post_check":"..."}]}}.
Ask one blocking question at a time. Do not call tools, emit YAML, shell, kubectl, credentials, reasoning traces, or execution authority."""


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
    if not isinstance(result, dict) or result.get("status") not in {"needs_input", "validating"}:
        raise ChangePlannerError("invalid_plan", "Change planning model returned an invalid status")
    return result


def _validate_payload(payload: dict[str, object]) -> None:
    required = {"change_request_id", "incident_id", "desired_outcome", "context", "facts", "inputs"}
    if set(payload) != required:
        raise ChangePlannerError("invalid_request", "Change planning request fields are invalid")
    if not all(isinstance(payload[field], str) for field in ("change_request_id", "incident_id", "desired_outcome", "context")):
        raise ChangePlannerError("invalid_request", "Change planning request text fields are invalid")
    if not isinstance(payload["facts"], dict) or not isinstance(payload["inputs"], list):
        raise ChangePlannerError("invalid_request", "Change planning facts and inputs are invalid")
