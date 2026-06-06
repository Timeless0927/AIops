"""Prometheus query tools and V1 query_metrics facade."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any
from typing import Protocol

from aiops.contracts import ErrorCode, EvidenceRef, ToolEnvelope, ToolError

if __package__:
    from .query_guard import resolve_service_url, validate_prometheus_query
else:  # pragma: no cover - compatibility with direct tool loading
    from query_guard import resolve_service_url, validate_prometheus_query

logger = logging.getLogger(__name__)
_QUERY_TIMEOUT_SECONDS = 30
_SLOW_QUERY_SECONDS = 10

_DEFAULT_MAX_SERIES = 50
_MAX_SERIES_CAP = 200
_REF_PREFIX = "ev_prom_"


class PrometheusBackendError(RuntimeError):
    """Raised when the Prometheus backend cannot be queried."""


class PrometheusTimeoutError(PrometheusBackendError):
    """Raised when the Prometheus backend times out."""


class PrometheusRunner(Protocol):
    """Minimal runner protocol used by query_metrics."""

    async def query_range(self, query: str, start: str, end: str, step: str) -> list[dict[str, Any]]:
        """Run a Prometheus range query."""


@dataclass(frozen=True)
class QueryMetricsLimits:
    """Normalized query limits exposed in the response data."""

    max_series: int


class HttpPrometheusRunner:
    """Prometheus query_range runner with runtime configuration lookup."""

    async def query_range(self, query: str, start: str, end: str, step: str) -> list[dict[str, Any]]:
        prometheus_url = await resolve_service_url("PROMETHEUS_URL", ("sre", "prometheus_url"))
        if not prometheus_url:
            raise PrometheusBackendError("未配置 PROMETHEUS_URL，无法执行查询")

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_run_prometheus_query, prometheus_url, query, start, end, step),
                timeout=_QUERY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise PrometheusTimeoutError("Prometheus 查询超时（30s）") from exc
        except PrometheusBackendError:
            raise
        except Exception as exc:
            raise PrometheusBackendError(f"Prometheus 查询失败: {exc}") from exc


def _run_prometheus_query(url: str, query: str, start: str, end: str, step: str = "60s") -> Any:
    """在线程中执行阻塞式 Prometheus 查询。"""
    try:
        from prometheus_api_client import PrometheusConnect
    except ImportError as exc:  # pragma: no cover - depends on deployment image
        raise PrometheusBackendError("缺少 prometheus_api_client 依赖，无法执行 Prometheus 查询") from exc

    client = PrometheusConnect(url=url, disable_ssl=True)
    return client.custom_query_range(
        query=query,
        start_time=start,
        end_time=end,
        step=step,
        timeout=_QUERY_TIMEOUT_SECONDS,
    )


def _stable_digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ref_id(query_digest: str) -> str:
    return f"{_REF_PREFIX}{query_digest[:16]}"


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
    audit_payload.setdefault("series_count", 0)
    audit_payload.setdefault("returned_series", 0)
    audit_payload.setdefault("truncated", False)
    audit_payload.setdefault("error_code", code.value)
    return ToolEnvelope(
        request_id=request_id,
        correlation_id=correlation_id,
        tool_name="query_metrics",
        status=status,
        summary=summary,
        data=data or {},
        audit=audit_payload,
        errors=(ToolError(code=code, message=message, details=details or {}),),
    )


def _base_audit(args: dict[str, Any], query_digest: str | None = None) -> dict[str, Any]:
    return {
        "actor": str(args.get("who") or args.get("user_id") or "unknown"),
        "reason": args.get("reason"),
        "cluster_id": args.get("cluster_id"),
        "namespace": args.get("namespace"),
        "query_digest": query_digest,
        "tool_name": "query_metrics",
    }


def _final_audit(
    args: dict[str, Any],
    *,
    query_digest: str,
    status: str,
    returned_bytes: int,
    series_count: int,
    returned_series: int,
    truncated: bool,
    error_code: str | None = None,
) -> dict[str, Any]:
    audit = _base_audit(args, query_digest)
    audit.update(
        {
            "status": status,
            "returned_bytes": returned_bytes,
            "series_count": series_count,
            "returned_series": returned_series,
            "truncated": truncated,
            "error_code": error_code,
        }
    )
    return audit


def _normalize_limits(args: dict[str, Any]) -> QueryMetricsLimits:
    max_series = int(args.get("max_series") or _DEFAULT_MAX_SERIES)
    if max_series <= 0:
        raise ValueError("max_series 必须大于 0")
    return QueryMetricsLimits(max_series=min(max_series, _MAX_SERIES_CAP))


def _extract_step(args: dict[str, Any]) -> str:
    step = str(args.get("step") or "60s").strip()
    if not step:
        raise ValueError("step 不能为空")
    return step


def _normalize_results(results: Any) -> list[dict[str, Any]]:
    if not isinstance(results, list):
        raise PrometheusBackendError("Prometheus 响应格式不合法")
    return [item for item in results if isinstance(item, dict)]


def _series_sample(series: dict[str, Any]) -> dict[str, Any]:
    values = series.get("values")
    sample_count = len(values) if isinstance(values, list) else 0
    first_value = values[0] if isinstance(values, list) and values else None
    last_value = values[-1] if isinstance(values, list) and values else None
    return {
        "metric": series.get("metric", {}) if isinstance(series.get("metric"), dict) else {},
        "sample_count": sample_count,
        "first_value": first_value,
        "last_value": last_value,
    }


def _build_summary(series_count: int, returned_series: int, truncated: bool) -> str:
    suffix = "，结果已截断" if truncated else ""
    if series_count == 0:
        return "Prometheus query_metrics 未返回时序"
    return f"Prometheus query_metrics 返回 {returned_series}/{series_count} 条时序{suffix}"


async def _record_query_audit(args: dict[str, Any], envelope: ToolEnvelope) -> None:
    try:
        if __package__:
            from . import audit_log
        else:  # pragma: no cover - compatibility with direct tool loading
            import audit_log

        await audit_log.record_audit(
            who=str(args.get("who") or args.get("user_id") or "unknown"),
            what="调用工具 query_metrics",
            cluster=args.get("cluster_id"),
            namespace=args.get("namespace"),
            trigger=str(args.get("trigger") or "manual"),
            tool_level="read",
            tool_name="query_metrics",
            result=envelope.status,
            incident_id=args.get("incident_id"),
        )
    except Exception:
        logger.warning("query_metrics audit failed", exc_info=True)


async def query_metrics(args: dict[str, Any], runner: PrometheusRunner | None = None) -> ToolEnvelope:
    """Run a bounded Prometheus query and return a V1 MCP envelope."""
    request_id = str(args.get("request_id") or "")
    correlation_id = args.get("correlation_id")
    if not request_id:
        request_id = f"query_metrics_{int(time.time() * 1000)}"

    required = [name for name in ("cluster_id", "reason") if not args.get(name)]
    if required:
        return _error_envelope(
            request_id=request_id,
            correlation_id=correlation_id,
            status="failed",
            summary="query_metrics 请求缺少必填字段",
            code=ErrorCode.INVALID_REQUEST,
            message=f"缺少必填字段: {', '.join(required)}",
            details={"missing": required},
            audit=_base_audit(args),
        )

    query = str(args.get("query") or args.get("promql") or "").strip()
    if not query:
        return _error_envelope(
            request_id=request_id,
            correlation_id=correlation_id,
            status="failed",
            summary="query_metrics 请求缺少 PromQL",
            code=ErrorCode.INVALID_REQUEST,
            message="query/promql 必填",
            audit=_base_audit(args),
        )

    try:
        limits = _normalize_limits(args)
        step = _extract_step(args)
        guard = await validate_prometheus_query(query=query, start=args.get("start"), end=args.get("end"))
    except (TypeError, ValueError) as exc:
        return _error_envelope(
            request_id=request_id,
            correlation_id=correlation_id,
            status="failed",
            summary="query_metrics 请求参数不合法",
            code=ErrorCode.INVALID_REQUEST,
            message=str(exc),
            audit=_base_audit(args),
        )

    query_digest = _stable_digest(
        {
            "cluster_id": args.get("cluster_id"),
            "namespace": args.get("namespace"),
            "query": query,
            "start": guard.get("start"),
            "end": guard.get("end"),
            "step": step,
        }
    )

    if not guard.get("allowed"):
        return _error_envelope(
            request_id=request_id,
            correlation_id=correlation_id,
            status="failed",
            summary="query_metrics 查询被护栏拒绝",
            code=ErrorCode.QUERY_REJECTED,
            message=str(guard.get("message") or "PromQL 查询被拒绝"),
            details={"query": query},
            audit=_base_audit(args, query_digest),
        )

    backend = runner or HttpPrometheusRunner()
    try:
        results = _normalize_results(await backend.query_range(guard["query"], guard["start"], guard["end"], step))
    except PrometheusTimeoutError as exc:
        return _error_envelope(
            request_id=request_id,
            correlation_id=correlation_id,
            status="failed",
            summary="query_metrics 后端超时",
            code=ErrorCode.TIMEOUT,
            message=str(exc),
            audit=_final_audit(
                args,
                query_digest=query_digest,
                status="failed",
                returned_bytes=0,
                series_count=0,
                returned_series=0,
                truncated=False,
                error_code=ErrorCode.TIMEOUT.value,
            ),
        )
    except PrometheusBackendError as exc:
        return _error_envelope(
            request_id=request_id,
            correlation_id=correlation_id,
            status="failed",
            summary="query_metrics 后端不可用",
            code=ErrorCode.BACKEND_UNAVAILABLE,
            message=str(exc),
            audit=_final_audit(
                args,
                query_digest=query_digest,
                status="failed",
                returned_bytes=0,
                series_count=0,
                returned_series=0,
                truncated=False,
                error_code=ErrorCode.BACKEND_UNAVAILABLE.value,
            ),
        )

    series_count = len(results)
    returned = results[: limits.max_series]
    truncated = series_count > len(returned)
    evidence_ref = EvidenceRef(
        ref_id=_ref_id(query_digest),
        source="prometheus",
        cluster_id=str(args["cluster_id"]),
        namespace=args.get("namespace"),
        service=args.get("service"),
        time_range=f"{guard['start']}/{guard['end']}",
        query_digest=query_digest,
    )
    data: dict[str, Any] = {
        "query_digest": query_digest,
        "promql": guard["query"],
        "start": guard["start"],
        "end": guard["end"],
        "step": step,
        "limits": asdict(limits),
        "series_count": series_count,
        "returned_series": len(returned),
        "series": [_series_sample(item) for item in returned],
        "ref": evidence_ref.ref_id,
    }
    returned_bytes = len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))
    status = "partial" if truncated else "succeeded"
    envelope = ToolEnvelope(
        request_id=request_id,
        correlation_id=correlation_id,
        tool_name="query_metrics",
        status=status,
        summary=_build_summary(series_count, len(returned), truncated),
        data=data,
        evidence_refs=(evidence_ref,),
        audit=_final_audit(
            args,
            query_digest=query_digest,
            status=status,
            returned_bytes=returned_bytes,
            series_count=series_count,
            returned_series=len(returned),
            truncated=truncated,
        ),
        truncated=truncated,
    )
    await _record_query_audit(args, envelope)
    return envelope


async def prometheus_query(query: str, start: str | None = None, end: str | None = None) -> dict:
    """执行带安全护栏的 PromQL 查询。"""
    guard = await validate_prometheus_query(query=query, start=start, end=end)
    if not guard["allowed"]:
        return guard

    prometheus_url = await resolve_service_url("PROMETHEUS_URL", ("sre", "prometheus_url"))
    if not prometheus_url:
        return {
            "allowed": True,
            "query": guard["query"],
            "start": guard["start"],
            "end": guard["end"],
            "error": "未配置 PROMETHEUS_URL，无法执行查询",
            "results": [],
        }

    started_at = time.perf_counter()
    try:
        results = await asyncio.wait_for(
            asyncio.to_thread(
                _run_prometheus_query,
                prometheus_url,
                guard["query"],
                guard["start"],
                guard["end"],
                "60s",
            ),
            timeout=_QUERY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return {
            "allowed": True,
            "query": guard["query"],
            "start": guard["start"],
            "end": guard["end"],
            "error": "Prometheus 查询超时（30s）",
            "results": [],
        }
    except Exception as exc:
        return {
            "allowed": True,
            "query": guard["query"],
            "start": guard["start"],
            "end": guard["end"],
            "error": f"Prometheus 查询失败: {exc}",
            "results": [],
        }

    elapsed = time.perf_counter() - started_at
    if elapsed > _SLOW_QUERY_SECONDS:
        logger.warning("Prometheus 慢查询: elapsed=%.2fs query=%s", elapsed, guard["query"])

    return {
        "allowed": True,
        "query": guard["query"],
        "start": guard["start"],
        "end": guard["end"],
        "elapsed_seconds": round(elapsed, 3),
        "results": results,
    }
