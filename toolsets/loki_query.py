"""Loki 查询工具与 AIO-48 query_logs facade。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

try:
    from .query_guard import resolve_service_url, validate_loki_query
except ImportError:  # pragma: no cover - 兼容脚本式直接导入
    from query_guard import resolve_service_url, validate_loki_query

try:  # pragma: no cover - Hermes registry 在本地轻量测试环境可能不存在
    from tools.registry import registry
except ImportError:  # pragma: no cover
    registry = None

logger = logging.getLogger(__name__)

_QUERY_TIMEOUT_SECONDS = 15
_SLOW_QUERY_SECONDS = 10
_MAX_LOG_LOOKBACK = timedelta(hours=2)
_DEFAULT_MAX_LINES = 200
_MAX_LINES = 1000
_DEFAULT_SAMPLE_SIZE = 20
_MAX_SAMPLE_SIZE = 50
_DEFAULT_MAX_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_RESPONSE_MODES = {"summary_only", "summary_samples", "raw_page", "ref_only"}
_MAX_CURSOR_OFFSET = _MAX_LINES - 1
_SCOPE_LABEL_ALIASES = {
    "namespace": ("namespace",),
    "service": ("service", "app"),
    "pod": ("pod",),
    "container": ("container",),
    "workload": ("workload",),
}

LokiRunner = Callable[..., Awaitable[dict[str, Any]]]


class LokiBackendUnavailable(Exception):
    """Loki 后端或依赖不可用。"""


class LokiQueryTimeout(Exception):
    """Loki 查询超时。"""


QUERY_LOGS_SCHEMA = {
    "name": "query_logs",
    "description": "按 AIO-48 V1 契约查询 Loki 日志，默认返回摘要、聚合模式、样本和 evidence refs。",
    "parameters": {
        "type": "object",
        "required": ["cluster_id", "time_range", "reason"],
        "properties": {
            "request_id": {"type": "string"},
            "correlation_id": {"type": "string"},
            "actor": {"type": "object"},
            "agent_id": {"type": "string"},
            "brain_provider": {"type": "string"},
            "cluster_id": {"type": "string"},
            "namespace": {"type": "string"},
            "service": {"type": "string"},
            "workload": {"type": "object"},
            "pod": {"type": "string"},
            "container": {"type": "string"},
            "time_range": {"type": "object"},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "severity": {"type": "array", "items": {"type": "string"}},
            "logql": {"type": "string"},
            "response_mode": {
                "type": "string",
                "enum": ["summary_only", "summary_samples", "raw_page", "ref_only"],
                "default": "summary_samples",
            },
            "max_lines": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
            "sample_size": {"type": "integer", "minimum": 0, "maximum": 50, "default": 20},
            "cursor": {"type": "string"},
            "reason": {"type": "string"},
        },
    },
}


async def loki_query(
    query: str,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """执行带安全护栏的旧版 LogQL 查询。"""
    guard = await validate_loki_query(query=query, start=start, end=end, limit=limit)
    if not guard["allowed"]:
        return guard

    try:
        result = await _default_loki_runner(
            query=guard["query"],
            start=guard["start"],
            end=guard["end"],
            limit=guard["limit"],
        )
    except LokiQueryTimeout:
        return {
            "allowed": True,
            "query": guard["query"],
            "start": guard["start"],
            "end": guard["end"],
            "limit": guard["limit"],
            "error": "Loki 查询超时（15s）",
            "results": [],
        }
    except LokiBackendUnavailable as exc:
        return {
            "allowed": True,
            "query": guard["query"],
            "start": guard["start"],
            "end": guard["end"],
            "limit": guard["limit"],
            "error": str(exc),
            "results": [],
        }
    except Exception as exc:
        return {
            "allowed": True,
            "query": guard["query"],
            "start": guard["start"],
            "end": guard["end"],
            "limit": guard["limit"],
            "error": f"Loki 查询失败: {exc}",
            "results": [],
        }

    return {
        "allowed": True,
        "query": guard["query"],
        "start": guard["start"],
        "end": guard["end"],
        "limit": guard["limit"],
        "elapsed_seconds": result.get("elapsed_seconds"),
        "results": result.get("results", []),
    }


async def query_logs(args: dict[str, Any], runner: LokiRunner | None = None) -> dict[str, Any]:
    """按 AIO-48 V1 契约查询日志并返回统一 envelope。"""
    requested_at = _now()
    request_id = _clean_str(args.get("request_id")) or f"req-log-{uuid.uuid4().hex[:12]}"
    correlation_id = _clean_str(args.get("correlation_id")) or None
    response_mode = _clean_str(args.get("response_mode")) or "summary_samples"
    cluster_id = _clean_str(args.get("cluster_id"))
    namespace = _clean_str(args.get("namespace"))
    service = _clean_str(args.get("service"))
    reason = _clean_str(args.get("reason"))

    actor = args.get("actor") if isinstance(args.get("actor"), dict) else {}
    workload = args.get("workload") if isinstance(args.get("workload"), dict) else {}
    base = _base_envelope(
        request_id=request_id,
        correlation_id=correlation_id,
        requested_at=requested_at,
        cluster_id=cluster_id,
        namespace=namespace,
        service=service,
        reason=reason,
        actor=actor,
        agent_id=_clean_str(args.get("agent_id")),
        brain_provider=_clean_str(args.get("brain_provider")),
        resource_kind=_clean_str(workload.get("kind")) if isinstance(workload, dict) else "",
        resource_name=_clean_str(workload.get("name")) if isinstance(workload, dict) else "",
    )

    invalid_error = _validate_contract(args, response_mode=response_mode)
    if invalid_error:
        envelope = _finish_envelope(base, "failed", "请求不符合 query_logs 契约。", {}, [], [invalid_error], "rejected")
        _record_audit_safely(envelope)
        return envelope

    try:
        max_lines = _bounded_int(args.get("max_lines"), _DEFAULT_MAX_LINES, 1, _MAX_LINES)
        sample_size = _bounded_int(args.get("sample_size"), _DEFAULT_SAMPLE_SIZE, 0, _MAX_SAMPLE_SIZE)
    except (TypeError, ValueError) as exc:
        envelope = _finish_envelope(
            base,
            "failed",
            "请求不符合 query_logs 契约。",
            {},
            [],
            [_error("invalid_request", str(exc), {})],
            "rejected",
        )
        _record_audit_safely(envelope)
        return envelope

    time_error, start, end = _resolve_time_range(args.get("time_range"))
    if time_error:
        envelope = _finish_envelope(
            base,
            "failed",
            "请求不符合 query_logs 契约。",
            {},
            [],
            [time_error],
            "rejected",
        )
        _record_audit_safely(envelope)
        return envelope

    query = _build_logql(args)
    query_digest = _query_digest(query)
    time_range_text = f"{start}/{end}"
    cursor_offset = _decode_cursor(args.get("cursor"))
    backend_limit = min(_MAX_LINES, cursor_offset + max_lines + 1)

    cost_error = _validate_query_cost(
        cluster_id=cluster_id,
        namespace=namespace,
        service=service,
        response_mode=response_mode,
        start=start,
        end=end,
        max_lines=max_lines,
        cursor_offset=cursor_offset,
        backend_limit=backend_limit,
        query=query,
        args=args,
    )
    if cost_error:
        envelope = _finish_envelope(base, "failed", cost_error["message"], {}, [], [cost_error], "rejected")
        envelope["audit"]["query_digest"] = query_digest
        _record_audit_safely(envelope)
        return envelope

    evidence_ref = _evidence_ref(
        cluster_id=cluster_id,
        namespace=namespace,
        service=service,
        time_range=time_range_text,
        query_digest=query_digest,
        cursor=args.get("cursor"),
    )

    if response_mode == "ref_only":
        data = {
            "total_matched": 0,
            "returned_lines": 0,
            "grouped_patterns": [],
            "samples": [],
        }
        envelope = _finish_envelope(base, "succeeded", "已返回日志 evidence ref，未拉取原始日志。", data, [evidence_ref], [], "allowed")
        envelope["next_cursor"] = _clean_str(args.get("cursor"))
        envelope["audit"]["query_digest"] = query_digest
        _record_audit_safely(envelope)
        return envelope

    guard = await validate_loki_query(query=query, start=start, end=end, limit=backend_limit)
    if not guard["allowed"]:
        envelope = _finish_envelope(
            base,
            "failed",
            guard.get("message", "LogQL 查询被拒绝。"),
            {},
            [],
            [_error("query_rejected", guard.get("message", "LogQL 查询被拒绝。"), {"query_digest": query_digest})],
            "rejected",
        )
        envelope["audit"]["query_digest"] = query_digest
        _record_audit_safely(envelope)
        return envelope

    started_at = time.perf_counter()
    try:
        query_result = await (runner or _default_loki_runner)(
            query=guard["query"],
            start=guard["start"],
            end=guard["end"],
            limit=guard["limit"],
        )
    except LokiQueryTimeout:
        envelope = _finish_envelope(
            base,
            "failed",
            "Loki 查询超时。",
            {},
            [evidence_ref],
            [_error("timeout", "Loki query timed out", {"timeout_seconds": _QUERY_TIMEOUT_SECONDS})],
            "rejected",
        )
        envelope["audit"]["query_digest"] = query_digest
        _record_audit_safely(envelope)
        return envelope
    except LokiBackendUnavailable as exc:
        envelope = _finish_envelope(
            base,
            "failed",
            "Loki 后端不可用。",
            {},
            [],
            [_error("backend_unavailable", str(exc), {"cluster_id": cluster_id})],
            "rejected",
        )
        envelope["audit"]["query_digest"] = query_digest
        _record_audit_safely(envelope)
        return envelope
    except Exception as exc:
        envelope = _finish_envelope(
            base,
            "failed",
            "Loki 查询失败。",
            {},
            [],
            [_error("backend_unavailable", f"Loki query failed: {exc}", {"cluster_id": cluster_id})],
            "rejected",
        )
        envelope["audit"]["query_digest"] = query_digest
        _record_audit_safely(envelope)
        return envelope

    elapsed = time.perf_counter() - started_at
    if elapsed > _SLOW_QUERY_SECONDS:
        logger.warning("Loki 慢查询: elapsed=%.2fs query_digest=%s", elapsed, query_digest)

    all_lines = _extract_log_lines(query_result.get("results", []))
    visible_lines = all_lines[cursor_offset:]
    returned_lines = visible_lines[:max_lines]
    has_more = len(visible_lines) > max_lines
    next_cursor = _encode_cursor(cursor_offset + len(returned_lines)) if has_more else None
    samples = _samples(returned_lines, sample_size) if response_mode == "summary_samples" else []
    grouped_patterns = _group_patterns(returned_lines, evidence_ref["ref_id"]) if response_mode in {"summary_only", "summary_samples"} else []

    if response_mode == "summary_samples":
        returned_count = len(samples)
    elif response_mode == "raw_page":
        returned_count = len(returned_lines)
    else:
        returned_count = 0

    data: dict[str, Any] = {
        "total_matched": len(all_lines),
        "returned_lines": returned_count,
        "grouped_patterns": grouped_patterns,
        "samples": samples,
    }
    if response_mode == "raw_page":
        data["raw_lines"] = returned_lines

    summary = _build_summary(
        cluster_id=cluster_id,
        namespace=namespace,
        service=service,
        total_matched=len(all_lines),
        returned_lines=data["returned_lines"],
        response_mode=response_mode,
    )

    errors = []
    status = "succeeded"
    truncated = has_more
    if truncated:
        status = "partial"
        errors.append(
            _error(
                "output_truncated",
                "log response exceeded max_lines; use next_cursor to continue",
                {"max_lines": max_lines, "returned_lines": len(returned_lines)},
            )
        )

    envelope = _finish_envelope(base, status, summary, data, [evidence_ref], errors, "partial" if truncated else "allowed")
    envelope["truncated"] = truncated
    envelope["next_cursor"] = next_cursor
    envelope["limits"]["returned_bytes"] = _json_size(data)
    envelope["audit"]["query_digest"] = query_digest
    envelope["audit"]["returned_bytes"] = envelope["limits"]["returned_bytes"]
    envelope["audit"]["truncated"] = truncated
    envelope["audit"]["elapsed_seconds"] = round(elapsed, 3)
    envelope = _truncate_response_if_needed(envelope)
    _record_audit_safely(envelope)
    return envelope


async def _default_loki_runner(query: str, start: str, end: str, limit: int) -> dict[str, Any]:
    """真实 Loki HTTP runner，依赖延迟导入。"""
    loki_url = await resolve_service_url("LOKI_URL", ("sre", "loki_url"))
    if not loki_url:
        raise LokiBackendUnavailable("未配置 LOKI_URL，无法执行查询")

    try:
        import httpx
    except ImportError as exc:
        raise LokiBackendUnavailable("缺少 httpx 依赖，无法执行 Loki HTTP 查询") from exc

    params = {
        "query": query,
        "start": start,
        "end": end,
        "limit": limit,
        "direction": "backward",
    }
    started_at = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=_QUERY_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{loki_url.rstrip('/')}/loki/api/v1/query_range", params=params)
            response.raise_for_status()
            payload = response.json()
    except httpx.TimeoutException as exc:
        raise LokiQueryTimeout() from exc
    except httpx.HTTPError as exc:
        raise LokiBackendUnavailable(f"Loki 查询失败: {exc}") from exc
    except Exception as exc:
        raise LokiBackendUnavailable(f"Loki 响应解析失败: {exc}") from exc

    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    results = data.get("result", []) if isinstance(data, dict) else []
    return {
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        "results": results,
    }


def _validate_contract(args: dict[str, Any], response_mode: str) -> dict[str, Any] | None:
    """校验 query_logs 的必填字段和枚举字段。"""
    missing = []
    for field in ("cluster_id", "time_range", "reason"):
        value = args.get(field)
        if isinstance(value, str):
            empty = not value.strip()
        else:
            empty = value in (None, {})
        if empty:
            missing.append(field)

    if missing:
        return _error("invalid_request", "missing required fields", {"missing_fields": missing})

    if response_mode not in _RESPONSE_MODES:
        return _error("invalid_request", "response_mode is not supported", {"response_mode": response_mode})

    time_range = args.get("time_range")
    if not isinstance(time_range, dict):
        return _error("invalid_request", "time_range must be an object", {"time_range": time_range})

    return None


def _resolve_time_range(value: Any) -> tuple[dict[str, Any] | None, str, str]:
    """解析 AIO-48 time_range，返回 UTC ISO8601 起止时间。"""
    now = _now_dt()
    if not isinstance(value, dict):
        return _error("invalid_request", "time_range must be an object", {"time_range": value}), "", ""

    range_type = _clean_str(value.get("type")) or "relative"
    try:
        if range_type == "relative":
            delta = _parse_relative_duration(_clean_str(value.get("value")) or "last_30m")
            end_time = now
            start_time = now - delta
        elif range_type == "absolute":
            start_time = _parse_time(_clean_str(value.get("start")))
            end_time = _parse_time(_clean_str(value.get("end")))
        else:
            return _error("invalid_request", "time_range.type is not supported", {"type": range_type}), "", ""
    except ValueError as exc:
        return _error("invalid_request", str(exc), {"time_range": value}), "", ""

    if start_time >= end_time:
        return _error("invalid_request", "time_range start must be before end", {"time_range": value}), "", ""
    if end_time - start_time > _MAX_LOG_LOOKBACK:
        return (
            _error(
                "query_cost_exceeded",
                "logs time_range exceeds maximum 2h",
                {"max_time_range": "2h", "requested_time_range_seconds": int((end_time - start_time).total_seconds())},
            ),
            "",
            "",
        )
    return None, _format_time(start_time), _format_time(end_time)


def _parse_relative_duration(value: str) -> timedelta:
    """解析 last_30m/1h/2h 这类相对时间。"""
    raw = value.strip().lower()
    if raw.startswith("last_"):
        raw = raw[5:]
    match = re.fullmatch(r"(\d+)([smhd])", raw)
    if not match:
        raise ValueError("time_range.value must look like last_30m")

    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "s":
        delta = timedelta(seconds=amount)
    elif unit == "m":
        delta = timedelta(minutes=amount)
    elif unit == "h":
        delta = timedelta(hours=amount)
    else:
        delta = timedelta(days=amount)

    if delta <= timedelta(0):
        raise ValueError("time_range.value must be positive")
    return delta


def _parse_time(value: str) -> datetime:
    if not value:
        raise ValueError("absolute time_range requires start and end")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_query_cost(
    cluster_id: str,
    namespace: str,
    service: str,
    response_mode: str,
    start: str,
    end: str,
    max_lines: int,
    cursor_offset: int,
    backend_limit: int,
    query: str,
    args: dict[str, Any],
) -> dict[str, Any] | None:
    """执行 V1 默认成本和 prod raw_page 宽查询保护。"""
    del start, end
    if cursor_offset > _MAX_CURSOR_OFFSET or cursor_offset + max_lines > _MAX_LINES:
        return _error(
            "query_cost_exceeded",
            "cursor offset exceeds maximum safe pagination window",
            {"cursor_offset": cursor_offset, "max_cursor_offset": _MAX_CURSOR_OFFSET, "max_backend_limit": _MAX_LINES},
        )
    if response_mode == "raw_page" and cluster_id.lower().startswith("prod") and not (namespace or service):
        return _error(
            "query_cost_exceeded",
            "log query is too broad; namespace or service is required for prod raw_page mode",
            {"cluster_id": cluster_id, "response_mode": response_mode},
        )
    if response_mode == "raw_page" and cluster_id.lower().startswith("prod") and _clean_str(args.get("logql")):
        missing_scope = _missing_raw_logql_scope(query=query, args=args)
        if missing_scope:
            return _error(
                "query_cost_exceeded",
                "raw LogQL must include request scope labels for prod raw_page mode",
                {"cluster_id": cluster_id, "response_mode": response_mode, "missing_scope": missing_scope},
            )
    if max_lines > _MAX_LINES:
        return _error("query_cost_exceeded", "max_lines exceeds maximum 1000", {"max_lines": max_lines})
    return None


def _missing_raw_logql_scope(query: str, args: dict[str, Any]) -> list[str]:
    """检查 raw LogQL selector 是否包含请求里声明的等价 scope。"""
    labels = _extract_first_selector_labels(query)
    if not labels:
        return ["selector"]

    missing = []
    scoped_fields = ("namespace", "service", "pod", "container")
    for field in scoped_fields:
        expected = _clean_str(args.get(field))
        if not expected:
            continue
        aliases = _SCOPE_LABEL_ALIASES[field]
        if not any(labels.get(alias) == expected for alias in aliases):
            missing.append(field)

    if not missing and not any(_clean_str(args.get(field)) for field in scoped_fields) and not isinstance(args.get("workload"), dict):
        return ["scope"]
    return missing


def _extract_first_selector_labels(query: str) -> dict[str, str]:
    """提取 LogQL 第一个 selector 中的等值标签。"""
    match = re.search(r"\{([^{}]*)\}", query)
    if not match:
        return {}

    labels: dict[str, str] = {}
    for item in match.group(1).split(","):
        item = item.strip()
        label_match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:\\.|[^"\\])*)"', item)
        if not label_match:
            continue
        key = label_match.group(1)
        value = label_match.group(2).replace(r"\"", '"').replace(r"\\", "\\")
        labels[key] = value
    return labels


def _build_logql(args: dict[str, Any]) -> str:
    """从 typed 参数或原始 logql 构造查询。"""
    raw_logql = _clean_str(args.get("logql"))
    if raw_logql:
        return raw_logql

    labels = []
    for key in ("namespace", "service", "pod", "container"):
        value = _clean_str(args.get(key))
        if value:
            loki_key = "app" if key == "service" else key
            labels.append(f'{loki_key}="{_escape_label(value)}"')

    workload = args.get("workload")
    if isinstance(workload, dict):
        workload_name = _clean_str(workload.get("name"))
        if workload_name and "pod" not in args:
            labels.append(f'workload="{_escape_label(workload_name)}"')

    selector = "{" + ",".join(labels) + "}" if labels else '{job=~".+"}'
    for keyword in _as_str_list(args.get("keywords")):
        selector += f' |= "{_escape_filter(keyword)}"'
    severities = _as_str_list(args.get("severity"))
    if severities:
        severity_filter = "|".join(re.escape(item) for item in severities)
        selector += f' |~ "(?i)({severity_filter})"'
    return selector


def _extract_log_lines(results: Any) -> list[dict[str, Any]]:
    """将 Loki stream result 展平为结构化日志行。"""
    lines: list[dict[str, Any]] = []
    if not isinstance(results, list):
        return lines

    for stream_result in results:
        if not isinstance(stream_result, dict):
            continue
        stream = stream_result.get("stream", {})
        if not isinstance(stream, dict):
            stream = {}
        values = stream_result.get("values", [])
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, (list, tuple)) or len(value) < 2:
                continue
            ts = _loki_ts_to_iso(str(value[0]))
            line = str(value[1])
            lines.append(
                {
                    "ts": ts,
                    "pod": _clean_str(stream.get("pod")),
                    "container": _clean_str(stream.get("container")),
                    "line": _truncate_line(line),
                }
            )

    return lines


def _samples(lines: list[dict[str, Any]], sample_size: int) -> list[dict[str, Any]]:
    """返回样本行。"""
    if sample_size <= 0:
        return []
    return lines[:sample_size]


def _group_patterns(lines: list[dict[str, Any]], sample_ref: str) -> list[dict[str, Any]]:
    """按简化日志模板聚合重复模式。"""
    grouped: dict[str, dict[str, Any]] = {}
    for item in lines:
        template = _message_template(item["line"])
        fingerprint = "fp-" + hashlib.sha256(template.encode("utf-8")).hexdigest()[:12]
        group = grouped.setdefault(
            fingerprint,
            {
                "fingerprint": fingerprint,
                "message_template": template,
                "count": 0,
                "first_seen": item["ts"],
                "last_seen": item["ts"],
                "pods": [],
                "severity": _detect_severity(item["line"]),
                "sample_ref": sample_ref,
            },
        )
        group["count"] += 1
        group["first_seen"] = min(group["first_seen"], item["ts"])
        group["last_seen"] = max(group["last_seen"], item["ts"])
        if item.get("pod") and item["pod"] not in group["pods"]:
            group["pods"].append(item["pod"])

    return sorted(grouped.values(), key=lambda item: item["count"], reverse=True)


def _message_template(line: str) -> str:
    """生成可读的简化日志模板。"""
    value = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", line, flags=re.IGNORECASE)
    value = re.sub(r"\b\d+\b", "<num>", value)
    value = re.sub(r"request_id=[^\s]+", "request_id=<id>", value)
    return value[:240]


def _detect_severity(line: str) -> str:
    lower = line.lower()
    if "fatal" in lower or "panic" in lower:
        return "critical"
    if "error" in lower or "exception" in lower:
        return "error"
    if "warn" in lower:
        return "warning"
    return "info"


def _build_summary(cluster_id: str, namespace: str, service: str, total_matched: int, returned_lines: int, response_mode: str) -> str:
    target = "/".join(item for item in (cluster_id, namespace, service) if item) or cluster_id
    if total_matched == 0:
        return f"{target} 在指定时间窗内未返回匹配日志。"
    if response_mode == "summary_only":
        return f"{target} 在指定时间窗内匹配 {total_matched} 条日志，已返回聚合模式摘要。"
    if response_mode == "raw_page":
        return f"{target} 在指定时间窗内匹配 {total_matched} 条日志，本页返回 {returned_lines} 条原始日志。"
    return f"{target} 在指定时间窗内匹配 {total_matched} 条日志，返回 {returned_lines} 条样本和聚合模式。"


def _base_envelope(
    request_id: str,
    correlation_id: str | None,
    requested_at: str,
    cluster_id: str,
    namespace: str,
    service: str,
    reason: str,
    actor: dict[str, Any],
    agent_id: str,
    brain_provider: str,
    resource_kind: str,
    resource_name: str,
) -> dict[str, Any]:
    """构造 V1 envelope 基础字段。"""
    return {
        "request_id": request_id,
        "correlation_id": correlation_id,
        "tool_name": "query_logs",
        "status": "failed",
        "summary": "",
        "data": {},
        "evidence_refs": [],
        "truncated": False,
        "next_cursor": None,
        "errors": [],
        "limits": {
            "timeout_seconds": _QUERY_TIMEOUT_SECONDS,
            "max_bytes": _DEFAULT_MAX_BYTES,
            "returned_bytes": 0,
        },
        "audit": {
            "decision": "rejected",
            "request_id": request_id,
            "correlation_id": correlation_id,
            "actor_type": _clean_str(actor.get("actor_type")) or None,
            "actor_id": _clean_str(actor.get("actor_id")) or None,
            "agent_id": agent_id or None,
            "brain_provider": brain_provider or None,
            "tool_name": "query_logs",
            "requested_at": requested_at,
            "finished_at": requested_at,
            "cluster_id": cluster_id,
            "namespace": namespace or None,
            "service": service or None,
            "resource_kind": resource_kind or None,
            "resource_name": resource_name or None,
            "reason": reason,
            "risk_level": "none",
        },
    }


def _finish_envelope(
    envelope: dict[str, Any],
    status: str,
    summary: str,
    data: dict[str, Any],
    evidence_refs: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    decision: str,
) -> dict[str, Any]:
    """补齐 envelope 结束字段。"""
    envelope["status"] = status
    envelope["summary"] = summary
    envelope["data"] = data
    envelope["evidence_refs"] = evidence_refs
    envelope["errors"] = errors
    envelope["limits"]["returned_bytes"] = _json_size(data)
    envelope["audit"]["decision"] = decision
    envelope["audit"]["finished_at"] = _now()
    envelope["audit"]["returned_bytes"] = envelope["limits"]["returned_bytes"]
    envelope["audit"]["truncated"] = envelope["truncated"]
    envelope["audit"]["error_code"] = errors[0]["code"] if errors else None
    return envelope


def _truncate_response_if_needed(envelope: dict[str, Any]) -> dict[str, Any]:
    """控制响应大小，超过默认 256KB 时降级为 refs + cursor。"""
    size = _json_size(envelope)
    if size <= _DEFAULT_MAX_BYTES:
        return envelope

    envelope["data"]["samples"] = envelope["data"].get("samples", [])[:5]
    envelope["data"]["grouped_patterns"] = envelope["data"].get("grouped_patterns", [])[:50]
    envelope["data"].pop("raw_lines", None)
    envelope["truncated"] = True
    envelope["status"] = "partial"
    envelope["summary"] = f"{envelope['summary']} 响应体超过 256KB，已降级为摘要和 evidence refs。"
    envelope["errors"].append(
        _error(
            "output_truncated",
            "response body exceeds max_bytes",
            {"max_bytes": _DEFAULT_MAX_BYTES, "returned_bytes_before_truncation": size},
        )
    )
    envelope["limits"]["returned_bytes"] = _json_size(envelope["data"])
    envelope["audit"]["decision"] = "partial"
    envelope["audit"]["returned_bytes"] = envelope["limits"]["returned_bytes"]
    envelope["audit"]["truncated"] = True
    envelope["audit"]["error_code"] = "output_truncated"

    while _json_size(envelope) > _DEFAULT_MAX_BYTES and envelope["data"].get("grouped_patterns"):
        envelope["data"]["grouped_patterns"] = envelope["data"]["grouped_patterns"][: max(0, len(envelope["data"]["grouped_patterns"]) // 2)]

    while _json_size(envelope) > _DEFAULT_MAX_BYTES and envelope["data"].get("samples"):
        envelope["data"]["samples"] = envelope["data"]["samples"][: max(0, len(envelope["data"]["samples"]) // 2)]

    envelope["limits"]["returned_bytes"] = _json_size(envelope["data"])
    envelope["audit"]["returned_bytes"] = envelope["limits"]["returned_bytes"]
    if _json_size(envelope) > _DEFAULT_MAX_BYTES:
        envelope["data"]["grouped_patterns"] = []
        envelope["data"]["samples"] = []
        envelope["limits"]["returned_bytes"] = _json_size(envelope["data"])
        envelope["audit"]["returned_bytes"] = envelope["limits"]["returned_bytes"]
    return envelope


def _record_audit_safely(envelope: dict[str, Any]) -> None:
    """包装审计写入，避免测试 monkeypatch 或运行时异常穿透 facade。"""
    try:
        _record_query_logs_audit(envelope)
    except Exception as exc:
        logger.warning("query_logs audit failed open: %s", exc)


def _record_query_logs_audit(envelope: dict[str, Any]) -> None:
    """尽力写入既有 audit_log，不让审计失败影响查询结果。"""
    try:
        from . import audit_log
    except Exception as exc:  # pragma: no cover - 兼容脚本式直接导入
        logger.warning("query_logs audit module import failed: %s", exc)
        try:
            import audit_log  # type: ignore
        except Exception as fallback_exc:
            logger.warning("query_logs audit fallback import failed: %s", fallback_exc)
            return

    actor = envelope.get("audit", {})
    what = {
        "tool_name": envelope.get("tool_name"),
        "query_digest": actor.get("query_digest"),
        "decision": actor.get("decision"),
        "error_code": actor.get("error_code"),
        "truncated": actor.get("truncated"),
    }

    async def _record() -> None:
        try:
            await audit_log.record_audit(
                who=str(actor.get("actor_id") or envelope.get("request_id") or "unknown"),
                what=json.dumps(what, ensure_ascii=False),
                cluster=actor.get("cluster_id"),
                namespace=actor.get("namespace"),
                trigger="mcp",
                tool_level="read",
                tool_name="query_logs",
                result=str(envelope.get("status")),
                incident_id=envelope.get("correlation_id"),
            )
        except Exception as exc:
            logger.warning("query_logs audit write failed: %s", exc)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(_record())
        except Exception as exc:
            logger.warning("query_logs audit task failed: %s", exc)
    else:
        try:
            loop.create_task(_record())
        except Exception as exc:
            logger.warning("query_logs audit task scheduling failed: %s", exc)


def _error(code: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"code": code, "message": message, "details": details}


def _evidence_ref(cluster_id: str, namespace: str, service: str, time_range: str, query_digest: str, cursor: Any) -> dict[str, Any]:
    return {
        "ref_id": f"ev-log-{uuid.uuid4().hex[:12]}",
        "source": "loki",
        "cluster_id": cluster_id,
        "namespace": namespace or None,
        "service": service or None,
        "time_range": time_range,
        "query_digest": query_digest,
        "cursor": _clean_str(cursor),
    }


def _query_digest(query: str) -> str:
    return "sha256:" + hashlib.sha256(query.encode("utf-8")).hexdigest()


def _decode_cursor(value: Any) -> int:
    raw = _clean_str(value)
    if not raw:
        return 0
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
    except Exception:
        return 0
    offset = payload.get("offset") if isinstance(payload, dict) else 0
    try:
        return max(0, int(offset))
    except (TypeError, ValueError):
        return 0


def _encode_cursor(offset: int) -> str:
    payload = json.dumps({"offset": offset}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    converted = int(value)
    if converted < minimum:
        raise ValueError(f"value must be >= {minimum}")
    if converted > maximum:
        raise ValueError(f"value must be <= {maximum}")
    return converted


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _escape_filter(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _truncate_line(line: str) -> str:
    if len(line) <= 1200:
        return line
    return f"{line[:600]} ... [truncated {len(line) - 1000} chars] ... {line[-400:]}"


def _loki_ts_to_iso(value: str) -> str:
    try:
        seconds = int(value) / 1_000_000_000
        return _format_time(datetime.fromtimestamp(seconds, tz=timezone.utc))
    except (TypeError, ValueError, OSError):
        return _now()


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _now() -> str:
    return _format_time(_now_dt())


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


async def _tool_query_logs(args: dict[str, Any], **_: Any) -> str:
    """Hermes registry handler。"""
    return json.dumps(await query_logs(args), ensure_ascii=False)


if registry is not None:  # pragma: no cover - 注册行为由集成环境覆盖
    registry.register(
        name="query_logs",
        toolset="sre",
        schema=QUERY_LOGS_SCHEMA,
        handler=_tool_query_logs,
        is_async=True,
        max_result_size_chars=_MAX_RESPONSE_BYTES,
    )
