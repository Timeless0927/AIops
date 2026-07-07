"""React Console Next shell contract tests."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONSOLE_WEB = ROOT / "apps" / "aiops_console_web"


def test_console_web_uses_react_router_and_chinese_first_shell() -> None:
    package = yaml.safe_load((CONSOLE_WEB / "package.json").read_text(encoding="utf-8"))
    app = (CONSOLE_WEB / "src" / "App.tsx").read_text(encoding="utf-8")
    html = (CONSOLE_WEB / "index.html").read_text(encoding="utf-8")

    assert package["scripts"]["build"] == "tsc --noEmit && vite build"
    assert "react-router" in package["dependencies"]
    assert '<html lang="zh-CN">' in html
    assert "BrowserRouter" in app
    assert "Routes" in app
    assert "activeView" not in app
    assert "window.history" not in app
    for label in (
        "AIOps 控制台",
        "登录 Gateway",
        "中文",
        "EN",
        "事件工作台",
        "Agent Runs",
        "Run 列表",
        "新建 Run",
        "时间线",
        "旁路",
        "提升到主线",
        "请求动作",
        "动作 Hash",
        "执行状态",
        "事件工作台",
        "事件报告",
        "生成草稿",
        "发布版本",
        "报告版本",
        "HTML 预览",
        "人工反馈",
        "暂停 Run",
        "人工接管",
        "恢复事件",
        "审批中心",
        "责任链审计",
        "责任链列表",
        "原始日志",
        "删除会话记录",
        "原始审计引用",
        "策略",
        "用户",
        "用户列表",
        "新建用户",
        "最近权限审计",
        "设置",
        "设置版本",
        "设置 JSON",
        "差异预览",
        "关键变更确认",
        "请输入精确确认文本",
        "回滚上一版本",
        "策略说明",
        "测试策略",
        "最近策略命中",
        "证据查询",
        "证据面板",
        "查询模板",
        "部分可用",
        "暂无证据",
        "证据可能已过期",
        "通知中心",
        "403 无权访问",
        "404 页面不存在",
    ):
        assert label in app or label in html


def test_console_web_uses_cookie_session_and_csrf_not_bearer_storage() -> None:
    app = (CONSOLE_WEB / "src" / "App.tsx").read_text(encoding="utf-8")
    vite_config = (CONSOLE_WEB / "vite.config.ts").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile.aiops").read_text(encoding="utf-8")

    assert "credentials: 'same-origin'" in app
    assert "'/auth/login'" in app
    assert "'/auth/me'" in app
    assert "'/auth/csrf'" in app
    assert "'/auth/logout'" in app
    assert "'/api/users'" in app
    assert "`/api/users/${encodeURIComponent(user.username)}`" in app
    assert "'/api/settings'" in app
    assert "'/api/settings/preview'" in app
    assert "'/api/settings/rollback'" in app
    assert "'/api/policies'" in app
    assert "'/api/policies/test'" in app
    assert "'/api/evidence/query'" in app
    assert "'/api/agent-runs'" in app
    assert "'/api/audit/chains'" in app
    assert "'/api/audit/raw?limit=20'" in app
    assert "'/api/audit/tombstones'" in app
    assert "'/api/actions/propose'" in app
    assert "/api/approval-requests/" in app
    assert "/workbench" in app
    assert "/controls" in app
    assert "/report/draft" in app
    assert "/report/publish" in app
    assert "/report?format=html" in app
    assert "'/api/feedback'" in app
    assert "/feedback" in app
    assert "EventSource" in app
    assert "permission=\"view_users\"" in app
    assert "permission=\"view_settings\"" in app
    assert "permission=\"view_policy\"" in app
    assert "permission=\"view_evidence\"" in app
    assert "manage_users" in app
    assert "manage_settings" in app
    assert "view_settings" in app
    assert "view_policy" in app
    assert "view_evidence" in app
    assert "'X-CSRF-Token'" in app
    assert "Authorization: `Bearer" not in app
    assert "oncall_approver" not in app
    assert "sessionStorage" not in app
    assert "localStorage.setItem(LOCALE_KEY" in app
    assert "'/api': 'http://127.0.0.1:18080'" in vite_config
    assert "'/auth': 'http://127.0.0.1:18080'" in vite_config
    assert "COPY --from=console-web-build /app/apps/aiops_console_web/dist /app/apps/aiops_console_web/dist" in dockerfile
    assert "AIOPS_CONSOLE_DIST_DIR=/app/apps/aiops_console_web/dist" in dockerfile

    for forbidden in ("hermes", "connector", "mcp", "prometheus", "loki", "feishu"):
        assert f"/{forbidden}" not in app.lower()
        assert f"{forbidden}://" not in app.lower()


def test_console_web_has_stable_responsive_layout() -> None:
    css = (CONSOLE_WEB / "src" / "styles.css").read_text(encoding="utf-8")

    assert ".console-shell" in css
    assert "grid-template-columns: 244px minmax(0, 1fr)" in css
    assert ".topbar" in css
    assert ".locale-switch" in css
    assert "@media (max-width: 980px)" in css
    assert "@media (max-width: 620px)" in css
    assert "border-radius: 8px" in css
