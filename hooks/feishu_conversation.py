"""飞书 incident 会话编排。"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import aiohttp

from runtime.feishu_approval_overlay import build_approval_card_payload


def _feishu_config(config: dict[str, Any]) -> dict[str, Any]:
    platforms = config.get("platforms") if isinstance(config.get("platforms"), dict) else {}
    feishu = platforms.get("feishu") if isinstance(platforms.get("feishu"), dict) else {}
    return feishu


def _resolve_main_chat_id(config: dict[str, Any]) -> str | None:
    env_chat_id = os.getenv("FEISHU_MAIN_CHAT_ID") or os.getenv("FEISHU_ALERT_CHAT_ID")
    if env_chat_id:
        return env_chat_id.strip()

    feishu = _feishu_config(config)
    for key in ("main_chat_id", "alert_chat_id", "ops_chat_id", "chat_id"):
        value = feishu.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _build_status_text(incident_id: str, alert: dict[str, Any]) -> str:
    return (
        f"[Incident {incident_id}] {alert.get('severity', 'info')} 告警: "
        f"{alert.get('alertname', '')} in {alert.get('namespace', '')}/{alert.get('cluster', '')}\n"
        f"状态: new\n{alert.get('description', '')}"
    )


def _extract_message_ids(response: dict[str, Any]) -> dict[str, str | None]:
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    body = data.get("body") if isinstance(data.get("body"), dict) else {}
    message_id = (
        data.get("message_id")
        or data.get("messageId")
        or body.get("message_id")
        or body.get("messageId")
        or response.get("message_id")
    )
    root_id = data.get("root_id") or data.get("root_message_id") or body.get("root_id") or message_id
    thread_id = data.get("thread_id") or data.get("threadId") or body.get("thread_id") or root_id
    return {
        "message_id": str(message_id) if message_id else None,
        "root_id": str(root_id) if root_id else None,
        "thread_id": str(thread_id) if thread_id else None,
    }


def _summary_reply_uuid(incident_id: Any) -> str:
    raw = str(incident_id or "").strip()
    if not raw:
        return "incident-summary"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]
    return f"incident-summary-{digest}"


def _approval_reply_uuid(approval_id: Any) -> str:
    raw = str(approval_id or "").strip()
    if not raw:
        return "approval-card"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]
    return f"approval-card-{digest}"


async def _tenant_access_token(config: dict[str, Any]) -> str | None:
    env_token = os.getenv("FEISHU_TENANT_ACCESS_TOKEN")
    if env_token:
        return env_token

    feishu = _feishu_config(config)
    app_id = os.getenv("FEISHU_APP_ID") or str(feishu.get("app_id") or "")
    app_secret = os.getenv("FEISHU_APP_SECRET") or str(feishu.get("app_secret") or "")
    if not app_id or not app_secret or app_id.startswith("${") or app_secret.startswith("${"):
        return None

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            payload = await response.json()
    token = payload.get("tenant_access_token")
    return str(token) if token else None


async def _send_feishu_message(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    token = await _tenant_access_token(config)
    if not token:
        return {}

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": payload["receive_id_type"]},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": payload["receive_id"],
                "msg_type": payload["msg_type"],
                "content": payload["content"],
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            return await response.json()


async def _reply_feishu_message(
    message_id: str,
    payload: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    token = await _tenant_access_token(config)
    if not token or not message_id.strip():
        return {}

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            return await response.json()


async def publish_incident_status(
    incident_id: str,
    alert: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, str | None]:
    """发送 incident 状态消息到飞书主群并返回绑定字段。"""
    chat_id = _resolve_main_chat_id(config)
    if not chat_id:
        return {"chat_id": None, "root_message_id": None, "thread_id": None, "status_card_message_id": None}

    payload = {
        "receive_id_type": "chat_id",
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": _build_status_text(incident_id, alert)}, ensure_ascii=False),
    }
    response = await _send_feishu_message(payload, config)
    ids = _extract_message_ids(response)
    status_card_message_id = ids["message_id"]
    root_message_id = ids["root_id"] or status_card_message_id
    thread_id = ids["thread_id"] or root_message_id
    return {
        "chat_id": chat_id,
        "root_message_id": root_message_id,
        "thread_id": thread_id,
        "status_card_message_id": status_card_message_id,
    }


async def publish_approval_card(
    approval: dict[str, Any],
    incident: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, str | None]:
    """发送审批交互卡片到 incident 对应会话并返回绑定字段。"""
    chat_id = str(incident.get("chat_id") or "").strip()
    if not chat_id:
        return {"message_id": None, "root_message_id": None, "thread_id": None}

    card_content = json.dumps(build_approval_card_payload(approval), ensure_ascii=False)
    reply_message_id = str(
        incident.get("root_message_id") or incident.get("status_card_message_id") or ""
    ).strip()
    payload = {
        "msg_type": "interactive",
        "content": card_content,
        "reply_in_thread": True,
        "uuid": _approval_reply_uuid(approval.get("approval_id") or approval.get("id")),
    }

    if reply_message_id:
        response = await _reply_feishu_message(reply_message_id, payload, config)
        ids = _extract_message_ids(response)
        root_message_id = ids["root_id"] or reply_message_id
        thread_id = ids["thread_id"] or str(incident.get("thread_id") or "").strip() or root_message_id
        return {
            "message_id": ids["message_id"],
            "root_message_id": root_message_id,
            "thread_id": thread_id,
        }

    response = await _send_feishu_message(
        {
            "receive_id_type": "chat_id",
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": card_content,
        },
        config,
    )
    ids = _extract_message_ids(response)
    root_message_id = ids["root_id"] or ids["message_id"]
    thread_id = ids["thread_id"] or root_message_id
    return {
        "message_id": ids["message_id"],
        "root_message_id": root_message_id,
        "thread_id": thread_id,
    }


async def publish_native_approval_notice(
    incident: dict[str, Any],
    approval: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, str | None]:
    """发送飞书原生审批链接通知到 incident thread。"""
    chat_id = str(incident.get("chat_id") or "").strip()
    root_message_id = str(
        incident.get("thread_id") or incident.get("root_message_id") or incident.get("status_card_message_id") or ""
    ).strip()
    if not chat_id and not root_message_id:
        return {"message_id": None, "root_message_id": None, "thread_id": None}

    text = (
        f"审批已发起: {approval.get('external_url') or approval.get('external_instance_code') or ''}\n"
        f"风险: {approval.get('risk_level') or ''}\n"
        f"操作: {approval.get('operation_summary') or approval.get('command') or ''}"
    )
    payload = {
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
        "reply_in_thread": True,
        "uuid": _approval_reply_uuid(approval.get("approval_id") or approval.get("external_instance_code")),
    }
    if root_message_id:
        response = await _reply_feishu_message(root_message_id, payload, config)
        ids = _extract_message_ids(response)
        return {
            "message_id": ids["message_id"],
            "root_message_id": ids["root_id"] or root_message_id,
            "thread_id": ids["thread_id"] or root_message_id,
        }

    response = await _send_feishu_message(
        {
            "receive_id_type": "chat_id",
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        config,
    )
    ids = _extract_message_ids(response)
    root_id = ids["root_id"] or ids["message_id"]
    return {"message_id": ids["message_id"], "root_message_id": root_id, "thread_id": ids["thread_id"] or root_id}


async def resolve_reply_target(incident: dict[str, Any] | None, event: dict[str, Any]) -> dict[str, Any]:
    """解析回复目标，优先回到 incident 绑定 thread。"""
    if incident and incident.get("platform") == "feishu" and incident.get("chat_id"):
        target = {
            "platform": "feishu",
            "receive_id_type": "chat_id",
            "receive_id": incident["chat_id"],
        }
        if incident.get("thread_id"):
            target["thread_id"] = incident["thread_id"]
        return target

    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    open_id = sender.get("open_id") or event.get("open_id")
    return {"platform": "feishu", "receive_id_type": "open_id", "receive_id": str(open_id or "")}


async def publish_incident_analysis_summary(
    incident: dict[str, Any],
    summary_text: str,
    config: dict[str, Any],
) -> dict[str, str | None]:
    """在线程内回复固定 incident 分析摘要。"""
    message_id = str(incident.get("root_message_id") or incident.get("status_card_message_id") or "").strip()
    if not message_id:
        return {"message_id": None, "root_message_id": None, "thread_id": None}

    response = await _reply_feishu_message(
        message_id,
        {
            "content": json.dumps({"text": summary_text}, ensure_ascii=False),
            "msg_type": "text",
            "reply_in_thread": True,
            "uuid": _summary_reply_uuid(incident.get("id")),
        },
        config,
    )
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    body = data.get("body") if isinstance(data.get("body"), dict) else {}
    ids = _extract_message_ids(response)
    explicit_root_message_id = data.get("root_id") or data.get("root_message_id") or body.get("root_id")
    explicit_thread_id = data.get("thread_id") or data.get("threadId") or body.get("thread_id")
    fallback_root_message_id = str(
        incident.get("root_message_id") or incident.get("status_card_message_id") or ""
    ).strip() or None
    fallback_thread_id = str(
        incident.get("thread_id")
        or incident.get("root_message_id")
        or incident.get("status_card_message_id")
        or ""
    ).strip() or None
    return {
        "message_id": ids["message_id"],
        "root_message_id": str(explicit_root_message_id).strip() if explicit_root_message_id else fallback_root_message_id,
        "thread_id": str(explicit_thread_id).strip() if explicit_thread_id else fallback_thread_id,
    }
