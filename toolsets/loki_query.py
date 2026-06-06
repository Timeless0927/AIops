"""Loki query tools and V1 query_logs facade."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from aiops.contracts import ErrorCode, EvidenceRef, ToolEnvelope, ToolError

if __package__:
    from .query_guard import resolve_service_url, validate_loki_query
else:  # pragma: no cover - compatibility with direct tool loading
    from query_guard import resolve_service_url, validate_loki_query

logger = logging.getLogger(__name__)
_QUERY_TIMEOUT_SECONDS = 30
_SLOW_QUERY_SECONDS = 10

_DEFAULT_MAX_LINES = 200
_DEFAULT_SAMPLE_SIZE = 20
_MAX_SAMPLE_SIZE = 50
_MAX_LINES_CAP = 1000
_MAX_RAW_PAGE_LINES = 100
_MAX_QUERY_WINDOW_SECONDS = 6 * 60 * 60
_MAX_ESTIMATED_COST = 2_000
_MAX_ENVELOPE_BYTES = 24_000
_REF_PREFIX = "ev_loki_"


class LokiBackendError(RuntimeError):
    """Raised when the Loki backend cannot be queried."""


class LokiTimeoutError(LokiBackendError):
    """Raised when the Loki backend times out."""


class LokiRunner(Protocol):
    """Minimal runner protocol used by query_logs."""

    async def query_range(self, query: str, start: str, end: str, limit: int) -> list[dict[str, Any]]:
        """Run a Loki query_range request."""


@dataclass(frozen=True)
class QueryLogsLimits:
    """Normalized query limits exposed in the response data."""

    max_lines: int
    sample_size: int
    max_bytes: int = _MAX_ENVELOPE_BYTES


class HttpLokiRunner:
    """HTTP Loki query_range runner with lazy optional dependency import."""

    async def query_range(self, query: str, start: str, end: str, limit: int) -> list[dict[str, Any]]:
        loki_url = await resolve_service_url("LOKI_URL", ("sre", "loki_url"))
        if not loki_url:
            raise LokiBackendError("未配置 LOKI_URL，无法执行查询")

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on deployment image
            raise LokiBackendError("缺少 httpx 依赖，无法执行 Loki 查询") from exc

        params = {
            "query": query,
            "start": start,
            "end": end,
            "limit": limit,
            "direction": "backward",
        }

        try:
            async with httpx.AsyncClient(timeout=_QUERY_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    f"{loki_url.rstrip('/')}/loki/api/v1/query_range",
                    params=params,
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.TimeoutException as exc:
            raise LokiTimeoutError("Loki 查询超时（30s）") from exc
        except httpx.HTTPError as exc:
            raise LokiBackendError(f"Loki 查询失败: {exc}") from exc
        except Exception as exc:
            raise LokiBackendError(f"Loki 响应解析失败: {exc}") from exc

        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        results = data.get("result", []) if isinstance(data, dict) else []
        if not isinstance(results, list):
            raise LokiBackendError("Loki 响应格式不合法")
        return results


def _error_envelope(
    *,
    request_id: str,
    correlation_id: str | None,
    status: str,
    summary: str,
    code: ErrorCode,
    message: str,
    details: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    audit: dict[str, Any] | None = None,
) -> ToolEnvelope:
    audit_payload = dict(audit or {})
    audit_payload.setdefault("status", status)
    audit_payload.setdefault("returned_bytes", len(json.dumps(data or {}, ensure_ascii=False, default=str).encode("utf-8")))
    audit_payload.setdefault("total_matched", 0)
    audit_payload.setdefault("returned_lines", 0)
    audit_payload.setdefault("truncated", False)
    audit_payload.setdefault("error_code", code.value)
    return ToolEnvelope(
        request_id=request_id,
        correlation_id=correlation_id,
        tool_name="query_logs",
        status=status,
        summary=summary,
        data=data or {},
        audit=audit_payload,
        errors=(ToolError(code=code, message=message, details=details or {}),),
    )


def _stable_digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _encode_cursor(offset: int, digest: str) -> str:
    raw = json.dumps({"offset": offset, "query_digest": digest}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str | None, digest: str) -> int:
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        data = json.loads(raw)
    except Exception as exc:
        raise ValueError("cursor 格式不合法") from exc
    if data.get("query_digest") != digest:
        raise ValueError("cursor 与当前查询不匹配")
    offset = int(data.get("offset", 0))
    if offset < 0:
        raise ValueError("cursor offset 不合法")
    return offset


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_time_range(time_range: Any) -> tuple[str, str, str | None]:
    if not isinstance(time_range, dict):
        raise ValueError("time_range 必须是对象")

    range_type = str(time_range.get("type") or "").strip()
    value = str(time_range.get("value") or "").strip()
    if not range_type or not value:
        raise ValueError("time_range.type 和 time_range.value 必填")

    if range_type == "absolute":
        if "/" not in value:
            raise ValueError("absolute time_range.value 必须是 start/end")
        start_raw, end_raw = value.split("/", 1)
        start_time = _parse_time(start_raw)
        end_time = _parse_time(end_raw)
    elif range_type == "relative":
        match = value.lower()
        units = {"m": "minutes", "h": "hours"}
        if len(match) < 2 or match[-1] not in units:
            raise ValueError("relative time_range.value 仅支持 Nm 或 Nh")
        amount = int(match[:-1])
        if amount <= 0:
            raise ValueError("relative time_range.value 必须大于 0")
        end_time = datetime.now(timezone.utc).replace(microsecond=0)
        start_time = end_time - timedelta(**{units[match[-1]]: amount})
    else:
        raise ValueError("time_range.type 仅支持 absolute 或 relative")

    if start_time >= end_time:
        raise ValueError("time_range start 必须早于 end")
    if (end_time - start_time).total_seconds() > _MAX_QUERY_WINDOW_SECONDS:
        raise OverflowError("查询时间窗口超过 6 小时")
    return _format_timestamp(start_time), _format_timestamp(end_time), f"{range_type}:{value}"


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_mode(args: dict[str, Any]) -> str:
    mode = str(args.get("response_mode") or args.get("mode") or "").strip()
    if not mode:
        if args.get("raw_page"):
            mode = "raw_page"
        elif args.get("ref_only"):
            mode = "ref_only"
        elif args.get("summary_only"):
            mode = "summary_only"
        else:
            mode = "summary_samples"
    if mode not in {"summary_only", "summary_samples", "raw_page", "ref_only"}:
        raise ValueError("mode 必须是 summary_only、summary_samples、raw_page 或 ref_only")
    return mode


def _normalize_limits(args: dict[str, Any], mode: str) -> QueryLogsLimits:
    max_lines = int(args.get("max_lines") or _DEFAULT_MAX_LINES)
    sample_size = int(args.get("sample_size") or args.get("summary_samples") or _DEFAULT_SAMPLE_SIZE)
    if max_lines <= 0:
        raise ValueError("max_lines 必须大于 0")
    if sample_size < 0:
        raise ValueError("sample_size 不能小于 0")
    max_lines = min(max_lines, _MAX_LINES_CAP)
    if mode == "raw_page":
        max_lines = min(max_lines, _MAX_RAW_PAGE_LINES)
    sample_size = min(sample_size, _MAX_SAMPLE_SIZE, max_lines)
    return QueryLogsLimits(max_lines=max_lines, sample_size=sample_size)


def _query_cost(start: str, end: str, max_lines: int, cursor_offset: int) -> int:
    duration_seconds = int((_parse_time(end) - _parse_time(start)).total_seconds())
    window_units = max(1, duration_seconds // 60)
    return window_units * max_lines + cursor_offset


def _requires_scoped_query(query: str) -> bool:
    compact = query.replace(" ", "")
    return compact in {"{}", "{namespace=~\".*\"}", "{namespace=~\".+\"}", "{pod=~\".*\"}", "{pod=~\".+\"}"}


def _extract_lines(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for stream in results:
        labels = stream.get("stream", {}) if isinstance(stream, dict) else {}
        values = stream.get("values", []) if isinstance(stream, dict) else []
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, list | tuple) or len(item) < 2:
                continue
            lines.append(
                {
                    "ts": str(item[0]),
                    "line": str(item[1]),
                    "labels": labels if isinstance(labels, dict) else {},
                }
            )
    return lines


def _line_ref(query_digest: str, line: dict[str, Any]) -> str:
    return f"{_REF_PREFIX}{_stable_digest({'query_digest': query_digest, 'ts': line['ts'], 'line': line['line']})[:16]}"


def _fingerprint(pattern: str) -> str:
    return hashlib.sha256(pattern.encode("utf-8")).hexdigest()[:16]


def _group_patterns(lines: list[dict[str, Any]], query_digest: str) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for item in lines:
        line = item["line"].strip()
        if not line:
            key = "<empty>"
        elif "error" in line.lower():
            key = "error"
        elif "warn" in line.lower():
            key = "warning"
        else:
            key = line[:80]
        current = groups.setdefault(
            key,
            {
                "fingerprint": _fingerprint(key),
                "pattern": key,
                "message_template": key,
                "count": 0,
                "first_seen": item["ts"],
                "last_seen": item["ts"],
                "sample_ref": _line_ref(query_digest, item),
            },
        )
        current["count"] += 1
        current["first_seen"] = min(current["first_seen"], item["ts"])
        current["last_seen"] = max(current["last_seen"], item["ts"])
    return [
        group
        for group in sorted(
            groups.values(),
            key=lambda row: (-int(row["count"]), str(row["message_template"])),
        )[:10]
    ]


def _build_summary(mode: str, lines: list[dict[str, Any]], truncated: bool) -> str:
    suffix = "，结果已截断" if truncated else ""
    if mode == "ref_only":
        return f"Loki query_logs 返回引用，匹配 {len(lines)} 行{suffix}"
    if not lines:
        return "Loki query_logs 未返回日志行"
    return f"Loki query_logs 匹配 {len(lines)} 行{suffix}"


def _ref_id(query_digest: str, offset: int) -> str:
    return f"{_REF_PREFIX}{query_digest[:16]}_{offset}"


def _trim_envelope(envelope: ToolEnvelope) -> ToolEnvelope:
    if len(json.dumps(asdict(envelope), ensure_ascii=False, default=str).encode("utf-8")) <= _MAX_ENVELOPE_BYTES:
        return envelope

    data = dict(envelope.data)
    audit = dict(envelope.audit)
    while data.get("samples"):
        data["samples"] = data["samples"][:-1]
        audit["status"] = "partial"
        audit["returned_bytes"] = len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))
        audit["returned_lines"] = len(data.get("samples", []))
        audit["truncated"] = True
        trimmed = ToolEnvelope(
            request_id=envelope.request_id,
            correlation_id=envelope.correlation_id,
            tool_name=envelope.tool_name,
            status="partial",
            summary=envelope.summary,
            data=data,
            evidence_refs=envelope.evidence_refs,
            audit=audit,
            truncated=True,
            next_cursor=envelope.next_cursor,
            errors=envelope.errors,
        )
        if len(json.dumps(asdict(trimmed), ensure_ascii=False, default=str).encode("utf-8")) <= _MAX_ENVELOPE_BYTES:
            return trimmed

    data.pop("raw_lines", None)
    audit["status"] = "partial"
    audit["returned_bytes"] = len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))
    audit["returned_lines"] = int(data.get("returned_lines") or 0)
    audit["truncated"] = True
    return ToolEnvelope(
        request_id=envelope.request_id,
        correlation_id=envelope.correlation_id,
        tool_name=envelope.tool_name,
        status="partial",
        summary=envelope.summary,
        data=data,
        evidence_refs=envelope.evidence_refs,
        audit=audit,
        truncated=True,
        next_cursor=envelope.next_cursor,
        errors=envelope.errors,
    )


def _base_audit(args: dict[str, Any], query_digest: str | None = None) -> dict[str, Any]:
    return {
        "actor": str(args.get("who") or args.get("user_id") or "unknown"),
        "reason": args.get("reason"),
        "cluster_id": args.get("cluster_id"),
        "namespace": args.get("namespace"),
        "query_digest": query_digest,
        "tool_name": "query_logs",
    }


def _final_audit(
    args: dict[str, Any],
    *,
    query_digest: str,
    status: str,
    returned_bytes: int,
    total_matched: int,
    returned_lines: int,
    truncated: bool,
    error_code: str | None = None,
) -> dict[str, Any]:
    audit = _base_audit(args, query_digest)
    audit.update(
        {
            "status": status,
            "returned_bytes": returned_bytes,
            "total_matched": total_matched,
            "returned_lines": returned_lines,
            "truncated": truncated,
            "error_code": error_code,
        }
    )
    return audit


async def _record_query_audit(args: dict[str, Any], envelope: ToolEnvelope) -> None:
    try:
        if __package__:
            from . import audit_log
        else:  # pragma: no cover - compatibility with direct tool loading
            import audit_log

        await audit_log.record_audit(
            who=str(args.get("who") or args.get("user_id") or "unknown"),
            what="调用工具 query_logs",
            cluster=args.get("cluster_id"),
            namespace=args.get("namespace"),
            trigger=str(args.get("trigger") or "manual"),
            tool_level="read",
            tool_name="query_logs",
            result=envelope.status,
            incident_id=args.get("incident_id"),
        )
    except Exception:
        logger.warning("query_logs audit failed", exc_info=True)


async def query_logs(args: dict[str, Any], runner: LokiRunner | None = None) -> ToolEnvelope:
    """Run a bounded Loki query and return a V1 MCP envelope."""
    request_id = str(args.get("request_id") or "")
    correlation_id = args.get("correlation_id")
    if not request_id:
        request_id = f"query_logs_{int(time.time() * 1000)}"

    required = [name for name in ("cluster_id", "time_range", "reason") if not args.get(name)]
    if required:
        return _error_envelope(
            request_id=request_id,
            correlation_id=correlation_id,
            status="failed",
            summary="query_logs 请求缺少必填字段",
            code=ErrorCode.INVALID_REQUEST,
            message=f"缺少必填字段: {', '.join(required)}",
            details={"missing": required},
            audit=_base_audit(args),
        )

    query = str(args.get("query") or args.get("logql") or "").strip()
    if not query:
        return _error_envelope(
            request_id=request_id,
            correlation_id=correlation_id,
            status="failed",
            summary="query_logs 请求缺少 LogQL",
            code=ErrorCode.INVALID_REQUEST,
            message="query/logql 必填",
            audit=_base_audit(args),
        )

    try:
        mode = _normalize_mode(args)
        limits = _normalize_limits(args, mode)
        start, end, evidence_time_range = _resolve_time_range(args.get("time_range"))
    except OverflowError as exc:
        return _error_envelope(
            request_id=request_id,
            correlation_id=correlation_id,
            status="failed",
            summary="query_logs 查询成本超过限制",
            code=ErrorCode.QUERY_COST_EXCEEDED,
            message=str(exc),
            audit=_base_audit(args),
        )
    except (TypeError, ValueError) as exc:
        return _error_envelope(
            request_id=request_id,
            correlation_id=correlation_id,
            status="failed",
            summary="query_logs 请求参数不合法",
            code=ErrorCode.INVALID_REQUEST,
            message=str(exc),
            audit=_base_audit(args),
        )

    query_digest = _stable_digest(
        {
            "cluster_id": args.get("cluster_id"),
            "namespace": args.get("namespace"),
            "query": query,
            "start": start,
            "end": end,
        }
    )

    try:
        cursor_offset = _decode_cursor(args.get("cursor"), query_digest)
    except ValueError as exc:
        return _error_envelope(
            request_id=request_id,
            correlation_id=correlation_id,
            status="failed",
            summary="query_logs cursor 不合法",
            code=ErrorCode.INVALID_REQUEST,
            message=str(exc),
            audit=_base_audit(args, query_digest),
        )

    if args.get("environment") == "prod" and mode == "raw_page" and _requires_scoped_query(query):
        return _error_envelope(
            request_id=request_id,
            correlation_id=correlation_id,
            status="failed",
            summary="query_logs 拒绝 prod raw_page 宽查询",
            code=ErrorCode.QUERY_REJECTED,
            message="prod raw_page 必须使用明确 label scope",
            details={"mode": mode, "environment": "prod"},
            audit=_base_audit(args, query_digest),
        )

    guard = await validate_loki_query(query=query, start=start, end=end, limit=limits.max_lines)
    if not guard.get("allowed"):
        return _error_envelope(
            request_id=request_id,
            correlation_id=correlation_id,
            status="failed",
            summary="query_logs 查询被护栏拒绝",
            code=ErrorCode.QUERY_REJECTED,
            message=str(guard.get("message") or "LogQL 查询被拒绝"),
            details={"query": query},
            audit=_base_audit(args, query_digest),
        )

    estimated_cost = _query_cost(start, end, limits.max_lines, cursor_offset)
    if estimated_cost > _MAX_ESTIMATED_COST:
        return _error_envelope(
            request_id=request_id,
            correlation_id=correlation_id,
            status="failed",
            summary="query_logs 查询成本超过限制",
            code=ErrorCode.QUERY_COST_EXCEEDED,
            message="查询窗口、max_lines 或 cursor 过大",
            details={"estimated_cost": estimated_cost, "max_cost": _MAX_ESTIMATED_COST},
            audit=_base_audit(args, query_digest),
        )

    backend = runner or HttpLokiRunner()
    try:
        results = await backend.query_range(guard["query"], guard["start"], guard["end"], guard["limit"])
    except LokiTimeoutError as exc:
        return _error_envelope(
            request_id=request_id,
            correlation_id=correlation_id,
            status="failed",
            summary="query_logs 后端超时",
            code=ErrorCode.TIMEOUT,
            message=str(exc),
            audit=_final_audit(
                args,
                query_digest=query_digest,
                status="failed",
                returned_bytes=0,
                total_matched=0,
                returned_lines=0,
                truncated=False,
                error_code=ErrorCode.TIMEOUT.value,
            ),
        )
    except LokiBackendError as exc:
        return _error_envelope(
            request_id=request_id,
            correlation_id=correlation_id,
            status="failed",
            summary="query_logs 后端不可用",
            code=ErrorCode.BACKEND_UNAVAILABLE,
            message=str(exc),
            audit=_final_audit(
                args,
                query_digest=query_digest,
                status="failed",
                returned_bytes=0,
                total_matched=0,
                returned_lines=0,
                truncated=False,
                error_code=ErrorCode.BACKEND_UNAVAILABLE.value,
            ),
        )

    all_lines = _extract_lines(results)
    page_lines = all_lines[cursor_offset : cursor_offset + limits.max_lines]
    has_more = cursor_offset + limits.max_lines < len(all_lines)
    next_cursor = _encode_cursor(cursor_offset + limits.max_lines, query_digest) if has_more else None
    evidence_ref = EvidenceRef(
        ref_id=_ref_id(query_digest, cursor_offset),
        source="loki",
        cluster_id=str(args["cluster_id"]),
        namespace=args.get("namespace"),
        service=args.get("service"),
        time_range=evidence_time_range,
        query_digest=query_digest,
        cursor=args.get("cursor"),
    )

    raw_lines = page_lines if mode == "raw_page" else []
    samples = page_lines[: limits.sample_size] if mode == "summary_samples" else []
    data: dict[str, Any] = {
        "query_digest": query_digest,
        "response_mode": mode,
        "mode": mode,
        "limits": asdict(limits),
        "total_matched": len(all_lines),
        "returned_lines": len(page_lines),
        "line_count": len(page_lines),
        "grouped_patterns": _group_patterns(page_lines, query_digest),
        "ref": evidence_ref.ref_id,
    }
    if mode == "summary_samples":
        data["samples"] = samples
    elif mode == "raw_page":
        data["raw_lines"] = raw_lines
    elif mode == "summary_only":
        data["samples"] = []

    returned_bytes = len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))
    status = "partial" if has_more else "succeeded"
    envelope = ToolEnvelope(
        request_id=request_id,
        correlation_id=correlation_id,
        tool_name="query_logs",
        status=status,
        summary=_build_summary(mode, page_lines, has_more),
        data=data,
        evidence_refs=(evidence_ref,),
        audit=_final_audit(
            args,
            query_digest=query_digest,
            status=status,
            returned_bytes=returned_bytes,
            total_matched=len(all_lines),
            returned_lines=len(page_lines),
            truncated=has_more,
        ),
        truncated=has_more,
        next_cursor=next_cursor,
    )
    envelope = _trim_envelope(envelope)
    await _record_query_audit(args, envelope)
    return envelope


async def loki_query(
    query: str,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
) -> dict:
    """Execute the legacy LogQL path with existing response compatibility."""
    guard = await validate_loki_query(query=query, start=start, end=end, limit=limit)
    if not guard["allowed"]:
        return guard

    started_at = time.perf_counter()
    try:
        results = await HttpLokiRunner().query_range(
            guard["query"],
            guard["start"],
            guard["end"],
            guard["limit"],
        )
    except LokiBackendError as exc:
        return {
            "allowed": True,
            "query": guard["query"],
            "start": guard["start"],
            "end": guard["end"],
            "limit": guard["limit"],
            "error": str(exc),
            "results": [],
        }

    elapsed = time.perf_counter() - started_at
    if elapsed > _SLOW_QUERY_SECONDS:
        logger.warning("Loki 慢查询: elapsed=%.2fs query=%s", elapsed, guard["query"])

    return {
        "allowed": True,
        "query": guard["query"],
        "start": guard["start"],
        "end": guard["end"],
        "limit": guard["limit"],
        "elapsed_seconds": round(elapsed, 3),
        "results": results,
    }


def run_query_logs(args: dict[str, Any], runner: LokiRunner | None = None) -> ToolEnvelope:
    """Synchronous helper for callers without an event loop."""
    return asyncio.run(query_logs(args, runner=runner))
