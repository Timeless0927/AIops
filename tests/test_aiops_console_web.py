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
        "策略规则",
        "差异预览",
        "关键变更确认",
        "请输入精确确认文本",
        "回滚上一版本",
        "策略说明",
        "测试策略",
        "最近策略命中",
        "动作允许列表",
        "动作类型",
        "后端",
        "模板",
        "允许范围",
        "默认风险",
        "预检",
        "后置检查",
        "要求回滚计划",
        "自动执行",
        "自审批",
        "可审批角色",
        "默认命名空间范围",
        "Owner Team",
        "自动动作",
        "OpenObserve 配置引用",
        "Connector 状态",
        "OpenObserve 状态",
        "范围字段映射",
        "最近查询健康",
        "失败摘要",
        "最近心跳",
        "运行状态更新时间",
        "配置状态",
        "Mutation 已禁用",
        "unconfigured",
        "证据查询",
        "证据面板",
        "过程图",
        "调用链",
        "结构化详情",
        "脱敏片段",
        "为什么重要",
        "查询模板",
        "部分可用",
        "暂无证据",
        "证据可能已过期",
        "通知中心",
        "Runbooks",
        "Runbook 管理",
        "Last run summary",
        "Last run status",
        "实时通知",
        "重试投递",
        "暂无通知",
        "全局搜索",
        "搜索结果",
        "证据摘要",
        "审批备注",
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
    assert "const next = safeNext(search.get('next'))" in app
    assert "navigate(next, { replace: true })" in app
    assert "'/auth/csrf'" in app
    assert "'/auth/logout'" in app
    assert "'/api/users'" in app
    assert "`/api/users/${encodeURIComponent(user.username)}`" in app
    assert "'/api/settings'" in app
    assert "'/api/settings/preview'" in app
    assert "'/api/settings/rollback'" in app
    assert "'/api/clusters'" in app
    assert "'/api/policies'" in app
    assert "'/api/policies/test'" in app
    assert "PolicyRulesTable" in app
    assert "ActionAllowlistTable" in app
    assert "JSON.stringify(policy?.policy" not in app
    assert "'/api/evidence/query'" in app
    assert "`/api/agent-runs/${encodeURIComponent(runId)}/evidence`" in app
    assert "JSON.stringify(source.samples" not in app
    assert "EvidenceNodesPanel" in app
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
    assert "'/api/notifications'" in app
    assert "'/api/notifications/retry'" in app
    assert "'/api/notifications/stream'" in app
    assert "'/api/runbooks'" in app
    assert "`/api/runbooks/${encodeURIComponent(runbook.id)}/toggle`" in app
    assert 'path="/runbooks"' in app
    assert "permission=\"view_runbooks\"" in app
    assert "manage_runbooks" in app
    assert "/api/search?" in app
    assert "DefaultIncidentRoute" in app
    assert "'/api/incidents/active'" in app
    assert "`/incidents/${encodeURIComponent(first.incident_id)}`" in app
    assert "EventSource" in app
    assert "`/api/agent-runs/${encodeURIComponent(runId)}/events`" in app
    assert "RunEventRefs" in app
    assert "<HumanValue value={value ?? {}} />" in app
    assert "<pre>{JSON.stringify(value ?? {}, null, 2)}</pre>" not in app
    assert "disabled={!canDecide}" in app
    assert "permission=\"view_users\"" in app
    assert "permission=\"view_settings\"" in app
    assert "path=\"/clusters\"" in app
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


def test_console_web_mobile_approval_accessibility_smoke() -> None:
    app = (CONSOLE_WEB / "src" / "App.tsx").read_text(encoding="utf-8")
    css = (CONSOLE_WEB / "src" / "styles.css").read_text(encoding="utf-8")

    assert "const next = safeNext(search.get('next'))" in app
    assert "navigate(next, { replace: true })" in app
    assert 'path="/approvals/:approvalId"' in app
    assert 'to={`/approvals/${encodeURIComponent(approval.approval_id)}`}' in app
    assert 'to={`/approvals/${encodeURIComponent(item.approval_id)}`}' in app
    assert 'className="approval-detail-grid mobile-approval"' in app
    assert "<EvidenceNodesPanel title={String(t.evidenceSummary)} nodes={evidenceNodes} />" in app
    assert "executionProgress(execution)" in app
    assert "approvalResponsibility(approval, execution)" in app
    assert "<label>{String(t.approvalRemark)}<textarea" in app
    assert "disabled={!canDecide}" in app
    assert "{String(t.approve)}: {approval.action_summary}" in app
    assert "{String(t.reject)}: {approval.action_summary}" in app
    assert 'role="status"' in app
    assert "String(t.noApprovals)" in app
    assert "String(t.emptyEvidence)" in app
    assert 'role="tablist"' in app
    assert 'role="tab"' in app
    assert 'role="tabpanel"' in app
    assert "ArrowRight" in app and "ArrowLeft" in app and "Home" in app and "End" in app
    assert 'aria-label={`${String(t.status)} ${approval.status}`}' in app
    assert 'role="dialog"' not in app
    assert ".workbench-panel textarea" in css
    assert "button:focus-visible" in css
    assert "@media (max-width: 620px)" in css
    assert ".approval-actions button" in css
    assert "min-height: 44px" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
