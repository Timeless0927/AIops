"""Incident diagnosis runtime skeleton for AIO-51."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

from toolsets.k8s_redact import redact_k8s_output, redact_sensitive_text
from toolsets.recommendations import normalize_recommendations, render_recommendations

logger = logging.getLogger(__name__)

EVIDENCE_SOURCES = {"metrics", "logs", "topology", "k8s_read"}
# 采集器版本,供回放区分证据来自哪一代诊断器。LLM tool-use 路径标 llm-tooluse-v1;
# 关键词回退路径(CONFIDENT 回退 helper)标 keyword-v1,二者按 run 路径分别标。
COLLECTOR_VERSION = "incident_diagnosis/llm-tooluse-v1"
FALLBACK_COLLECTOR_VERSION = "incident_diagnosis/keyword-v1"
LLM_TOOLUSE_MAX_TURNS = 6
SESSION_STATES = {"running", "diagnosed", "partial", "needs_human", "failed"}
TERMINAL_FAILURE_CODES = {"backend_unavailable", "connector_offline", "timeout"}
K8S_DEFAULT_SELECTOR_LABEL = "app.kubernetes.io/name"
LOGS_DEFAULT_MAX_LINES = 50
LOGS_MAX_SAFE_WINDOW_MINUTES = 30
K8S_READ_SUBCOMMANDS = {"get", "describe", "logs", "rollout"}
K8S_READ_MUTATING_SUBCOMMANDS = {"apply", "create", "delete", "edit", "exec", "patch", "replace", "scale", "set"}
BROAD_LOG_QUERIES = {"{}", '{namespace=~".*"}', '{namespace=~".+"}', '{pod=~".*"}', '{pod=~".+"}'}

ToolAdapter = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class CollectedToolObservation:
    observation: dict[str, Any]
    evidence: dict[str, Any] | None
    missing: dict[str, Any] | None
    hard_failure: bool
    partial: bool


def build_tool_arguments(
    tool: str,
    incident: dict[str, Any],
    evidence_refs: list[dict[str, Any]],
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    args = _build_tool_args(tool, incident, evidence_refs)
    frozen = _looks_iso8601(str(incident.get("start") or "")) and _looks_iso8601(str(incident.get("end") or ""))
    for key, value in (overrides or {}).items():
        if value not in (None, "", [], {}) and not (frozen and key in {"start", "end", "time_range"}):
            args[key] = value
    return _normalize_llm_tool_args(tool, incident, args)


async def collect_tool_observation(
    tool: str,
    args: dict[str, Any],
    adapter: ToolAdapter | None,
    incident: dict[str, Any],
    incident_store: Any | None,
) -> CollectedToolObservation:
    observation = await _observe_tool(tool, args, adapter)
    await _collect_evidence(incident, observation, args, incident_store)
    evidence = _evidence_from_observation(observation) if observation["evidence_ref"] else None
    missing = None if evidence is not None else {
        "source_type": observation["source_type"],
        "tool": observation["tool"],
        "reason": observation["missing_reason"],
        "audit": observation["audit"],
    }
    return CollectedToolObservation(
        observation=observation,
        evidence=evidence,
        missing=missing,
        hard_failure=False if evidence is not None else _is_hard_failure(observation),
        partial=observation["status"] == "partial",
    )


def compose_llm_diagnosis(
    incident: dict[str, Any],
    evidence_refs: list[dict[str, Any]],
    llm_diagnosis: dict[str, Any],
) -> dict[str, Any]:
    evidence_chain, _missing_sources = _build_evidence_chain(evidence_refs)
    candidates = llm_diagnosis.get("root_cause_candidates") or []
    confidence = llm_diagnosis.get("confidence") or {}
    llm_score = float(confidence.get("score") or 0.0)
    guard_score = _score_confidence(evidence_chain, candidates)
    score = max(llm_score, guard_score)
    diagnosis = build_diagnosis(
        incident=incident,
        evidence_refs=evidence_refs,
        recommended_actions=llm_diagnosis.get("recommended_actions") or [],
    )
    diagnosis["root_cause_candidates"] = candidates
    diagnosis["confidence"] = {
        "score": score,
        "level": confidence.get("level") or _confidence_level(score),
    }
    if llm_score < guard_score:
        diagnosis["degraded"] = True
    diagnosis["markdown"] = render_markdown(diagnosis)
    return diagnosis


def build_fallback_diagnosis(
    incident: dict[str, Any],
    evidence_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    return build_diagnosis(
        incident=incident,
        evidence_refs=evidence_refs,
        memory_hints=list(incident.get("memory_hints") or []),
        recommended_actions=_build_action_proposals(incident, evidence_refs),
    )


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
                "cause": "缺少非 Memory 证据",
                "confidence": 0.2,
                "evidence_refs": [],
                "optional_hints": [hint["summary"] for hint in hints],
            }
        ]

    diagnosis = {
        "summary": _build_summary(incident, level, evidence_chain),
        "root_cause_candidates": candidates,
        "evidence_chain": evidence_chain,
        "recommended_actions": normalize_recommendations(recommended_actions or []),
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


def _normalize_llm_tool_args(tool: str, incident: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    if tool == "run_k8s_read":
        return _normalize_llm_k8s_read_args(incident, args)
    if tool == "query_logs":
        return _normalize_llm_logs_args(incident, args)
    if tool == "get_service_topology":
        return _normalize_llm_topology_args(incident, args)
    return args


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
    lines.extend(render_recommendations(diagnosis["recommended_actions"]))

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


def fallback_session_plan(incident: dict[str, Any]) -> list[dict[str, str]]:
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
    service = _incident_service_name(incident, allow_namespace=True)
    request_id = f"{incident.get('incident_id') or 'incident'}:{tool}"
    time_range = incident.get("time_range") or {"type": "relative", "value": "30m"}
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
                "query": incident.get("logs_query") or _default_logs_query_for_incident(incident, service),
                "time_range": time_range,
                "response_mode": "summary_samples",
                "max_lines": int(incident.get("max_log_lines") or LOGS_DEFAULT_MAX_LINES),
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


def _incident_service_name(incident: dict[str, Any], *, allow_namespace: bool) -> str:
    service = _first_text(
        incident.get("service"),
        incident.get("service_name"),
        incident.get("app"),
        incident.get("workload_name"),
    )
    if service:
        return service
    pod_hint = _workload_hint_from_pod_name(_first_text(incident.get("pod_name"), incident.get("pod")))
    if pod_hint:
        return pod_hint
    return _first_text(incident.get("namespace")) if allow_namespace else ""


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _workload_hint_from_pod_name(pod_name: str) -> str:
    parts = pod_name.split("-")
    if len(parts) >= 3 and parts[-1] and parts[-2]:
        return "-".join(parts[:-2])
    return pod_name


def _normalize_llm_k8s_read_args(incident: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(args)
    namespace = str(normalized.get("namespace") or incident.get("namespace") or "").strip()
    selector = str(normalized.get("selector") or "").strip()
    argv = normalized.get("argv")
    if _safe_llm_k8s_read_argv(argv, namespace):
        safe_argv = list(argv)
        if namespace and not _namespace_from_k8s_argv(safe_argv):
            safe_argv.extend(["-n", namespace])
        if selector and not _selector_from_k8s_read_argv(safe_argv):
            safe_argv.extend(["-l", selector])
    else:
        safe_argv = _default_k8s_read_argv(namespace, selector)
    normalized["argv"] = safe_argv
    normalized["command"] = " ".join(safe_argv)
    normalized["namespace"] = namespace
    return normalized


def _safe_llm_k8s_read_argv(argv: Any, namespace: str) -> bool:
    if not isinstance(argv, list) or len(argv) < 2:
        return False
    if not all(isinstance(item, str) and item.strip() for item in argv):
        return False
    tokens = [item.strip() for item in argv]
    if tokens[0] != "kubectl":
        return False
    if any(token in {";", "|", "&&", "||"} for token in tokens):
        return False
    if any(token in {"-A", "--all-namespaces"} for token in tokens):
        return False
    ns = _namespace_from_k8s_argv(tokens)
    if namespace and ns and ns != namespace:
        return False
    subcommand = tokens[1].lower()
    if subcommand in K8S_READ_MUTATING_SUBCOMMANDS:
        return False
    if subcommand not in K8S_READ_SUBCOMMANDS:
        return False
    if subcommand == "rollout" and (len(tokens) < 3 or tokens[2].lower() not in {"history", "status"}):
        return False
    return True


def _namespace_from_k8s_argv(argv: Any) -> str:
    if not isinstance(argv, list):
        return ""
    for index, item in enumerate(argv):
        if not isinstance(item, str):
            continue
        if item in {"-n", "--namespace"} and index + 1 < len(argv):
            return str(argv[index + 1] or "").strip()
        for prefix in ("-n=", "--namespace="):
            if item.startswith(prefix):
                return item.removeprefix(prefix).strip()
    return ""


def _normalize_llm_logs_args(incident: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(args)
    service = _incident_service_name(incident, allow_namespace=True)
    query = str(normalized.get("query") or normalized.get("logql") or "").strip()
    if not query or query.replace(" ", "") in BROAD_LOG_QUERIES:
        normalized["query"] = _default_logs_query_for_incident(incident, service)
    normalized["time_range"] = _safe_logs_time_range(normalized.get("time_range"), incident)
    normalized["max_lines"] = _safe_log_line_count(normalized.get("max_lines"))
    normalized.setdefault("response_mode", "summary_samples")
    return normalized


def _safe_logs_time_range(value: Any, incident: dict[str, Any] | None = None) -> dict[str, str]:
    start, end = str((incident or {}).get("start") or ""), str((incident or {}).get("end") or "")
    frozen = {"type": "absolute", "value": f"{start}/{end}"} if _looks_iso8601(start) and _looks_iso8601(end) else None
    fallback = frozen or {"type": "relative", "value": f"{LOGS_MAX_SAFE_WINDOW_MINUTES}m"}
    if not isinstance(value, dict):
        return fallback
    range_type, raw = str(value.get("type") or "").strip(), str(value.get("value") or "").strip()
    if range_type == "relative" and (minutes := _relative_minutes(raw)) and minutes <= LOGS_MAX_SAFE_WINDOW_MINUTES:
        return frozen or {"type": "relative", "value": raw}
    if range_type == "absolute" and _absolute_window_minutes(raw) <= LOGS_MAX_SAFE_WINDOW_MINUTES:
        return frozen or {"type": "absolute", "value": raw}
    return fallback


def _relative_minutes(value: str) -> int | None:
    if len(value) < 2 or value[-1].lower() not in {"m", "h"}:
        return None
    try:
        amount = int(value[:-1])
    except ValueError:
        return None
    if amount <= 0:
        return None
    return amount if value[-1].lower() == "m" else amount * 60


def _absolute_window_minutes(value: str) -> int:
    if "/" not in value:
        return LOGS_MAX_SAFE_WINDOW_MINUTES + 1
    start_raw, end_raw = value.split("/", 1)
    if not (_looks_iso8601(start_raw) and _looks_iso8601(end_raw)):
        return LOGS_MAX_SAFE_WINDOW_MINUTES + 1
    try:
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
    except ValueError:
        return LOGS_MAX_SAFE_WINDOW_MINUTES + 1
    if start >= end:
        return LOGS_MAX_SAFE_WINDOW_MINUTES + 1
    return int((end - start).total_seconds() // 60)


def _safe_log_line_count(value: Any) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return LOGS_DEFAULT_MAX_LINES
    if count <= 0:
        return LOGS_DEFAULT_MAX_LINES
    return min(count, LOGS_DEFAULT_MAX_LINES)


def _normalize_llm_topology_args(incident: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(args)
    namespace = str(normalized.get("namespace") or incident.get("namespace") or "").strip()
    service = _incident_service_name(incident, allow_namespace=False)
    if service:
        normalized["service"] = service
    elif str(normalized.get("service") or "").strip() == namespace:
        normalized["service"] = ""
    return normalized


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


def _default_logs_query_for_incident(incident: dict[str, Any], service: str) -> str:
    namespace = str(incident.get("namespace") or "").strip()
    if service and service != namespace:
        return _default_logs_query(service)
    if namespace:
        return f'{{namespace="{_logql_label_value(namespace)}"}}'
    return _default_logs_query(service)


def _default_logs_query(service: str) -> str:
    app_selector = service or "unknown"
    return f'{{app="{_logql_label_value(app_selector)}"}}'


def _logql_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


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


def derive_session_status(
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
    if any(token in text for token in ("crashloopbackoff", "crashlooping", "crash loop", "missing", "exit code")):
        return [
            {
                "summary": "Restore the workload through a controlled configuration correction or rollout.",
                "safeguards": [
                    "Create a Change Request and validate the exact live target before approval.",
                    "Verify workload readiness after the change.",
                ],
            }
        ]
    if any(token in text for token in ("5xx", "error rate", "timeout", "payment")):
        return [
            {"summary": "Confirm upstream dependency health before selecting a recovery change."},
            {
                "summary": "Restore service health through a controlled rollback or traffic mitigation change if regression is confirmed.",
                "safeguards": [
                    "Create a Change Request from current evidence.",
                    "Require a verified post-change health check.",
                ],
            },
        ]
    return [{"summary": "Collect missing read-only evidence before proposing a Change Request."}]


def resolve_incident_store(incident_store: Any | None) -> Any | None:
    return incident_store


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
    incident_id = incident.get("incident_id")
    status = observation["status"]
    if not incident_id or status == "failed":
        return
    store = resolve_incident_store(incident_store)
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


async def persist_diagnosis(incident: dict[str, Any], diagnosis: dict[str, Any], incident_store: Any | None) -> None:
    incident_id = incident.get("incident_id")
    if not incident_id:
        return
    store = resolve_incident_store(incident_store)
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
            summary = f"{source_type} 证据引用 {source_ref}"
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
    alert_name = incident.get("alert_name") or incident.get("name") or "未知 Alert"
    namespace = incident.get("namespace") or "未知 namespace"
    cluster = incident.get("cluster") or "未知 Cluster"
    return (
        f"{alert_name}（{namespace}/{cluster}）：基于 {len(evidence_chain)} 条非 Memory 证据，"
        f"诊断置信度为 {level}。"
    )


def _default_rollback_plan() -> list[str]:
    return [
        "Do not execute rollback automatically.",
        "Prepare a human-approved rollback path before any mutation.",
        "Verify service health and alert recovery after approved changes.",
    ]


def _build_open_questions(missing_sources: list[str], evidence_chain: list[dict[str, Any]]) -> list[str]:
    if not evidence_chain:
        return ["哪项 metrics/logs/topology/k8s_read evidence 能确认当前症状？"]
    return [f"缺少用于交叉核验的 {source} 证据。" for source in missing_sources]


def _default_next_verification(missing_sources: list[str]) -> list[str]:
    if missing_sources:
        return [f"获取 {source} evidence ref。" for source in missing_sources[:2]]
    return ["在任何已审批的修复后重新检查症状。", "通过只读 Signal 确认 Alert 已恢复。"]
