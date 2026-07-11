"""Built-in Feishu group-bot presentation for Notification Requests."""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

from aiops.contracts.notification import notification_request


_COLORS = {"info": "blue", "warning": "orange", "error": "red", "critical": "red"}


def feishu_signature(timestamp: str, secret: str) -> str:
    digest = hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def render_feishu_card(payload: dict[str, Any], console_base_url: str) -> dict[str, object]:
    request = notification_request(**payload)
    return {
        "header": {
            "template": _COLORS[str(request["severity"])],
            "title": {"tag": "plain_text", "content": str(request["summary"])},
        },
        "elements": [
            {"tag": "markdown", "content": f"**Event** {request['event_type']}\n**Severity** {request['severity']}"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Open in AIOps"},
                        "type": "primary",
                        "url": f"{console_base_url.rstrip('/')}{request['console_path']}",
                    }
                ],
            },
        ],
    }

