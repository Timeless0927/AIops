"""Loki 查询工具。"""

from __future__ import annotations

import logging
import time

import httpx

try:
    from .query_guard import resolve_service_url, validate_loki_query
except ImportError:  # pragma: no cover - 兼容脚本式直接导入
    from query_guard import resolve_service_url, validate_loki_query

logger = logging.getLogger(__name__)
_QUERY_TIMEOUT_SECONDS = 30
_SLOW_QUERY_SECONDS = 10


async def loki_query(
    query: str,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
) -> dict:
    """执行带安全护栏的 LogQL 查询。"""
    guard = await validate_loki_query(query=query, start=start, end=end, limit=limit)
    if not guard["allowed"]:
        return guard

    loki_url = await resolve_service_url("LOKI_URL", ("sre", "loki_url"))
    if not loki_url:
        return {
            "allowed": True,
            "query": guard["query"],
            "start": guard["start"],
            "end": guard["end"],
            "limit": guard["limit"],
            "error": "未配置 LOKI_URL，无法执行查询",
            "results": [],
        }

    params = {
        "query": guard["query"],
        "start": guard["start"],
        "end": guard["end"],
        "limit": guard["limit"],
        "direction": "backward",
    }

    started_at = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=_QUERY_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{loki_url.rstrip('/')}/loki/api/v1/query_range",
                params=params,
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.TimeoutException:
        return {
            "allowed": True,
            "query": guard["query"],
            "start": guard["start"],
            "end": guard["end"],
            "limit": guard["limit"],
            "error": "Loki 查询超时（30s）",
            "results": [],
        }
    except httpx.HTTPError as exc:
        return {
            "allowed": True,
            "query": guard["query"],
            "start": guard["start"],
            "end": guard["end"],
            "limit": guard["limit"],
            "error": f"Loki 查询失败: {exc}",
            "results": [],
        }
    except Exception as exc:
        return {
            "allowed": True,
            "query": guard["query"],
            "start": guard["start"],
            "end": guard["end"],
            "limit": guard["limit"],
            "error": f"Loki 响应解析失败: {exc}",
            "results": [],
        }

    elapsed = time.perf_counter() - started_at
    if elapsed > _SLOW_QUERY_SECONDS:
        logger.warning("Loki 慢查询: elapsed=%.2fs query=%s", elapsed, guard["query"])

    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    results = data.get("result", []) if isinstance(data, dict) else []
    return {
        "allowed": True,
        "query": guard["query"],
        "start": guard["start"],
        "end": guard["end"],
        "limit": guard["limit"],
        "elapsed_seconds": round(elapsed, 3),
        "results": results,
    }
