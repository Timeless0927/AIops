"""LLM tool-use session orchestration for incident diagnosis."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from toolsets.recommendations import output_instruction

# Imported after incident_diagnosis has defined its evidence and rendering helpers.
from toolsets import incident_diagnosis as core


logger = logging.getLogger(__name__)


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


def _llm_tooluse_max_turns() -> int:
    raw = os.getenv("AIOPS_LLM_TOOLUSE_MAX_TURNS") or os.getenv("AIOPS_AGENT_MAX_TURNS")
    if not raw:
        return core.LLM_TOOLUSE_MAX_TURNS
    try:
        value = int(raw)
    except ValueError:
        return core.LLM_TOOLUSE_MAX_TURNS
    return value if value > 0 else core.LLM_TOOLUSE_MAX_TURNS


def _build_tool_args_from_llm(
    tool: str,
    incident: dict[str, Any],
    llm_args: dict[str, Any],
    evidence_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    args = core._build_tool_args(tool, incident, evidence_refs)
    for key, value in (llm_args or {}).items():
        if value not in (None, "", [], {}):
            args[key] = value
    return core._normalize_llm_tool_args(tool, incident, args)


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
    observation: dict[str, Any],
) -> bool:
    session["steps"].append(observation)
    if observation["evidence_ref"]:
        evidence_refs.append(core._evidence_from_observation(observation))
        return False
    missing_evidence.append(
        {
            "source_type": observation["source_type"],
            "tool": observation["tool"],
            "reason": observation["missing_reason"],
            "audit": observation["audit"],
        }
    )
    return core._is_hard_failure(observation)


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
    max_turns = _llm_tooluse_max_turns()
    for _ in range(max_turns):
        turn_start = time.time()
        result = await provider.chat_with_tools(messages, _LLM_TOOL_SCHEMA)
        messages.append(result.message)
        await _record_provider_cost(session_id, result, turn_start, incident_store)
        if not result.tool_calls:
            return _diagnosis_from_llm(result.message.get("content"))
        for call in result.tool_calls:
            args = _build_tool_args_from_llm(call.name, incident, call.arguments, evidence_refs)
            observation = await core._observe_tool(call.name, args, adapters.get(call.name))
            hard = _record_observation_step(session, evidence_refs, missing_evidence, observation)
            state.hard_failure = state.hard_failure or hard
            state.has_partial_observation = (
                state.has_partial_observation or observation["status"] == "partial"
            )
            await core._collect_evidence(incident, observation, args, incident_store)
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
    store = core._resolve_store(incident_store)
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
) -> None:
    latency_ms = int((time.time() - turn_start) * 1000)
    usage = getattr(result, "usage", {}) or {}
    input_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    output_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
    model = getattr(result, "model", None) or ""
    if input_tokens is None and output_tokens is None:
        return
    recorder = getattr(core._resolve_store(incident_store), "record_cost", None)
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
        raise ValueError("empty assistant final content")
    payload = _extract_json_object(content.strip())
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"assistant final content was not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("assistant final JSON was not an object")
    return parsed


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


def _apply_confidence_guardrail(
    evidence_chain: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    llm_confidence: dict[str, Any],
) -> tuple[float, str, bool]:
    llm_score = float(llm_confidence.get("score") or 0.0)
    guard_score = core._score_confidence(evidence_chain, candidates)
    score = max(llm_score, guard_score)
    return score, llm_confidence.get("level") or core._confidence_level(score), llm_score < guard_score


def _compose_diagnosis(
    *,
    incident: dict[str, Any],
    evidence_refs: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    recommended_actions: list[dict[str, Any]],
    confidence: float,
    level: str,
    degraded: bool,
) -> dict[str, Any]:
    diagnosis = core.build_diagnosis(
        incident=incident,
        evidence_refs=evidence_refs,
        recommended_actions=recommended_actions,
    )
    if candidates:
        diagnosis["root_cause_candidates"] = candidates
    diagnosis["confidence"] = {"score": confidence, "level": level}
    if degraded:
        diagnosis["degraded"] = True
    diagnosis["markdown"] = core.render_markdown(diagnosis)
    return diagnosis


async def run_diagnosis_session(
    incident: dict[str, Any],
    *,
    metrics_adapter: core.ToolAdapter | None = None,
    logs_adapter: core.ToolAdapter | None = None,
    topology_adapter: core.ToolAdapter | None = None,
    k8s_read_adapter: core.ToolAdapter | None = None,
    provider: Any | None = None,
    incident_store: Any | None = None,
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
            )
        except Exception as exc:
            logger.warning("diagnosis LLM tool-use failed, falling back to keyword plan: %s", exc)
            llm_diagnosis = None
            session["collector_version"] = core.FALLBACK_COLLECTOR_VERSION

    hard_failure = state.hard_failure
    has_partial_observation = state.has_partial_observation
    if llm_diagnosis is None:
        session["collector_version"] = core.FALLBACK_COLLECTOR_VERSION
        for step in core._build_session_plan(incident):
            args = core._build_tool_args(step["tool"], incident, evidence_refs)
            observation = await core._observe_tool(step["tool"], args, adapters[step["tool"]])
            hard_failure = (
                _record_observation_step(session, evidence_refs, missing_evidence, observation)
                or hard_failure
            )
            has_partial_observation = has_partial_observation or observation["status"] == "partial"
            await core._collect_evidence(incident, observation, args, incident_store)

    if llm_diagnosis is not None:
        evidence_chain, _missing_sources = core._build_evidence_chain(evidence_refs)
        candidates = llm_diagnosis.get("root_cause_candidates") or []
        confidence, level, degraded = _apply_confidence_guardrail(
            evidence_chain,
            candidates,
            llm_diagnosis.get("confidence") or {},
        )
        diagnosis = _compose_diagnosis(
            incident=incident,
            evidence_refs=evidence_refs,
            candidates=candidates,
            recommended_actions=llm_diagnosis.get("recommended_actions") or [],
            confidence=confidence,
            level=level,
            degraded=degraded,
        )
    else:
        diagnosis = core.build_diagnosis(
            incident=incident,
            evidence_refs=evidence_refs,
            memory_hints=list(incident.get("memory_hints") or []),
            recommended_actions=core._build_action_proposals(incident, evidence_refs),
        )
    session["diagnosis"] = diagnosis
    session["action_proposals"] = diagnosis["recommended_actions"]
    status = core._derive_session_status(
        evidence_refs,
        missing_evidence,
        hard_failure,
        has_partial_observation,
    )
    if status not in core.SESSION_STATES:
        status = "failed"
    session["status"] = status
    session["state_transitions"].append(status)
    await core._persist_diagnosis(incident, diagnosis, incident_store)
    return session
