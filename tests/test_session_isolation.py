"""验证飞书私聊会话隔离。"""

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
            "platform_user_id": "ou_admin_a",
            "role": "admin",
            "can_approve": True,
        },
        {
            "name": "运维员",
            "platform": "feishu",
            "platform_user_id": "ou_operator_b",
            "role": "operator",
            "can_approve": False,
        },
    ]


@pytest.mark.asyncio
async def test_feishu_private_chat_sessions_are_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """不同飞书发送者应绑定到各自独立的操作者资料。"""
    monkeypatch.setattr(identity, "_load_operators", _mock_load_operators)

    admin_event = {
        "platform": "feishu",
        "sender": {"open_id": "ou_admin_a"},
    }
    operator_event = {
        "platform": "feishu",
        "sender": {"open_id": "ou_operator_b"},
    }

    admin_session = await identity.on_session_start(admin_event)
    operator_session = await identity.on_session_start(operator_event)

    assert admin_session["allowed"] is True
    assert operator_session["allowed"] is True
    assert admin_session["operator_profile"] != operator_session["operator_profile"]
    assert admin_session["operator_profile"]["name"] == "管理员"
    assert operator_session["operator_profile"]["name"] == "运维员"
    assert admin_session["session_context"]["operator"]["platform_user_id"] == "ou_admin_a"
    assert operator_session["session_context"]["operator"]["platform_user_id"] == "ou_operator_b"
    assert admin_session["session_context"]["operator"] != operator_session["session_context"]["operator"]
