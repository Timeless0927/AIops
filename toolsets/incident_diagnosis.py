"""Incident diagnosis runtime skeleton for AIO-51."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

from toolsets.k8s_redact import redact_k8s_output, redact_sensitive_text

logger = logging.getLogger(__name__)

EVIDENCE_SOURCES = {"metrics", "logs", "topology", "k8s_read"}
# 采集器版本,供回放区分证据来自哪一代诊断器。LLM tool-use 路径标 llm-tooluse-v1;
# 关键词回退路径(CONFIDENT 回退 helper)标 keyword-v1,二者按 run 路径分别标。
COLLECTOR_VERSION = "incident_diagnosis/llm-tooluse-v1"
FALLBACK_COLLECTOR_VERSION = "incident_diagnosis/keyword-v1"
LLM_TOOLUSE_MAX_TURNS = 6  # ponytail: 硬编上限,大脑稳定后应 env 化
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

SESSION_STATES = {"running", "diagnosed", "partial", "needs_human", "failed"}
TERMINAL_FAILURE_CODES = {"backend_unavailable", "connector_offline", "timeout"}
K8S_DEFAULT_SELECTOR_LABEL = "app.kubernetes.io/name"

ToolAdapter = Callable[[dict[str, Any]], Awaitable[Any]]


def build_diagnosis(
    *,
    incident: dict[str, Any],
    evidence_refs: list[dict[str, Any]] | None = None,
    memory_hints: list[dict[str, Any]] | None = None,
    recommended_actions: list[dict[str, Any]] | None = None,
    rollback_plan: list[str] | None = None,
    next_verification: list[str] | None = None,
) -> dict[str, Any]:
    """Build a structured diagnosis without performing remediation."""
    evidence_chain, missing_sources = _build_evidence_chain(evidence_refs or [])
    hints = [_normalize_hint(item) for item in memory_hints or []]
    candidates = _build_root_cause_candidates(evidence_chain, hints)
    confidence = _score_confidence(evidence_chain, candidates)
    level = _confidence_level(confidence)

    if not evidence_chain:
        candidates = [
            {
                "cause": "insufficient non-memory evidence",
                "confidence": 0.2,
                "evidence_refs": [],
                "optional_hints": [hint["summary"] for hint in hints],
            }
        ]

    diagnosis = {
        "summary": _build_summary(incident, level, evidence_chain),
        "root_cause_candidates": candidates,
        "evidence_chain": evidence_chain,
        "recommended_actions": _normalize_actions(recommended_actions or [], level),
        "rollback_plan": rollback_plan or _default_rollback_plan(),
        "open_questions": _build_open_questions(missing_sources, evidence_chain),
        "next_verification": next_verification or _default_next_verification(missing_sources),
        "confidence": {"score": confidence, "level": level},
        "optional_memory_hints": hints,
        "trace_refs": [],
        "automation": {"unattended_remediation_allowed": False},
    }
    diagnosis["markdown"] = render_markdown(diagnosis)
    return diagnosis


@dataclass
class _TooluseAccumulator:
    """Mutable cross-step state for the LLM tool-use loop."""
    hard_failure: bool = False
    has_partial_observation: bool = False


# OpenAI tool function schemas for the four MCP evidence adapters. Parameters are
# optional — the model fills what it knows; `_build_tool_args_from_llm` merges the
# defaults (`_default_metrics_query` / k8s selector resolution / time windows) for
# whatever the model left blank, reusing `_build_tool_args` plumbing.
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
            "parameters": {"type": "object", "properties": {"service": {"type": "string"}}},
        },
    },
]


def _build_tool_args_from_llm(
    tool: str,
    incident: dict[str, Any],
    llm_args: dict[str, Any],
    evidence_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build adapter args from LLM-supplied args, filling defaults for blanks."""
    base = _build_tool_args(tool, incident, evidence_refs)
    # LLM-supplied fields override defaults (e.g. a custom metrics query or selector).
    for key, value in (llm_args or {}).items():
        if value not in (None, "", [], {}):
            base[key] = value
    return base


def _build_tooluse_system_prompt(
    incident: dict[str, Any],
    memory_hints: list[dict[str, Any]],
) -> str:
    """System prompt: role, tools overview, runbook + similar-case hints, output shape."""
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
    runbook_hint = _runbook_hints_for_alert(incident)
    if runbook_hint:
        lines.append("")
        lines.append("Suggested investigation path (runbook):")
        lines.append(runbook_hint)
    if memory_hints:
        lines.append("")
        lines.append("Similar past incidents (optional leads — do not override on-the-ground evidence):")
        for hint in memory_hints:
            summary = hint.get("summary") if isinstance(hint, dict) else str(hint)
            if summary:
                lines.append(f"- {summary}")
    lines.append("")
    lines.append(
        "When you have enough evidence, reply with a non-tool message whose content is JSON with: "
        '{"root_cause_candidates":[{"cause":"<text>","confidence":<0-1>,"evidence_refs":[...]}],'
        '"recommended_actions":[{"summary":"<text>","action_type":"read|k8s_write|mutation",'
        '"approval_required":<bool>}],"confidence":{"score":<0-1>,"level":"high|medium|low"}}'
    )
    return "\n".join(lines)


def _runbook_hints_for_alert(incident: dict[str, Any]) -> str:
    """Read a matching runbook README under skills/sre/runbooks/<alert-type>/ if present."""
    try:
        from pathlib import Path

        runbooks_root = Path(__file__).resolve().parent.parent / "skills" / "sre" / "runbooks"
        alert = str(incident.get("alert_name") or "").lower()
        alias = {
            "crashloopbackoff": "pod-crashloop",
            "crashloop": "pod-crashloop",
            "highmemory": "high-memory",
            "node-not-ready": "node-not-ready",
            "certexpir": "certificate-expiry",
            "pvc": "pvc-full",
        }
        target = None
        for needle, folder in alias.items():
            if needle in alert:
                target = runbooks_root / folder
                break
        if target is None:
            for folder in runbooks_root.iterdir() if runbooks_root.exists() else []:
                name = folder.name.lower().replace("-", "").replace("_", "")
                if name and name in alert:
                    target = folder
                    break
        if target is None or not target.exists():
            return ""
        readme = target / "README.md"
        if not readme.exists():
            return ""
        text = readme.read_text(encoding="utf-8")
        return text[:2000]
    except OSError:
        return ""


def _record_observation_step(
    session: dict[str, Any],
    evidence_refs: list[dict[str, Any]],
    missing_evidence: list[dict[str, Any]],
    observation: dict[str, Any],
) -> bool:
    """Append a step's observation to session state; return whether it was a hard failure."""
    session["steps"].append(observation)
    if observation["evidence_ref"]:
        evidence_refs.append(_evidence_from_observation(observation))
        return False
    missing_evidence.append(
        {
            "source_type": observation["source_type"],
            "tool": observation["tool"],
            "reason": observation["missing_reason"],
            "audit": observation["audit"],
        }
    )
    return _is_hard_failure(observation)


async def _run_llm_tooluse_session(
    incident: dict[str, Any],
    adapters: dict[str, ToolAdapter | None],
    provider: Any,
    incident_store: Any | None,
    session_id: str,
    *,
    session: dict[str, Any],
    evidence_refs: list[dict[str, Any]],
    missing_evidence: list[dict[str, Any]],
    state: _TooluseAccumulator,
) -> dict[str, Any] | None:
    """Drive the thin LLM tool-use loop; mutate shared session/evidence state; return final diagnosis dict.

    Returns the parsed LLM final diagnosis, or None (letting the caller fall back to keyword
    plan) only if the model never produced a usable final message — provider/network/parse
    failures are caught by the caller. Per tool-use step we record evidence (``_collect_evidence``),
    span trace (``diagnosis_trace``), and cost (``cost_records`` w/ latency). The raw
    ``incident_store`` is forwarded to ``_collect_evidence`` so its ValueError guard sees the
    caller-supplied vs resolved-default distinction unchanged.
    """
    import time

    memory_hints = list(incident.get("memory_hints") or [])
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _build_tooluse_system_prompt(incident, memory_hints)},
        {"role": "user", "content": f"Diagnose incident {incident.get('incident_id') or 'unknown'}: {incident.get('summary') or incident.get('alert_name')}"},
    ]
    step_index = 0
    for _ in range(LLM_TOOLUSE_MAX_TURNS):
        turn_start = time.time()
        result = await provider.chat_with_tools(messages, _LLM_TOOL_SCHEMA)
        messages.append(result.message)
        await _record_provider_cost(session_id, result, turn_start)
        if not result.tool_calls:
            return _diagnosis_from_llm(result.message.get("content"))
        for call in result.tool_calls:
            adapter = adapters.get(call.name)
            args = _build_tool_args_from_llm(call.name, incident, call.arguments, evidence_refs)
            observation = await _observe_tool(call.name, args, adapter)
            hard = _record_observation_step(session, evidence_refs, missing_evidence, observation)
            state.hard_failure = state.hard_failure or hard
            state.has_partial_observation = state.has_partial_observation or observation["status"] == "partial"
            await _collect_evidence(incident, observation, args, incident_store)
            await _add_trace_row(incident_store, session_id, step_index, call, observation, result)
            step_index += 1
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps({"status": observation["status"], "summary": observation["summary"]}, ensure_ascii=False),
                }
            )
    logger.warning("diagnosis LLM tool-use hit max_turns (%d) without a final answer", LLM_TOOLUSE_MAX_TURNS)
    return None


async def _add_trace_row(
    incident_store: Any | None,
    session_id: str,
    step_index: int,
    call: Any,
    observation: dict[str, Any],
    result: Any,
) -> None:
    store = _resolve_store(incident_store)
    add_trace = getattr(store, "add_diagnosis_trace", None)
    if add_trace is None:
        return
    usage = getattr(result, "usage", {}) or {}
    model = getattr(result, "model", None) or ""
    try:
        await add_trace(
            session_id=session_id,
            step_index=step_index,
            tool_name=getattr(call, "name", "") or "",
            tool_args=getattr(call, "arguments", None),
            observation_ref=observation.get("evidence_ref"),
            duration_ms=None,
            model=model,
            input_tokens=usage.get("prompt_tokens") if isinstance(usage, dict) else None,
            output_tokens=usage.get("completion_tokens") if isinstance(usage, dict) else None,
        )
    except ValueError:
        if incident_store is not None:
            raise


async def _record_provider_cost(session_id: str, result: Any, turn_start: float) -> None:
    """Best-effort cost+latency record; module import may fail on the pre-existing submodule gap."""
    import time

    latency_ms = int((time.time() - turn_start) * 1000)
    usage = getattr(result, "usage", {}) or {}
    input_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    output_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
    model = getattr(result, "model", None) or ""
    if input_tokens is None and output_tokens is None:
        return
    try:
        from toolsets import cost_guard

        await cost_guard.record_cost(
            model=model or "diagnosis-llm",
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            estimated_cost=0.0,
            session_id=session_id,
            latency_ms=latency_ms,
        )
    except Exception:  # pragma: no cover - pre-existing tools ImportError; cost is best-effort
        logger.debug("cost_guard record skipped (module import gap)", exc_info=True)


def _diagnosis_from_llm(content: Any) -> dict[str, Any]:
    """Parse the model's final JSON content into a diagnosis input dict, or raise to trigger fallback."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty assistant final content")
    # tolerant: model may wrap JSON in prose / fences
    payload = content.strip()
    if payload.startswith("```"):
        payload = payload.strip("`")
        if payload.lower().startswith("json"):
            payload = payload[4:]
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"assistant final content was not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("assistant final JSON was not an object")
    return parsed


def _apply_confidence_guardrail(
    evidence_chain: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    llm_confidence: dict[str, Any],
) -> tuple[float, str, bool]:
    """Trust-boundary check: the model's score must not exceed the evidence-based guard.

    Returns (score, level, degraded). LLM optimism under weak evidence is capped by
    `_score_confidence` (the existing hand-tuned floor); if the floor beats the model,
    the session is marked degraded so the on-call human sees it was padded.
    """
    llm_score = float(llm_confidence.get("score") or 0.0)
    guard_score = _score_confidence(evidence_chain, candidates)
    score = max(llm_score, guard_score)
    degraded = llm_score < guard_score
    level = llm_confidence.get("level") or _confidence_level(score)
    return score, level, degraded


def _compose_diagnosis(
    *,
    incident: dict[str, Any],
    evidence_refs: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    recommended_actions: list[dict[str, Any]],
    confidence: float,
    level: str,
    degraded: bool,
    missing_sources: list[str],
) -> dict[str, Any]:
    """Reuse build_diagnosis's full schema, then overlay the LLM root cause + guarded confidence."""
    diagnosis = build_diagnosis(
        incident=incident,
        evidence_refs=evidence_refs,
        recommended_actions=recommended_actions,
    )
    if candidates:
        diagnosis["root_cause_candidates"] = candidates
    diagnosis["confidence"] = {"score": confidence, "level": level}
    if degraded:
        diagnosis["degraded"] = True
    diagnosis["markdown"] = render_markdown(diagnosis)
    return diagnosis


async def run_diagnosis_session(
    incident: dict[str, Any],
    *,
    metrics_adapter: ToolAdapter | None = None,
    logs_adapter: ToolAdapter | None = None,
    topology_adapter: ToolAdapter | None = None,
    k8s_read_adapter: ToolAdapter | None = None,
    provider: Any | None = None,
    incident_store: Any | None = None,
) -> dict[str, Any]:
    """Run a Hermes diagnosis session and persist the final diagnosis.

    ADR-0003: when a diagnosis ``provider`` is available this drives a thin LLM
    tool-use loop — the model picks which MCP evidence tools to call and in what
    order, then returns a structured root cause. If the provider is absent or
    fails (``ProviderUnavailable`` / bad JSON), the session falls back to the
    keyword plan so the evidence-collection + state-machine guarantees survive.
    Both paths share evidence collection (``_collect_evidence``) and the
    ``_derive_session_status`` state machine.
    """
    session_id = str(incident.get("session_id") or incident.get("incident_id") or "diagnosis-session")
    session: dict[str, Any] = {
        "session_id": session_id,
        "incident_id": incident.get("incident_id"),
        "state_transitions": ["running"],
        "status": "running",
        "steps": [],
        "missing_evidence": [],
        "action_proposals": [],
        "collector_version": COLLECTOR_VERSION,
    }
    evidence_refs: list[dict[str, Any]] = []
    missing_evidence: list[dict[str, Any]] = session["missing_evidence"]
    hard_failure = False
    has_partial_observation = False

    adapters = {
        "query_metrics": metrics_adapter,
        "query_logs": logs_adapter,
        "run_k8s_read": k8s_read_adapter,
        "get_service_topology": topology_adapter,
    }

    # NOTE: pass the *original* incident_store through — `_collect_evidence` /
    # `_persist_diagnosis` guard `except ValueError: if incident_store is not None:
    # raise` on the raw arg (caller-supplied vs auto-resolved default module).
    # Pre-resolving here would defeat that guard on tests with synthetic incident ids.
    llm_diagnosis: dict[str, Any] | None = None
    state = _TooluseAccumulator()
    if provider is not None:
        try:
            llm_diagnosis = await _run_llm_tooluse_session(
                incident, adapters, provider, incident_store, session_id,
                session=session, evidence_refs=evidence_refs,
                missing_evidence=missing_evidence,
                state=state,
            )
        except Exception as exc:  # ProviderUnavailable / JSON parse / network
            logger.warning("diagnosis LLM tool-use failed, falling back to keyword plan: %s", exc)
            llm_diagnosis = None
            session["collector_version"] = FALLBACK_COLLECTOR_VERSION

    if llm_diagnosis is None:
        session["collector_version"] = FALLBACK_COLLECTOR_VERSION
        plan = _build_session_plan(incident)
        for step in plan:
            args = _build_tool_args(step["tool"], incident, evidence_refs)
            observation = await _observe_tool(step["tool"], args, adapters[step["tool"]])
            hard_failure = _record_observation_step(
                session, evidence_refs, missing_evidence, observation
            ) or hard_failure
            has_partial_observation = has_partial_observation or observation["status"] == "partial"
            await _collect_evidence(incident, observation, args, incident_store)
    else:
        hard_failure = hard_failure or state.hard_failure
        has_partial_observation = has_partial_observation or state.has_partial_observation

    if llm_diagnosis is not None:
        evidence_chain, missing_sources = _build_evidence_chain(evidence_refs)
        candidates = llm_diagnosis.get("root_cause_candidates") or []
        recommended_actions = llm_diagnosis.get("recommended_actions") or []
        confidence, level, degraded = _apply_confidence_guardrail(
            evidence_chain, candidates, llm_diagnosis.get("confidence") or {}
        )
        diagnosis = _compose_diagnosis(
            incident=incident,
            evidence_refs=evidence_refs,
            candidates=candidates,
            recommended_actions=recommended_actions,
            confidence=confidence,
            level=level,
            degraded=degraded,
            missing_sources=missing_sources,
        )
    else:
        action_proposals = _build_action_proposals(incident, evidence_refs)
        diagnosis = build_diagnosis(
            incident=incident,
            evidence_refs=evidence_refs,
            memory_hints=list(incident.get("memory_hints") or []),
            recommended_actions=action_proposals,
        )
    session["diagnosis"] = diagnosis
    session["action_proposals"] = diagnosis["recommended_actions"]

    status = _derive_session_status(
        evidence_refs,
        missing_evidence,
        hard_failure,
        has_partial_observation,
    )
    if status not in SESSION_STATES:
        status = "failed"
    session["status"] = status
    session["state_transitions"].append(status)

    await _persist_diagnosis(incident, diagnosis, incident_store)
    return session


def render_markdown(diagnosis: dict[str, Any]) -> str:
    """Render the diagnosis as readable Markdown while keeping JSON parseable separately."""
    lines = [
        f"# Incident diagnosis: {diagnosis['confidence']['level']}",
        "",
        diagnosis["summary"],
        "",
        "## Root cause candidates",
    ]
    for candidate in diagnosis["root_cause_candidates"]:
        refs = ", ".join(candidate.get("evidence_refs") or ["no direct evidence"])
        lines.append(f"- {candidate['cause']} (confidence={candidate['confidence']:.2f}; evidence={refs})")

    lines.extend(["", "## Evidence chain"])
    if diagnosis["evidence_chain"]:
        for item in diagnosis["evidence_chain"]:
            lines.append(f"- {item['source_type']} `{item['source_ref']}`: {item['summary']}")
    else:
        lines.append("- No non-memory evidence was supplied.")

    lines.extend(["", "## Recommended actions"])
    for action in diagnosis["recommended_actions"]:
        approval = "approval required" if action["approval_required"] else "read-only"
        lines.append(f"- [{approval}] {action['summary']}")

    lines.extend(["", "## Open questions"])
    for question in diagnosis["open_questions"]:
        lines.append(f"- {question}")

    lines.extend(["", "## Next verification"])
    for step in diagnosis["next_verification"]:
        lines.append(f"- {step}")
    return "\n".join(lines)


def to_json(diagnosis: dict[str, Any]) -> str:
    """Serialize diagnosis output for tool callers."""
    return json.dumps(diagnosis, ensure_ascii=False, sort_keys=True)


def _build_session_plan(incident: dict[str, Any]) -> list[dict[str, str]]:
    text = _incident_text(incident)
    if any(token in text for token in ("crashloopbackoff", "crash loop", "oomkilled", "pod")):
        return [
            {"tool": "run_k8s_read"},
            {"tool": "query_logs"},
        ]
    if any(token in text for token in ("payment", "5xx", "error rate", "timeout", "latency")):
        return [
            {"tool": "query_metrics"},
            {"tool": "query_logs"},
            {"tool": "run_k8s_read"},
            {"tool": "get_service_topology"},
        ]
    return [
        {"tool": "query_metrics"},
        {"tool": "query_logs"},
        {"tool": "run_k8s_read"},
        {"tool": "get_service_topology"},
    ]


def _incident_text(incident: dict[str, Any]) -> str:
    values = [
        incident.get("alert_name"),
        incident.get("name"),
        incident.get("summary"),
        incident.get("service"),
        incident.get("namespace"),
    ]
    return " ".join(str(value or "") for value in values).lower()


def _build_tool_args(tool: str, incident: dict[str, Any], evidence_refs: list[dict[str, Any]]) -> dict[str, Any]:
    cluster = str(incident.get("cluster") or incident.get("cluster_id") or "")
    namespace = str(incident.get("namespace") or "")
    service = str(incident.get("service") or incident.get("app") or namespace or "")
    request_id = f"{incident.get('incident_id') or 'incident'}:{tool}"
    time_range = incident.get("time_range") or {
        "type": "relative",
        "value": "30m",
    }
    args: dict[str, Any] = {
        "request_id": request_id,
        "correlation_id": incident.get("incident_id") or incident.get("session_id"),
        "cluster_id": cluster,
        "namespace": namespace,
        "service": service,
        "reason": _build_step_reason(tool, incident, evidence_refs),
    }
    if tool == "query_metrics":
        start, end = _metrics_time_window(incident)
        args.update(
            {
                "query": incident.get("metrics_query") or _default_metrics_query(service),
                "start": start,
                "end": end,
                "step": incident.get("step") or "60s",
            }
        )
    elif tool == "query_logs":
        args.update(
            {
                "query": incident.get("logs_query") or _default_logs_query(service),
                "time_range": time_range,
                "response_mode": "summary_samples",
                "max_lines": int(incident.get("max_log_lines") or 50),
            }
        )
    elif tool == "run_k8s_read":
        configured_argv = incident.get("k8s_read_argv")
        selector_conflict = _k8s_selector_conflict(
            explicit_selector=incident.get("k8s_selector"),
            argv=configured_argv,
        )
        selector = _resolve_k8s_selector(
            service=service,
            explicit_selector=incident.get("k8s_selector"),
            argv=configured_argv,
        )
        argv = _k8s_read_argv_with_selector(configured_argv, selector) or _default_k8s_read_argv(namespace, selector)
        args.update(
            {
                "argv": argv,
                "command": incident.get("k8s_read_command") or _default_k8s_read_command(namespace, selector),
                "selector": selector,
            }
        )
        if selector_conflict:
            args["selector_conflict"] = selector_conflict
    elif tool == "get_service_topology":
        args.update({"service": service})
    return args


def _build_step_reason(tool: str, incident: dict[str, Any], evidence_refs: list[dict[str, Any]]) -> str:
    if evidence_refs:
        latest = evidence_refs[-1]["summary"]
        return f"{_source_label(tool)} suggested {latest}"
    summary = str(incident.get("summary") or incident.get("alert_name") or "incident diagnosis")
    return f"investigate {summary}"


def _source_label(tool: str) -> str:
    return {
        "query_metrics": "incident",
        "query_logs": "metrics",
        "run_k8s_read": "logs",
        "get_service_topology": "k8s_read",
    }.get(tool, "previous evidence")


def _default_metrics_query(service: str) -> str:
    app_selector = service or "unknown"
    return f'sum(rate(http_requests_total{{app="{app_selector}",status=~"5.."}}[5m]))'


def _default_logs_query(service: str) -> str:
    app_selector = service or "unknown"
    return f'{{app="{app_selector}"}}'


def _default_k8s_selector(service: str) -> str:
    if not service:
        return ""
    return f"{K8S_DEFAULT_SELECTOR_LABEL}={service}"


def _resolve_k8s_selector(*, service: str, explicit_selector: Any, argv: Any) -> str:
    selector = str(explicit_selector or "").strip()
    argv_selector = _selector_from_k8s_read_argv(argv)
    if argv_selector:
        return argv_selector
    if selector:
        return selector
    if isinstance(argv, list):
        return ""
    return _default_k8s_selector(service)


def _k8s_selector_conflict(*, explicit_selector: Any, argv: Any) -> dict[str, str]:
    selector = str(explicit_selector or "").strip()
    argv_selector = _selector_from_k8s_read_argv(argv)
    if selector and argv_selector and selector != argv_selector:
        return {
            "explicit_selector": selector,
            "argv_selector": argv_selector,
            "selector_used": argv_selector,
        }
    return {}


def _k8s_read_argv_with_selector(argv: Any, selector: str) -> list[str] | None:
    if not isinstance(argv, list):
        return None
    normalized = list(argv)
    if selector and not _selector_from_k8s_read_argv(normalized):
        normalized.extend(["-l", selector])
    return normalized


def _selector_from_k8s_read_argv(argv: Any) -> str:
    if not isinstance(argv, list):
        return ""
    for index, item in enumerate(argv):
        if not isinstance(item, str):
            continue
        if item in {"-l", "--selector"} and index + 1 < len(argv):
            return str(argv[index + 1] or "").strip()
        for prefix in ("-l=", "--selector="):
            if item.startswith(prefix):
                return item.removeprefix(prefix).strip()
    return ""


def _default_k8s_read_command(namespace: str, selector: str) -> str:
    scope = f"-n {namespace} " if namespace else ""
    label = f"-l {selector}" if selector else ""
    return f"kubectl get pods {scope}{label}".strip()


def _default_k8s_read_argv(namespace: str, selector: str) -> list[str]:
    argv = ["kubectl", "get", "pods"]
    if namespace:
        argv.extend(["-n", namespace])
    if selector:
        argv.extend(["-l", selector])
    return argv


def _metrics_time_window(incident: dict[str, Any]) -> tuple[str, str]:
    start = str(incident.get("start") or "")
    end = str(incident.get("end") or "")
    if _looks_iso8601(start) and _looks_iso8601(end):
        return start, end
    window_end = datetime.now(UTC).replace(microsecond=0)
    window_start = window_end - timedelta(minutes=30)
    return _format_iso8601_z(window_start), _format_iso8601_z(window_end)


def _looks_iso8601(value: str) -> bool:
    if not value:
        return False
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return True


def _format_iso8601_z(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _observe_tool(tool: str, args: dict[str, Any], adapter: ToolAdapter | None) -> dict[str, Any]:
    if adapter is None:
        return _missing_observation(tool, args, _adapter_missing_reason(tool))
    try:
        envelope = await adapter(args)
    except Exception as exc:  # pragma: no cover - defensive guard for runtime adapters
        return _missing_observation(tool, args, f"{tool} adapter raised {type(exc).__name__}: {exc}", status="failed")
    return _observation_from_envelope(tool, args, envelope)


def _missing_observation(
    tool: str,
    args: dict[str, Any],
    reason: str,
    *,
    status: str = "skipped",
) -> dict[str, Any]:
    return {
        "tool": tool,
        "status": status,
        "source_type": _source_type_for_tool(tool),
        "evidence_ref": None,
        "summary": reason,
        "missing_reason": reason,
        "payload": {},
        "audit": {
            "status": status,
            "tool_name": tool,
            "missing_reason": reason,
            "request_id": args.get("request_id"),
        },
    }


def _adapter_missing_reason(tool: str) -> str:
    if tool == "run_k8s_read":
        return "Gateway run_k8s_read adapter unavailable"
    if tool == "get_service_topology":
        return "Topology facade adapter unavailable"
    return f"{tool} adapter unavailable"


def _observation_from_envelope(tool: str, args: dict[str, Any], envelope: Any) -> dict[str, Any]:
    data = _as_mapping(envelope)
    status = str(data.get("status") or "failed")
    summary = str(data.get("summary") or f"{tool} returned {status}")
    payload = dict(data.get("data") or {})
    audit = {
        **dict(data.get("audit") or {}),
        "request_id": data.get("request_id") or args.get("request_id"),
        "tool_name": data.get("tool_name") or tool,
    }
    if tool == "run_k8s_read":
        _annotate_k8s_observation(args, payload, audit)
        if status == "succeeded" and payload.get("resource_match_count") == 0:
            status = "partial"
            summary = (
                f"K8s selector {payload.get('selector') or '<none>'} returned 0 matching resources; "
                "treating this read as low-confidence evidence."
            )
    evidence_ref = _first_evidence_ref(data)
    missing_reason = None if evidence_ref else _missing_reason_from_envelope(summary, data)
    audit["missing_reason"] = missing_reason
    return {
        "tool": tool,
        "status": status,
        "source_type": _source_type_for_tool(tool),
        "evidence_ref": evidence_ref,
        "summary": summary,
        "missing_reason": missing_reason,
        "payload": payload,
        "audit": audit,
    }


def _annotate_k8s_observation(args: dict[str, Any], payload: dict[str, Any], audit: dict[str, Any]) -> None:
    selector = str(args.get("selector") or payload.get("selector") or "").strip()
    if selector:
        payload["selector"] = selector
        audit["selector"] = selector
    selector_conflict = args.get("selector_conflict")
    if isinstance(selector_conflict, dict) and selector_conflict:
        payload["selector_conflict"] = dict(selector_conflict)
        audit["selector_conflict"] = dict(selector_conflict)
    match_count = _k8s_resource_match_count(payload)
    if match_count is not None:
        payload["resource_match_count"] = match_count
        audit["resource_match_count"] = match_count


def _k8s_resource_match_count(payload: dict[str, Any]) -> int | None:
    for key in ("resource_match_count", "match_count", "total_matched"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return max(value, 0)
        if isinstance(value, str) and value.isdigit():
            return int(value)
    items = payload.get("items")
    if isinstance(items, list):
        return len(items)
    resources = payload.get("resources")
    if isinstance(resources, list):
        return len(resources)
    return None


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if is_dataclass(value):
        return asdict(value)
    return {
        "status": getattr(value, "status", "failed"),
        "summary": getattr(value, "summary", ""),
        "data": getattr(value, "data", {}),
        "evidence_refs": getattr(value, "evidence_refs", ()),
        "audit": getattr(value, "audit", {}),
        "errors": getattr(value, "errors", ()),
        "request_id": getattr(value, "request_id", None),
        "tool_name": getattr(value, "tool_name", None),
    }


def _first_evidence_ref(data: dict[str, Any]) -> str | None:
    refs = data.get("evidence_refs") or ()
    if not refs:
        payload = data.get("data") or {}
        ref = payload.get("ref")
        return str(ref) if ref else None
    first = refs[0]
    if isinstance(first, dict):
        return str(first.get("ref_id") or "") or None
    return str(getattr(first, "ref_id", "") or "") or None


def _missing_reason_from_envelope(summary: str, data: dict[str, Any]) -> str:
    errors = data.get("errors") or ()
    if errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("message") or summary)
        return str(getattr(first, "message", summary))
    return summary


def _source_type_for_tool(tool: str) -> str:
    return {
        "query_metrics": "metrics",
        "query_logs": "logs",
        "run_k8s_read": "k8s_read",
        "get_service_topology": "topology",
    }.get(tool, tool)


def _evidence_from_observation(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_type": observation["source_type"],
        "source_ref": observation["evidence_ref"],
        "summary": observation["summary"],
        "payload": observation["payload"],
        "confidence": _confidence_for_observation(observation),
    }


def _confidence_for_observation(observation: dict[str, Any]) -> float:
    if observation["source_type"] == "k8s_read" and observation.get("payload", {}).get("resource_match_count") == 0:
        return 0.25
    if observation["status"] == "succeeded":
        return 0.8
    if observation["status"] == "partial":
        return 0.55
    return 0.3


def _is_hard_failure(observation: dict[str, Any]) -> bool:
    if observation["status"] != "failed":
        return False
    error_code = str(observation.get("audit", {}).get("error_code") or "")
    return error_code in TERMINAL_FAILURE_CODES


def _derive_session_status(
    evidence_refs: list[dict[str, Any]],
    missing_evidence: list[dict[str, Any]],
    hard_failure: bool,
    has_partial_observation: bool = False,
) -> str:
    """Derive session status from evidence completeness.

    ``hard_failure`` (a terminal per-tool failure such as ``backend_unavailable``)
    no longer one-shot vetoes the whole session to ``failed``. A single
    unreachable backend should not invalidate evidence collected by the other
    tools. Status is derived from evidence completeness instead:

    - No non-memory evidence at all → ``needs_human`` (the persisted diagnosis
      artifact still gives a human something to pick up), regardless of whether a
      backend was hard-down.
    - Some evidence but incomplete (a hard failure on one tool, missing evidence,
      or a partial observation) → ``partial``.
    - All tools succeeded with no gaps → ``diagnosed``.

    ``failed`` is no longer returned here; it is reserved as the illegal-state
    fallback in ``run_diagnosis_session`` (status not in ``SESSION_STATES``).
    """
    if not evidence_refs:
        return "needs_human"
    if hard_failure or missing_evidence or has_partial_observation:
        return "partial"
    return "diagnosed"


def _build_action_proposals(incident: dict[str, Any], evidence_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text = f"{_incident_text(incident)} {' '.join(item['summary'].lower() for item in evidence_refs)}"
    if any(token in text for token in ("crashloopbackoff", "crash loop", "missing", "exit code")):
        return [
            {
                "summary": "Propose deployment configuration correction or restart only after human approval.",
                "action_type": "mutation",
                "approval_required": True,
                "execute_automatically": False,
            }
        ]
    if any(token in text for token in ("5xx", "error rate", "timeout", "payment")):
        return [
            {"summary": "Query upstream dependency health before remediation.", "action_type": "read"},
            {
                "summary": "Prepare rollback or traffic mitigation proposal if regression is confirmed.",
                "action_type": "k8s_write",
                "approval_required": True,
                "execute_automatically": False,
            },
        ]
    return [{"summary": "Collect missing read-only evidence before remediation.", "action_type": "read"}]


def _resolve_store(incident_store: Any | None) -> Any | None:
    if incident_store is not None:
        return incident_store
    try:
        from toolsets import incident_store as default_store
    except Exception:
        return None
    return default_store


async def _collect_evidence(
    incident: dict[str, Any],
    observation: dict[str, Any],
    args: dict[str, Any],
    incident_store: Any | None,
) -> None:
    """把 observation 冻进 incident_evidence 供回放评测集复现现场(决策 2)。

    succeeded 存全量 payload;partial 存部分 payload、低 confidence;
    skipped 存空 payload、summary 记 reason;failed(adapter 抛错)不落,只走现有 audit。
    """
    # ponytail: Hermes 直连 incident_store,边界收口见 ISSUE-F。
    incident_id = incident.get("incident_id")
    status = observation["status"]
    if not incident_id or status == "failed":
        return
    store = _resolve_store(incident_store)
    adder = getattr(store, "add_evidence", None)
    if adder is None:
        return

    payload = await _redact_payload(observation)
    window_start, window_end = _evidence_window(observation, args)
    try:
        await adder(
            str(incident_id),
            observation["source_type"],
            observation.get("evidence_ref"),
            observation["summary"],
            payload=payload,
            window_start_ts=window_start,
            window_end_ts=window_end,
            collector_version=COLLECTOR_VERSION,
            confidence=_confidence_for_observation(observation),
        )
    except ValueError:
        if incident_store is not None:
            raise


async def _redact_payload(observation: dict[str, Any]) -> dict[str, Any]:
    """脱敏(决策 3):k8s 路走 redact_k8s_output,其余走 redact_sensitive_text 兜底。"""
    payload = observation.get("payload") or {}
    if not payload:
        return {}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if observation["source_type"] == "k8s_read":
        command = str(payload.get("command") or "")
        redacted_text = await redact_k8s_output(raw, command)
    else:
        redacted_text = redact_sensitive_text(raw)
    try:
        return json.loads(redacted_text)
    except (ValueError, TypeError):
        # 脱敏破坏了 JSON 结构(罕见),退化为带原文文本的包装,不丢证据。
        return {"redacted_text": redacted_text}


def _evidence_window(observation: dict[str, Any], args: dict[str, Any]) -> tuple[float | None, float | None]:
    """metrics 路有显式 ISO 时间窗,转 epoch 让证据可按时间回放。"""
    if observation["tool"] != "query_metrics":
        return None, None
    return _iso_to_epoch(args.get("start")), _iso_to_epoch(args.get("end"))


def _iso_to_epoch(value: Any) -> float | None:
    text = str(value or "")
    if not _looks_iso8601(text):
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(candidate).timestamp()
    except ValueError:
        return None


async def _persist_diagnosis(incident: dict[str, Any], diagnosis: dict[str, Any], incident_store: Any | None) -> None:
    incident_id = incident.get("incident_id")
    if not incident_id:
        return
    store = _resolve_store(incident_store)
    recorder = getattr(store, "record_incident_diagnosis", None)
    if recorder is None:
        return
    try:
        await recorder(str(incident_id), diagnosis)
    except ValueError:
        if incident_store is not None:
            raise


def _build_evidence_chain(evidence_refs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    chain: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for index, item in enumerate(evidence_refs, start=1):
        source_type = str(item.get("source_type") or item.get("type") or "").strip()
        if source_type not in EVIDENCE_SOURCES:
            continue
        source_ref = str(item.get("source_ref") or item.get("ref") or f"{source_type}:{index}")
        summary = str(item.get("summary") or item.get("description") or "").strip()
        if not summary:
            summary = f"{source_type} evidence reference {source_ref}"
        seen_sources.add(source_type)
        chain.append(
            {
                "id": f"ev-{index}",
                "source_type": source_type,
                "source_ref": source_ref,
                "summary": summary,
                "payload": item.get("payload") or {},
                "confidence": float(item.get("confidence", 0.6)),
            }
        )
    return chain, sorted(EVIDENCE_SOURCES - seen_sources)


def _normalize_hint(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": str(item.get("source") or "memory"),
        "summary": str(item.get("summary") or item.get("description") or ""),
        "weight": "optional",
    }


def _build_root_cause_candidates(
    evidence_chain: list[dict[str, Any]],
    hints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    text = " ".join(item["summary"].lower() for item in evidence_chain)
    evidence_ids = [item["id"] for item in evidence_chain]
    candidates: list[dict[str, Any]] = []

    if any(token in text for token in ("5xx", "error rate", "timeout", "latency", "payment")):
        candidates.append(
            {
                "cause": "service error-rate regression or upstream dependency failure",
                "confidence": min(0.9, 0.35 + len(evidence_ids) * 0.15),
                "evidence_refs": evidence_ids,
                "optional_hints": [hint["summary"] for hint in hints],
            }
        )
    if any(token in text for token in ("crashloopbackoff", "oomkilled", "restart", "back-off", "exit code")):
        candidates.append(
            {
                "cause": "workload crash loop caused by application/runtime or resource failure",
                "confidence": min(0.9, 0.35 + len(evidence_ids) * 0.15),
                "evidence_refs": evidence_ids,
                "optional_hints": [hint["summary"] for hint in hints],
            }
        )
    if not candidates and evidence_chain:
        candidates.append(
            {
                "cause": "undifferentiated incident requiring more evidence",
                "confidence": min(0.55, 0.25 + len(evidence_ids) * 0.1),
                "evidence_refs": evidence_ids,
                "optional_hints": [hint["summary"] for hint in hints],
            }
        )
    return candidates


def _score_confidence(evidence_chain: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> float:
    if not evidence_chain:
        return 0.2
    source_count = len({item["source_type"] for item in evidence_chain})
    avg_evidence_confidence = sum(item["confidence"] for item in evidence_chain) / len(evidence_chain)
    candidate_bonus = 0.15 if candidates else 0.0
    score = 0.2 + source_count * 0.14 + avg_evidence_confidence * 0.25 + candidate_bonus
    return round(min(score, 0.92), 2)


def _confidence_level(score: float) -> str:
    if score >= 0.75:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


def _build_summary(incident: dict[str, Any], level: str, evidence_chain: list[dict[str, Any]]) -> str:
    alert_name = incident.get("alert_name") or incident.get("name") or "unknown alert"
    namespace = incident.get("namespace") or "unknown namespace"
    cluster = incident.get("cluster") or "unknown cluster"
    return (
        f"{alert_name} in {namespace}/{cluster}: diagnosis confidence is {level} "
        f"based on {len(evidence_chain)} non-memory evidence item(s)."
    )


def _normalize_actions(actions: list[dict[str, Any]], level: str) -> list[dict[str, Any]]:
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


def _default_rollback_plan() -> list[str]:
    return [
        "Do not execute rollback automatically.",
        "Prepare a human-approved rollback path before any mutation.",
        "Verify service health and alert recovery after approved changes.",
    ]


def _build_open_questions(missing_sources: list[str], evidence_chain: list[dict[str, Any]]) -> list[str]:
    if not evidence_chain:
        return ["Which metrics/logs/topology/k8s_read evidence confirms the current symptom?"]
    return [f"Missing {source} evidence for cross-checking." for source in missing_sources]


def _default_next_verification(missing_sources: list[str]) -> list[str]:
    if missing_sources:
        return [f"Collect {source} evidence ref." for source in missing_sources[:2]]
    return ["Re-check symptoms after any approved remediation.", "Confirm alert recovery from read-only signals."]
