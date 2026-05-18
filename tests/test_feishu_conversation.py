"""测试飞书 incident 会话编排。"""

from __future__ import annotations

import json
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
async def test_publish_approval_card_uses_incident_thread_and_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """审批卡片应优先回复到 incident 线程，并带上 requester。"""
    module = _load_module()
    calls: list[tuple[str, dict, dict]] = []

    async def _fake_reply(message_id, payload, config):
        calls.append((message_id, payload, config))
        return {"data": {"message_id": "om_approval", "root_id": "om_root", "thread_id": "omt_thread"}}

    monkeypatch.setattr(module, "_reply_feishu_message", _fake_reply, raising=False)

    result = await module.publish_approval_card(
        {
            "approval_id": "ap-1",
            "incident_id": "inc-1",
            "operation_type": "k8s_write",
            "namespace": "default",
            "risk_level": "medium",
            "requester": "alice",
            "command": "kubectl scale deployment web --replicas=3",
        },
        {
            "chat_id": "oc_ops",
            "root_message_id": "om_root",
            "thread_id": "omt_thread",
            "status_card_message_id": "om_card",
        },
        {},
    )

    assert calls[0][0] == "om_root"
    assert calls[0][1]["msg_type"] == "interactive"
    assert calls[0][1]["reply_in_thread"] is True
    card = json.loads(calls[0][1]["content"])
    assert "**请求人:** alice" in card["elements"][0]["content"]
    assert card["elements"][1]["actions"][0]["value"] == {
        "aiops_action": "approval_decision",
        "approval_id": "ap-1",
        "decision": "approved",
    }
    assert result == {
        "message_id": "om_approval",
        "root_message_id": "om_root",
        "thread_id": "omt_thread",
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
    """分析摘要应回到 incident 根消息线程，并返回标准化消息 id。"""
    module = _load_module()
    calls = []

    async def _fake_reply(message_id, payload, config):
        calls.append(
            {
                "message_id": message_id,
                "payload": payload,
                "config": config,
            }
        )
        return {"data": {"message_id": "om_summary", "root_id": "om_root", "thread_id": "omt_thread"}}

    monkeypatch.setattr(module, "_reply_feishu_message", _fake_reply, raising=False)

    result = await module.publish_incident_analysis_summary(
        {
            "id": "incident-42",
            "root_message_id": "om_root",
            "status_card_message_id": "om_card",
        },
        "【当前判断】\n已形成初步结论",
        {"platforms": {"feishu": {"main_chat_id": "oc_ops"}}},
    )

    assert len(calls) == 1
    assert calls[0]["message_id"] == "om_root"
    assert calls[0]["payload"]["content"] == '{"text": "【当前判断】\\n已形成初步结论"}'
    assert calls[0]["payload"]["msg_type"] == "text"
    assert calls[0]["payload"]["reply_in_thread"] is True
    assert calls[0]["payload"]["uuid"].startswith("incident-summary-")
    assert len(calls[0]["payload"]["uuid"]) <= 50
    assert calls[0]["config"] == {"platforms": {"feishu": {"main_chat_id": "oc_ops"}}}
    assert result == {
        "message_id": "om_summary",
        "root_message_id": "om_root",
        "thread_id": "omt_thread",
    }


@pytest.mark.asyncio
async def test_publish_incident_analysis_summary_falls_back_to_status_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少根消息时，应回到状态卡片消息继续在线程中回复。"""
    module = _load_module()
    calls = []

    async def _fake_reply(message_id, payload, config):
        calls.append((message_id, payload, config))
        return {"data": {"message_id": "om_summary", "root_id": "om_card", "thread_id": "om_card"}}

    monkeypatch.setattr(module, "_reply_feishu_message", _fake_reply, raising=False)

    result = await module.publish_incident_analysis_summary(
        {
            "id": "incident-99",
            "root_message_id": None,
            "status_card_message_id": "om_card",
        },
        "【当前判断】\n仍在补充证据",
        {},
    )

    assert calls[0][0] == "om_card"
    assert calls[0][1]["content"] == '{"text": "【当前判断】\\n仍在补充证据"}'
    assert calls[0][1]["msg_type"] == "text"
    assert calls[0][1]["reply_in_thread"] is True
    assert calls[0][1]["uuid"].startswith("incident-summary-")
    assert len(calls[0][1]["uuid"]) <= 50
    assert result == {
        "message_id": "om_summary",
        "root_message_id": "om_card",
        "thread_id": "om_card",
    }


@pytest.mark.asyncio
async def test_publish_incident_analysis_summary_preserves_existing_thread_when_reply_ids_are_sparse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reply 返回缺少 root/thread id 时，应保留 incident 原有线程绑定。"""
    module = _load_module()

    async def _fake_reply(message_id, payload, config):
        assert message_id == "om_root"
        assert payload["uuid"].startswith("incident-summary-")
        assert len(payload["uuid"]) <= 50
        assert config == {}
        return {"data": {"message_id": "om_summary"}}

    monkeypatch.setattr(module, "_reply_feishu_message", _fake_reply, raising=False)

    result = await module.publish_incident_analysis_summary(
        {
            "id": "incident-42",
            "root_message_id": "om_root",
            "thread_id": "omt_thread",
            "status_card_message_id": "om_card",
        },
        "【当前判断】\n已形成初步结论",
        {},
    )

    assert result == {
        "message_id": "om_summary",
        "root_message_id": "om_root",
        "thread_id": "omt_thread",
    }


@pytest.mark.asyncio
async def test_publish_incident_analysis_summary_extracts_message_id_from_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """reply 成功但 message_id 只出现在 data.body 时，也应返回标准化消息 id。"""
    module = _load_module()

    async def _fake_reply(message_id, payload, config):
        assert message_id == "om_root"
        assert payload["reply_in_thread"] is True
        assert len(payload["uuid"]) <= 50
        assert config == {}
        return {
            "code": 0,
            "data": {
                "body": {
                    "message_id": "om_summary",
                    "root_id": "om_root",
                    "thread_id": "omt_thread",
                }
            },
        }

    monkeypatch.setattr(module, "_reply_feishu_message", _fake_reply, raising=False)

    result = await module.publish_incident_analysis_summary(
        {
            "id": "5038b175-931e-44fa-8b1e-bee8269abc07",
            "root_message_id": "om_root",
            "thread_id": "om_root",
            "status_card_message_id": "om_card",
        },
        "【当前判断】\n已形成初步结论",
        {},
    )

    assert result == {
        "message_id": "om_summary",
        "root_message_id": "om_root",
        "thread_id": "omt_thread",
    }
