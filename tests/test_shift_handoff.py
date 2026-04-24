"""测试运维交接工具。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "shift_handoff.py"
    spec = importlib.util.spec_from_file_location("test_shift_handoff_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_shift_handoff_updates_operator_and_audits(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """交接应生成摘要、更新负责人并写审计。"""
    module = _load_module()
    updated: list[tuple[str, str]] = []
    audits: list[dict] = []

    async def _fake_list_active():
        return [
            {"id": "inc-1", "alert_name": "PodCrash", "namespace": "default", "cluster": "prod", "status": "active"},
            {"id": "inc-2", "alert_name": "HighMemory", "namespace": "ops", "cluster": "prod", "status": "investigating"},
        ]

    async def _fake_get_timeline(incident_id: str):
        return [
            {"event_type": "triage_start", "tool_name": "k8s_read", "output_summary": f"{incident_id}-1"},
            {"event_type": "investigate_end", "tool_name": "loki_query", "output_summary": f"{incident_id}-2"},
            {"event_type": "remediate_proposed", "tool_name": "k8s_write", "output_summary": f"{incident_id}-3"},
            {"event_type": "remediate_verified", "tool_name": "prometheus_query", "output_summary": f"{incident_id}-4"},
        ]

    async def _fake_update_operator(incident_id: str, operator: str):
        updated.append((incident_id, operator))

    async def _fake_record_audit(**kwargs):
        audits.append(kwargs)
        return len(audits)

    monkeypatch.setattr(module.incident_store, "list_active", _fake_list_active)
    monkeypatch.setattr(module.incident_store, "get_timeline", _fake_get_timeline)
    monkeypatch.setattr(module.incident_store, "update_operator", _fake_update_operator)
    monkeypatch.setattr(module.audit_log, "record_audit", _fake_record_audit)

    result = await module.sre_shift_handoff("夜班值班员")

    assert result["ok"] is True
    assert result["handoff_to"] == "夜班值班员"
    assert len(result["incidents"]) == 2
    assert len(result["incidents"][0]["recent_events"]) == 3
    assert updated == [("inc-1", "夜班值班员"), ("inc-2", "夜班值班员")]
    assert len(audits) == 2
    assert audits[0]["what"] == "运维交接"
