"""V1 多集群 MCP 契约骨架与观测 facade。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine
from uuid import uuid4

MCP_ERROR_CODES = {
    "invalid_request",
    "unauthorized",
    "cluster_not_found",
    "namespace_not_found",
    "service_not_found",
    "query_rejected",
    "query_cost_exceeded",
    "timeout",
    "output_truncated",
    "backend_unavailable",
    "connector_offline",
    "approval_required",
    "approval_denied",
    "execution_failed",
    "task_not_found",
    "trace_deferred",
}

DEFAULT_LIMITS = {
    "timeout_seconds": 15,
    "max_bytes": 262144,
}

COMMON_PROPERTIES = {
    "request_id": {"type": "string"},
    "correlation_id": {"type": "string"},
    "actor": {"type": "object"},
    "agent_id": {"type": "string"},
    "brain_provider": {"type": "string"},
    "cluster_id": {"type": "string"},
    "namespace": {"type": "string"},
    "service": {"type": "string"},
    "workload": {"type": "object"},
    "time_range": {"type": "object"},
    "reason": {"type": "string"},
}

MCP_TOOL_SCHEMAS = {
    "query_metrics": {
        "type": "object",
        "required": ["cluster_id", "time_range", "reason"],
        "properties": {
            **COMMON_PROPERTIES,
            "metric": {
                "type": "string",
                "enum": [
                    "error_rate",
                    "latency_p95",
                    "latency_p99",
                    "traffic_rps",
                    "cpu_usage",
                    "memory_usage",
                    "restart_count",
                    "custom",
                ],
            },
            "promql": {"type": "string"},
            "labels": {"type": "object"},
            "step_seconds": {"type": "integer", "minimum": 10, "maximum": 300},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        },
        "oneOf": [{"required": ["metric"]}, {"required": ["promql"]}],
    },
    "query_logs": {
        "type": "object",
        "required": ["cluster_id", "time_range", "reason"],
        "properties": {
            **COMMON_PROPERTIES,
            "pod": {"type": "string"},
            "container": {"type": "string"},
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
        },
    },
    "get_service_topology": {
        "type": "object",
        "required": ["cluster_id", "namespace", "service", "reason"],
        "properties": {
            **COMMON_PROPERTIES,
            "direction": {"type": "string", "enum": ["upstream", "downstream", "both"], "default": "both"},
            "depth": {"type": "integer", "minimum": 1, "maximum": 2, "default": 1},
            "include_metrics": {"type": "boolean", "default": False},
        },
    },
    "run_k8s_read": {
        "type": "object",
        "required": ["cluster_id", "namespace", "argv", "reason"],
        "properties": {
            **COMMON_PROPERTIES,
            "argv": {"type": "array", "items": {"type": "string"}},
            "parse_mode": {"type": "string", "enum": ["stdout", "json", "summary"], "default": "stdout"},
        },
    },
    "submit_k8s_change": {
        "type": "object",
        "required": ["cluster_id", "namespace", "change", "reason"],
        "properties": {
            **COMMON_PROPERTIES,
            "change": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["typed_action", "kubectl_argv"]},
                    "action": {"type": "string"},
                    "resource": {"type": "object"},
                    "argv": {"type": "array", "items": {"type": "string"}},
                    "params": {"type": "object"},
                },
            },
            "dry_run": {"type": "boolean", "default": True},
            "idempotency_key": {"type": "string"},
        },
    },
    "get_k8s_execution_status": {
        "type": "object",
        "required": ["cluster_id", "reason"],
        "properties": {
            **COMMON_PROPERTIES,
            "task_id": {"type": "string"},
            "command_id": {"type": "string"},
            "include_events": {"type": "boolean", "default": True},
        },
        "oneOf": [{"required": ["task_id"]}, {"required": ["command_id"]}],
    },
}

MetricQueryRunner = Callable[[str, str | None, str | None], Coroutine[Any, Any, dict[str, Any]]]


async def _default_prometheus_runner(query: str, start: str | None, end: str | None) -> dict[str, Any]:
    """懒加载当前 Prometheus 工具，避免 contract 导入强依赖客户端包。"""
    try:
        from .prometheus_query import prometheus_query
    except ImportError:  # pragma: no cover - 兼容脚本式直接导入
        from prometheus_query import prometheus_query

    return await prometheus_query(query, start, end)


def utc_now() -> str:
    """返回 UTC ISO8601 时间。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def query_digest(query: str) -> str:
    """生成查询摘要，供审计和 evidence ref 使用。"""
    return f"sha256:{hashlib.sha256(query.encode('utf-8')).hexdigest()}"


def _time_range_bounds(time_range: dict[str, Any] | None) -> tuple[str | None, str | None, str]:
    """从 V1 time_range 中取 Prometheus 查询起止时间。"""
    if not isinstance(time_range, dict):
        return None, None, ""
    if time_range.get("type") == "absolute":
        start = time_range.get("start")
        end = time_range.get("end")
        return (
            str(start) if start else None,
            str(end) if end else None,
            f"{start or ''}/{end or ''}",
        )
    value = str(time_range.get("value") or "")
    return None, None, value


def _error(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造标准错误对象。"""
    safe_code = code if code in MCP_ERROR_CODES else "invalid_request"
    return {
        "code": safe_code,
        "message": message,
        "details": details or {},
    }


def _base_envelope(
    *,
    args: dict[str, Any],
    tool_name: str,
    status: str,
    started_at: str,
    finished_at: str,
    summary: str,
    data: dict[str, Any] | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
    truncated: bool = False,
    decision: str = "allowed",
    returned_bytes: int = 0,
    error_code: str | None = None,
) -> dict[str, Any]:
    """构造 AIO-48 统一响应 envelope。"""
    return {
        "request_id": args.get("request_id") or f"req-{uuid4()}",
        "correlation_id": args.get("correlation_id"),
        "tool_name": tool_name,
        "status": status,
        "summary": summary,
        "data": data or {},
        "evidence_refs": evidence_refs or [],
        "truncated": truncated,
        "next_cursor": None,
        "errors": errors or [],
        "limits": {
            **DEFAULT_LIMITS,
            "returned_bytes": returned_bytes,
        },
        "audit": {
            "decision": decision,
            "requested_at": started_at,
            "finished_at": finished_at,
            "actor": args.get("actor"),
            "agent_id": args.get("agent_id"),
            "brain_provider": args.get("brain_provider"),
            "cluster_id": args.get("cluster_id"),
            "namespace": args.get("namespace"),
            "service": args.get("service"),
            "reason": args.get("reason"),
            "error_code": error_code,
        },
    }


def _build_metric_promql(args: dict[str, Any]) -> tuple[str, str]:
    """把 typed metric 转成当前可执行的 PromQL。"""
    promql = str(args.get("promql") or "").strip()
    if promql:
        return promql, "promql"

    metric = str(args.get("metric") or "").strip()
    namespace = str(args.get("namespace") or "").strip()
    service = str(args.get("service") or "").strip()
    if not metric:
        raise ValueError("metric 或 promql 至少需要提供一个")
    if metric == "custom":
        raise ValueError("custom metric 必须提供 promql")
    allowed_metrics = set(MCP_TOOL_SCHEMAS["query_metrics"]["properties"]["metric"]["enum"])
    if metric not in allowed_metrics:
        raise ValueError(f"不支持的 metric: {metric}")
    if metric in {"error_rate", "latency_p95", "latency_p99", "traffic_rps"} and (not namespace or not service):
        raise ValueError(f"{metric} typed query 需要 namespace 和 service")

    selector = f'namespace="{namespace}",service="{service}"'
    mapping = {
        "error_rate": (
            f'sum(rate(http_requests_total{{{selector},status=~"5.."}}[5m])) '
            f'/ clamp_min(sum(rate(http_requests_total{{{selector}}}[5m])), 1)'
        ),
        "latency_p95": (
            "histogram_quantile(0.95, "
            f'sum(rate(http_request_duration_seconds_bucket{{{selector}}}[5m])) by (le))'
        ),
        "latency_p99": (
            "histogram_quantile(0.99, "
            f'sum(rate(http_request_duration_seconds_bucket{{{selector}}}[5m])) by (le))'
        ),
        "traffic_rps": f'sum(rate(http_requests_total{{{selector}}}[5m]))',
        "cpu_usage": "sum(rate(container_cpu_usage_seconds_total[5m]))",
        "memory_usage": "sum(container_memory_working_set_bytes)",
        "restart_count": "sum(kube_pod_container_status_restarts_total)",
    }
    return mapping[metric], "typed"


def _metric_summary(result: dict[str, Any], metric: str, query_mode: str) -> str:
    """生成简短查询摘要。"""
    results = result.get("results")
    count = len(results) if isinstance(results, list) else 0
    if result.get("error"):
        return f"Prometheus {metric or query_mode} 查询失败：{result['error']}"
    return f"Prometheus {metric or query_mode} 查询完成，返回 {count} 组序列。"


async def query_metrics(args: dict[str, Any], runner: MetricQueryRunner | None = None) -> dict[str, Any]:
    """执行 AIO-48 query_metrics，并返回统一 envelope。"""
    started_at = utc_now()
    metric = str(args.get("metric") or "").strip()
    try:
        promql, query_mode = _build_metric_promql(args)
    except ValueError as exc:
        finished_at = utc_now()
        return _base_envelope(
            args=args,
            tool_name="query_metrics",
            status="failed",
            summary=str(exc),
            started_at=started_at,
            finished_at=finished_at,
            errors=[_error("invalid_request", str(exc))],
            decision="rejected",
            error_code="invalid_request",
        )

    start, end, time_range_label = _time_range_bounds(args.get("time_range"))
    active_runner = runner or _default_prometheus_runner
    result = await active_runner(promql, start, end)
    finished_at = utc_now()
    digest = query_digest(promql)
    data = {
        "query_mode": query_mode,
        "metric": metric or "custom",
        "promql": promql,
        "query_digest": digest,
        "series": result.get("results", []),
        "analysis": {},
    }
    returned_bytes = len(str(data).encode("utf-8"))

    if not result.get("allowed", True):
        message = str(result.get("message") or "Prometheus 查询被策略拒绝")
        return _base_envelope(
            args=args,
            tool_name="query_metrics",
            status="failed",
            summary=message,
            data=data,
            started_at=started_at,
            finished_at=finished_at,
            errors=[_error("query_rejected", message, {"query_digest": digest})],
            decision="rejected",
            returned_bytes=returned_bytes,
            error_code="query_rejected",
        )

    if result.get("error"):
        message = str(result["error"])
        return _base_envelope(
            args=args,
            tool_name="query_metrics",
            status="failed",
            summary=_metric_summary(result, metric, query_mode),
            data=data,
            started_at=started_at,
            finished_at=finished_at,
            evidence_refs=[
                {
                    "ref_id": f"ev-metric-{digest.removeprefix('sha256:')[:12]}",
                    "source": "prometheus",
                    "cluster_id": args.get("cluster_id"),
                    "namespace": args.get("namespace"),
                    "service": args.get("service"),
                    "time_range": time_range_label,
                    "query_digest": digest,
                    "cursor": None,
                }
            ],
            errors=[_error("backend_unavailable", message, {"query_digest": digest})],
            decision="partial",
            returned_bytes=returned_bytes,
            error_code="backend_unavailable",
        )

    evidence_ref = {
        "ref_id": f"ev-metric-{digest.removeprefix('sha256:')[:12]}",
        "source": "prometheus",
        "cluster_id": args.get("cluster_id"),
        "namespace": args.get("namespace"),
        "service": args.get("service"),
        "time_range": time_range_label,
        "query_digest": digest,
        "cursor": None,
    }
    return _base_envelope(
        args=args,
        tool_name="query_metrics",
        status="succeeded",
        summary=_metric_summary(result, metric, query_mode),
        data=data,
        evidence_refs=[evidence_ref],
        started_at=started_at,
        finished_at=finished_at,
        returned_bytes=returned_bytes,
    )
