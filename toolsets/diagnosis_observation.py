"""Bounded, redacted Observation projection for the Diagnosis tool loop."""

from __future__ import annotations

import json
import re
from typing import Any

from toolsets.k8s_redact import redact_k8s_output

MAX_OBSERVATION_BYTES = 4096
_MAX_FACTS = 12
_MAX_SAMPLES = 3
_MAX_TEXT_BYTES = 256
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|credential|password|secret|token|api[_-]?key|secure[_-]?input|system[_-]?prompt|chain[_-]?of[_-]?thought|raw[_-]?payload)",
    re.IGNORECASE,
)
_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+|(?:authorization|credential|password|secret|token|api[_-]?key)\s*[:=]\s*)[^\s,;]+"
)
_SECURE_INPUT = re.compile(r"\{\{secure-input:[^}]+\}\}", re.IGNORECASE)
_PURPOSES = {
    "query_metrics": "检查 Prometheus 指标是否支持当前告警",
    "query_logs": "检查 Loki 日志中的代表性错误",
    "run_k8s_read": "检查授权范围内的 Kubernetes 资源状态",
    "get_service_topology": "检查服务依赖拓扑是否存在异常",
}
_SCOPE_FIELDS = (
    "deployment_target_id", "cluster_id", "namespace", "service_id", "service",
    "workload_kind", "workload_name",
)
_TIME_FIELDS = ("start", "end", "time_range")
_AUDIT_FIELDS = (
    "status",
    "tool_name",
    "reason_code",
    "error_code",
    "selector",
    "selector_conflict",
    "resource_match_count",
)
_SAMPLE_FIELDS = {"series", "lines", "items", "resources", "edges", "samples", "results"}


async def normalize_observation(
    observation: dict[str, Any],
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project one internal tool result without carrying its raw payload forward."""
    args = args or {}
    payload, redacted = await _safe_payload(observation.get("payload"))
    tool = str(observation.get("tool") or "unknown")
    status = str(observation.get("status") or "failed")
    empty = _has_empty_result(payload)
    summary = _safe_text(observation.get("summary") or f"{tool} returned {status}")
    missing_reason = _safe_text(observation.get("missing_reason")) if observation.get("missing_reason") else None
    if status == "succeeded" and empty:
        status = "partial"
        summary = f"{tool} returned no data"
        missing_reason = "tool returned no data"

    facts, samples, truncated = _facts_and_samples(payload)
    result: dict[str, Any] = {
        "tool": tool,
        "status": status,
        "source_type": str(observation.get("source_type") or tool),
        "evidence_ref": _safe_reference(observation.get("evidence_ref")),
        "summary": summary,
        "missing_reason": missing_reason,
        "purpose": _PURPOSES.get(tool, f"检查 {tool} 返回的证据"),
        "authorized_scope": _scope(args),
        "time_range": _time_range(args),
        "key_facts": facts,
        "representative_samples": samples,
        "truncation": {"truncated": truncated, "limit_bytes": MAX_OBSERVATION_BYTES},
        "redaction": {
            "applied": bool(redacted or observation.get("payload")),
            "note": "敏感字段已移除",
        },
        "audit": _audit(observation.get("audit")),
    }
    return _fit(result, truncated)


def activity_from_observation(
    observation: dict[str, Any], *, evidence_step_id: str | None = None
) -> dict[str, Any]:
    activity = dict(observation)
    activity.pop("audit", None)
    activity.pop("id", None)
    if evidence_step_id is not None:
        activity["evidence_step_id"] = evidence_step_id
    return activity


async def _safe_payload(value: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, dict) or not value:
        return {}, False
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    redacted = await redact_k8s_output(raw, "")
    try:
        parsed = json.loads(redacted)
    except (TypeError, ValueError):
        parsed = {"result": redacted}
    return _strip_sensitive(parsed), redacted != raw


def _strip_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _strip_sensitive(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_strip_sensitive(child) for child in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub(r"\1[REDACTED]", _SECURE_INPUT.sub("[REDACTED]", value))
    return value


def _facts_and_samples(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], bool]:
    facts: list[dict[str, Any]] = []
    samples: list[str] = []
    truncated = False
    for key, value in payload.items():
        if len(facts) >= _MAX_FACTS:
            truncated = True
            break
        if isinstance(value, list):
            facts.append({"name": f"{key}_count", "value": len(value)})
            if len(value) > 1:
                truncated = True
            if value and key in _SAMPLE_FIELDS and len(samples) < _MAX_SAMPLES:
                samples.append(_clip_json(value[0], 512))
        elif isinstance(value, dict):
            scalar = {name: child for name, child in value.items() if not isinstance(child, (dict, list))}
            if scalar:
                facts.append({"name": key, "value": _bounded(scalar, 256)})
            else:
                truncated = True
        elif value not in (None, ""):
            facts.append({"name": key, "value": _bounded(value, 256)})
    return facts, samples, truncated


def _has_empty_result(payload: dict[str, Any]) -> bool:
    metadata = {"labels", "query", "selector", "resource_match_count", "match_count", "total_matched"}
    has_empty_collection = any(
        key in _SAMPLE_FIELDS and isinstance(value, list) and not value
        for key, value in payload.items()
    )
    return has_empty_collection and all(
        key in metadata or value in (None, "", [], {}) for key, value in payload.items()
    )


def _scope(args: dict[str, Any]) -> dict[str, str]:
    return {
        field: _clip_text(args.get(field), 160)
        for field in _SCOPE_FIELDS
        if args.get(field) not in (None, "")
    }


def _time_range(args: dict[str, Any]) -> dict[str, Any]:
    return {
        field: _bounded(args[field], 200)
        for field in _TIME_FIELDS
        if field in args and args[field] not in (None, "")
    }


def _audit(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {field: _bounded(value[field], 200) for field in _AUDIT_FIELDS if field in value}


def _safe_reference(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _safe_text(value[key])
            for key in ("ref_id", "source_ref", "id")
            if key in value
        }
    return _safe_text(value) if value else None


def _fit(value: dict[str, Any], truncated: bool) -> dict[str, Any]:
    while len(_encoded(value)) > MAX_OBSERVATION_BYTES:
        samples = value["representative_samples"]
        facts = value["key_facts"]
        if samples:
            samples.pop()
        elif facts:
            facts.pop()
        elif len(value["summary"]) > 64:
            value["summary"] = _clip_text(value["summary"], len(value["summary"]) // 2)
        else:
            value["audit"] = {}
            break
        truncated = True
    value["truncation"]["truncated"] = truncated
    return value


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _bounded(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return _clip_text(value, limit)
    if isinstance(value, dict):
        return {str(key): _bounded(child, limit) for key, child in list(value.items())[:8]}
    if isinstance(value, list):
        return [_bounded(child, limit) for child in value[:3]]
    return value


def _clip_json(value: Any, limit: int) -> str:
    return _clip_text(json.dumps(_bounded(value, limit), ensure_ascii=False, sort_keys=True), limit)


def _safe_text(value: Any) -> str:
    text = _SECURE_INPUT.sub("[REDACTED]", str(value))
    return _clip_text(_SECRET_TEXT.sub(r"\1[REDACTED]", text), _MAX_TEXT_BYTES)


def _clip_text(value: Any, limit: int) -> str:
    text = str(value or "")
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")
