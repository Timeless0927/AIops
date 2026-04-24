"""语音消息上下文增强 Hook。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


VOICE_MARKER = "[The user sent a voice message"


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


async def handle(event_type: str, context: dict[str, Any]) -> dict[str, Any]:
    """在语音转录消息前注入活跃事件摘要。"""
    if event_type != "session:message":
        return {"modified": False}

    message_text = _extract_message_text(context)
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
