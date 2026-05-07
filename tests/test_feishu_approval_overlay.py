"""测试父项目 Feishu 审批 runtime overlay。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _FakeFeishuAdapter:
    pass


@pytest.mark.asyncio
async def test_overlay_intercepts_approve_reply(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """批准文本应被 overlay 拦截，不进入 Hermes 原始流程。"""
    from runtime.feishu_approval_overlay import install

    original_calls: list[str] = []

    async def _original(self, *, data, message, sender_id, chat_type, message_id):
        del self, data, message, sender_id, chat_type, message_id
        original_calls.append("called")

    _FakeFeishuAdapter._process_inbound_message = _original

    approval_calls: list[tuple[str, str]] = []

    class _ApprovalReply:
        @staticmethod
        async def handle_approval_reply(text, approver):
            approval_calls.append((text, approver))
            return {"handled": True, "ok": True, "approval_id": "ap-1", "status": "approved"}

    monkeypatch.setattr("runtime.feishu_approval_overlay._load_approval_reply_module", lambda: _ApprovalReply)
    install(_FakeFeishuAdapter)

    adapter = _FakeFeishuAdapter()
    adapter.send = AsyncMock()
    message = SimpleNamespace(
        chat_id="oc_ops",
        thread_id="omt_thread",
        message_type="text",
        content='{"text":"批准 ap-1"}',
    )

    await adapter._process_inbound_message(
        data=SimpleNamespace(event=SimpleNamespace(message=message)),
        message=message,
        sender_id=SimpleNamespace(open_id="ou_admin", user_id="u_admin"),
        chat_type="p2p",
        message_id="om_reply",
    )

    assert approval_calls == [("批准 ap-1", "ou_admin")]
    adapter.send.assert_awaited_once_with(
        chat_id="oc_ops",
        content="审批已批准：ap-1",
        reply_to="om_reply",
        metadata={"thread_id": "omt_thread"},
    )
    assert original_calls == []


@pytest.mark.asyncio
async def test_overlay_passes_normal_text_to_original(**_: object) -> None:
    """普通文本不应被 overlay 吃掉。"""
    from runtime.feishu_approval_overlay import install

    original_calls: list[tuple[str, str]] = []

    async def _original(self, *, data, message, sender_id, chat_type, message_id):
        del self, data, sender_id, chat_type
        original_calls.append((message.content, message_id))

    _FakeFeishuAdapter._process_inbound_message = _original
    install(_FakeFeishuAdapter)

    adapter = _FakeFeishuAdapter()
    message = SimpleNamespace(
        chat_id="oc_ops",
        thread_id=None,
        message_type="text",
        content='{"text":"继续排查"}',
    )

    await adapter._process_inbound_message(
        data=SimpleNamespace(event=SimpleNamespace(message=message)),
        message=message,
        sender_id=SimpleNamespace(open_id="ou_user", user_id=None),
        chat_type="p2p",
        message_id="om_text",
    )

    assert original_calls == [('{"text":"继续排查"}', "om_text")]


@pytest.mark.asyncio
async def test_overlay_refuses_missing_sender_without_state_change(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """缺少审批人身份时应回复失败，不调用审批处理。"""
    from runtime.feishu_approval_overlay import install

    async def _original(self, *, data, message, sender_id, chat_type, message_id):
        raise AssertionError("approval replies must not reach original Hermes flow")

    _FakeFeishuAdapter._process_inbound_message = _original
    approval_reply = SimpleNamespace(handle_approval_reply=AsyncMock())
    monkeypatch.setattr("runtime.feishu_approval_overlay._load_approval_reply_module", lambda: approval_reply)
    install(_FakeFeishuAdapter)

    adapter = _FakeFeishuAdapter()
    adapter.send = AsyncMock()
    message = SimpleNamespace(
        chat_id="oc_ops",
        thread_id=None,
        message_type="text",
        content='{"text":"批准 ap-1"}',
    )

    await adapter._process_inbound_message(
        data=SimpleNamespace(event=SimpleNamespace(message=message)),
        message=message,
        sender_id=SimpleNamespace(open_id="", user_id=""),
        chat_type="p2p",
        message_id="om_reply",
    )

    approval_reply.handle_approval_reply.assert_not_awaited()
    adapter.send.assert_awaited_once_with(
        chat_id="oc_ops",
        content="审批处理失败：无法识别审批人身份",
        reply_to="om_reply",
        metadata={},
    )


def test_overlay_install_fails_when_adapter_shape_changes() -> None:
    """Hermes 私有方法缺失时应 fail-fast。"""
    from runtime.feishu_approval_overlay import install

    class _BrokenAdapter:
        pass

    with pytest.raises(RuntimeError, match="_process_inbound_message"):
        install(_BrokenAdapter)


def test_gateway_wrapper_installs_overlay_before_running_gateway(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """启动 wrapper 应先安装 overlay，再进入 Hermes gateway runner。"""
    from runtime import hermes_gateway

    calls: list[tuple[str, object]] = []

    monkeypatch.setattr("runtime.feishu_approval_overlay.install", lambda: calls.append(("install", None)))

    def _run_gateway(*, verbose=0, quiet=False, replace=False):
        calls.append(("run_gateway", (verbose, quiet, replace)))

    monkeypatch.setitem(
        __import__("sys").modules,
        "hermes_cli.gateway",
        SimpleNamespace(run_gateway=_run_gateway),
    )

    hermes_gateway.main()

    assert calls == [("install", None), ("run_gateway", (0, False, True))]
