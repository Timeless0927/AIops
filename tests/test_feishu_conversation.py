"""测试飞书 incident 会话编排。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "hooks" / "feishu_conversation.py"
    module_name = "test_feishu_conversation_module"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_publish_incident_status_uses_main_chat_not_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """告警状态消息应优先投递到主群并返回 thread 绑定。"""
    module = _load_module()
    sent = []

    async def _fake_send(payload, config):
        sent.append(payload)
        return {
            "message_id": "om_card",
            "root_id": "om_root",
            "thread_id": "omt_thread",
        }

    monkeypatch.setattr(module, "_send_feishu_message", _fake_send)

    result = await module.publish_incident_status(
        "inc-1",
        {"alertname": "PodCrash", "severity": "critical", "namespace": "default", "cluster": "prod", "description": "pod crash"},
        {"platforms": {"feishu": {"main_chat_id": "oc_ops"}}},
    )

    assert sent[0]["receive_id_type"] == "chat_id"
    assert sent[0]["receive_id"] == "oc_ops"
    assert result == {
        "chat_id": "oc_ops",
        "root_message_id": "om_root",
        "thread_id": "omt_thread",
        "status_card_message_id": "om_card",
    }


@pytest.mark.asyncio
async def test_resolve_reply_target_prefers_incident_thread() -> None:
    """回复目标应优先使用 incident 绑定 thread 而不是用户私聊。"""
    module = _load_module()

    target = await module.resolve_reply_target(
        incident={"platform": "feishu", "chat_id": "oc_ops", "thread_id": "omt_thread"},
        event={"sender": {"open_id": "ou_user"}},
    )

    assert target == {"platform": "feishu", "receive_id_type": "chat_id", "receive_id": "oc_ops", "thread_id": "omt_thread"}


@pytest.mark.asyncio
async def test_publish_incident_analysis_summary_replies_to_root_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """分析摘要应回到 incident 根消息线程，并使用固定 uuid。"""
    module = _load_module()
    calls = []

    async def _fake_reply(message_id, text, config, *, reply_in_thread, uuid):
        calls.append(
            {
                "message_id": message_id,
                "text": text,
                "config": config,
                "reply_in_thread": reply_in_thread,
                "uuid": uuid,
            }
        )
        return {"data": {"message_id": "om_summary"}}

    monkeypatch.setattr(module, "_reply_feishu_message", _fake_reply, raising=False)

    result = await module.publish_incident_analysis_summary(
        {
            "id": "incident-42",
            "root_message_id": "om_root",
            "status_card_message_id": "om_card",
            "analysis": {
                "supporting_evidence": [{"summary": "payments-api 重启 7 次"}],
                "suspected_root_causes": ["容器内存不足导致 OOMKilled"],
                "next_best_actions": ["先提升内存 limit 并观察 10 分钟"],
            },
            "alertname": "PodCrashLooping",
            "namespace": "payments",
            "cluster": "prod-a",
        },
        {"platforms": {"feishu": {"main_chat_id": "oc_ops"}}},
    )

    assert calls == [
        {
            "message_id": "om_root",
            "text": (
                "【当前判断】\n"
                "payments/prod-a 的 PodCrashLooping 已有初步结论，仍需在线程内持续跟进。\n\n"
                "【关键证据】\n"
                "- payments-api 重启 7 次\n\n"
                "【根因候选】\n"
                "- 容器内存不足导致 OOMKilled\n\n"
                "【建议下一步】\n"
                "- 先提升内存 limit 并观察 10 分钟"
            ),
            "config": {"platforms": {"feishu": {"main_chat_id": "oc_ops"}}},
            "reply_in_thread": True,
            "uuid": "incident-summary-incident-42",
        }
    ]
    assert result == {"data": {"message_id": "om_summary"}}


@pytest.mark.asyncio
async def test_publish_incident_analysis_summary_falls_back_to_status_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少根消息时，应回到状态卡片消息继续在线程中回复。"""
    module = _load_module()
    calls = []

    async def _fake_reply(message_id, text, config, *, reply_in_thread, uuid):
        calls.append((message_id, text, reply_in_thread, uuid))
        return {"data": {"message_id": "om_summary"}}

    monkeypatch.setattr(module, "_reply_feishu_message", _fake_reply, raising=False)

    await module.publish_incident_analysis_summary(
        {
            "id": "incident-99",
            "root_message_id": None,
            "status_card_message_id": "om_card",
            "analysis": {},
            "alertname": "PodCrashLooping",
            "namespace": "default",
            "cluster": "prod-a",
        },
        {},
    )

    assert calls[0][0] == "om_card"
    assert calls[0][2] is True
    assert calls[0][3] == "incident-summary-incident-99"
