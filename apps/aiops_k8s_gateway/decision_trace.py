"""Safe, user-visible Decision Trace projection from Diagnosis writeback."""

from __future__ import annotations

import re
from typing import Any


JSON = dict[str, object]
_RELATIONS = {"supports", "refutes", "uncertain"}
_STATUSES = {"running", "succeeded", "partial", "failed", "skipped", "needs_human"}
_SCOPE_FIELDS = ("cluster_id", "namespace", "service", "workload_kind", "workload_name")
_SECRET = re.compile(
    r"(?i)(bearer\s+|(?:authorization|credential|password|secret|token|api[_-]?key)\s*[:=]\s*)[^\s,;]+"
)
_SECURE_INPUT = re.compile(r"\{\{secure-input:[^}]+\}\}", re.IGNORECASE)


def project_decision_trace(payload: JSON) -> JSON:
    """Whitelist accepted goals, tool facts, candidate relations, and stop metadata."""
    diagnosis = payload.get("diagnosis")
    hypothesis = payload.get("hypothesis_state")
    validation = payload.get("completion_validation")
    diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
    hypothesis = hypothesis if isinstance(hypothesis, dict) else {}
    validation = validation if isinstance(validation, dict) else {}
    candidates = _candidates(hypothesis, diagnosis)
    activities = payload.get("tool_activity")
    activities = activities if isinstance(activities, list) else []
    stopping_reason = _text(validation.get("stopping_reason") or payload.get("status") or "unknown")
    projected = [
        _activity(item, candidates, stopping_reason=stopping_reason if index == len(activities) - 1 else None)
        for index, item in enumerate(activities[:100])
        if isinstance(item, dict)
    ]
    return {
        "goal": _text(hypothesis.get("goal") or diagnosis.get("summary") or "定位 Incident 根因"),
        "tool_activity": projected,
        "candidates": candidates,
        "completion": {
            "status": _text(validation.get("status") or "unknown"),
            "issues": _texts(validation.get("issues"), limit=20),
            "repair_attempts": _integer(validation.get("repair_attempts"), maximum=1),
            "stopping_reason": stopping_reason,
            "remaining_evidence_steps": _integer(validation.get("remaining_evidence_steps"), maximum=10_000),
        },
    }


def project_diagnosis_output(payload: JSON, *, recommended_action_ids: list[str]) -> JSON:
    """Build the complete safe payload for one durable diagnosis.output event."""
    trace = project_decision_trace(payload)
    diagnosis = payload.get("diagnosis")
    diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
    confidence = diagnosis.get("confidence")
    confidence = confidence if isinstance(confidence, dict) else {}
    human_inputs = diagnosis.get("human_input_event_ids")
    human_inputs = human_inputs if isinstance(human_inputs, list) else []
    missing = payload.get("missing_evidence")
    missing = missing if isinstance(missing, list) else []
    return {
        "status": _text(payload.get("status") or "failed"),
        "diagnosis": {
            "summary": _text(diagnosis.get("summary") or "Diagnosis 未提供摘要"),
            "root_cause_candidates": trace["candidates"],
            "confidence": {
                "score": _confidence_score(confidence.get("score")),
                "level": str(confidence.get("level"))
                if confidence.get("level") in {"high", "medium", "low"}
                else "low",
            },
            "recommended_action_ids": [_text(item) for item in recommended_action_ids[:100]],
            "human_input_event_ids": [
                item for item in human_inputs[:100] if isinstance(item, int) and not isinstance(item, bool) and item > 0
            ],
            "next_verification": _texts(diagnosis.get("next_verification"), limit=20),
        },
        "missing_evidence": [
            {
                "source_type": _text(item.get("source_type") or "unknown"),
                "tool": _text(item.get("tool") or "unknown"),
                "reason": _text(item.get("reason") or "Evidence unavailable"),
            }
            for item in missing[:100]
            if isinstance(item, dict)
        ],
        "decision_trace": trace,
    }


def _activity(item: JSON, candidates: list[JSON], *, stopping_reason: str | None) -> JSON:
    evidence_refs = _references(item.get("evidence_references") or item.get("evidence_ref"))
    status = str(item.get("status")) if item.get("status") in _STATUSES else "failed"
    result: JSON = {
        "tool": _text(item.get("tool") or "unknown"),
        "purpose": _text(item.get("purpose") or "检查工具 Observation"),
        "authorized_scope": _scope(item.get("authorized_scope")),
        "time_range": _bounded_mapping(item.get("time_range")),
        "status": status,
        "duration_ms": _integer_or_none(item.get("duration_ms"), maximum=24 * 60 * 60 * 1000),
        "summary": _text(item.get("summary") or "工具未返回摘要"),
        "evidence_references": evidence_refs,
        "candidate_impacts": [
            {"cause": candidate["cause"], "relation": relation["relation"]}
            for candidate in candidates
            for relation in candidate.get("evidence_relations", [])  # type: ignore[union-attr]
            if isinstance(relation, dict) and relation.get("evidence_ref") in evidence_refs
        ],
        "truncation": _truncation(item.get("truncation")),
        "redaction": _redaction(item.get("redaction")),
    }
    if item.get("missing_reason"):
        result["missing_reason"] = _text(item["missing_reason"])
    if status in {"failed", "skipped"}:
        result["status_reason"] = _text(item.get("missing_reason") or item.get("summary") or "未报告原因")
    if stopping_reason is not None:
        result["stopping_reason"] = stopping_reason
    else:
        result["continuation_reason"] = _text(
            item.get("continuation_reason") or "继续检查剩余候选原因或必需来源"
        )
    return result


def _candidates(hypothesis: JSON, diagnosis: JSON) -> list[JSON]:
    confidences = {
        str(candidate.get("cause")): candidate.get("confidence")
        for candidate in diagnosis.get("root_cause_candidates", [])  # type: ignore[union-attr]
        if isinstance(candidate, dict)
    } if isinstance(diagnosis.get("root_cause_candidates"), list) else {}
    result: list[JSON] = []
    raw_candidates = hypothesis.get("candidates")
    for candidate in raw_candidates[:8] if isinstance(raw_candidates, list) else []:
        if not isinstance(candidate, dict) or not candidate.get("cause"):
            continue
        cause = _text(candidate["cause"])
        relations = [
            {"evidence_ref": _text(item["evidence_ref"]), "relation": str(item["relation"])}
            for item in candidate.get("evidence_relations", [])[:100]
            if isinstance(item, dict)
            and item.get("evidence_ref")
            and item.get("relation") in _RELATIONS
        ] if isinstance(candidate.get("evidence_relations"), list) else []
        projected: JSON = {
            "cause": cause,
            "evidence_relations": relations,
            "unknowns": _texts(candidate.get("unknowns"), limit=20),
            "next_checks": _texts(candidate.get("next_checks"), limit=20),
        }
        confidence = confidences.get(str(candidate.get("cause")))
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            projected["confidence"] = min(1.0, max(0.0, float(confidence)))
        result.append(projected)
    return result


def _references(value: object) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in values[:100]:
        if isinstance(item, dict):
            item = item.get("ref_id") or item.get("source_ref") or item.get("id")
        if item:
            reference = _text(item)
            if reference not in result:
                result.append(reference)
    return result


def _scope(value: object) -> JSON:
    if not isinstance(value, dict):
        return {}
    return {field: _text(value[field]) for field in _SCOPE_FIELDS if value.get(field)}


def _bounded_mapping(value: object) -> JSON:
    if not isinstance(value, dict):
        return {}
    result: JSON = {}
    for key, child in list(value.items())[:8]:
        if isinstance(child, dict):
            result[_text(key)] = {
                _text(nested_key): _text(nested_value)
                for nested_key, nested_value in list(child.items())[:8]
            }
        elif isinstance(child, (str, int, float, bool)):
            result[_text(key)] = _text(child) if isinstance(child, str) else child
    return result


def _truncation(value: object) -> JSON:
    value = value if isinstance(value, dict) else {}
    truncated = bool(value.get("truncated"))
    limit_bytes = _integer(value.get("limit_bytes"), maximum=10_000_000)
    result: JSON = {"truncated": truncated, "limit_bytes": limit_bytes}
    if truncated:
        result["reason"] = _text(
            value.get("reason")
            or (f"已按 {limit_bytes} 字节安全上限截断" if limit_bytes else "安全投影已截断结果")
        )
    return result


def _redaction(value: object) -> JSON:
    value = value if isinstance(value, dict) else {}
    return {
        "applied": bool(value.get("applied")),
        "note": _text(value.get("note") or ("敏感字段已移除" if value.get("applied") else "未报告脱敏")),
    }


def _texts(value: object, *, limit: int) -> list[str]:
    return [_text(item) for item in value[:limit] if item] if isinstance(value, list) else []


def _integer(value: object, *, maximum: int) -> int:
    number = _integer_or_none(value, maximum=maximum)
    return number if number is not None else 0


def _confidence_score(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _integer_or_none(value: object, *, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return min(maximum, max(0, int(value)))
    except (TypeError, ValueError):
        return None


def _text(value: object) -> str:
    text = _SECURE_INPUT.sub("[REDACTED]", str(value or ""))
    text = _SECRET.sub(r"\1[REDACTED]", text)
    return text.encode("utf-8")[:1000].decode("utf-8", errors="ignore")
