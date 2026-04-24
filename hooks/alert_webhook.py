"""Alertmanager Webhook 处理 Hook。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Dict, List
import sys

from aiohttp import web
import yaml


def _load_alert_dedup_module():
    """优先从当前项目路径加载本地 alert_dedup 模块。"""
    module_name = "aiops_alert_dedup"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = _project_root() / "toolsets" / "alert_dedup.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _project_root() -> Path:
    """返回项目根目录。"""
    return Path(__file__).resolve().parent.parent


alert_dedup = _load_alert_dedup_module()


def _config_path() -> Path:
    """返回配置文件路径。"""
    return _project_root() / "config.yaml"


def _load_config_sync() -> Dict[str, Any]:
    """同步读取配置。"""
    path = _config_path()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


async def _load_config() -> Dict[str, Any]:
    """异步读取配置。"""
    return await asyncio.to_thread(_load_config_sync)


def _resolve_hmac_secret(config: Dict[str, Any]) -> str | None:
    """从环境变量或配置中解析 webhook HMAC 密钥。"""
    env_secret = os.getenv("ALERTMANAGER_WEBHOOK_SECRET")
    if env_secret:
        return env_secret

    candidates = [
        (("alertmanager_webhook", "secret"),),
        (("webhooks", "alertmanager", "secret"),),
        (("hooks", "alertmanager", "secret"),),
    ]
    for group in candidates:
        for path in group:
            current: Any = config
            for key in path:
                if not isinstance(current, dict):
                    current = None
                    break
                current = current.get(key)
            if isinstance(current, str) and current:
                return current
    return None


def _verify_hmac_signature(body: bytes, secret: str, signature: str | None) -> bool:
    """校验 webhook HMAC 签名。"""
    if not signature:
        return False
    received = signature.strip()
    if received.startswith("sha256="):
        received = received.split("=", 1)[1]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


def _extract_alert(alert: Dict[str, Any]) -> Dict[str, Any]:
    """从 Alertmanager 单条告警中提取标准字段。"""
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    annotations = alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
    return {
        "alertname": str(labels.get("alertname", "")).strip(),
        "severity": str(labels.get("severity", "info")).strip().lower() or "info",
        "namespace": str(labels.get("namespace", "default")).strip() or "default",
        "cluster": str(labels.get("cluster", "default")).strip() or "default",
        "description": str(
            annotations.get("description") or annotations.get("summary") or ""
        ).strip(),
        "status": str(alert.get("status", "")).strip().lower(),
    }


def _build_triage_prompt(alert: Dict[str, Any]) -> str:
    """格式化 triage 提示词。"""
    return (
        f"[Alertmanager] {alert['severity']} 告警: {alert['alertname']} "
        f"in {alert['namespace']}/{alert['cluster']}. {alert['description']}. 请执行 triage 流程。"
    )


async def _handle_alertmanager(request: web.Request) -> web.Response:
    """处理 Alertmanager webhook 请求。"""
    config = request.app.get("alert_webhook_config")
    if config is None:
        config = await _load_config()
        request.app["alert_webhook_config"] = config

    body = await request.read()
    secret = _resolve_hmac_secret(config)
    if secret:
        signature = request.headers.get("X-Signature") or request.headers.get("X-Hub-Signature-256")
        if not _verify_hmac_signature(body, secret, signature):
            return web.json_response({"ok": False, "message": "签名校验失败"}, status=401)

    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "message": "无效的 JSON payload"}, status=400)

    raw_alerts = payload.get("alerts") if isinstance(payload.get("alerts"), list) else []
    prompts: List[str] = []
    processed = 0
    skipped = 0

    for raw_alert in raw_alerts:
        if not isinstance(raw_alert, dict):
            skipped += 1
            continue

        alert = _extract_alert(raw_alert)
        if alert["status"] == "resolved":
            skipped += 1
            continue

        if await alert_dedup.should_process(alert):
            processed += 1
            prompts.append(_build_triage_prompt(alert))
        else:
            skipped += 1

    return web.json_response(
        {
            "ok": True,
            "processed": processed,
            "skipped": skipped,
            "prompts": prompts,
        }
    )


async def setup_alert_webhook(app: web.Application) -> None:
    """在 aiohttp 应用中注册 Alertmanager webhook 路由。"""
    if "alert_webhook_config" not in app:
        app["alert_webhook_config"] = await _load_config()
    app.router.add_post("/webhooks/alertmanager", _handle_alertmanager)
