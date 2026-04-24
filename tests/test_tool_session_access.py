"""验证工具可以访问会话级状态。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hooks import identity


async def _mock_load_operators() -> list[dict]:
    """返回测试专用的操作者列表。"""
    return [
        {
            "name": "管理员",
            "platform": "feishu",
            "platform_user_id": "ou_admin_tool",
            "role": "admin",
            "can_approve": True,
        }
    ]


async def _mock_tool(session_context: dict) -> dict:
    """模拟工具从 session_context 中读取操作者信息。"""
    operator = session_context["operator"]
    return {
        "name": operator["name"],
        "role": operator["role"],
        "can_approve": operator["can_approve"],
    }


@pytest.mark.asyncio
async def test_tool_can_access_session_level_operator_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具执行时应能读取会话中的 operator 状态。"""
    monkeypatch.setattr(identity, "_load_operators", _mock_load_operators)

    event = {
        "platform": "feishu",
        "sender": {"open_id": "ou_admin_tool"},
    }

    session_start_result = await identity.on_session_start(event)

    assert session_start_result["allowed"] is True

    tool_result = await _mock_tool(session_start_result["session_context"])

    assert tool_result == {
        "name": "管理员",
        "role": "admin",
        "can_approve": True,
    }
