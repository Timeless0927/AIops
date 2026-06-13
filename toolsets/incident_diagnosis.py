"""Incident diagnosis runtime skeleton for AIO-51."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable


EVIDENCE_SOURCES = {"metrics", "logs", "topology", "k8s_read"}
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


async def run_diagnosis_session(
    incident: dict[str, Any],
    *,
    metrics_adapter: ToolAdapter | None = None,
    logs_adapter: ToolAdapter | None = None,
    topology_adapter: ToolAdapter | None = None,
    k8s_read_adapter: ToolAdapter | None = None,
    incident_store: Any | None = None,
) -> dict[str, Any]:
    """Run a rule-based Hermes diagnosis session and persist the final diagnosis."""
    session_id = str(incident.get("session_id") or incident.get("incident_id") or "diagnosis-session")
    session: dict[str, Any] = {
        "session_id": session_id,
        "incident_id": incident.get("incident_id"),
        "state_transitions": ["running"],
        "status": "running",
        "steps": [],
        "missing_evidence": [],
        "action_proposals": [],
    }
    evidence_refs: list[dict[str, Any]] = []

    plan = _build_session_plan(incident)
    hard_failure = False
    has_partial_observation = False
    for step in plan:
        adapter = {
            "query_metrics": metrics_adapter,
            "query_logs": logs_adapter,
            "run_k8s_read": k8s_read_adapter,
            "get_service_topology": topology_adapter,
        }[step["tool"]]
        args = _build_tool_args(step["tool"], incident, evidence_refs)
        observation = await _observe_tool(step["tool"], args, adapter)
        session["steps"].append(observation)
        if observation["evidence_ref"]:
            evidence_refs.append(_evidence_from_observation(observation))
        else:
            session["missing_evidence"].append(
                {
                    "source_type": observation["source_type"],
                    "tool": observation["tool"],
                    "reason": observation["missing_reason"],
                    "audit": observation["audit"],
                }
            )
        hard_failure = hard_failure or _is_hard_failure(observation)
        has_partial_observation = has_partial_observation or observation["status"] == "partial"

    action_proposals = _build_action_proposals(incident, evidence_refs)
    session["action_proposals"] = action_proposals
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
        session["missing_evidence"],
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
        selector = _resolve_k8s_selector(
            service=service,
            explicit_selector=incident.get("k8s_selector"),
            argv=configured_argv,
        )
        args.update(
            {
                "argv": configured_argv or _default_k8s_read_argv(namespace, selector),
                "command": incident.get("k8s_read_command") or _default_k8s_read_command(namespace, selector),
                "selector": selector,
            }
        )
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
    if selector:
        return selector
    argv_selector = _selector_from_k8s_read_argv(argv)
    if argv_selector:
        return argv_selector
    if isinstance(argv, list):
        return ""
    return _default_k8s_selector(service)


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
    if hard_failure:
        return "failed"
    if not evidence_refs:
        return "needs_human"
    if missing_evidence or has_partial_observation:
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


async def _persist_diagnosis(incident: dict[str, Any], diagnosis: dict[str, Any], incident_store: Any | None) -> None:
    incident_id = incident.get("incident_id")
    if not incident_id:
        return
    store = incident_store
    if store is None:
        try:
            from toolsets import incident_store as default_store
        except Exception:
            return
        store = default_store
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
