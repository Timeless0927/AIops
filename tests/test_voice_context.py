"""测试语音上下文增强 Hook。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "hooks" / "voice_context.py"
    module_name = "test_voice_context_module"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_non_session_message_event_returns_unmodified(**_: object) -> None:
    """非 session:message 事件应直接忽略。"""
    module = _load_module()

    result = await module.handle("agent:step", {"text": "hello"})

    assert result == {"modified": False}


@pytest.mark.asyncio
async def test_plain_text_message_returns_unmodified(**_: object) -> None:
    """普通文本消息不应被增强。"""
    module = _load_module()

    result = await module.handle("session:message", {"text": "这是一条普通文本"})

    assert result == {"modified": False}


@pytest.mark.asyncio
async def test_voice_message_with_active_incidents(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """语音消息存在活跃事件时应注入摘要。"""
    module = _load_module()

    class _IncidentStore:
        @staticmethod
        async def list_active():
            return [
                {"id": "inc-1", "alert_name": "PodCrash", "namespace": "default", "status": "active"},
                {"id": "inc-2", "alert_name": "HighMemory", "namespace": "ops", "status": "investigating"},
            ]

    monkeypatch.setattr(module, "_load_incident_store_module", lambda: _IncidentStore)

    result = await module.handle("session:message", {"text": "[The user sent a voice message and it was transcribed]\n帮我看下当前告警"})

    assert result["modified"] is True
    assert "当前活跃事件" in result["enriched_text"]
    assert "inc-1 PodCrash in default, status=active" in result["enriched_text"]


@pytest.mark.asyncio
async def test_thread_message_uses_bound_incident_context(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """同一飞书 thread 的后续消息应优先注入绑定 incident 上下文。"""
    module = _load_module()

    class _IncidentStore:
        @staticmethod
        async def find_by_feishu_context(chat_id=None, thread_id=None, message_id=None):
            assert chat_id == "oc_ops"
            assert thread_id == "omt_thread"
            assert message_id == "om_reply"
            return {"id": "inc-1", "alert_name": "PodCrash", "namespace": "default", "status": "triaging"}

        @staticmethod
        async def get_timeline(incident_id):
            assert incident_id == "inc-1"
            return [{"event_type": "alert_fired", "output_summary": "pod 重启次数持续增加"}]

    monkeypatch.setattr(module, "_load_incident_store_module", lambda: _IncidentStore)

    result = await module.handle(
        "session:message",
        {
            "platform": "feishu",
            "chat_id": "oc_ops",
            "thread_id": "omt_thread",
            "message_id": "om_reply",
            "text": "继续排查",
        },
    )

    assert result["modified"] is True
    assert result["incident_id"] == "inc-1"
    assert "绑定事件: inc-1 PodCrash in default, status=triaging" in result["enriched_text"]
    assert "alert_fired: pod 重启次数持续增加" in result["enriched_text"]


@pytest.mark.asyncio
async def test_thread_message_prefers_structured_analysis(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """同一飞书 thread 的上下文应优先注入结构化 analysis。"""
    module = _load_module()

    class _IncidentStore:
        @staticmethod
        async def find_by_feishu_context(chat_id=None, thread_id=None, message_id=None):
            del chat_id, thread_id, message_id
            return {"id": "inc-1", "alert_name": "PodCrash", "namespace": "default", "status": "triaging"}

        @staticmethod
        async def get_timeline(incident_id):
            assert incident_id == "inc-1"
            return [{"event_type": "alert_fired", "output_summary": "pod 重启次数持续增加"}]

        @staticmethod
        async def get_analysis(incident_id):
            assert incident_id == "inc-1"
            return {
                "symptoms": ["PodCrash firing in default/prod-a"],
                "likely_scope": "workload",
                "suspected_root_causes": [{"summary": "应用容器异常退出", "confidence": 0.4}],
                "missing_evidence": ["缺少 pod 日志摘要"],
                "next_best_actions": ["检查最近 15 分钟 Pod 日志"],
                "confidence": 0.35,
            }

    monkeypatch.setattr(module, "_load_incident_store_module", lambda: _IncidentStore)

    result = await module.handle(
        "session:message",
        {
            "platform": "feishu",
            "chat_id": "oc_ops",
            "thread_id": "omt_thread",
            "message_id": "om_reply",
            "text": "继续排查",
        },
    )

    assert result["modified"] is True
    assert "结构化分析" in result["enriched_text"]
    assert "范围=workload" in result["enriched_text"]
    assert "缺少 pod 日志摘要" in result["enriched_text"]


@pytest.mark.asyncio
async def test_thread_message_shows_top_root_cause_and_next_action(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """结构化摘要应突出 top suspected root cause 和 top next action。"""
    module = _load_module()

    class _IncidentStore:
        @staticmethod
        async def find_by_feishu_context(chat_id=None, thread_id=None, message_id=None):
            del chat_id, thread_id, message_id
            return {"id": "inc-1", "alert_name": "PodCrash", "namespace": "default", "status": "triaging"}

        @staticmethod
        async def get_timeline(incident_id):
            assert incident_id == "inc-1"
            return [{"event_type": "alert_fired", "output_summary": "pod 重启次数持续增加"}]

        @staticmethod
        async def get_analysis(incident_id):
            assert incident_id == "inc-1"
            return {
                "symptoms": ["PodCrash firing in default/prod-a"],
                "likely_scope": "workload",
                "suspected_root_causes": [
                    {"summary": "资源压力可能导致工作负载异常", "confidence": 0.7},
                    {"summary": "Kubernetes events 显示工作负载异常", "confidence": 0.65},
                ],
                "missing_evidence": ["缺少 pod 日志摘要"],
                "next_best_actions": [
                    "检查 Pod CPU/内存指标与资源配置",
                    "检查异常 Pod 的事件与探针失败细节",
                ],
                "confidence": 0.7,
            }

    monkeypatch.setattr(module, "_load_incident_store_module", lambda: _IncidentStore)

    result = await module.handle(
        "session:message",
        {
            "platform": "feishu",
            "chat_id": "oc_ops",
            "thread_id": "omt_thread",
            "message_id": "om_reply",
            "text": "继续排查",
        },
    )

    assert result["modified"] is True
    assert "Top根因=资源压力可能导致工作负载异常" in result["enriched_text"]
    assert "Top下一步=检查 Pod CPU/内存指标与资源配置" in result["enriched_text"]


@pytest.mark.asyncio
async def test_voice_message_without_active_incidents(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """无活跃事件时应只返回原始语音文本。"""
    module = _load_module()

    class _IncidentStore:
        @staticmethod
        async def list_active():
            return []

    monkeypatch.setattr(module, "_load_incident_store_module", lambda: _IncidentStore)
    text = "[The user sent a voice message and it was transcribed]\n查询一下集群状态"

    result = await module.handle("session:message", {"text": text})

    assert result["modified"] is True
    assert result["enriched_text"] == text
    assert "当前活跃事件" not in result["enriched_text"]


@pytest.mark.asyncio
async def test_incident_store_load_failure_degrades_gracefully(monkeypatch: pytest.MonkeyPatch, **_: object) -> None:
    """incident_store 加载失败时应优雅降级。"""
    module = _load_module()
    text = "[The user sent a voice message and it was transcribed]\n请继续"

    def _raise_error():
        raise RuntimeError("load failed")

    monkeypatch.setattr(module, "_load_incident_store_module", _raise_error)

    result = await module.handle("session:message", {"text": text})

    assert result["modified"] is True
    assert result["enriched_text"] == text
