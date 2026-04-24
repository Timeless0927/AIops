"""SRE 效果度量与周报生成。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict


def _load_tool_module(module_filename: str, module_name: str):
    """按文件路径加载工具模块。"""
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = Path(__file__).resolve().parent / module_filename
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _ensure_registry_import() -> None:
    """确保可以导入 Hermes 的工具注册器。"""
    hermes_root = Path(__file__).resolve().parents[1] / "hermes-agent"
    if str(hermes_root) not in sys.path:
        sys.path.insert(0, str(hermes_root))


_ensure_registry_import()

from tools.registry import registry  # noqa: E402


incident_store = _load_tool_module("incident_store.py", "toolsets.incident_store.metrics")
approval_async = _load_tool_module("approval_async.py", "toolsets.approval_async.metrics")
audit_log = _load_tool_module("audit_log.py", "toolsets.audit_log.metrics")


async def compute_metrics(days: int = 7) -> dict:
    """计算最近一段时间的核心效果指标。"""
    since = time.time() - max(1, int(days)) * 86400

    incidents = await incident_store.list_active()
    recent_incidents = [incident for incident in incidents if float(incident.get("created_at", 0)) >= since]

    mttd_values: list[float] = []
    for incident in recent_incidents:
        timeline = await incident_store.get_timeline(str(incident.get("id", "")))
        alert_fired_ts = None
        triage_start_ts = None
        for event in timeline:
            event_type = str(event.get("event_type", ""))
            timestamp = float(event.get("timestamp", 0.0))
            if event_type == "alert_fired" and alert_fired_ts is None:
                alert_fired_ts = timestamp
            if event_type == "triage_start" and triage_start_ts is None:
                triage_start_ts = timestamp
        if alert_fired_ts is not None and triage_start_ts is not None and triage_start_ts >= alert_fired_ts:
            mttd_values.append(triage_start_ts - alert_fired_ts)

    def _read_approvals() -> tuple[int, int]:
        with approval_async._DB._lock:
            row = approval_async._DB._conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved_count,
                    SUM(CASE WHEN status = 'denied' THEN 1 ELSE 0 END) AS denied_count
                FROM approvals
                WHERE created_at >= ? AND status IN ('approved', 'denied')
                """,
                (since,),
            ).fetchone()
        approved = int((row["approved_count"] if row and row["approved_count"] is not None else 0) or 0)
        denied = int((row["denied_count"] if row and row["denied_count"] is not None else 0) or 0)
        return approved, denied

    approved_count, denied_count = await asyncio.to_thread(_read_approvals)
    total_approvals = approved_count + denied_count
    adoption_rate = (approved_count / total_approvals) if total_approvals else None

    audit_rows = await audit_log.query_audit(time_start=since, limit=100000)
    rollback_count = sum(1 for row in audit_rows if int(row.get("rollback", 0) or 0) == 1)
    rollback_rate = (rollback_count / len(audit_rows)) if audit_rows else None

    return {
        "period_days": int(days),
        "mttd_seconds": (sum(mttd_values) / len(mttd_values)) if mttd_values else None,
        "adoption_rate": adoption_rate,
        "rollback_rate": rollback_rate,
        "total_incidents": len(recent_incidents),
        "total_approvals": total_approvals,
    }


async def generate_weekly_summary() -> str:
    """生成最近 7 天的中文周报。"""
    metrics = await compute_metrics(days=7)
    mttd = "N/A" if metrics["mttd_seconds"] is None else f"{metrics['mttd_seconds']:.1f}"
    adoption_rate = "N/A" if metrics["adoption_rate"] is None else f"{metrics['adoption_rate'] * 100:.1f}"
    rollback_rate = "N/A" if metrics["rollback_rate"] is None else f"{metrics['rollback_rate'] * 100:.1f}"
    return (
        "📊 SRE Agent 周报（最近 7 天）\n"
        f"- 处理事件：{metrics['total_incidents']} 起\n"
        f"- 平均诊断时间（MTTD）：{mttd} 秒\n"
        f"- 方案采纳率：{adoption_rate}%\n"
        f"- 回滚率：{rollback_rate}%"
    )


SRE_METRICS_SCHEMA = {
    "name": "sre_metrics",
    "description": "计算 SRE Agent 近期核心效果指标。",
    "parameters": {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "统计周期，默认 7 天"},
        },
    },
}

SRE_WEEKLY_SUMMARY_SCHEMA = {
    "name": "sre_weekly_summary",
    "description": "生成最近 7 天的 SRE Agent 中文周报。",
    "parameters": {"type": "object", "properties": {}},
}


async def _tool_sre_metrics(args: Dict[str, Any], **_: Any) -> str:
    """工具入口：计算指标。"""
    result = await compute_metrics(int(args.get("days", 7) or 7))
    return json.dumps(result, ensure_ascii=False)


async def _tool_sre_weekly_summary(args: Dict[str, Any], **_: Any) -> str:
    """工具入口：生成周报。"""
    del args
    result = {"summary": await generate_weekly_summary()}
    return json.dumps(result, ensure_ascii=False)


registry.register(name="sre_metrics", toolset="sre", schema=SRE_METRICS_SCHEMA, handler=_tool_sre_metrics, is_async=True)
registry.register(name="sre_weekly_summary", toolset="sre", schema=SRE_WEEKLY_SUMMARY_SCHEMA, handler=_tool_sre_weekly_summary, is_async=True)
