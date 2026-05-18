"""飞书原生审批事件 webhook。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

from aiohttp import web


_SUPPORTED_STATUSES = {"APPROVED", "REJECTED", "CANCELED", "CANCELLED"}
_SEEN_NONCES: dict[str, float] = {}
_CONFIG_KEY = web.AppKey("feishu_approval_config", dict)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_approval_async_module():
    module_name = "aiops_feishu_approval_event_approval_async"
    if module_name in sys.modules:
        return sys.modules[module_name]
    module_path = _project_root() / "toolsets" / "approval_async.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


approval_async = _load_approval_async_module()


def _feishu_config(config: dict[str, Any] | None) -> dict[str, Any]:
    root = config if isinstance(config, dict) else {}
    platforms = root.get("platforms") if isinstance(root.get("platforms"), dict) else {}
    feishu = platforms.get("feishu") if isinstance(platforms.get("feishu"), dict) else {}
    return feishu


def _approval_config(config: dict[str, Any] | None) -> dict[str, Any]:
    feishu = _feishu_config(config)
    approval = feishu.get("approval") if isinstance(feishu.get("approval"), dict) else {}
    return approval


def _webhook_config(config: dict[str, Any] | None) -> dict[str, Any]:
    approval = _approval_config(config)
    webhook = approval.get("webhook") if isinstance(approval.get("webhook"), dict) else {}
    return webhook


def _header_value(headers: dict[str, Any], name: str) -> str:
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return str(value)
    return ""


def _expected_token(config: dict[str, Any] | None) -> str:
    feishu = _feishu_config(config)
    approval = _approval_config(config)
    webhook = _webhook_config(config)
    return str(
        approval.get("event_verification_token")
        or webhook.get("verification_token")
        or feishu.get("event_verification_token")
        or feishu.get("verification_token")
        or ""
    ).strip()


def _encrypt_key(config: dict[str, Any] | None) -> str:
    approval = _approval_config(config)
    webhook = _webhook_config(config)
    return str(approval.get("event_encrypt_key") or webhook.get("encrypt_key") or "").strip()


def _signature_ttl_seconds(config: dict[str, Any] | None) -> int:
    approval = _approval_config(config)
    webhook = _webhook_config(config)
    try:
        return max(1, int(approval.get("signature_ttl_seconds") or webhook.get("signature_ttl_seconds") or 300))
    except (TypeError, ValueError):
        return 300


def _canonical_body(payload: dict[str, Any], body: bytes | str | None) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _prune_seen_nonces(now: float) -> None:
    expired = [nonce for nonce, expires_at in _SEEN_NONCES.items() if expires_at <= now]
    for nonce in expired:
        _SEEN_NONCES.pop(nonce, None)


def _signature_rejection_reason(
    payload: dict[str, Any],
    headers: dict[str, Any],
    config: dict[str, Any] | None,
    body: bytes | str | None,
) -> str | None:
    signature = _header_value(headers, "X-Lark-Signature")
    timestamp = _header_value(headers, "X-Lark-Request-Timestamp")
    nonce = _header_value(headers, "X-Lark-Request-Nonce")
    if not signature:
        if timestamp or nonce:
            return "missing_signature"
        return None
    key = _encrypt_key(config)
    if not key or not timestamp or not nonce:
        return "invalid_signature"
    try:
        timestamp_seconds = int(timestamp)
    except (TypeError, ValueError):
        return "invalid_timestamp"
    ttl_seconds = _signature_ttl_seconds(config)
    now = time.time()
    if abs(now - timestamp_seconds) > ttl_seconds:
        return "expired_timestamp"
    _prune_seen_nonces(now)
    nonce_key = f"{timestamp}:{nonce}:{signature}"
    if nonce_key in _SEEN_NONCES:
        return "replayed_nonce"
    body_bytes = _canonical_body(payload, body)
    base = timestamp.encode("utf-8") + nonce.encode("utf-8") + body_bytes
    digest = hmac.new(key.encode("utf-8"), base, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    if not hmac.compare_digest(signature, expected):
        return "invalid_signature"
    _SEEN_NONCES[nonce_key] = now + ttl_seconds
    return None


def _valid_signature(
    payload: dict[str, Any],
    headers: dict[str, Any],
    config: dict[str, Any] | None,
    body: bytes | str | None,
) -> bool:
    return _signature_rejection_reason(payload, headers, config, body) is None


def _callback_path(config: dict[str, Any] | None) -> str:
    path = str(_approval_config(config).get("callback_path") or "/webhooks/feishu/approval").strip()
    if not path.startswith("/"):
        path = f"/{path}"
    return path


def _route_registered(app: web.Application, *, method: str, path: str) -> bool:
    expected_method = method.upper()
    for route in app.router.routes():
        resource = getattr(route, "resource", None)
        canonical = getattr(resource, "canonical", "")
        route_method = str(getattr(route, "method", "")).upper()
        if canonical == path and route_method == expected_method:
            return True
    return False


def _event_header(payload: dict[str, Any]) -> dict[str, Any]:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    return header


def _event_body(payload: dict[str, Any]) -> dict[str, Any]:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    return event


def _status_from_event(event: dict[str, Any]) -> str:
    status = str(event.get("status") or "").strip().upper()
    if status:
        return "CANCELED" if status == "CANCELLED" else status
    task_list = event.get("task_list") if isinstance(event.get("task_list"), list) else []
    for task in reversed(task_list):
        if isinstance(task, dict) and task.get("status"):
            value = str(task.get("status")).strip().upper()
            return "CANCELED" if value == "CANCELLED" else value
    return ""


def _rejected(reason: str) -> dict[str, Any]:
    return {"ok": False, "status": "rejected", "reason": reason}


async def handle_feishu_approval_event(
    payload: dict[str, Any],
    *,
    headers: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    body: bytes | str | None = None,
) -> dict[str, Any]:
    """校验飞书事件并同步本地 approval 状态。"""
    if not isinstance(payload, dict):
        return _rejected("invalid_payload")

    headers = headers if isinstance(headers, dict) else {}
    header = _event_header(payload)
    event = _event_body(payload)

    expected_token = _expected_token(config)
    actual_token = str(header.get("token") or payload.get("token") or "").strip()
    if expected_token and actual_token != expected_token:
        return _rejected("invalid_token")

    expected_app_id = str(_feishu_config(config).get("app_id") or "").strip()
    actual_app_id = str(header.get("app_id") or "").strip()
    if expected_app_id and actual_app_id != expected_app_id:
        return _rejected("invalid_app_id")

    signature_rejection = _signature_rejection_reason(payload, headers, config, body)
    if signature_rejection:
        return _rejected(signature_rejection)

    external_status = _status_from_event(event)
    if external_status not in _SUPPORTED_STATUSES:
        return {"ok": False, "status": "ignored", "reason": "unsupported_status"}
    if external_status == "CANCELLED":
        external_status = "CANCELED"

    resolver = getattr(approval_async, "resolve_external_approval", None)
    if not callable(resolver):
        return {"ok": False, "status": "ignored", "reason": "sync_unavailable"}
    return await resolver(
        external_uuid=str(event.get("uuid") or "").strip(),
        external_instance_code=str(event.get("instance_code") or "").strip(),
        external_status=external_status,
        source="feishu_event",
        raw_event=payload,
    )


async def handle_feishu_approval_request(request: web.Request) -> web.Response:
    """aiohttp request handler for Feishu approval callbacks."""
    body = await request.read()
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return web.json_response(_rejected("invalid_json"), status=400)
    if not isinstance(payload, dict):
        return web.json_response(_rejected("invalid_payload"), status=400)

    headers = dict(request.headers)
    if not _header_value(headers, "X-Lark-Signature"):
        return web.json_response(_rejected("missing_signature"), status=401)

    config = request.app.get(_CONFIG_KEY)
    if not isinstance(config, dict):
        fallback_config = request.app.get("config")
        config = fallback_config if isinstance(fallback_config, dict) else None
    result = await handle_feishu_approval_event(
        payload,
        headers=headers,
        config=config,
        body=body,
    )
    status = 200
    if result.get("status") == "rejected":
        status = 401
    elif result.get("status") == "ignored":
        status = 202
    return web.json_response(result, status=status)


handle_feishu_approval_callback = handle_feishu_approval_request


def setup_feishu_approval_webhook(app: web.Application, *, config: dict[str, Any] | None = None) -> web.Application:
    """Register Feishu approval callback route."""
    if isinstance(config, dict):
        app[_CONFIG_KEY] = config
    path = _callback_path(config)
    if not _route_registered(app, method="POST", path=path):
        app.router.add_post(path, handle_feishu_approval_request)
    return app


setup_feishu_approval_event_webhook = setup_feishu_approval_webhook
