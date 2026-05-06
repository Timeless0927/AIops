"""测试飞书文本审批回复处理。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "hooks" / "approval_reply.py"
    module_name = "test_approval_reply_module"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_approve_reply() -> None:
    """批准文本应解析为 approved 决策。"""
    module = _load_module()

    parsed = module.parse_approval_reply("批准 abc-123")

    assert parsed == {"decision": "approved", "approval_id": "abc-123", "reason": None}


def test_parse_deny_reply_with_reason() -> None:
    """拒绝文本应保留拒绝原因。"""
    module = _load_module()

    parsed = module.parse_approval_reply("拒绝 abc-123 风险过高")

    assert parsed == {"decision": "denied", "approval_id": "abc-123", "reason": "风险过高"}


def test_parse_non_approval_reply() -> None:
    """非审批文本应忽略。"""
    module = _load_module()

    assert module.parse_approval_reply("看一下 nginx") is None


@pytest.mark.asyncio
async def test_handle_approve_reply_records_timeline(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """批准回复应 resolve approval 并记录 timeline。"""
    module = _load_module()
    events: list[tuple] = []

    class _ApprovalAsync:
        @staticmethod
        async def resolve_approval(approval_id, decision, approver, reason=None):
            assert (approval_id, decision, approver, reason) == ("ap-1", "approved", "ou_admin", None)
            return {"ok": True, "approval_id": approval_id, "status": "approved"}

        @staticmethod
        async def check_approval(approval_id):
            assert approval_id == "ap-1"
            return {"approval_id": approval_id, "incident_id": "inc-1"}

    class _IncidentStore:
        @staticmethod
        async def add_event(incident_id, event_type, tool_name, input_summary, output_summary, metadata=None):
            events.append((incident_id, event_type, tool_name, input_summary, output_summary, metadata))
            return 1

    monkeypatch.setattr(module, "approval_async", _ApprovalAsync)
    monkeypatch.setattr(module, "incident_store", _IncidentStore)

    result = await module.handle_approval_reply("批准 ap-1", "ou_admin")

    assert result == {"handled": True, "ok": True, "approval_id": "ap-1", "status": "approved"}
    assert events == [("inc-1", "approval_approved", "approval_reply", "ap-1", "ou_admin", None)]


@pytest.mark.asyncio
async def test_handle_deny_reply_records_timeline(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """拒绝回复应 resolve approval 并记录 timeline。"""
    module = _load_module()
    events: list[tuple] = []

    class _ApprovalAsync:
        @staticmethod
        async def resolve_approval(approval_id, decision, approver, reason=None):
            assert (approval_id, decision, approver, reason) == ("ap-2", "denied", "ou_admin", "风险过高")
            return {"ok": True, "approval_id": approval_id, "status": "denied"}

        @staticmethod
        async def check_approval(approval_id):
            assert approval_id == "ap-2"
            return {"approval_id": approval_id, "incident_id": "inc-2"}

    class _IncidentStore:
        @staticmethod
        async def add_event(incident_id, event_type, tool_name, input_summary, output_summary, metadata=None):
            events.append((incident_id, event_type, tool_name, input_summary, output_summary, metadata))
            return 1

    monkeypatch.setattr(module, "approval_async", _ApprovalAsync)
    monkeypatch.setattr(module, "incident_store", _IncidentStore)

    result = await module.handle_approval_reply("拒绝 ap-2 风险过高", "ou_admin")

    assert result == {"handled": True, "ok": True, "approval_id": "ap-2", "status": "denied"}
    assert events == [("inc-2", "approval_denied", "approval_reply", "ap-2", "ou_admin", None)]


@pytest.mark.asyncio
async def test_handle_unknown_approval_reply_returns_error(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """未知审批 ID 应返回清晰错误且不写 timeline。"""
    module = _load_module()

    class _ApprovalAsync:
        @staticmethod
        async def resolve_approval(approval_id, decision, approver, reason=None):
            del decision, approver, reason
            return {"ok": False, "approval_id": approval_id, "message": "审批记录不存在"}

    monkeypatch.setattr(module, "approval_async", _ApprovalAsync)

    result = await module.handle_approval_reply("批准 missing", "ou_admin")

    assert result == {"handled": True, "ok": False, "approval_id": "missing", "message": "审批记录不存在"}
