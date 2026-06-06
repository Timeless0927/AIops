"""查询安全护栏。"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

_FORBIDDEN_LOKI_PATTERNS = (
    re.compile(r'\{\s*job\s*=~\s*"\.\+"\s*\}'),
    re.compile(r'\{\s*job\s*=~\s*"\.\*"\s*\}'),
)
_DEFAULT_LOOKBACK = timedelta(hours=1)
_DEFAULT_LOKI_LIMIT = 200


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parent.parent


def _runtime_config_candidates() -> list[Path]:
    """返回运行时配置候选路径，按优先级排序。"""
    candidates: list[Path] = []

    hermes_config = os.getenv("HERMES_CONFIG")
    if hermes_config:
        candidates.append(Path(hermes_config).expanduser())

    hermes_home = os.getenv("HERMES_HOME")
    if hermes_home:
        candidates.append(Path(hermes_home).expanduser() / "config.yaml")
    return candidates


def _expand_env_value(value: Any) -> Any:
    """展开配置中的环境变量占位符。"""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {key: _expand_env_value(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_expand_env_value(item) for item in value]
    return value


def _load_config_sync() -> Dict[str, Any]:
    """同步读取运行时配置。"""
    try:
        import yaml
    except ModuleNotFoundError:
        return {}

    for config_path in _runtime_config_candidates():
        if not config_path.exists():
            continue
        with config_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        if isinstance(config, dict):
            return _expand_env_value(config)
    return {}


async def load_runtime_config() -> Dict[str, Any]:
    """异步读取项目配置。"""
    return await asyncio.to_thread(_load_config_sync)


def _format_timestamp(value: datetime) -> str:
    """统一输出 UTC ISO8601 时间戳。"""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    """解析常见时间字符串。"""
    if value is None:
        return None

    raw = value.strip()
    if not raw:
        return None

    normalized = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def resolve_service_url(env_key: str, config_path: tuple[str, ...]) -> str:
    """从环境变量或配置文件读取服务地址。"""
    env_value = os.getenv(env_key, "").strip()
    if env_value:
        return env_value

    config = await load_runtime_config()
    current: Any = config
    for key in config_path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)

    if isinstance(current, str):
        return current.strip()
    return ""


async def validate_prometheus_query(query: str, start: str | None, end: str | None) -> dict:
    """校验 PromQL，返回规范化后的查询参数。"""
    normalized_query = (query or "").strip()
    if not normalized_query:
        return {
            "allowed": False,
            "query": "",
            "start": "",
            "end": "",
            "message": "PromQL 不能为空",
        }

    try:
        end_time = _parse_timestamp(end) or datetime.now(timezone.utc)
        start_time = _parse_timestamp(start) or (end_time - _DEFAULT_LOOKBACK)
    except ValueError:
        return {
            "allowed": False,
            "query": normalized_query,
            "start": start or "",
            "end": end or "",
            "message": "时间格式不合法，请使用 ISO8601 时间",
        }

    if start_time >= end_time:
        return {
            "allowed": False,
            "query": normalized_query,
            "start": _format_timestamp(start_time),
            "end": _format_timestamp(end_time),
            "message": "开始时间必须早于结束时间",
        }

    return {
        "allowed": True,
        "query": normalized_query,
        "start": _format_timestamp(start_time),
        "end": _format_timestamp(end_time),
        "message": "ok",
    }


async def validate_loki_query(query: str, start: str | None, end: str | None, limit: int | None) -> dict:
    """校验 LogQL，返回规范化后的查询参数。"""
    normalized_query = (query or "").strip()
    if not normalized_query:
        return {
            "allowed": False,
            "query": "",
            "start": "",
            "end": "",
            "limit": _DEFAULT_LOKI_LIMIT,
            "message": "LogQL 不能为空",
        }

    if any(pattern.search(normalized_query) for pattern in _FORBIDDEN_LOKI_PATTERNS):
        return {
            "allowed": False,
            "query": normalized_query,
            "start": "",
            "end": "",
            "limit": _DEFAULT_LOKI_LIMIT,
            "message": "禁止使用 {job=~\".+\"} 这类全量匹配查询",
        }

    try:
        end_time = _parse_timestamp(end) or datetime.now(timezone.utc)
        start_time = _parse_timestamp(start) or (end_time - _DEFAULT_LOOKBACK)
    except ValueError:
        return {
            "allowed": False,
            "query": normalized_query,
            "start": start or "",
            "end": end or "",
            "limit": _DEFAULT_LOKI_LIMIT if limit is None else limit,
            "message": "时间格式不合法，请使用 ISO8601 时间",
        }

    if start_time >= end_time:
        return {
            "allowed": False,
            "query": normalized_query,
            "start": _format_timestamp(start_time),
            "end": _format_timestamp(end_time),
            "limit": _DEFAULT_LOKI_LIMIT if limit is None else limit,
            "message": "开始时间必须早于结束时间",
        }

    normalized_limit = _DEFAULT_LOKI_LIMIT if limit is None else int(limit)
    if normalized_limit <= 0:
        return {
            "allowed": False,
            "query": normalized_query,
            "start": _format_timestamp(start_time),
            "end": _format_timestamp(end_time),
            "limit": normalized_limit,
            "message": "limit 必须大于 0",
        }

    return {
        "allowed": True,
        "query": normalized_query,
        "start": _format_timestamp(start_time),
        "end": _format_timestamp(end_time),
        "limit": normalized_limit,
        "message": "ok",
    }
