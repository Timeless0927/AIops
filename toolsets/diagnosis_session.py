"""LLM tool-use session orchestration for incident diagnosis."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from toolsets.recommendations import output_instruction

# Imported after incident_diagnosis has defined its evidence and rendering helpers.
from toolsets import incident_diagnosis as core


logger = logging.getLogger(__name__)


class ModelResponseError(ValueError):
    code = "invalid_response"
    no_retry = True


@dataclass
class _TooluseAccumulator:
    hard_failure: bool = False
    has_partial_observation: bool = False


_LLM_TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "query_metrics",
            "description": "Query Prometheus metrics for the incident's service over a time window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "start": {"type": "string", "description": "ISO8601 window start"},
                    "end": {"type": "string", "description": "ISO8601 window end"},
                    "step": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_logs",
            "description": "Query Loki logs for the incident's service.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "time_range": {"type": "object"},
                    "max_lines": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_k8s_read",
            "description": "Run a read-only kubectl command against the cluster via the Gateway.",
            "parameters": {
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}},
                    "selector": {"type": "string"},
                    "command": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_service_topology",
            "description": "Retrieve the service dependency topology for the incident's service.",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string"}},
            },
        },
    },
]


def _deterministic_clock() -> float:
    return 0.0


def _build_tool_args_from_llm(
    tool: str,
    incident: dict[str, Any],
    llm_args: dict[str, Any],
    evidence_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    return core.build_tool_arguments(tool, incident, evidence_refs, llm_args)


def _build_tooluse_system_prompt(
    incident: dict[str, Any],
    memory_hints: list[dict[str, Any]],
) -> str:
    alert_name = str(incident.get("alert_name") or incident.get("summary") or "incident")
    namespace = str(incident.get("namespace") or "")
    service = str(incident.get("service") or namespace or "")
    lines = [
        "You are the AIOps diagnosis brain. Read the on-the-ground evidence by calling the provided tools, then output a root cause.",
        "Pick the tools and order yourself based on the alert. Each tool call returns evidence; use it to decide the next step.",
        "Never propose an executable mutation as final — any remediation is an action proposal that the human-owned Gateway gates.",
        "",
        f"Alert: {alert_name}",
        f"Namespace: {namespace} | Service: {service}",
    ]
    if memory_hints:
        lines.extend(["", "Similar past incidents (optional leads — do not override on-the-ground evidence):"])
        for hint in memory_hints:
            summary = hint.get("summary") if isinstance(hint, dict) else str(hint)
            if summary:
                lines.append(f"- {summary}")
    lines.extend(
        [
            "",
            "When you have enough evidence, reply with a non-tool message whose content is JSON with: "
            '{"root_cause_candidates":[{"cause":"<text>","category":"<root_cause_category>",'
            '"confidence":<0-1>,"evidence_refs":[...]}],'
            f'{output_instruction()},"confidence":{{"score":<0-1>,"level":"high|medium|low"}}}}',
            "The `category` is a single root-cause-class label (snake_case, e.g. "
            "'resource_pressure_memory','certificate_expiry','connection_pool_exhaustion',"
            "'config_error','upstream_dependency_down','node_not_ready','pvc_disk_full',"
            "'bad_release_deploy','resource_pressure_cpu'). Pick the closest fit; the replay "
            "harness scores against the ground-truth category with tolerance, so a precise "
            "label beats free text. Output 'undifferentiated' only when no specific class fits.",
        ]
    )
    return "\n".join(lines)


def _record_observation_step(
    session: dict[str, Any],
    evidence_refs: list[dict[str, Any]],
    missing_evidence: list[dict[str, Any]],
    collected: core.CollectedToolObservation,
) -> bool:
    observation = collected.observation
    session["steps"].append(observation)
    if collected.evidence is not None:
        evidence_refs.append(collected.evidence)
        return False
    if collected.missing is not None:
        missing_evidence.append(collected.missing)
    return collected.hard_failure


async def _run_llm_tooluse_session(
    incident: dict[str, Any],
    adapters: dict[str, core.ToolAdapter | None],
    provider: Any,
    incident_store: Any | None,
    session_id: str,
    *,
    session: dict[str, Any],
    evidence_refs: list[dict[str, Any]],
    missing_evidence: list[dict[str, Any]],
    state: _TooluseAccumulator,
    max_turns: int,
    clock: Callable[[], float],
) -> dict[str, Any] | None:
    memory_hints = list(incident.get("memory_hints") or [])
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _build_tooluse_system_prompt(incident, memory_hints)},
        {
            "role": "user",
            "content": f"Diagnose incident {incident.get('incident_id') or 'unknown'}: "
            f"{incident.get('summary') or incident.get('alert_name')}",
        },
    ]
    step_index = 0
    for _ in range(max_turns):
        turn_start = clock()
        result = await provider.chat_with_tools(messages, _LLM_TOOL_SCHEMA)
        messages.append(result.message)
        await _record_provider_cost(session_id, result, turn_start, incident_store, clock)
        if not result.tool_calls:
            return _diagnosis_from_llm(result.message.get("content"))
        for call in result.tool_calls:
            args = _build_tool_args_from_llm(call.name, incident, call.arguments, evidence_refs)
            collected = await core.collect_tool_observation(
                call.name,
                args,
                adapters.get(call.name),
                incident,
                incident_store,
            )
            observation = collected.observation
            hard = _record_observation_step(session, evidence_refs, missing_evidence, collected)
            state.hard_failure = state.hard_failure or hard
            state.has_partial_observation = state.has_partial_observation or collected.partial
            await _add_trace_row(incident_store, session_id, step_index, call, observation, result)
            step_index += 1
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(
                        {"status": observation["status"], "summary": observation["summary"]},
                        ensure_ascii=False,
                    ),
                }
            )
    logger.warning("diagnosis LLM tool-use hit max_turns (%d) without a final answer", max_turns)
    return None


async def _add_trace_row(
    incident_store: Any | None,
    session_id: str,
    step_index: int,
    call: Any,
    observation: dict[str, Any],
    result: Any,
) -> None:
    store = core.resolve_incident_store(incident_store)
    add_trace = getattr(store, "add_diagnosis_trace", None)
    if add_trace is None:
        return
    usage = getattr(result, "usage", {}) or {}
    try:
        await add_trace(
            session_id=session_id,
            step_index=step_index,
            tool_name=getattr(call, "name", "") or "",
            tool_args=getattr(call, "arguments", None),
            observation_ref=observation.get("evidence_ref"),
            duration_ms=None,
            model=getattr(result, "model", None) or "",
            input_tokens=usage.get("prompt_tokens") if isinstance(usage, dict) else None,
            output_tokens=usage.get("completion_tokens") if isinstance(usage, dict) else None,
        )
    except ValueError:
        if incident_store is not None:
            raise


async def _record_provider_cost(
    session_id: str,
    result: Any,
    turn_start: float,
    incident_store: Any | None = None,
    clock: Callable[[], float] = _deterministic_clock,
) -> None:
    latency_ms = max(0, int((clock() - turn_start) * 1000))
    usage = getattr(result, "usage", {}) or {}
    input_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    output_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
    model = getattr(result, "model", None) or ""
    if input_tokens is None and output_tokens is None:
        return
    recorder = getattr(core.resolve_incident_store(incident_store), "record_cost", None)
    if recorder is not None:
        try:
            await recorder(
                model=model or "diagnosis-llm",
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                estimated_cost=0.0,
                session_id=session_id,
                latency_ms=latency_ms,
            )
        except Exception:  # pragma: no cover - injected observability is non-fatal
            logger.debug("injected store record_cost skipped", exc_info=True)
        return
    logger.info(
        "diagnosis_provider_usage model=%s input_tokens=%s output_tokens=%s latency_ms=%s session_id=%s",
        model or "diagnosis-llm",
        int(input_tokens or 0),
        int(output_tokens or 0),
        latency_ms,
        session_id,
    )


def _diagnosis_from_llm(content: Any) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise ModelResponseError("empty assistant final content")
    payload = _extract_json_object(content.strip())
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ModelResponseError("assistant final content was not JSON") from exc
    if not isinstance(parsed, dict):
        raise ModelResponseError("assistant final JSON was not an object")
    _validate_diagnosis_fields(parsed)
    return parsed


def _validate_diagnosis_fields(payload: dict[str, Any]) -> None:
    candidates = payload.get("root_cause_candidates")
    actions = payload.get("recommended_actions", [])
    confidence = payload.get("confidence")
    if not isinstance(candidates, list) or not candidates:
        raise ModelResponseError("root_cause_candidates must be a non-empty list")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ModelResponseError("root_cause_candidates must contain objects")
        score = candidate.get("confidence")
        if (
            not isinstance(candidate.get("cause"), str)
            or not str(candidate["cause"]).strip()
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not 0 <= float(score) <= 1
        ):
            raise ModelResponseError("root cause candidate fields are invalid")
        refs = candidate.get("evidence_refs", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise ModelResponseError("root cause evidence_refs must contain strings")
    if not isinstance(actions, list) or any(
        not isinstance(action, dict)
        or not isinstance(action.get("summary"), str)
        or not str(action["summary"]).strip()
        for action in actions
    ):
        raise ModelResponseError("recommended_actions fields are invalid")
    if not isinstance(confidence, dict):
        raise ModelResponseError("confidence must be an object")
    score = confidence.get("score")
    if (
        not isinstance(score, (int, float))
        or isinstance(score, bool)
        or not 0 <= float(score) <= 1
        or confidence.get("level") not in {"high", "medium", "low"}
    ):
        raise ModelResponseError("confidence fields are invalid")


def _extract_json_object(content: str) -> str:
    payload = content.strip()
    if payload.startswith("```"):
        lines = payload.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        payload = "\n".join(lines).strip()
    if payload.startswith("{"):
        return payload
    start = payload.find("{")
    if start < 0:
        return payload
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(payload[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return payload[start:index + 1]
    return payload[start:]


async def run_diagnosis_session(
    incident: dict[str, Any],
    *,
    metrics_adapter: core.ToolAdapter | None = None,
    logs_adapter: core.ToolAdapter | None = None,
    topology_adapter: core.ToolAdapter | None = None,
    k8s_read_adapter: core.ToolAdapter | None = None,
    provider: Any | None = None,
    incident_store: Any | None = None,
    max_turns: int = core.LLM_TOOLUSE_MAX_TURNS,
    clock: Callable[[], float] = _deterministic_clock,
) -> dict[str, Any]:
    session_id = str(incident.get("session_id") or incident.get("incident_id") or "diagnosis-session")
    session: dict[str, Any] = {
        "session_id": session_id,
        "incident_id": incident.get("incident_id"),
        "state_transitions": ["running"],
        "status": "running",
        "steps": [],
        "missing_evidence": [],
        "action_proposals": [],
        "collector_version": core.COLLECTOR_VERSION,
    }
    evidence_refs: list[dict[str, Any]] = []
    missing_evidence: list[dict[str, Any]] = session["missing_evidence"]
    adapters = {
        "query_metrics": metrics_adapter,
        "query_logs": logs_adapter,
        "run_k8s_read": k8s_read_adapter,
        "get_service_topology": topology_adapter,
    }
    state = _TooluseAccumulator()
    llm_diagnosis = None
    if provider is not None:
        try:
            llm_diagnosis = await _run_llm_tooluse_session(
                incident,
                adapters,
                provider,
                incident_store,
                session_id,
                session=session,
                evidence_refs=evidence_refs,
                missing_evidence=missing_evidence,
                state=state,
                max_turns=max(1, max_turns),
                clock=clock,
            )
            if llm_diagnosis is None:
                raise ModelResponseError("provider did not return a final diagnosis")
        except Exception as exc:
            session["status"] = "failed"
            session["state_transitions"].append("failed")
            exc.partial_result = session
            raise

    hard_failure = state.hard_failure
    has_partial_observation = state.has_partial_observation
    if llm_diagnosis is None:
        session["collector_version"] = core.FALLBACK_COLLECTOR_VERSION
        for step in core.fallback_session_plan(incident):
            args = core.build_tool_arguments(step["tool"], incident, evidence_refs)
            collected = await core.collect_tool_observation(
                step["tool"],
                args,
                adapters[step["tool"]],
                incident,
                incident_store,
            )
            hard_failure = (
                _record_observation_step(session, evidence_refs, missing_evidence, collected)
                or hard_failure
            )
            has_partial_observation = has_partial_observation or collected.partial

    if llm_diagnosis is not None:
        diagnosis = core.compose_llm_diagnosis(incident, evidence_refs, llm_diagnosis)
    else:
        diagnosis = core.build_fallback_diagnosis(incident, evidence_refs)
    session["diagnosis"] = diagnosis
    session["action_proposals"] = diagnosis["recommended_actions"]
    status = core.derive_session_status(
        evidence_refs,
        missing_evidence,
        hard_failure,
        has_partial_observation,
    )
    if status not in core.SESSION_STATES:
        status = "failed"
    session["status"] = status
    session["state_transitions"].append(status)
    await core.persist_diagnosis(incident, diagnosis, incident_store)
    return session
