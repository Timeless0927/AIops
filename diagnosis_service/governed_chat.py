"""Knowledge and environment Chat profiles over the governed Diagnosis loop."""

from __future__ import annotations

import json
from typing import Any

from aiops.contracts.governed_tools import capability_binding
from diagnosis_service.loop_checkpoint import GovernedLoopCheckpoint
from toolsets import incident_diagnosis as core
from toolsets.diagnosis_session import run_diagnosis_session


JSON = dict[str, Any]


class GovernedChatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def answer_governed_chat(
    request: JSON,
    *,
    provider: Any,
    checkpoints: Any,
    adapters: dict[str, Any],
    max_turns: int = 6,
) -> JSON:
    accepted = _request(request)
    request_id = accepted["request_id"]
    checkpoints.accept(request_id, accepted)
    checkpoint = GovernedLoopCheckpoint(checkpoints, request_id, max_turns=max(1, max_turns))
    completed = checkpoint.completed_result()
    if completed is not None:
        return completed
    if accepted.get("scope") is None:
        result = await _knowledge(accepted, checkpoint.provider(provider))
    else:
        result = await _environment(
            accepted,
            checkpoint.provider(provider),
            {name: checkpoint.adapter(name, adapter) for name, adapter in adapters.items()},
            max_turns=max_turns,
        )
    checkpoint.complete(result)
    return result


async def _knowledge(request: JSON, provider: Any) -> JSON:
    result = await provider.chat_with_tools([
        {
            "role": "system",
            "content": (
                "Answer the AIOps knowledge question without tools. Do not claim current live-environment facts, "
                "request credentials, or imply Approval or execution authority."
            ),
        },
        *request["messages"],
    ], [])
    content = result.message.get("content")
    if result.tool_calls or not isinstance(content, str) or not content.strip():
        raise GovernedChatError("invalid_model_response", "knowledge model response is invalid")
    return {
        "mode": "knowledge",
        "answer": content.strip(),
        "scope": None,
        "tool_activity": [],
        "evidence_references": [],
        "uncertainty": None,
        "next_step": None,
        "completion": {"status": "completed", "stopping_reason": "knowledge_answered"},
    }


async def _environment(
    request: JSON,
    provider: Any,
    adapters: dict[str, Any],
    *,
    max_turns: int,
) -> JSON:
    scope = request["scope"]
    resources = scope["resources"]
    first = resources[0]
    context = {
        "session_id": request["request_id"],
        "summary": request["messages"][-1]["content"],
        "chat_question": request["messages"][-1]["content"],
        "authorized_scope": scope,
        **_resource_context(first, scope),
    }

    def authorize(tool: str, args: JSON, evidence_refs: list[JSON]) -> tuple[JSON, str | None]:
        binding, denied = capability_binding(tool, request["capabilities"])
        target_id = str(args.get("deployment_target_id") or "")
        if not target_id and len(resources) == 1:
            target_id = str(first["deployment_target_id"])
        selected = next(
            (item for item in resources if item["deployment_target_id"] == target_id), None,
        )
        if denied is None and selected is None:
            denied = "tool request widens or does not narrow the frozen resource scope"
        if denied is not None:
            return {
                "request_id": f"{request['request_id']}:{tool}",
                "deployment_target_id": str(first["deployment_target_id"]),
                **_resource_context(first, scope),
            }, denied
        safe_overrides = {
            key: args[key]
            for key in ({"step"} if tool == "query_metrics" else {"max_lines"} if tool == "query_logs" else set())
            if key in args
        }
        authorized = core.build_tool_arguments(
            tool, _resource_context(selected, scope), evidence_refs, safe_overrides,
        )
        authorized.update({
            "request_id": f"{request['request_id']}:{tool}",
            "correlation_id": request["request_id"],
            "deployment_target_id": selected["deployment_target_id"],
            "service_id": selected["service_id"],
            "workload_kind": selected["workload_kind"],
            "workload_name": selected["workload_name"],
            "_mcp": binding,
        })
        return authorized, None

    session = await run_diagnosis_session(
        context,
        metrics_adapter=adapters.get("query_metrics"),
        logs_adapter=adapters.get("query_logs"),
        k8s_read_adapter=adapters.get("run_k8s_read"),
        topology_adapter=adapters.get("get_service_topology"),
        provider=provider,
        incident_store=False,
        max_turns=max(1, max_turns),
        profile="environment_chat",
        tool_authorizer=authorize,
    )
    return _project_environment(session, scope)


def _project_environment(session: JSON, scope: JSON) -> JSON:
    observations = session.get("steps") if isinstance(session.get("steps"), list) else []
    refs = list(dict.fromkeys(
        str(item["evidence_ref"])
        for item in observations
        if isinstance(item, dict)
        and item.get("status") in {"succeeded", "partial"}
        and item.get("evidence_ref")
    ))
    validation = session.get("completion_validation")
    validation = dict(validation) if isinstance(validation, dict) else {
        "status": "safe_partial", "stopping_reason": "validation_missing", "issues": ["validation unavailable"],
    }
    diagnosis = session.get("diagnosis")
    candidates = diagnosis.get("root_cause_candidates") if isinstance(diagnosis, dict) else None
    candidate = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
    answer = str(candidate.get("cause") or "").strip()
    if not refs:
        answer = "无法取得授权的实时 Observation，不能确认当前环境状态。"
    next_checks = candidate.get("next_checks") if isinstance(candidate, dict) else None
    return {
        "mode": "environment",
        "answer": answer or "已取得部分 Observation，但无法形成经过校验的环境结论。",
        "scope": scope,
        "tool_activity": list(session.get("tool_activity") or []),
        "evidence_references": refs,
        "uncertainty": {
            "status": validation.get("status"),
            "reasons": list(validation.get("issues") or []),
        },
        "next_step": (
            str(next_checks[0])
            if isinstance(next_checks, list) and next_checks
            else "根据已接受的 Observation 继续只读核对。"
        ),
        "completion": validation,
    }


def _resource_context(resource: JSON, scope: JSON) -> JSON:
    return {
        "cluster_id": resource["cluster_id"],
        "namespace": resource["namespace"],
        "service": resource["service_name"],
        "service_id": resource["service_id"],
        "workload_kind": resource["workload_kind"],
        "workload_name": resource["workload_name"],
        "time_range": scope["time_range"],
    }


def _request(value: JSON) -> JSON:
    if set(value) - {"request_id", "messages", "scope", "capabilities"}:
        raise GovernedChatError("invalid_request", "Chat execution request is invalid")
    request_id = value.get("request_id")
    messages = value.get("messages")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
        raise GovernedChatError("invalid_request", "request_id is required")
    if not isinstance(messages, list) or not messages or len(messages) > 100:
        raise GovernedChatError("invalid_messages", "messages are required")
    accepted_messages = []
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message.get("role") not in {"user", "assistant"}
            or not isinstance(message.get("content"), str)
            or not message["content"].strip()
            or len(message["content"]) > 16_000
        ):
            raise GovernedChatError("invalid_messages", "messages are invalid")
        accepted_messages.append({"role": message["role"], "content": message["content"].strip()})
    accepted: JSON = {"request_id": request_id.strip(), "messages": accepted_messages}
    scope = value.get("scope")
    if scope is not None:
        if not _valid_scope(scope) or not isinstance(value.get("capabilities"), dict):
            raise GovernedChatError("invalid_scope", "environment Chat scope is invalid")
        accepted.update({
            "scope": json.loads(json.dumps(scope, ensure_ascii=False)),
            "capabilities": json.loads(json.dumps(value["capabilities"], ensure_ascii=False)),
        })
    return accepted


def _valid_scope(scope: object) -> bool:
    selection_fields = {"cluster_id", "namespace", "service_id", "deployment_target_id", "incident_id"}
    resource_fields = {
        "deployment_target_id", "cluster_id", "namespace", "service_id", "service_name",
        "workload_kind", "workload_name",
    }
    selection = scope.get("selection") if isinstance(scope, dict) else None
    return (
        isinstance(scope, dict)
        and set(scope) == {"selection", "resources", "time_range", "revision"}
        and isinstance(selection, dict)
        and bool(selection)
        and not set(selection) - selection_fields
        and all(isinstance(value, str) and 0 < len(value.strip()) <= 200 for value in selection.values())
        and isinstance(scope.get("revision"), str)
        and len(scope["revision"]) == 64
        and all(character in "0123456789abcdef" for character in scope["revision"])
        and isinstance(scope.get("resources"), list)
        and 0 < len(scope["resources"]) <= 200
        and all(
            isinstance(item, dict)
            and set(item) == resource_fields
            and all(isinstance(item.get(field), str) and item[field] for field in resource_fields)
            and all(field == "incident_id" or item.get(field) == expected for field, expected in selection.items())
            for item in scope["resources"]
        )
        and scope.get("time_range") == {"type": "relative", "value": "30m"}
    )
