"""Prometheus 查询工具。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from prometheus_api_client import PrometheusConnect

from query_guard import resolve_service_url, validate_prometheus_query

logger = logging.getLogger(__name__)
_QUERY_TIMEOUT_SECONDS = 30
_SLOW_QUERY_SECONDS = 10


def _run_prometheus_query(url: str, query: str, start: str, end: str) -> Any:
    """在线程中执行阻塞式 Prometheus 查询。"""
    client = PrometheusConnect(url=url, disable_ssl=True)
    return client.custom_query_range(
        query=query,
        start_time=start,
        end_time=end,
        step="60s",
        timeout=_QUERY_TIMEOUT_SECONDS,
    )


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
