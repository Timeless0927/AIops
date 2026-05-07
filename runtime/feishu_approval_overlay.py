"""Runtime overlay for AIOps Feishu text approval replies.

This module patches Hermes' Feishu adapter at process startup without changing
the vendored ``hermes-agent`` submodule.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)
_PATCH_MARKER = "_aiops_approval_overlay_original"


def _load_approval_reply_module() -> Any:
    module_name = "aiops_hooks_approval_reply"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = Path(__file__).resolve().parents[1] / "hooks" / "approval_reply.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load approval reply hook: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _extract_message_text(message: Any) -> str:
    raw_content = getattr(message, "content", "") or ""
    if not isinstance(raw_content, str):
        return ""
    stripped = raw_content.strip()
    if not stripped.startswith("{"):
        return raw_content.strip()

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return raw_content.strip()

    text = parsed.get("text") if isinstance(parsed, dict) else None
    return text.strip() if isinstance(text, str) else ""


def _parse_approval_text(text: str) -> tuple[str, str | None] | None:
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 2:
        return None
    if parts[0] == "批准" and len(parts) == 2:
        return parts[1], None
    if parts[0] == "拒绝" and len(parts) == 3 and parts[2].strip():
        return parts[1], parts[2].strip()
    return None


def _build_response_text(result: dict[str, Any]) -> str:
    approval_id = str(result.get("approval_id") or "")
    if not result.get("ok"):
        return f"审批处理失败：{result.get('message') or '未知错误'}"

    status = result.get("status")
    if status == "approved":
        return f"审批已批准：{approval_id}"
    if status == "denied":
        return f"审批已拒绝：{approval_id}"
    return f"审批状态已更新：{approval_id}"


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def install(feishu_adapter_class: type | None = None) -> None:
    """Install the approval overlay on Hermes' FeishuAdapter class."""
    if feishu_adapter_class is None:
        from gateway.platforms.feishu import FeishuAdapter

        feishu_adapter_class = FeishuAdapter

    original = getattr(feishu_adapter_class, "_process_inbound_message", None)
    if original is None:
        raise RuntimeError("Hermes FeishuAdapter missing _process_inbound_message; overlay cannot install")
    if getattr(original, _PATCH_MARKER, None) is not None:
        return

    async def _patched_process_inbound_message(
        self,
        *,
        data: Any,
        message: Any,
        sender_id: Any,
        chat_type: str,
        message_id: str,
    ) -> Any:
        text = _extract_message_text(message)
        if _parse_approval_text(text) is None:
            return await original(
                self,
                data=data,
                message=message,
                sender_id=sender_id,
                chat_type=chat_type,
                message_id=message_id,
            )

        chat_id = getattr(message, "chat_id", "") or ""
        thread_id = getattr(message, "thread_id", None) or None
        metadata = {"thread_id": str(thread_id).strip()} if thread_id else {}
        approver = _first_text(getattr(sender_id, "open_id", None), getattr(sender_id, "user_id", None))
        if not approver:
            await self.send(
                chat_id=chat_id,
                content="审批处理失败：无法识别审批人身份",
                reply_to=message_id,
                metadata=metadata,
            )
            return None

        try:
            approval_reply = _load_approval_reply_module()
            result = await approval_reply.handle_approval_reply(text.strip(), approver)
            response_text = _build_response_text(result)
        except Exception as exc:
            logger.warning("AIOps Feishu approval overlay failed: %s", exc, exc_info=True)
            response_text = f"审批处理失败：{exc}"

        await self.send(
            chat_id=chat_id,
            content=response_text,
            reply_to=message_id,
            metadata=metadata,
        )
        return None

    setattr(_patched_process_inbound_message, _PATCH_MARKER, original)
    feishu_adapter_class._process_inbound_message = _patched_process_inbound_message
    logger.info("AIOps Feishu approval overlay installed")

