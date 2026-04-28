"""语音消息上下文增强 Hook。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


VOICE_MARKER = "[The user sent a voice message"


def _load_incident_analysis_summary_module():
    """按文件路径加载 incident_analysis_summary 模块。"""
    module_name = "hooks.incident_analysis_summary"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = Path(__file__).resolve().parent / "incident_analysis_summary.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_incident_store_module():
    """按文件路径加载 incident_store 模块。"""
    module_name = "toolsets.incident_store"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = Path(__file__).resolve().parent.parent / "toolsets" / "incident_store.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _extract_message_text(context: dict[str, Any]) -> str:
    """从上下文提取消息文本。"""
    for key in ("text", "message", "content"):
        value = context.get(key)
        if isinstance(value, str):
            return value
    return ""


def _build_incident_prefix(incidents: list[dict[str, Any]]) -> str:
    """构建活跃事件摘要前缀。"""
    if not incidents:
        return ""

    parts = []
    for incident in incidents:
        parts.append(
            f"{incident.get('id', '')} {incident.get('alert_name', '')} in {incident.get('namespace', '')}, status={incident.get('status', '')}"
        )
    return f"[当前活跃事件: {'; '.join(parts)}]"


def _extract_feishu_context(context: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """从消息上下文提取飞书 chat/thread/message 标识。"""
    chat_id = context.get("chat_id") or context.get("chatId")
    thread_id = context.get("thread_id") or context.get("threadId")
    message_id = context.get("message_id") or context.get("messageId")

    message = context.get("message")
    if isinstance(message, dict):
        chat_id = chat_id or message.get("chat_id") or message.get("chatId")
        thread_id = thread_id or message.get("thread_id") or message.get("threadId")
        message_id = message_id or message.get("message_id") or message.get("messageId")

    return (
        str(chat_id).strip() if chat_id else None,
        str(thread_id).strip() if thread_id else None,
        str(message_id).strip() if message_id else None,
    )


def _build_bound_incident_prefix(incident: dict[str, Any], timeline: list[dict[str, Any]]) -> str:
    """构建绑定 incident 的上下文摘要。"""
    header = (
        f"[绑定事件: {incident.get('id', '')} {incident.get('alert_name', '')} "
        f"in {incident.get('namespace', '')}, status={incident.get('status', '')}]"
    )
    if not timeline:
        return header

    event_parts = []
    for item in timeline[-5:]:
        output = item.get("output_summary") or item.get("input_summary") or ""
        event_parts.append(f"{item.get('event_type', '')}: {output}")
    return f"{header}\n[事件时间线: {'; '.join(event_parts)}]"


def _extract_analysis_from_timeline(timeline: list[dict[str, Any]]) -> dict[str, Any] | None:
    """从最近的事件元数据中恢复分析摘要。"""
    for item in reversed(timeline):
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else None
        if not metadata:
            continue
        analysis = metadata.get("analysis")
        if isinstance(analysis, dict):
            return analysis
    return None


async def handle(event_type: str, context: dict[str, Any]) -> dict[str, Any]:
    """为消息注入 incident 上下文。"""
    if event_type != "session:message":
        return {"modified": False}

    message_text = _extract_message_text(context)
    platform = str(context.get("platform") or context.get("source") or "").strip().lower()
    if platform == "feishu":
        chat_id, thread_id, message_id = _extract_feishu_context(context)
        try:
            incident_store = _load_incident_store_module()
            incident = await incident_store.find_by_feishu_context(
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
            )
            if incident is not None:
                analysis = incident.get("analysis") if isinstance(incident.get("analysis"), dict) else None
                timeline = await incident_store.get_timeline(incident["id"])
                if analysis is None:
                    analysis = _extract_analysis_from_timeline(timeline)
                if analysis is not None:
                    incident_analysis_summary = _load_incident_analysis_summary_module()
                    prefix = incident_analysis_summary.render_context_summary(incident, analysis)
                else:
                    prefix = _build_bound_incident_prefix(incident, timeline)
                return {
                    "modified": True,
                    "incident_id": incident["id"],
                    "enriched_text": f"{prefix}\n{message_text}" if message_text else prefix,
                    "session_context": {"incident_id": incident["id"], "incident": incident},
                    "reply_target": {
                        "platform": "feishu",
                        "receive_id_type": "chat_id",
                        "receive_id": incident.get("chat_id"),
                        "thread_id": incident.get("thread_id"),
                    },
                }
        except Exception:
            pass

    if VOICE_MARKER not in message_text:
        return {"modified": False}

    try:
        incident_store = _load_incident_store_module()
        incidents = await incident_store.list_active()
    except Exception:
        incidents = []

    prefix = _build_incident_prefix(incidents)
    enriched_text = message_text if not prefix else f"{prefix}\n{message_text}"
    return {"modified": True, "enriched_text": enriched_text}
