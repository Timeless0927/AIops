"""验证身份 Hook 可以提取平台用户 ID。"""

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
            "name": "飞书管理员",
            "platform": "feishu",
            "platform_user_id": "ou_test_user",
            "role": "admin",
            "can_approve": True,
        },
        {
            "name": "钉钉运维",
            "platform": "dingtalk",
            "platform_user_id": "staff_operator_01",
            "role": "operator",
            "can_approve": False,
        },
    ]


@pytest.mark.asyncio
async def test_feishu_event_exposes_platform_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """飞书事件中的 sender.open_id 应被正确返回。"""
    monkeypatch.setattr(identity, "_load_operators", _mock_load_operators)

    event = {
        "platform": "feishu",
        "sender": {"open_id": "ou_test_user"},
    }

    result = await identity.on_session_start(event)

    assert result["allowed"] is True
    assert result["platform_user_id"] == "ou_test_user"


@pytest.mark.asyncio
async def test_dingtalk_event_exposes_platform_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """钉钉事件中的 senderStaffId 应被正确返回。"""
    monkeypatch.setattr(identity, "_load_operators", _mock_load_operators)

    event = {
        "platform": "dingtalk",
        "senderStaffId": "staff_operator_01",
    }

    result = await identity.on_session_start(event)

    assert result["allowed"] is True
    assert result["platform_user_id"] == "staff_operator_01"


@pytest.mark.asyncio
async def test_missing_platform_user_id_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少平台用户 ID 的事件应直接拒绝。"""
    monkeypatch.setattr(identity, "_load_operators", _mock_load_operators)

    event = {
        "platform": "feishu",
        "sender": {},
    }

    result = await identity.on_session_start(event)

    assert result["allowed"] is False
    assert result["message"] == "你没有权限使用此 agent，请联系管理员"
    assert result["reason"] == "missing_platform_identity"
