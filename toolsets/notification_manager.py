"""通知防疲劳管理器。"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

try:
    from tools.registry import registry
except ImportError:  # pragma: no cover - 测试环境兼容
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-agent"))
    from tools.registry import registry


SEVERITY_ORDER = {"info": 0, "warning": 1, "error": 2, "critical": 3}


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


def _load_notification_config() -> dict[str, Any]:
    """读取 notification 配置段。"""
    try:
        config = {}
        for path in _runtime_config_candidates():
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
            config = data if isinstance(data, dict) else {}
            break
        notification = config.get("notification")
        return notification if isinstance(notification, dict) else {}
    except Exception:
        return {}


class NotificationManager:
    """通知防疲劳内存管理器。"""

    def __init__(self) -> None:
        self._config = _load_notification_config()
        self._hourly_counter = 0
        self._hour_key = self._current_hour_key()
        self._digest_queue: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def _current_hour_key(self) -> str:
        """生成当前小时标识。"""
        now = datetime.now()
        return now.strftime("%Y%m%d%H")

    def _now(self) -> datetime:
        """返回当前本地时间。"""
        return datetime.now()

    def _reset_if_new_hour(self) -> None:
        """跨小时后重置计数器。"""
        current = self._current_hour_key()
        if current != self._hour_key:
            self._hour_key = current
            self._hourly_counter = 0

    def _severity_value(self, severity: str) -> int:
        """将严重级别转为数值。"""
        return SEVERITY_ORDER.get(str(severity).strip().lower(), 0)

    def _is_quiet_hours(self, now: datetime, severity: str) -> bool:
        """判断当前是否命中安静时段。"""
        quiet_hours = self._config.get("quiet_hours")
        if not isinstance(quiet_hours, dict):
            return False

        except_severity = str(quiet_hours.get("except", "")).strip().lower()
        if str(severity).strip().lower() == except_severity:
            return False

        start_raw = str(quiet_hours.get("start", "")).strip()
        end_raw = str(quiet_hours.get("end", "")).strip()
        if not start_raw or not end_raw:
            return False

        start_time = datetime.strptime(start_raw, "%H:%M").time()
        end_time = datetime.strptime(end_raw, "%H:%M").time()
        current_time = now.time()
        if start_time <= end_time:
            return start_time <= current_time <= end_time
        return current_time >= start_time or current_time <= end_time

    async def should_notify(self, alert: dict[str, Any]) -> dict[str, Any]:
        """判断告警是否应立即通知。"""
        with self._lock:
            self._reset_if_new_hour()
            severity = str(alert.get("severity", "info")).strip().lower()
            threshold = str(self._config.get("severity_filter", "info")).strip().lower()

            if self._severity_value(severity) < self._severity_value(threshold):
                self._digest_queue.append(dict(alert))
                return {"notify": False, "reason": "severity_below_threshold", "queued_for_digest": True}

            now = self._now()
            if self._is_quiet_hours(now, severity):
                self._digest_queue.append(dict(alert))
                return {"notify": False, "reason": "quiet_hours", "queued_for_digest": True}

            max_per_hour = int(self._config.get("max_per_hour", 10))
            if self._hourly_counter >= max_per_hour:
                self._digest_queue.append(dict(alert))
                return {"notify": False, "reason": "hourly_limit_reached", "queued_for_digest": True}

            self._hourly_counter += 1
            return {"notify": True, "reason": "allowed"}

    async def get_digest(self) -> list[dict[str, Any]]:
        """返回并清空摘要队列。"""
        with self._lock:
            items = list(self._digest_queue)
            self._digest_queue.clear()
            return items


_MANAGER = NotificationManager()


async def should_notify(alert: dict[str, Any]) -> dict[str, Any]:
    """模块级通知检查入口。"""
    return await _MANAGER.should_notify(alert)


async def get_digest() -> list[dict[str, Any]]:
    """模块级摘要读取入口。"""
    return await _MANAGER.get_digest()


SRE_NOTIFICATION_CHECK_SCHEMA = {
    "name": "sre_notification_check",
    "description": "检查告警是否应立即通知。",
    "parameters": {
        "type": "object",
        "properties": {
            "alert": {
                "type": "object",
                "properties": {
                    "alertname": {"type": "string"},
                    "severity": {"type": "string"},
                    "namespace": {"type": "string"},
                    "cluster": {"type": "string"},
                },
                "required": ["alertname", "severity"],
            }
        },
        "required": ["alert"],
    },
}

SRE_NOTIFICATION_DIGEST_SCHEMA = {
    "name": "sre_notification_digest",
    "description": "获取并清空当前通知摘要队列。",
    "parameters": {"type": "object", "properties": {}},
}


async def _tool_notification_check(args: dict[str, Any], **_: Any) -> str:
    """工具入口：通知检查。"""
    result = await should_notify(args.get("alert", {}))
    return json.dumps(result, ensure_ascii=False)


async def _tool_notification_digest(args: dict[str, Any], **_: Any) -> str:
    """工具入口：读取摘要。"""
    del args
    result = await get_digest()
    return json.dumps(result, ensure_ascii=False)


registry.register(name="sre_notification_check", toolset="sre", schema=SRE_NOTIFICATION_CHECK_SCHEMA, handler=_tool_notification_check, is_async=True)
registry.register(name="sre_notification_digest", toolset="sre", schema=SRE_NOTIFICATION_DIGEST_SCHEMA, handler=_tool_notification_digest, is_async=True)
