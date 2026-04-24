"""测试自动审计 Hook。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "hooks" / "audit_hook.py"
    spec = importlib.util.spec_from_file_location("test_audit_hook_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_handle_records_sre_tools(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """SRE 工具调用应自动写入审计。"""
    module = _load_module()
    calls: list[dict] = []

    async def _fake_record_audit(**kwargs):
        calls.append(kwargs)
        return len(calls)

    monkeypatch.setattr(module.audit_log, "record_audit", _fake_record_audit)

    result = await module.handle(
        "agent:step",
        {
            "tool_names": ["k8s_read", "prometheus_query", "search_files"],
            "user_id": "feishu:ou_event",
            "cluster": "prod",
            "namespace": "default",
            "incident_id": "inc-100",
        },
    )

    assert result["recorded"] == 2
    assert len(calls) == 2
    assert calls[0]["who"] == "feishu:ou_event"
    assert calls[0]["tool_level"] == "read"
    assert calls[1]["tool_name"] == "prometheus_query"


@pytest.mark.asyncio
async def test_handle_ignores_non_sre_tools_and_extracts_user(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """非 SRE 工具不应记录，且可从 event 中提取用户。"""
    module = _load_module()
    calls: list[dict] = []

    async def _fake_record_audit(**kwargs):
        calls.append(kwargs)
        return len(calls)

    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.setattr(module.audit_log, "record_audit", _fake_record_audit)

    result_non_sre = await module.handle("agent:step", {"tool_names": ["read_file"], "user_id": "feishu:ou_x"})
    result_sre = await module.handle("agent:step", {"tool_names": ["k8s_write"], "user_id": "feishu:ou_x"})

    assert result_non_sre["recorded"] == 0
    assert result_sre["recorded"] == 1
    assert calls[0]["who"] == "feishu:ou_x"
    assert calls[0]["tool_level"] == "write"
