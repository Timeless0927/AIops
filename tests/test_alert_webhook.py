"""测试 Alertmanager Webhook 处理模块。"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import sys

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "hooks" / "alert_webhook.py"
    spec = importlib.util.spec_from_file_location("test_alert_webhook_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _payload(status: str = "firing") -> dict:
    """构造标准 Alertmanager payload。"""
    return {
        "alerts": [
            {
                "status": status,
                "labels": {
                    "alertname": "PodCrashLooping",
                    "severity": "critical",
                    "namespace": "default",
                    "cluster": "prod-a",
                },
                "annotations": {
                    "description": "pod 重启次数持续增加",
                },
            }
        ]
    }


@pytest.mark.asyncio
async def test_webhook_formats_prompt_and_skips_resolved(monkeypatch, **_kwargs) -> None:
    """firing 告警应生成 triage 提示词，resolved 告警应跳过。"""
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {}
    await module.setup_alert_webhook(app)

    async def _should_process(alert: dict) -> bool:
        return True

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        data = await response.json()

        response_resolved = await client.post("/webhooks/alertmanager", json=_payload("resolved"))
        data_resolved = await response_resolved.json()
    finally:
        await client.close()

    assert data["ok"] is True
    assert data["processed"] == 1
    assert data["prompts"][0] == (
        "[Alertmanager] critical 告警: PodCrashLooping in default/prod-a. "
        "pod 重启次数持续增加. 请执行 triage 流程。"
    )
    assert data_resolved["processed"] == 0
    assert data_resolved["skipped"] == 1


@pytest.mark.asyncio
async def test_webhook_integrates_dedup(monkeypatch, **_kwargs) -> None:
    """当去重器拒绝时 webhook 不应生成提示词。"""
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {}
    await module.setup_alert_webhook(app)

    calls = {"count": 0}

    async def _should_process(alert: dict) -> bool:
        calls["count"] += 1
        return calls["count"] == 1

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        first = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        second = await client.post("/webhooks/alertmanager", json=_payload("firing"))
        first_data = await first.json()
        second_data = await second.json()
    finally:
        await client.close()

    assert first_data["processed"] == 1
    assert second_data["processed"] == 0
    assert second_data["skipped"] == 1


@pytest.mark.asyncio
async def test_webhook_hmac_validation(monkeypatch, **_kwargs) -> None:
    """配置了密钥时应校验 HMAC。"""
    module = _load_module()
    app = web.Application()
    app["alert_webhook_config"] = {"webhooks": {"alertmanager": {"secret": "top-secret"}}}
    await module.setup_alert_webhook(app)

    async def _should_process(alert: dict) -> bool:
        return True

    monkeypatch.setattr(module.alert_dedup, "should_process", _should_process)

    body = json.dumps(_payload("firing"), ensure_ascii=False).encode("utf-8")
    good_sig = hmac.new(b"top-secret", body, hashlib.sha256).hexdigest()

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        bad_response = await client.post(
            "/webhooks/alertmanager",
            data=body,
            headers={"Content-Type": "application/json", "X-Signature": "bad-signature"},
        )
        good_response = await client.post(
            "/webhooks/alertmanager",
            data=body,
            headers={"Content-Type": "application/json", "X-Signature": f"sha256={good_sig}"},
        )
        bad_data = await bad_response.json()
        good_data = await good_response.json()
    finally:
        await client.close()

    assert bad_response.status == 401
    assert bad_data["ok"] is False
    assert good_response.status == 200
    assert good_data["processed"] == 1
