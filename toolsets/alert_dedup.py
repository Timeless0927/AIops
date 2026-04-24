"""告警去重与风暴聚合模块。"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List

import sys
import yaml


def _ensure_registry_import() -> None:
    """确保可以导入 Hermes 的工具注册器。"""
    hermes_root = Path(__file__).resolve().parents[1] / "hermes-agent"
    if str(hermes_root) not in sys.path:
        sys.path.insert(0, str(hermes_root))


_ensure_registry_import()

from tools.registry import registry  # noqa: E402


def _load_dedup_window() -> int:
    """从配置读取 dedup_window，失败时返回默认值。"""
    try:
        config_path = Path(__file__).resolve().parents[1] / "config.yaml"
        if not config_path.exists():
            return 300
        with config_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        notification = config.get("notification")
        if not isinstance(notification, dict):
            return 300
        return int(notification.get("dedup_window", 300))
    except Exception:
        return 300


DEFAULT_DEDUP_WINDOW_SECONDS = _load_dedup_window()
DEFAULT_STORM_THRESHOLD = 20


@dataclass
class AlertGroup:
    """单个告警分组的聚合状态。"""

    key: str
    first_seen: float
    last_seen: float
    count: int = 0
    alerts: List[Dict[str, Any]] = field(default_factory=list)


class AlertDeduplicator:
    """线程安全的告警去重器。"""

    def __init__(
        self,
        dedup_window_seconds: int = DEFAULT_DEDUP_WINDOW_SECONDS,
        storm_threshold_per_minute: int = DEFAULT_STORM_THRESHOLD,
    ) -> None:
        self.dedup_window_seconds = dedup_window_seconds
        self.storm_threshold_per_minute = storm_threshold_per_minute
        self._lock = threading.Lock()
        self._groups: Dict[str, AlertGroup] = {}
        self._recent_alerts: Deque[tuple[float, str]] = deque()

    def _make_key(self, alert: Dict[str, Any]) -> str:
        """生成告警去重键。"""
        return "|".join(
            [
                str(alert.get("alertname", "")).strip(),
                str(alert.get("namespace", "")).strip(),
                str(alert.get("cluster", "")).strip(),
            ]
        )

    def _cleanup_locked(self, now: float) -> None:
        """在持锁状态下清理过期数据。"""
        expired_keys = [
            key
            for key, group in self._groups.items()
            if now - group.last_seen > self.dedup_window_seconds
        ]
        for key in expired_keys:
            self._groups.pop(key, None)

        while self._recent_alerts and now - self._recent_alerts[0][0] > 60:
            self._recent_alerts.popleft()

    def should_process(self, alert: Dict[str, Any]) -> bool:
        """判断告警是否应进入后续处理流程。"""
        now = time.time()
        severity = str(alert.get("severity", "")).strip().lower()
        key = self._make_key(alert)

        with self._lock:
            self._cleanup_locked(now)
            self._recent_alerts.append((now, severity))

            if severity != "critical" and len(self._recent_alerts) > self.storm_threshold_per_minute:
                return False

            group = self._groups.get(key)
            if group is None or now - group.last_seen > self.dedup_window_seconds:
                self._groups[key] = AlertGroup(
                    key=key,
                    first_seen=now,
                    last_seen=now,
                    count=1,
                    alerts=[dict(alert)],
                )
                return True

            group.last_seen = now
            group.count += 1
            group.alerts.append(dict(alert))
            return False

    def get_summary(self) -> Dict[str, Any]:
        """返回当前聚合摘要。"""
        now = time.time()
        with self._lock:
            self._cleanup_locked(now)
            groups = []
            total_alerts = 0
            for group in self._groups.values():
                total_alerts += group.count
                groups.append(
                    {
                        "key": group.key,
                        "first_seen": group.first_seen,
                        "last_seen": group.last_seen,
                        "count": group.count,
                        "alerts": list(group.alerts),
                    }
                )

            return {
                "total_groups": len(self._groups),
                "total_alerts": total_alerts,
                "storm_active": len(self._recent_alerts) > self.storm_threshold_per_minute,
                "groups": groups,
            }

    def cleanup(self) -> None:
        """显式清理过期分组。"""
        now = time.time()
        with self._lock:
            self._cleanup_locked(now)


_DEDUP = AlertDeduplicator()


async def should_process(alert: Dict[str, Any]) -> bool:
    """异步包装的去重判断接口。"""
    return _DEDUP.should_process(alert)


async def get_summary() -> Dict[str, Any]:
    """异步包装的摘要接口。"""
    return _DEDUP.get_summary()


async def cleanup() -> None:
    """异步包装的清理接口。"""
    _DEDUP.cleanup()


ALERT_DEDUP_STATUS_SCHEMA = {
    "name": "alert_dedup_status",
    "description": "查看当前告警去重与风暴检测状态。",
    "parameters": {"type": "object", "properties": {}},
}


async def _alert_dedup_status_handler(args: Dict[str, Any], **kwargs: Any) -> str:
    """返回去重器摘要。"""
    return json.dumps(await get_summary(), ensure_ascii=False)


registry.register(
    name="alert_dedup_status",
    toolset="sre",
    schema=ALERT_DEDUP_STATUS_SCHEMA,
    handler=_alert_dedup_status_handler,
    is_async=True,
)
