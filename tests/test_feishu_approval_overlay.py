"""测试父项目 Feishu 审批 runtime overlay。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _FakeFeishuAdapter:
    pass


class CallBackCard:
    def __init__(self) -> None:
        self.type = None
        self.data = None


class P2CardActionTriggerResponse:
    def __init__(self) -> None:
        self.card = None


def _card_action_data(
    value: dict,
    *,
    open_id: str | None = "ou_admin",
    operator_fields: dict | None = None,
    chat_id: str = "oc_ops",
    message_id: str = "om_card",
) -> SimpleNamespace:
    operator_payload = {"open_id": open_id}
    if operator_fields:
        operator_payload.update(operator_fields)
    operator = SimpleNamespace(**operator_payload) if open_id is not None else None
    return SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(value=value),
            operator=operator,
            context=SimpleNamespace(open_chat_id=chat_id),
            open_message_id=message_id,
            token="tok-card",
        ),
    )


def _contains_card_tag(node: object, tag: str) -> bool:
    if isinstance(node, dict):
        return node.get("tag") == tag or any(_contains_card_tag(value, tag) for value in node.values())
    if isinstance(node, list):
        return any(_contains_card_tag(value, tag) for value in node)
    return False


def test_build_approval_card_payload_contains_approve_and_reject_buttons() -> None:
    """审批卡片 payload 应带 approve/reject callback value。"""
    from runtime.feishu_approval_overlay import build_approval_card_payload

    payload = build_approval_card_payload(
        {
            "approval_id": "ap-1",
            "incident_id": "inc-1",
            "operation_type": "k8s_write",
            "namespace": "default",
            "risk_level": "medium",
            "command": "kubectl scale deployment web --replicas=3",
        },
    )

    actions = payload["elements"][1]["actions"]
    values = [action["value"] for action in actions]
    assert values == [
        {"aiops_action": "approval_decision", "approval_id": "ap-1", "decision": "approved"},
        {"aiops_action": "approval_decision", "approval_id": "ap-1", "decision": "denied"},
    ]
    assert payload["header"]["title"]["content"] == "AIOps 审批请求"


@pytest.mark.asyncio
async def test_card_callback_approve_button_uses_decision_service(
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    """Approve button callback 应走 approval_reply normalized service。"""
    from runtime.feishu_approval_overlay import handle_approval_card_callback

    calls: list[dict] = []

    class _ApprovalReply:
        @staticmethod
        async def handle_approval_decision(**kwargs):
            calls.append(kwargs)
            return {"handled": True, "ok": True, "approval_id": kwargs["approval_id"], "status": "approved"}

    monkeypatch.setattr("runtime.feishu_approval_overlay._load_approval_reply_module", lambda: _ApprovalReply)
    adapter = SimpleNamespace(send=AsyncMock())
    data = _card_action_data(
        {"aiops_action": "approval_decision", "approval_id": "ap-1", "decision": "approved"},
    )

    result = await handle_approval_card_callback(adapter, data)

    assert result == {"handled": True, "ok": True, "approval_id": "ap-1", "status": "approved"}
    assert calls == [
        {
            "approval_id": "ap-1",
            "decision": "approved",
            "reason": None,
            "approver_id": "ou_admin",
            "source": "feishu_card",
        },
    ]
    adapter.send.assert_awaited_once_with(
        chat_id="oc_ops",
        content="审批已批准：ap-1",
        reply_to="om_card",
        metadata={},
    )


@pytest.mark.asyncio
async def test_card_callback_reject_button_preserves_reason(
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    """Reject button callback 应保留 reason。"""
    from runtime.feishu_approval_overlay import handle_approval_card_callback

    calls: list[dict] = []

    class _ApprovalReply:
        @staticmethod
        async def handle_approval_decision(**kwargs):
            calls.append(kwargs)
            return {"handled": True, "ok": True, "approval_id": kwargs["approval_id"], "status": "denied"}

    monkeypatch.setattr("runtime.feishu_approval_overlay._load_approval_reply_module", lambda: _ApprovalReply)
    adapter = SimpleNamespace(send=AsyncMock())
    data = _card_action_data(
        {
            "aiops_action": "approval_decision",
            "approval_id": "ap-2",
            "decision": "denied",
            "reason": "风险过高",
        },
    )

    result = await handle_approval_card_callback(adapter, data)

    assert result == {"handled": True, "ok": True, "approval_id": "ap-2", "status": "denied"}
    assert calls == [
        {
            "approval_id": "ap-2",
            "decision": "denied",
            "reason": "风险过高",
            "approver_id": "ou_admin",
            "source": "feishu_card",
        },
    ]
    adapter.send.assert_awaited_once_with(
        chat_id="oc_ops",
        content="审批已拒绝：ap-2",
        reply_to="om_card",
        metadata={},
    )


@pytest.mark.asyncio
async def test_card_callback_unauthorized_returns_failure_from_decision_service(
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    """未授权 card callback 只返回失败，不绕过 authorization service。"""
    from runtime.feishu_approval_overlay import handle_approval_card_callback

    class _ApprovalReply:
        @staticmethod
        async def handle_approval_decision(**kwargs):
            return {"handled": True, "ok": False, "approval_id": kwargs["approval_id"], "message": "审批人未授权"}

    monkeypatch.setattr("runtime.feishu_approval_overlay._load_approval_reply_module", lambda: _ApprovalReply)
    adapter = SimpleNamespace(send=AsyncMock())
    data = _card_action_data(
        {"aiops_action": "approval_decision", "approval_id": "ap-1", "decision": "approved"},
        open_id="ou_unknown",
    )

    result = await handle_approval_card_callback(adapter, data)

    assert result == {"handled": True, "ok": False, "approval_id": "ap-1", "message": "审批人未授权"}
    adapter.send.assert_awaited_once_with(
        chat_id="oc_ops",
        content="审批处理失败：审批人未授权",
        reply_to="om_card",
        metadata={},
    )


@pytest.mark.asyncio
async def test_card_callback_missing_fields_fail_closed(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """缺 approval_id 或 operator/open_id 不得调用 decision service。"""
    from runtime.feishu_approval_overlay import handle_approval_card_callback

    approval_reply = SimpleNamespace(handle_approval_decision=AsyncMock())
    monkeypatch.setattr("runtime.feishu_approval_overlay._load_approval_reply_module", lambda: approval_reply)
    adapter = SimpleNamespace(send=AsyncMock())

    missing_approval = _card_action_data({"aiops_action": "approval_decision", "decision": "approved"})
    missing_operator = _card_action_data(
        {"aiops_action": "approval_decision", "approval_id": "ap-1", "decision": "approved"},
        open_id=None,
    )

    result_missing_approval = await handle_approval_card_callback(adapter, missing_approval)
    result_missing_operator = await handle_approval_card_callback(adapter, missing_operator)

    assert result_missing_approval == {"handled": True, "ok": False, "approval_id": "", "message": "缺少 approval_id"}
    assert result_missing_operator == {
        "handled": True,
        "ok": False,
        "approval_id": "ap-1",
        "message": "无法识别审批人身份",
    }
    approval_reply.handle_approval_decision.assert_not_awaited()


def test_overlay_installs_card_callback_patch(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """install 应拦截 AIOps card callback，其它 card action 保持 Hermes 原逻辑。"""
    from runtime import feishu_approval_overlay

    calls: list[object] = []

    class _Adapter:
        _loop = object()

        async def _process_inbound_message(self, *, data, message, sender_id, chat_type, message_id):
            del self, data, message, sender_id, chat_type, message_id

        def _on_card_action_trigger(self, data):
            calls.append(("original", data))
            return "original"

    monkeypatch.setattr(feishu_approval_overlay, "_loop_accepts_callbacks", lambda adapter, loop: True)
    monkeypatch.setattr(
        feishu_approval_overlay,
        "_schedule_card_callback",
        lambda adapter, loop, data: calls.append(("scheduled", data)),
    )

    feishu_approval_overlay.install(_Adapter)
    adapter = _Adapter()
    approval_data = _card_action_data(
        {"aiops_action": "approval_decision", "approval_id": "ap-1", "decision": "approved"},
    )
    other_data = _card_action_data({"other_action": "keep-original"})

    response = adapter._on_card_action_trigger(approval_data)

    assert response is not None
    assert response.card is not None
    assert response.card.type == "raw"
    card = response.card.data
    assert card["header"]["title"]["content"] == "AIOps 审批请求已提交"
    assert "批准" in card["elements"][0]["content"]
    assert "ou_admin" in card["elements"][0]["content"]
    assert not _contains_card_tag(card, "action")
    assert not _contains_card_tag(card, "button")
    assert adapter._on_card_action_trigger(other_data) == "original"
    assert calls == [("scheduled", approval_data), ("original", other_data)]


def test_card_submitted_state_uses_config_operator_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **_: object,
) -> None:
    """提交态卡片应从 Hermes runtime config operators fallback 到姓名。"""
    from runtime import feishu_approval_overlay

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        """
sre_permissions:
  operators:
    - name: "本地测试审批人"
      platform: "feishu"
      platform_user_id: "ou_config_user"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(feishu_approval_overlay, "_loop_accepts_callbacks", lambda adapter, loop: True)
    monkeypatch.setattr(feishu_approval_overlay, "_schedule_card_callback", lambda adapter, loop, data: None)

    class _Adapter:
        _loop = object()

        async def _process_inbound_message(self, *, data, message, sender_id, chat_type, message_id):
            del self, data, message, sender_id, chat_type, message_id

        def _on_card_action_trigger(self, data):
            return "original"

    feishu_approval_overlay.install(_Adapter)
    adapter = _Adapter()
    data = _card_action_data(
        {"aiops_action": "approval_decision", "approval_id": "ap-1", "decision": "approved"},
        open_id="ou_config_user",
    )

    response = adapter._on_card_action_trigger(data)
    content = response.card.data["elements"][0]["content"]

    assert "本地测试审批人" in content
    assert "ou_config_user" not in content


def test_card_submitted_state_cached_name_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **_: object,
) -> None:
    """adapter cached name 优先于 callback operator 和 config。"""
    from runtime import feishu_approval_overlay

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        """
sre_permissions:
  operators:
    - name: "配置审批人"
      platform: "feishu"
      platform_user_id: "ou_cached_user"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(feishu_approval_overlay, "_loop_accepts_callbacks", lambda adapter, loop: True)
    monkeypatch.setattr(feishu_approval_overlay, "_schedule_card_callback", lambda adapter, loop, data: None)

    class _Adapter:
        _loop = object()

        async def _process_inbound_message(self, *, data, message, sender_id, chat_type, message_id):
            del self, data, message, sender_id, chat_type, message_id

        def _on_card_action_trigger(self, data):
            return "original"

        def _get_cached_sender_name(self, open_id):
            assert open_id == "ou_cached_user"
            return "缓存审批人"

    feishu_approval_overlay.install(_Adapter)
    adapter = _Adapter()
    data = _card_action_data(
        {"aiops_action": "approval_decision", "approval_id": "ap-1", "decision": "denied"},
        open_id="ou_cached_user",
        operator_fields={"name": "事件审批人", "user_name": "事件用户名"},
    )

    response = adapter._on_card_action_trigger(data)
    content = response.card.data["elements"][0]["content"]

    assert "缓存审批人" in content
    assert "事件审批人" not in content
    assert "配置审批人" not in content
    assert "ou_cached_user" not in content


def test_card_callback_does_not_import_or_call_execution() -> None:
    """Feishu card callback 只做审批状态变更，不直接触发修复执行。"""
    source = (Path(__file__).resolve().parents[1] / "runtime" / "feishu_approval_overlay.py").read_text(
        encoding="utf-8",
    )
    forbidden_tokens = [
        "approval_execution",
        "remediation_execution",
        "process_pending_executions",
        "process_approval_execution",
    ]

    assert [token for token in forbidden_tokens if token in source] == []


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


def test_gateway_wrapper_starts_worker_after_overlay_before_gateway(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """启动 wrapper 应先安装 overlay，再启动执行 worker，再进入 Hermes gateway runner。"""
    from runtime import hermes_gateway

    monkeypatch.delenv("AIOPS_APPROVAL_EXECUTION_WORKER_ENABLED", raising=False)
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr("runtime.feishu_approval_overlay.install", lambda: calls.append(("install", None)))

    class _Worker:
        def stop(self):
            calls.append(("stop_worker", None))
            return True

    monkeypatch.setattr(
        "runtime.approval_execution_worker.start_approval_execution_worker",
        lambda: calls.append(("start_worker", None)) or _Worker(),
    )

    def _run_gateway(*, verbose=0, quiet=False, replace=False):
        calls.append(("run_gateway", (verbose, quiet, replace)))

    monkeypatch.setitem(
        __import__("sys").modules,
        "hermes_cli.gateway",
        SimpleNamespace(run_gateway=_run_gateway),
    )

    hermes_gateway.main()

    assert calls == [
        ("install", None),
        ("start_worker", None),
        ("run_gateway", (0, False, True)),
        ("stop_worker", None),
    ]


def test_gateway_wrapper_skips_worker_when_env_disabled(
    monkeypatch: pytest.MonkeyPatch,
    **_: object,
) -> None:
    """本地只测审批/卡片时可通过 env 跳过 execution worker。"""
    from runtime import hermes_gateway

    monkeypatch.setenv("AIOPS_APPROVAL_EXECUTION_WORKER_ENABLED", "0")
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr("runtime.feishu_approval_overlay.install", lambda: calls.append(("install", None)))

    def _start_worker():
        calls.append(("start_worker", None))
        raise AssertionError("worker should be disabled")

    monkeypatch.setitem(
        sys.modules,
        "runtime.approval_execution_worker",
        SimpleNamespace(start_approval_execution_worker=_start_worker),
    )

    def _run_gateway(*, verbose=0, quiet=False, replace=False):
        calls.append(("run_gateway", (verbose, quiet, replace)))

    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.gateway",
        SimpleNamespace(run_gateway=_run_gateway),
    )

    hermes_gateway.main()

    assert calls == [
        ("install", None),
        ("run_gateway", (0, False, True)),
    ]


def test_gateway_wrapper_stops_worker_when_gateway_raises(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """Hermes gateway 异常退出时应停止执行 worker。"""
    from runtime import hermes_gateway

    monkeypatch.delenv("AIOPS_APPROVAL_EXECUTION_WORKER_ENABLED", raising=False)
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr("runtime.feishu_approval_overlay.install", lambda: calls.append(("install", None)))

    class _Worker:
        def stop(self):
            calls.append(("stop_worker", None))
            return True

    monkeypatch.setattr(
        "runtime.approval_execution_worker.start_approval_execution_worker",
        lambda: calls.append(("start_worker", None)) or _Worker(),
    )

    def _run_gateway(*, verbose=0, quiet=False, replace=False):
        calls.append(("run_gateway", (verbose, quiet, replace)))
        raise RuntimeError("gateway stopped")

    monkeypatch.setitem(
        __import__("sys").modules,
        "hermes_cli.gateway",
        SimpleNamespace(run_gateway=_run_gateway),
    )

    with pytest.raises(RuntimeError, match="gateway stopped"):
        hermes_gateway.main()

    assert calls == [
        ("install", None),
        ("start_worker", None),
        ("run_gateway", (0, False, True)),
        ("stop_worker", None),
    ]


def test_gateway_wrapper_continues_when_worker_start_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    **_: object,
) -> None:
    """执行 worker 启动失败不应阻断 Hermes gateway。"""
    from runtime import hermes_gateway

    monkeypatch.delenv("AIOPS_APPROVAL_EXECUTION_WORKER_ENABLED", raising=False)
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr("runtime.feishu_approval_overlay.install", lambda: calls.append(("install", None)))

    def _start_worker():
        calls.append(("start_worker", None))
        raise RuntimeError("worker start failed")

    monkeypatch.setattr(
        "runtime.approval_execution_worker.start_approval_execution_worker",
        _start_worker,
    )

    def _run_gateway(*, verbose=0, quiet=False, replace=False):
        calls.append(("run_gateway", (verbose, quiet, replace)))

    monkeypatch.setitem(
        __import__("sys").modules,
        "hermes_cli.gateway",
        SimpleNamespace(run_gateway=_run_gateway),
    )

    with caplog.at_level("ERROR", logger="runtime.hermes_gateway"):
        hermes_gateway.main()

    assert calls == [
        ("install", None),
        ("start_worker", None),
        ("run_gateway", (0, False, True)),
    ]
    assert "approval execution worker failed to start" in caplog.text
