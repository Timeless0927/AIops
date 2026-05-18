"""Runtime overlay for AIOps Feishu text approval replies.

This module patches Hermes' Feishu adapter at process startup without changing
the vendored ``hermes-agent`` submodule.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)
_PATCH_MARKER = "_aiops_approval_overlay_original"
_CARD_CALLBACK_PATCH_MARKER = "_aiops_approval_card_callback_original"
_APPROVAL_CARD_ACTION = "approval_decision"


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


def build_approval_card_payload(approval: dict[str, Any]) -> dict[str, Any]:
    """Build a Feishu interactive approval card payload."""
    approval_id = str(approval.get("approval_id") or approval.get("id") or "").strip()
    command = str(approval.get("command") or "").strip()
    namespace = str(approval.get("namespace") or "-").strip() or "-"
    risk_level = str(approval.get("risk_level") or "-").strip() or "-"
    operation_type = str(approval.get("operation_type") or "-").strip() or "-"
    incident_id = str(approval.get("incident_id") or "-").strip() or "-"
    requester = str(approval.get("requester") or "-").strip() or "-"
    cmd_preview = command[:3000] + "..." if len(command) > 3000 else command

    content = (
        f"**审批 ID:** {approval_id}\n"
        f"**Incident:** {incident_id}\n"
        f"**操作:** {operation_type}\n"
        f"**命名空间:** {namespace}\n"
        f"**风险:** {risk_level}\n"
        f"**请求人:** {requester}"
    )
    if cmd_preview:
        content += f"\n```bash\n{cmd_preview}\n```"

    def _button(label: str, decision: str, button_type: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": button_type,
            "value": {
                "aiops_action": _APPROVAL_CARD_ACTION,
                "approval_id": approval_id,
                "decision": decision,
            },
        }

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "AIOps 审批请求"},
        },
        "elements": [
            {"tag": "markdown", "content": content},
            {
                "tag": "action",
                "actions": [
                    _button("批准", "approved", "primary"),
                    _button("拒绝", "denied", "danger"),
                ],
            },
        ],
    }


def build_approval_card_content(approval: dict[str, Any]) -> str:
    """Return Feishu message content JSON for an approval interactive card."""
    return json.dumps(build_approval_card_payload(approval), ensure_ascii=False)


def _extract_card_action_value(data: Any) -> dict[str, Any]:
    event = getattr(data, "event", None)
    action = getattr(event, "action", None)
    value = getattr(action, "value", {}) or {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return value if isinstance(value, dict) else {}


def _is_aiops_approval_card_action(action_value: dict[str, Any]) -> bool:
    return str(action_value.get("aiops_action") or "").strip() == _APPROVAL_CARD_ACTION


def _extract_card_operator_open_id(data: Any) -> str:
    event = getattr(data, "event", None)
    operator = getattr(event, "operator", None)
    return str(getattr(operator, "open_id", "") or "").strip()


def _extract_card_chat_id(data: Any) -> str:
    event = getattr(data, "event", None)
    context = getattr(event, "context", None)
    return str(getattr(context, "open_chat_id", "") or "").strip()


def _extract_card_reply_to(data: Any) -> str | None:
    event = getattr(data, "event", None)
    for attr in ("open_message_id", "message_id", "token"):
        value = str(getattr(event, attr, "") or "").strip()
        if value:
            return value
    return None


async def _send_card_callback_reply(adapter: Any, data: Any, content: str) -> None:
    chat_id = _extract_card_chat_id(data)
    send = getattr(adapter, "send", None)
    if not chat_id or not callable(send):
        return
    await send(
        chat_id=chat_id,
        content=content,
        reply_to=_extract_card_reply_to(data),
        metadata={},
    )


async def handle_approval_card_callback(adapter: Any, data: Any) -> dict[str, Any]:
    """Normalize a Feishu card callback into approval_reply decision handling."""
    action_value = _extract_card_action_value(data)
    if not _is_aiops_approval_card_action(action_value):
        return {"handled": False}

    approval_id = str(action_value.get("approval_id") or "").strip()
    decision = str(action_value.get("decision") or "").strip().lower()
    reason_value = action_value.get("reason")
    reason = reason_value.strip() if isinstance(reason_value, str) and reason_value.strip() else None
    open_id = _extract_card_operator_open_id(data)

    if not approval_id:
        result = {"handled": True, "ok": False, "approval_id": "", "message": "缺少 approval_id"}
        await _send_card_callback_reply(adapter, data, _build_response_text(result))
        return result
    if not open_id:
        result = {
            "handled": True,
            "ok": False,
            "approval_id": approval_id,
            "message": "无法识别审批人身份",
        }
        await _send_card_callback_reply(adapter, data, _build_response_text(result))
        return result
    if decision not in {"approved", "denied"}:
        result = {
            "handled": True,
            "ok": False,
            "approval_id": approval_id,
            "message": "decision 仅支持 approved 或 denied",
        }
        await _send_card_callback_reply(adapter, data, _build_response_text(result))
        return result

    approval_reply = _load_approval_reply_module()
    result = await approval_reply.handle_approval_decision(
        approval_id=approval_id,
        decision=decision,
        reason=reason,
        approver_id=open_id,
        source="feishu_card",
    )
    await _send_card_callback_reply(adapter, data, _build_response_text(result))
    return result


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


def _operator_field(operator: Any, field: str) -> str:
    if isinstance(operator, dict):
        value = operator.get(field)
    else:
        value = getattr(operator, field, "")
    return value.strip() if isinstance(value, str) else ""


def _readable_name(value: str, open_id: str) -> str:
    text = value.strip()
    if not text or text == open_id:
        return ""
    return text


def _hermes_config_path() -> Path:
    """Resolve the config.yaml path Hermes reads at runtime."""
    override = os.getenv("HERMES_CONFIG") or os.getenv("HERMES_CONFIG_PATH")
    if override:
        return Path(override).expanduser()

    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        hermes_home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
    else:
        hermes_home = get_hermes_home()
    return hermes_home / "config.yaml"


def _load_operator_name_from_config(open_id: str) -> str:
    if not open_id:
        return ""
    try:
        import yaml
    except ImportError:
        return ""

    config_path = _hermes_config_path()
    if not config_path.exists():
        return ""
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except Exception as exc:
        logger.warning("AIOps Feishu approval overlay cannot read config operators: %s", exc)
        return ""

    permissions = config.get("sre_permissions") if isinstance(config, dict) else None
    operators = permissions.get("operators") if isinstance(permissions, dict) else None
    if not isinstance(operators, list):
        return ""

    for operator in operators:
        if not isinstance(operator, dict):
            continue
        platform = str(operator.get("platform") or "").strip().lower()
        platform_user_id = str(operator.get("platform_user_id") or "").strip()
        if platform == "feishu" and platform_user_id == open_id:
            return _readable_name(str(operator.get("name") or ""), open_id)
    return ""


def _extract_card_operator_label(adapter: Any, data: Any) -> str:
    event = getattr(data, "event", None)
    operator = getattr(event, "operator", None)
    open_id = _extract_card_operator_open_id(data)
    cached_name = ""
    get_cached_sender_name = getattr(adapter, "_get_cached_sender_name", None)
    if open_id and callable(get_cached_sender_name):
        cached_name = str(get_cached_sender_name(open_id) or "").strip()

    return _first_text(
        _readable_name(cached_name, open_id),
        _readable_name(_operator_field(operator, "name"), open_id),
        _readable_name(_operator_field(operator, "user_name"), open_id),
        _load_operator_name_from_config(open_id),
        open_id,
        "未知",
    )


def _build_submitted_approval_card(action_value: dict[str, Any], operator_label: str) -> dict[str, Any]:
    approval_id = str(action_value.get("approval_id") or "").strip() or "-"
    decision = str(action_value.get("decision") or "").strip().lower()
    decision_label = {"approved": "批准", "denied": "拒绝"}.get(decision, decision or "-")
    content = (
        "**状态:** 审批请求已提交，后台处理中\n"
        f"**审批 ID:** {approval_id}\n"
        f"**选择:** {decision_label}\n"
        f"**操作者:** {operator_label}"
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "AIOps 审批请求已提交"},
        },
        "elements": [
            {"tag": "markdown", "content": content},
        ],
    }


def _card_action_response(original: Any, card_payload: dict[str, Any] | None = None) -> Any:
    response_class = getattr(original, "__globals__", {}).get("P2CardActionTriggerResponse")
    if response_class is None:
        return None
    response = response_class()
    callback_card_class = getattr(original, "__globals__", {}).get("CallBackCard")
    if card_payload is not None and callback_card_class is not None:
        card = callback_card_class()
        card.type = "raw"
        card.data = card_payload
        response.card = card
    return response


def _loop_accepts_callbacks(adapter: Any, loop: Any) -> bool:
    checker = getattr(adapter, "_loop_accepts_callbacks", None)
    if callable(checker):
        return bool(checker(loop))
    return loop is not None and not bool(getattr(loop, "is_closed", lambda: False)())


def _schedule_card_callback(adapter: Any, loop: Any, data: Any) -> None:
    coro = handle_approval_card_callback(adapter, data)
    submit = getattr(adapter, "_submit_on_loop", None)
    if callable(submit):
        submit(loop, coro)
        return
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    log_background_failure = getattr(adapter, "_log_background_failure", None)
    if callable(log_background_failure):
        future.add_done_callback(log_background_failure)


def _patch_card_callback(feishu_adapter_class: type) -> None:
    original = getattr(feishu_adapter_class, "_on_card_action_trigger", None)
    if original is None or getattr(original, _CARD_CALLBACK_PATCH_MARKER, None) is not None:
        return

    def _patched_on_card_action_trigger(self, data: Any) -> Any:
        action_value = _extract_card_action_value(data)
        if not _is_aiops_approval_card_action(action_value):
            return original(self, data)

        loop = getattr(self, "_loop", None)
        if _loop_accepts_callbacks(self, loop):
            _schedule_card_callback(self, loop, data)
        else:
            logger.warning("AIOps Feishu approval card callback dropped before adapter loop is ready")
        operator_label = _extract_card_operator_label(self, data)
        card_payload = _build_submitted_approval_card(action_value, operator_label)
        return _card_action_response(original, card_payload)

    setattr(_patched_on_card_action_trigger, _CARD_CALLBACK_PATCH_MARKER, original)
    feishu_adapter_class._on_card_action_trigger = _patched_on_card_action_trigger
    logger.info("AIOps Feishu approval card callback overlay installed")


def install(feishu_adapter_class: type | None = None) -> None:
    """Install the approval overlay on Hermes' FeishuAdapter class."""
    if feishu_adapter_class is None:
        from gateway.platforms.feishu import FeishuAdapter

        feishu_adapter_class = FeishuAdapter

    original = getattr(feishu_adapter_class, "_process_inbound_message", None)
    if original is None:
        raise RuntimeError("Hermes FeishuAdapter missing _process_inbound_message; overlay cannot install")
    if getattr(original, _PATCH_MARKER, None) is None:

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

    _patch_card_callback(feishu_adapter_class)
