"""飞书 incident 会话编排。"""

from __future__ import annotations

import json
import os
from typing import Any

import aiohttp

from hooks.incident_analysis_summary import render_thread_summary


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
    message_id = data.get("message_id") or data.get("messageId") or response.get("message_id")
    root_id = data.get("root_id") or data.get("root_message_id") or body.get("root_id") or message_id
    thread_id = data.get("thread_id") or data.get("threadId") or body.get("thread_id") or root_id
    return {
        "message_id": str(message_id) if message_id else None,
        "root_id": str(root_id) if root_id else None,
        "thread_id": str(thread_id) if thread_id else None,
    }


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
    text: str,
    config: dict[str, Any],
    *,
    reply_in_thread: bool,
    uuid: str,
) -> dict[str, Any]:
    token = await _tenant_access_token(config)
    if not token or not message_id.strip():
        return {}

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "msg_type": "text",
                "reply_in_thread": reply_in_thread,
                "uuid": uuid,
            },
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


async def publish_incident_analysis_summary(incident: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """在线程内回复固定 incident 分析摘要。"""
    message_id = str(incident.get("root_message_id") or incident.get("status_card_message_id") or "").strip()
    if not message_id:
        return {}

    summary = render_thread_summary(incident)
    return await _reply_feishu_message(
        message_id,
        summary,
        config,
        reply_in_thread=True,
        uuid=f"incident-summary-{incident.get('id', '')}",
    )
