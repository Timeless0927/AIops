"""测试 Alertmanager webhook 独立服务入口。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from aiohttp import web
import pytest


def _load_module():
    """按文件路径加载 server 模块。"""
    module_path = Path(__file__).resolve().parents[1] / "hooks" / "alert_webhook_server.py"
    module_name = "test_alert_webhook_server_module"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_create_app_registers_alertmanager_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """独立服务应注册 Alertmanager webhook 路由。"""
    module = _load_module()
    called = {}

    async def _fake_setup(app):
        called["app"] = app
        app.router.add_post("/webhooks/alertmanager", lambda request: None)

    monkeypatch.setattr(module, "setup_alert_webhook", _fake_setup)

    app = await module.create_app()

    assert called["app"] is app
    assert any(route.resource.canonical == "/webhooks/alertmanager" for route in app.router.routes())


@pytest.mark.asyncio
async def test_create_app_starts_feishu_approval_polling_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """独立 webhook 服务应在 polling 配置开启时启动后台补偿循环。"""
    module = _load_module()
    ticked = asyncio.Event()
    calls: list[dict] = []

    async def _fake_setup(app):
        app["alert_webhook_config"] = {
            "platforms": {
                "feishu": {
                    "approval": {
                        "polling_enabled": "true",
                        "polling_interval_seconds": 60,
                    }
                }
            }
        }
        app.router.add_post("/webhooks/alertmanager", lambda request: None)

    def _fake_setup_feishu(app, *, config=None):
        del config
        app.router.add_post("/webhooks/feishu/approval", lambda request: None)

    async def _fake_poll(*, config=None):
        calls.append(config or {})
        ticked.set()
        return {"ok": True, "enabled": True, "scanned": 1, "synced": 1, "failed": 0}

    monkeypatch.setattr(module, "setup_alert_webhook", _fake_setup)
    monkeypatch.setattr(module, "setup_feishu_approval_webhook", _fake_setup_feishu)
    monkeypatch.setattr(module.recovery, "poll_external_pending_approvals", _fake_poll)

    app = await module.create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await asyncio.wait_for(ticked.wait(), timeout=1)
    finally:
        await runner.cleanup()

    assert calls
    assert calls[0]["platforms"]["feishu"]["approval"]["polling_enabled"] == "true"


def test_parser_uses_environment_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务端口和监听地址可由环境变量控制。"""
    module = _load_module()
    monkeypatch.setenv("AIOPS_ALERT_WEBHOOK_HOST", "127.0.0.1")
    monkeypatch.setenv("AIOPS_ALERT_WEBHOOK_PORT", "9876")

    args = module._build_parser().parse_args([])

    assert args.host == "127.0.0.1"
    assert args.port == 9876
