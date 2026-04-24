"""测试告警去重聚合模块。"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "alert_dedup.py"
    spec = importlib.util.spec_from_file_location("test_alert_dedup_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_alert(severity: str = "warning") -> dict:
    """构造标准测试告警。"""
    return {
        "alertname": "HighErrorRate",
        "severity": severity,
        "namespace": "default",
        "cluster": "prod-a",
        "description": "错误率持续升高",
    }


@pytest.mark.asyncio
async def test_dedup_returns_false_within_window() -> None:
    """同一窗口内重复告警应被去重。"""
    module = _load_module()
    dedup = module.AlertDeduplicator(dedup_window_seconds=60, storm_threshold_per_minute=100)

    assert dedup.should_process(_make_alert()) is True
    assert dedup.should_process(_make_alert()) is False


@pytest.mark.asyncio
async def test_dedup_returns_true_after_window_expired() -> None:
    """窗口过期后相同告警应重新进入处理流程。"""
    module = _load_module()
    dedup = module.AlertDeduplicator(dedup_window_seconds=1, storm_threshold_per_minute=100)

    assert dedup.should_process(_make_alert()) is True
    time.sleep(1.1)
    assert dedup.should_process(_make_alert()) is True


@pytest.mark.asyncio
async def test_storm_detection_filters_non_critical_alerts() -> None:
    """告警风暴期间非 critical 告警应被丢弃。"""
    module = _load_module()
    dedup = module.AlertDeduplicator(dedup_window_seconds=300, storm_threshold_per_minute=2)

    assert dedup.should_process(_make_alert("warning")) is True
    assert dedup.should_process(_make_alert("critical")) is False
    assert dedup.should_process(_make_alert("warning")) is False

    summary = dedup.get_summary()
    assert summary["storm_active"] is True


@pytest.mark.asyncio
async def test_cleanup_removes_expired_groups() -> None:
    """cleanup 应移除窗口外的分组。"""
    module = _load_module()
    dedup = module.AlertDeduplicator(dedup_window_seconds=1, storm_threshold_per_minute=100)

    assert dedup.should_process(_make_alert()) is True
    time.sleep(1.1)
    dedup.cleanup()

    summary = dedup.get_summary()
    assert summary["total_groups"] == 0
    assert summary["total_alerts"] == 0


@pytest.mark.asyncio
async def test_thread_safety_under_concurrent_calls() -> None:
    """多线程并发调用时应只有一个请求进入处理流程。"""
    module = _load_module()
    dedup = module.AlertDeduplicator(dedup_window_seconds=60, storm_threshold_per_minute=1000)
    results: list[bool] = []
    results_lock = threading.Lock()

    def _worker() -> None:
        outcome = dedup.should_process(_make_alert())
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=_worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 20
    assert results.count(True) == 1
    assert results.count(False) == 19
