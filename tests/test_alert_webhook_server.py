"""测试 Alertmanager webhook 独立服务入口。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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


def test_parser_uses_environment_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务端口和监听地址可由环境变量控制。"""
    module = _load_module()
    monkeypatch.setenv("AIOPS_ALERT_WEBHOOK_HOST", "127.0.0.1")
    monkeypatch.setenv("AIOPS_ALERT_WEBHOOK_PORT", "9876")

    args = module._build_parser().parse_args([])

    assert args.host == "127.0.0.1"
    assert args.port == 9876
