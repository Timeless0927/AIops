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
        "历史 / 时间线 / 审计",
        "审计状态",
        "审计引用",
        "暂无审计记录",
        "缺失证据",
        "建议动作",
        "只读展示",
        "审批中心",
        "审批请求",
        "审批详情",
        "审批上下文",
        "回滚计划",
        "证据 / 审计引用",
        "审批决策",
        "执行跟踪",
        "执行生命周期",
        "执行入口未开放",
        "无执行授权",
        "通过",
        "拒绝",
        "通知中心",
        "通知投递",
        "通知类型目录",
        "失败 / 死信",
        "应用筛选",
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
    assert "'/api/approval-requests'" in app
    assert "`/api/approval-requests/${approvalId}`" in app
    assert "`/api/approval-requests/${approvalId}/execution`" in app
    assert "`/api/approval-requests/${approvalId}/${decision}`" in app
    assert "'/api/notifications/types'" in app
    assert "`/api/notifications/deliveries${query.toString() ? `?${query.toString()}` : ''}`" in app
    assert "Authorization: `Bearer ${activeToken}`" in app
    assert "/audit/query" not in app
    assert "/api/audit" not in app
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
