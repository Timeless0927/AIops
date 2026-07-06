"""Independent React Console web app contract tests."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONSOLE_WEB = ROOT / "apps" / "aiops_console_web"


def test_console_web_is_vite_react_and_chinese_first() -> None:
    package = yaml.safe_load((CONSOLE_WEB / "package.json").read_text(encoding="utf-8"))
    app = (CONSOLE_WEB / "src" / "App.tsx").read_text(encoding="utf-8")
    html = (CONSOLE_WEB / "index.html").read_text(encoding="utf-8")

    assert package["scripts"]["build"] == "tsc --noEmit && vite build"
    assert package["scripts"]["dev"] == "vite --host 0.0.0.0"
    assert "react" in package["dependencies"]
    assert "@vitejs/plugin-react" in package["devDependencies"]
    assert '<html lang="zh-CN">' in html
    for label in (
        "AIOps 控制台",
        "活跃事件",
        "登录 Gateway",
        "事件详情",
        "诊断摘要",
        "根因",
        "证据",
        "时间线",
        "缺失证据",
        "建议动作",
        "只读展示",
        "审批中心",
        "通知记录",
        "审计历史",
    ):
        assert label in app or label in html


def test_console_web_calls_gateway_relative_api_only() -> None:
    app = (CONSOLE_WEB / "src" / "App.tsx").read_text(encoding="utf-8")
    vite_config = (CONSOLE_WEB / "vite.config.ts").read_text(encoding="utf-8")
    nginx_config = (ROOT / "deploy" / "nginx" / "console-web.conf").read_text(encoding="utf-8")

    assert "fetch(url" in app
    assert "'/auth/login'" in app
    assert "'/api/incidents/active'" in app
    assert "`/api/incidents/${incidentId}/diagnosis-process`" in app
    assert "Authorization: `Bearer ${activeToken}`" in app
    assert "/api/approval" not in app
    assert "/api/notifications" not in app
    assert "'/api': 'http://127.0.0.1:18080'" in vite_config
    assert "'/auth': 'http://127.0.0.1:18080'" in vite_config
    assert "proxy_pass http://aiops-gateway:8080;" in nginx_config
    assert "try_files $uri $uri/ /index.html;" in nginx_config

    for forbidden in ("hermes", "connector", "mcp", "prometheus", "loki", "feishu"):
        assert f"/{forbidden}" not in app.lower()
        assert f"{forbidden}://" not in app.lower()


def test_console_web_has_stable_responsive_layout() -> None:
    css = (CONSOLE_WEB / "src" / "styles.css").read_text(encoding="utf-8")

    assert ".console-shell" in css
    assert "grid-template-columns: 300px minmax(0, 1fr)" in css
    assert ".content-grid" in css
    assert "@media (max-width: 900px)" in css
    assert "@media (max-width: 560px)" in css
    assert "border-radius: 6px" in css
